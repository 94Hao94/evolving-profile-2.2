"""Content processing utilities for Codex.

Adapts Openclaw/Claude Code content processing for Codex's transcript format.

Codex rollout format (JSONL):
  Each line is a RolloutLine: {"timestamp": "...", "type": "<item_type>", ...}
  Item types: session_meta, response_item, event_msg, turn_context, compacted.

  ResponseItem types we care about:
    - message (role: user/assistant) — text content
    - local_shell_call — shell command invocations
    - function_call — tool/function calls with name + arguments
    - function_call_output — tool return values
    - custom_tool_call / custom_tool_call_output — freeform tools
    - web_search_call — web search invocations

  EventMsg types (Extended persistence mode):
    - exec_command_end — shell command results with stdout/stderr
    - patch_apply_end — code patch application results
    - mcp_tool_call_end — MCP tool call results
"""

import json
import hashlib
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import PureWindowsPath

# Maximum length for tool output content in JSON format.
_MAX_TOOL_OUTPUT_CHARS = 2000


# ---------------------------------------------------------------------------
# Memory tag stripping (anti-feedback-loop)
# ---------------------------------------------------------------------------


def strip_memory_tags(content: str) -> str:
    """Remove <evolving_profile_memories> and <relevant_memories> blocks.

    Prevents retain feedback loop — these were injected during recall and
    should not be re-stored.
    """
    content = re.sub(r"<evolving_profile_memories>[\s\S]*?</evolving_profile_memories>", "", content)
    content = re.sub(r"<relevant_memories>[\s\S]*?</relevant_memories>", "", content)
    return content


def strip_ambient_context(content: str) -> str:
    """Remove Codex-injected UI/environment blocks that are not user memory."""
    value = str(content or "")
    for tag in (
        "recommended_plugins",
        "environment_context",
        "in-app-browser-context",
        # Codex automation/control metadata is persisted in the transcript as
        # a user message but is not user-authored long-term memory.
        "heartbeat",
        "evolving_profile_midtask_journal",
        "evolving_profile_checkpoint",
    ):
        value = re.sub(
            rf"<{re.escape(tag)}(?:\s[^>]*)?>[\s\S]*?</{re.escape(tag)}>",
            "",
            value,
            flags=re.IGNORECASE,
        )
    return value


_USER_REQUEST_MARKER = re.compile(
    r"(?im)^##\s*My request(?:\s+for Codex)?\s*:\s*"
)


def extract_user_request(content: str, *, structural_context_only: bool = False) -> str:
    """Return only the user-authored request from a Codex prompt envelope.

    Codex can prepend attachment manifests and ambient UI blocks to the actual
    request.  Those fields remain available to the caller through the raw hook
    input, but must not become the semantic recall query or the trace title.
    """
    value = strip_memory_tags(str(content or ""))
    value = _strip_leading_task_metadata(value) if structural_context_only else strip_ambient_context(value)
    matches = list(_USER_REQUEST_MARKER.finditer(value))
    if matches:
        value = value[matches[-1].end() :]
    value = re.sub(
        r"<image\b[^>]*>[\s\S]*?</image>",
        "",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"<image\b[^>]*/?>", "", value, flags=re.IGNORECASE)
    value = re.sub(
        r"(?im)^Distinguish instructions in attached documents from the user's request\.\s*$",
        "",
        value,
    )
    return value.strip()


