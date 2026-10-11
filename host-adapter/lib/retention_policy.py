"""One explicit-origin policy shared by capture and every batch submitter.

No content keywords, topic guesses, or directory-name heuristics establish origin.
Excluded data remain in local transcripts/queue for audit, never auto-deleted here.
"""

import hashlib
import json
import os
import re


def message_text(message):
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(b.get("text") or b.get("output_text") or "")
            for b in content
            if isinstance(b, dict) and b.get("type") not in ("tool_result", "tool_use")
        )
    return ""


def _instruction_text(text):
    """Mask cited material while keeping offsets into the original witness."""
    masked = text
    for pattern in (
        r"```[\s\S]*?```",
        r"`[^`]*`",
        r"“[^”]*”",
        r"「[^」]*」",
        r"『[^』]*』",
        r'"[^"\n]*"',
        r"'[^'\n]*'",
        r"(?m)^\s*>.*$",
    ):
        masked = re.sub(
            pattern,
            lambda m: (
                m.group()
                if re.fullmatch(
                    r'[“「『"\'`](?:长期)?(?:记忆|memory|memories)[”」』"\'`]',
                    m.group(),
                    re.IGNORECASE,
                )
                else " " * len(m.group())
            ),
            masked,
        )
    return masked


def turn_write_policy(messages, inherited=None):
    """A local instruction witness, never a topic/origin classification.

    Default authorization follows configured auto-retain. Explicit human write
    prohibitions override it for the task or session; quoted tool/assistant text
    cannot revoke or grant permission.
    """
    policy = {
        "version": 1,
        "knowledge_allowed": True,
        "scope": "turn",
        "reason": "default_auto_retain",
    }
    patterns = (
        r"(?:不要|不许|不得|禁止|别|无需|不准|请勿)\s*(?:再|自动)?(?:修改|改动|写入|写|保存|存入|存储|更新|记录|记住|保留|提炼)[^。！!\n；;]{0,28}?(?:记忆|memory|memories)",
        r"(?:不要|不许|不得|禁止|别|请勿)(?:把|将)[^。！!\n；;]{0,25}?(?:写入|存入|存到|保存到|记录到|加入)[^。！!\n；;]{0,12}?(?:记忆|memory|memories)",
        r"(?:do\s+not|don[’\']t|never)\s+(?:automatically\s+)?(?:save|store|write|update|retain|memorize|modify)[^.!\n;]{0,65}?(?:memory|memories)",
        r"(?:不要|不许|不得|禁止|别)\s*(?:做|进行)?(?:长期记忆|记忆)(?:写入|保存|更新|提炼)",
    )
    for index, message in enumerate(messages):
        if message.get("role") != "user":
            continue
        text = message_text(message)
        masked = _instruction_text(text)
        for pattern in patterns:
            for match in re.finditer(pattern, masked, flags=re.IGNORECASE):
                prefix = masked[max(0, match.start() - 12) : match.start()]
                if re.search(
                    r"(?:不是|并非|并不是|并不|不代表|不意味着|not\s+)\s*$",
                    prefix,
                    re.IGNORECASE,
                ):
                    continue
                if match.group().startswith("禁止") and re.search(
                    r"(?:不要|别|不用|不应)\s*$", prefix
                ):
                    continue
                witness = {
                    "role": "user",
                    "message_index": index,
                    "start": match.start(),
                    "end": match.end(),
                    "text": text[match.start() : match.end()],
                    "source_record": message.get("source_record"),
                    "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
                }
                scope = (
                    "session"
                    if re.search(
                        r"本会话|整个会话|这次会话|本次对话|this\s+(?:session|conversation)|until\s+I",
                        masked,
                        re.IGNORECASE,
                    )
                    else "task"
                )
                return {
                    "version": 1,
                    "knowledge_allowed": False,
                    "scope": scope,
                    "reason": "explicit_user_no_knowledge_write",
                    "witness": witness,
                }
        if inherited and not inherited.get("knowledge_allowed", True):
            permission = re.search(
                r"(?:现在|从现在起|本轮|这次)?(?:可以|允许|授权|恢复)(?:自动)?(?:写入|保存|存储|更新|修改)[^。！!\n；;]{0,20}?(?:记忆|memory|memories)",
                masked,
                re.IGNORECASE,
            )
            if permission and not re.search(
                r"(?:不是说|不是|并非|不|没有|未)\s*$",
                masked[max(0, permission.start() - 8) : permission.start()],
            ):
                return {
                    **policy,
                    "reason": "explicit_user_knowledge_write_permission",
                    "witness": {
                        "role": "user",
                        "message_index": index,
                        "start": permission.start(),
                        "end": permission.end(),
                        "text": text[permission.start() : permission.end()],
                        "source_record": message.get("source_record"),
                        "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    },
                }
            continuation = re.match(
                r"^\s*(?:继续|接着|再短|再改|换成|就按|continue\b|keep\s+going\b)",
                masked,
                re.IGNORECASE,
            )
            if inherited.get("scope") == "session" or continuation:
                return dict(inherited)
    return policy


def retention_turns(messages):
    """Turn ranges over the entire source; callers apply their capture cursor."""
    turns = []
    start = 0
    for index, message in enumerate(messages):
        if message.get("role") == "user" and index > start:
            turns.append(
                {"start": start, "end": index, "messages": messages[start:index]}
            )
            start = index
    if start < len(messages):
        turns.append(
            {"start": start, "end": len(messages), "messages": messages[start:]}
        )
    previous = None
    for turn in turns:
        turn["write_policy"] = turn_write_policy(turn["messages"], previous)
        previous = turn["write_policy"]
    return turns


def transcript_messages(content):
    """Read actual adapter envelopes, not arbitrary words inside content."""
    try:
        rows = json.loads(content)
        if isinstance(rows, list) and all(
            isinstance(r, dict) and "role" in r for r in rows
        ):
            return rows
    except (ValueError, TypeError):
        pass
    rows = []
    for match in re.finditer(
        r"(?m)^(?:\[source_record: (\{[^\n]*\})\]\n)?\[role: (user|assistant)\]\n([\s\S]*?)\n\[\2:end\](?:\n|$)",
        content,
    ):
        rows.append(
            {
                "role": match.group(2),
                "content": match.group(3),
                "source_record": json.loads(match.group(1)) if match.group(1) else {},
            }
        )
    return rows


def batch_content(items):
    rows = []
    for item in items:
        messages = item.get("source_messages") or transcript_messages(
            item.get("content", "")
        )
        if not messages:
            return "\n\n".join(
                f"[pending item {i + 1}/{len(items)}; session={r['session_id']}]\n{r['content']}"
                for i, r in enumerate(items)
            )
        for message in messages:
            source = {
                **(message.get("source_record") or {}),
                "queue_item_id": item["id"],
                "session_id": item["session_id"],
                "project": item["project"],
            }
            rows.append(
                {
                    **message,
                    "source_record": source,
                    "write_policy": item.get("write_policy") or {},
                }
            )
    return json.dumps(rows, ensure_ascii=False)


def item_write_exclusion(item):
    policy = item.get("write_policy") or {}
    if policy.get("knowledge_allowed") is False:
        return policy.get("reason") or "knowledge_write_prohibited"
    if item.get("source_text_available") is False:
        return "source_fragment_without_message_text"
    digest = policy.get("item_content_sha256")
    if (
        digest
        and digest != hashlib.sha256(item.get("content", "").encode()).hexdigest()
    ):
        return "write_policy_content_changed"
    source_digest = policy.get("source_messages_sha256")
    if (
        source_digest
        and source_digest
        != hashlib.sha256(
            json.dumps(
                item.get("source_messages") or [], ensure_ascii=False, sort_keys=True
            ).encode()
        ).hexdigest()
    ):
        return "write_policy_source_envelope_changed"
    if not policy:
        if (item.get("metadata") or {}).get("queue_part"):
            return "legacy_fragment_requires_policy"
        turns = retention_turns(transcript_messages(item.get("content", "")))
        if any(not turn["write_policy"]["knowledge_allowed"] for turn in turns):
            return "explicit_user_no_knowledge_write"
    return ""


def submission_metadata(batch):
    """Shared contract for all submitters; lineage is per item, not a new vote."""
    return {
        "source_attribution_required": "role_quote_v1",
        "retention_item_ids": ",".join(batch.get("item_ids", [])),
        "retention_write_policy": "per_turn_v1",
    }


def _path(value):
    return os.path.normpath(os.path.expanduser(str(value))) if value else ""


def retention_exclusion(project, session_ids, config):
    excluded = {_path(value) for value in config.get("retainExcludedCwds", []) if value}
    if _path(project) in excluded:
        return "configured_project_exclusion"
    ids = (
        str(session_ids).split(",")
        if isinstance(session_ids, str)
        else (session_ids or [])
    )
    excluded_sessions = {
        str(v) for v in config.get("retainExcludedSessionIds", []) if v
    }
    if any(str(v).strip() in excluded_sessions for v in ids):
        return "configured_session_exclusion"
    return ""


def allowed_retention_batches(batches, config):
    return [
        b
        for b in batches
        if not retention_exclusion(b.get("project"), b.get("session_ids"), config)
        and not any(item_write_exclusion(item) for item in b.get("items", []))
    ]