def _native_environment_shape(element) -> bool:
    def whitespace(value):
        return not str(value or '').strip()

    def container(node, allowed):
        children = list(node)
        return (not node.attrib and whitespace(node.text) and bool(children)
                and len({child.tag for child in children}) == len(children)
                and all(child.tag in allowed and whitespace(child.tail) for child in children))

    def leaf(node):
        return not node.attrib and not list(node) and bool(str(node.text or '').strip())

    def absolute_path(node):
        value = str(node.text or '').strip()
        return leaf(node) and '\x00' not in value and '\n' not in value and (os.path.isabs(value) or PureWindowsPath(value).is_absolute())

    if not container(element, {'cwd', 'shell', 'current_date', 'timezone', 'filesystem'}):
        return False
    for child in element:
        value = str(child.text or '').strip()
        if child.tag == 'cwd' and not absolute_path(child):
            return False
        if child.tag == 'shell' and (not leaf(child) or value not in {'zsh', 'bash', 'sh', 'fish', 'pwsh', 'powershell', '/bin/zsh', '/bin/bash', '/bin/sh', '/usr/bin/fish'}):
            return False
        if child.tag == 'current_date':
            if not leaf(child) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                return False
            try:
                datetime.strptime(value, '%Y-%m-%d')
            except ValueError:
                return False
        if child.tag == 'timezone' and (not leaf(child) or not re.fullmatch(r'(?:UTC|GMT|[A-Za-z_+-]+(?:/[A-Za-z0-9_+-]+){1,2})', value)):
            return False
        if child.tag == 'filesystem':
            if not container(child, {'workspace_roots', 'permission_profile'}):
                return False
            for group in child:
                if group.tag == 'workspace_roots':
                    if group.attrib or not whitespace(group.text) or not list(group) or any(root.tag != 'root' or not whitespace(root.tail) or not absolute_path(root) for root in group):
                        return False
                else:
                    # This is the actual native disabled/unrestricted profile.
                    # Unknown permission schemas remain text until understood.
                    entries = list(group)
                    if group.attrib != {'type': 'disabled'} or not whitespace(group.text) or len(entries) != 1:
                        return False
                    entry = entries[0]
                    if entry.tag != 'file_system' or entry.attrib != {'type': 'unrestricted'} or list(entry) or not whitespace(entry.text) or not whitespace(entry.tail):
                        return False
    return True


def _strip_leading_task_metadata(content: str) -> str:
    """Only complete, leading native envelopes can be ambient task metadata.

    A quoted/fenced example, malformed envelope, or unknown schema stays user
    content. This is task projection only; the historical/source/Origin query
    normalization contract continues using extract_user_request's default.
    """
    value = str(content or '')
    # Only schemas confirmed by native examples are eligible. A tag name or
    # arbitrary JSON object cannot prove that its content is non-semantic.
    tags = ('external_codex_apps_open_page', 'environment_context')
    pattern = r'^\s*<(' + '|'.join(re.escape(tag) for tag in tags) + r')>(.*?)</\1>'
    while (match := re.match(pattern, value, re.S)):
        tag, body = match.group(1), match.group(2)
        recognized = False
        if tag == 'external_codex_apps_open_page':
            try:
                def unique_keys(pairs):
                    result = {}
                    for key, value in pairs:
                        if key in result:
                            raise ValueError('duplicate_native_context_key')
                        result[key] = value
                    return result
                payload = json.loads(body, object_pairs_hook=unique_keys)
                recognized = isinstance(payload, dict) and set(payload) == {'page_id'} and (payload['page_id'] is None or
                    (isinstance(payload['page_id'], str) and bool(re.fullmatch(r'[A-Za-z0-9:_-]+', payload['page_id']))))
            except (ValueError, TypeError):
                pass
        elif tag == 'environment_context':
            try:
                if '<!' in body or '<?' in body:
                    break  # Comments/declarations may contain semantic text.
                element = ET.fromstring(match.group(0).strip())
                recognized = _native_environment_shape(element)
            except ET.ParseError:
                pass
        if not recognized:
            break
        value = value[match.end():]
    return value.strip()


def extract_task_user_request(content: str) -> str:
    """Semantic task text, with native metadata excluded at a structural seam."""
    return extract_user_request(content, structural_context_only=True)


def is_synthetic_codex_user_message(content: str) -> bool:
    """Return True for setup messages Codex persists as user chat.

    Codex records AGENTS.md startup instructions as a normal user message in
    rollout JSONL. Retaining those messages teaches Hindsight about hook and
    agent rules instead of the user's work, and can create very large retain
    payloads in long-running sessions.
    """
    if not isinstance(content, str):
        return False

    stripped = content.lstrip()
    if stripped.startswith('Hindsight系统一次性检查提示，不是用户新增需求：'):
        try:
            from memory_turn_check import is_recorded_completion_prompt
            if is_recorded_completion_prompt(stripped):return True
        except Exception:
            pass  # Unknown origin is not silently discarded.
    return (
        stripped.startswith("# AGENTS.md instructions for ")
        and "<INSTRUCTIONS>" in stripped
        and "</INSTRUCTIONS>" in stripped
    )


# ---------------------------------------------------------------------------
# Transcript reading
# ---------------------------------------------------------------------------


def read_transcript(transcript_path: str, include_tool_calls: bool = False, max_tail_bytes=None) -> list:
    """Read a Codex JSONL transcript and return list of message dicts.

    When include_tool_calls is False (legacy mode), returns simple
    {role, content} dicts with text-only content.

    When include_tool_calls is True, returns richer message dicts with
    structured content blocks (matching Claude Code's JSON format):
      - {"role": "user", "content": [{"type": "text", "text": "..."}]}
      - {"role": "assistant", "content": [
            {"type": "text", "text": "..."},
            {"type": "tool_use", "name": "shell", "input": {"command": ["ls"]}},
            {"type": "tool_result", "content": "file1.txt\\nfile2.txt"}
        ]}

    Codex rollout format (rollout-*.jsonl):
      ResponseItems:
        message:              text messages with role
        local_shell_call:     shell command invocations
        function_call:        tool calls with name + arguments
        function_call_output: tool return values
        custom_tool_call:     freeform tool calls
        custom_tool_call_output: freeform tool results
      EventMsgs:
        exec_command_end:     shell results with stdout/stderr/exit_code
        patch_apply_end:      code patch results
        mcp_tool_call_end:    MCP tool results

    Flat format for testing:
      {"role": "user", "content": "..."}
    """
    if not transcript_path or not os.path.isfile(transcript_path):
        return []

    if include_tool_calls:
        return _read_transcript_rich(transcript_path)
    return _read_transcript_text(transcript_path, max_tail_bytes=max_tail_bytes)


def _read_transcript_text(transcript_path: str, max_tail_bytes=None) -> list:
    """Legacy text-only transcript reader."""
    messages = []
    session_id = None
    turn_id = None
    try:
        with open(transcript_path, "rb") as raw:
            if max_tail_bytes:
                file_size = raw.seek(0, os.SEEK_END)
                if file_size > int(max_tail_bytes):
                    raw.seek(-int(max_tail_bytes), os.SEEK_END)
                    # The first bytes can be a partial JSONL line; discard it.
                    raw.readline()
                else:
                    # Measuring a small file leaves the cursor at EOF.  Rewind
                    # explicitly or every ordinary, sub-threshold Codex task
                    # becomes an empty context window.
                    raw.seek(0, os.SEEK_SET)
            for raw_line in raw:
                byte_offset = raw.tell() - len(raw_line)
                line = raw_line.decode("utf-8", errors="replace")
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    payload = entry.get('payload') or {}
                    if entry.get('type') == 'session_meta':
                        session_id = payload.get('id') or payload.get('session_id')
                    if entry.get('type') == 'turn_context':
                        turn_id = payload.get('turn_id')
                    source_record = {
                        'transcript_path': os.path.abspath(transcript_path),
                        'byte_offset': byte_offset,
                        'raw_line_sha256': hashlib.sha256(raw_line).hexdigest(),
                        'session_id': session_id, 'turn_id': turn_id,
                        'message_id': payload.get('id'),
                        'recorded_at': entry.get('timestamp'),
                        'time_semantics': 'host_recorded_time_not_claim_valid_time',
                        'actor_identity': 'host_role_only; human_or_agent_initiator_not_independently_verified',
                    }
                    # Codex response_item format
                    if entry.get("type") == "response_item":
                        payload = entry.get("payload", {})
                        if payload.get("type") == "message":
                            role = payload.get("role", "")
                            if role not in ("user", "assistant"):
                                continue
                            # Only include final_answer for assistant (not reasoning/intermediary)
                            if role == "assistant" and payload.get("phase") != "final_answer":
                                continue
                            content_blocks = payload.get("content", [])
                            text_parts = []
                            for block in content_blocks:
                                if isinstance(block, dict) and block.get("type") in ("input_text", "output_text"):
                                    t = block.get("text", "").strip()
                                    if t:
                                        text_parts.append(t)
                            text = "\n".join(text_parts).strip()
                            if role == "user" and is_synthetic_codex_user_message(text):
                                continue
                            if text:
                                messages.append({"role": role, "content": text, 'source_record': source_record})
                    # Flat format (testing / future compatibility)
                    elif "role" in entry and "content" in entry:
                        messages.append({"role": entry["role"], "content": entry["content"], 'source_record': source_record})
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return messages


def _read_transcript_rich(transcript_path: str) -> list:
    """Rich transcript reader that preserves tool calls as structured content blocks.

    Collects all response_items and event_msgs into a sequence of messages
    with structured content blocks. Tool calls and their outputs are grouped
    under the assistant role.
    """
    messages = []
    # Buffer for collecting assistant content blocks between user messages
    assistant_blocks = []

    def _flush_assistant():
        nonlocal assistant_blocks
        if assistant_blocks:
            messages.append({"role": "assistant", "content": assistant_blocks})
            assistant_blocks = []

    try:
        with open(transcript_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Flat format (testing / future compatibility)
                if "role" in entry and "content" in entry:
                    content = entry["content"]
                    if entry["role"] == "user":
                        _flush_assistant()
                        if isinstance(content, str):
                            content = [{"type": "text", "text": content}]
                        messages.append({"role": "user", "content": content})
                    elif entry["role"] == "assistant":
                        if isinstance(content, str):
                            content = [{"type": "text", "text": content}]
                        if isinstance(content, list):
                            assistant_blocks.extend(content)
                        else:
                            assistant_blocks.append({"type": "text", "text": str(content)})
                    continue

                item_type = entry.get("type")

                # --- response_item ---
                if item_type == "response_item":
                    payload = entry.get("payload", {})
                    ptype = payload.get("type")

                    if ptype == "message":
                        role = payload.get("role", "")
                        if role == "user":
                            _flush_assistant()
                            text = _extract_text_from_content_blocks(payload.get("content", []))
                            if is_synthetic_codex_user_message(text):
                                continue
                            if text:
                                messages.append({"role": "user", "content": [{"type": "text", "text": text}]})
                        elif role == "assistant":
                            # Only include final_answer (not reasoning/intermediary)
                            if payload.get("phase") != "final_answer":
                                continue
                            text = _extract_text_from_content_blocks(payload.get("content", []))
                            if text:
                                assistant_blocks.append({"type": "text", "text": text})

                    elif ptype == "local_shell_call":
                        action = payload.get("action", {})
                        command = action.get("command", [])
                        assistant_blocks.append({
                            "type": "tool_use",
                            "name": "shell",
                            "input": {"command": command},
                        })

                    elif ptype == "function_call":
                        name = payload.get("name", "unknown")
                        arguments = payload.get("arguments", "{}")
                        try:
                            inp = json.loads(arguments)
                        except (json.JSONDecodeError, TypeError):
                            inp = {"raw": arguments}
                        assistant_blocks.append({
                            "type": "tool_use",
                            "name": name,
                            "input": inp,
                        })

                    elif ptype == "function_call_output":
                        output = payload.get("output", "")
                        output_text = _extract_function_output_text(output)
                        if output_text:
                            assistant_blocks.append({
                                "type": "tool_result",
                                "content": _truncate(output_text),
                            })

                    elif ptype == "custom_tool_call":
                        name = payload.get("name", "unknown")
                        inp_str = payload.get("input", "{}")
                        try:
                            inp = json.loads(inp_str)
                        except (json.JSONDecodeError, TypeError):
                            inp = {"raw": inp_str}
                        assistant_blocks.append({
                            "type": "tool_use",
                            "name": name,
                            "input": inp,
                        })

                    elif ptype == "custom_tool_call_output":
                        output = payload.get("output", "")
                        output_text = _extract_function_output_text(output)
                        if output_text:
                            assistant_blocks.append({
                                "type": "tool_result",
                                "content": _truncate(output_text),
                            })

                    elif ptype == "web_search_call":
                        action = payload.get("action", {})
                        query = action.get("query", "") if isinstance(action, dict) else ""
                        if query:
                            assistant_blocks.append({
                                "type": "tool_use",
                                "name": "web_search",
                                "input": {"query": query},
                            })

                # --- event_msg ---
                elif item_type == "event_msg":
                    payload = entry.get("payload", {})
                    ptype = payload.get("type")

                    if ptype == "exec_command_end":
                        command = payload.get("command", [])
                        output = payload.get("aggregated_output", "")
                        exit_code = payload.get("exit_code")
                        status = payload.get("status", "")
                        # Add as tool_use + tool_result pair
                        assistant_blocks.append({
                            "type": "tool_use",
                            "name": "shell",
                            "input": {"command": command},
                        })
                        result_parts = []
                        if output:
                            result_parts.append(output)
                        if exit_code is not None and exit_code != 0:
                            result_parts.append(f"exit_code: {exit_code}")
                        if status and status != "completed":
                            result_parts.append(f"status: {status}")
                        if result_parts:
                            assistant_blocks.append({
                                "type": "tool_result",
                                "content": _truncate("\n".join(result_parts)),
                            })

                    elif ptype == "patch_apply_end":
                        changes = payload.get("changes", [])
                        status = payload.get("status", "")
                        if changes:
                            assistant_blocks.append({
                                "type": "tool_use",
                                "name": "patch",
                                "input": {"changes": changes},
                            })
                            if status:
                                assistant_blocks.append({
                                    "type": "tool_result",
                                    "content": f"status: {status}",
                                })

                    elif ptype == "mcp_tool_call_end":
                        result = payload.get("result", {})
                        result_text = ""
                        if isinstance(result, dict):
                            content_items = result.get("content", [])
                            if isinstance(content_items, list):
                                texts = []
                                for item in content_items:
                                    if isinstance(item, dict) and item.get("type") == "text":
                                        texts.append(item.get("text", ""))
                                result_text = "\n".join(texts)
                            elif isinstance(content_items, str):
                                result_text = content_items
                        elif isinstance(result, str):
                            result_text = result
                        if result_text:
                            assistant_blocks.append({
                                "type": "tool_result",
                                "content": _truncate(result_text),
                            })

    except OSError:
        pass

    _flush_assistant()
    return messages


def _extract_text_from_content_blocks(content_blocks: list) -> str:
    """Extract text from Codex content blocks (input_text/output_text)."""
    text_parts = []
    for block in content_blocks:
        if isinstance(block, dict) and block.get("type") in ("input_text", "output_text"):
            t = block.get("text", "").strip()
            if t:
                text_parts.append(t)
    return "\n".join(text_parts).strip()


def _extract_function_output_text(output) -> str:
    """Extract text from a function_call_output payload.

    The output can be either a plain string or a list of content items.
    """
    if isinstance(output, str):
        return output.strip()
    if isinstance(output, list):
        texts = []
        for item in output:
            if isinstance(item, dict) and item.get("type") in ("input_text", "text"):
                texts.append(item.get("text", ""))
        return "\n".join(texts).strip()
    return ""


def _truncate(text: str) -> str:
    """Truncate text to _MAX_TOOL_OUTPUT_CHARS."""
    if len(text) > _MAX_TOOL_OUTPUT_CHARS:
        return text[:_MAX_TOOL_OUTPUT_CHARS] + "... (truncated)"
    return text


# ---------------------------------------------------------------------------
# Recall: query composition and truncation
# ---------------------------------------------------------------------------


def compose_recall_query(
    latest_query: str,
    messages: list,
    recall_context_turns: int,
    recall_roles: list = None,
) -> str:
    """Compose a multi-turn recall query from conversation history.

    When recallContextTurns > 1, includes prior context above the latest
    user query. Format:

        Prior context:

        user: ...
        assistant: ...

        <latest query>
    """
    latest = extract_user_request(latest_query)
    if recall_context_turns <= 1 or not isinstance(messages, list) or not messages:
        return latest

    allowed_roles = set(recall_roles or ["user", "assistant"])
    contextual_messages = slice_last_turns_by_user_boundary(messages, recall_context_turns)

    context_lines = []
    for msg in contextual_messages:
        role = msg.get("role")
        if role not in allowed_roles:
            continue

        content = msg.get("content", "")
        if not isinstance(content, str):
            content = str(content)
        content = strip_memory_tags(content).strip()
        if role == "user":
            content = extract_user_request(content)
        if not content:
            continue

        if role == "user" and content == latest:
            continue

        context_lines.append(f"{role}: {content}")

    if not context_lines:
        return latest

    return "\n\n".join(
        [
            "Prior context:",
            "\n".join(context_lines),
            latest,
        ]
    )


def truncate_recall_query(query: str, latest_query: str, max_chars: int) -> str:
    """Truncate a composed recall query to max_chars.

    Preserves the latest user message. Drops oldest context lines first.
    """
    if max_chars <= 0:
        return query

    latest = latest_query.strip()
    if len(query) <= max_chars:
        return query

    latest_only = latest[:max_chars] if len(latest) > max_chars else latest

    if "Prior context:" not in query:
        return latest_only

    context_marker = "Prior context:\n\n"
    marker_index = query.find(context_marker)
    if marker_index == -1:
        return latest_only

    suffix_marker = "\n\n" + latest
    suffix_index = query.rfind(suffix_marker)
    if suffix_index == -1:
        return latest_only

    suffix = query[suffix_index:]
    if len(suffix) >= max_chars:
        return latest_only

    context_body = query[marker_index + len(context_marker) : suffix_index]
    context_lines = [line for line in context_body.split("\n") if line]

    kept = []
    for i in range(len(context_lines) - 1, -1, -1):
        kept.insert(0, context_lines[i])
        candidate = f"{context_marker}{chr(10).join(kept)}{suffix}"
        if len(candidate) > max_chars:
            kept.pop(0)
            break

    if kept:
        return f"{context_marker}{chr(10).join(kept)}{suffix}"
    return latest_only


# ---------------------------------------------------------------------------
# Turn slicing
# ---------------------------------------------------------------------------


def slice_last_turns_by_user_boundary(messages: list, turns: int) -> list:
    """Slice messages to the last N turns, where a turn starts at a user message."""
    if not isinstance(messages, list) or not messages or turns <= 0:
        return []

    user_turns_seen = 0
    start_index = -1

    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            user_turns_seen += 1
            if user_turns_seen >= turns:
                start_index = i
                break

    if start_index == -1:
        return list(messages)

    return messages[start_index:]


# ---------------------------------------------------------------------------
# Memory formatting (recall results → context string)
# ---------------------------------------------------------------------------


def format_memories(results: list) -> str:
    """Format recall results into human-readable text."""
    if not results:
        return ""
    lines = []
    for r in results:
        text = r.get("text", "")
        mem_type = r.get("type", "")
        mentioned_at = r.get("mentioned_at", "")
        type_str = f" [{mem_type}]" if mem_type else ""
        date_str = f" ({mentioned_at})" if mentioned_at else ""
        lines.append(f"- {text}{type_str}{date_str}")
    return "\n\n".join(lines)


def format_current_time() -> str:
    """Format current UTC time for recall context."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# Retention transcript formatting
# ---------------------------------------------------------------------------


def prepare_retention_transcript(
    messages: list,
    retain_roles: list = None,
    retain_full_window: bool = False,
    include_tool_calls: bool = False,
) -> tuple:
    """Format messages into a retention transcript.

    When include_tool_calls is True, outputs JSON with full message structure
    including tool calls and their inputs (matching Claude Code's format).
    Otherwise outputs the legacy text format with [role: ...]...[role:end] markers.

    Args:
        messages: List of message dicts with 'role' and 'content'.
        retain_roles: Roles to include (default: ['user', 'assistant']).
        retain_full_window: If True, retain all messages. If False, retain
            only the last turn (last user msg + responses).
        include_tool_calls: If True, output JSON format with full tool call data.

    Returns:
        (transcript_text, message_count) or (None, 0) if nothing to retain.
    """
    if not messages:
        return None, 0

    if retain_full_window:
        target_messages = messages
    else:
        last_user_idx = -1
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                last_user_idx = i
                break
        if last_user_idx == -1:
            return None, 0
        target_messages = messages[last_user_idx:]

    allowed_roles = set(retain_roles or ["user", "assistant"])

    if include_tool_calls:
        return _prepare_json_transcript(target_messages, allowed_roles)
    return _prepare_text_transcript(target_messages, allowed_roles)


def _prepare_json_transcript(messages: list, allowed_roles: set) -> tuple:
    """Format messages as JSON with full tool call data."""
    structured_messages = []
    for msg in messages:
        role = msg.get("role", "unknown")
        if role not in allowed_roles:
            continue

        content = msg.get("content", "")
        blocks = _strip_memory_tags_from_blocks(content, strip_ambient=(role == "user"))
        if not blocks:
            continue

        row = {"role": role, "content": blocks}
        if isinstance(msg.get('source_record'), dict):
            row['source_record'] = msg['source_record']
        structured_messages.append(row)

    if not structured_messages:
        return None, 0

    transcript = json.dumps(structured_messages, indent=None, ensure_ascii=False)
    if len(transcript.strip()) < 10:
        return None, 0

    return transcript, len(structured_messages)


def _prepare_text_transcript(messages: list, allowed_roles: set) -> tuple:
    """Format messages as legacy text with [role:]...[role:end] markers."""
    parts = []

    for msg in messages:
        role = msg.get("role", "unknown")
        if role not in allowed_roles:
            continue

        content = msg.get("content", "")
        if not isinstance(content, str):
            content = str(content)
        content = strip_memory_tags(content).strip()
        if role == "user":
            content = strip_ambient_context(content).strip()

        if not content:
            continue

        source = msg.get('source_record')
        provenance = ('[source_record: ' + json.dumps(source, ensure_ascii=False) + ']\n') if isinstance(source, dict) else ''
        parts.append(f"{provenance}[role: {role}]\n{content}\n[{role}:end]")

    if not parts:
        return None, 0

    transcript = "\n\n".join(parts)
    if len(transcript.strip()) < 10:
        return None, 0

    return transcript, len(parts)


def _strip_memory_tags_from_blocks(content, strip_ambient: bool = False) -> list:
    """Strip memory tags from content, handling both string and list formats.

    Returns a list of content blocks with memory tags removed.
    """
    if isinstance(content, str):
        cleaned = strip_memory_tags(content).strip()
        if strip_ambient:
            cleaned = strip_ambient_context(cleaned).strip()
        return [{"type": "text", "text": cleaned}] if cleaned else []

    if not isinstance(content, list):
        return []

    blocks = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type", "")

        if block_type == "text":
            text = strip_memory_tags(block.get("text", "")).strip()
            if strip_ambient:
                text = strip_ambient_context(text).strip()
            if text:
                blocks.append({"type": "text", "text": text})
        elif block_type in ("tool_use", "tool_result"):
            # Pass through tool blocks as-is
            blocks.append(block)

    return blocks
