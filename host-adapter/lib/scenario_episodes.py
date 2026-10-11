"""Strict source partitioning for separately readable Session episodes."""

from __future__ import annotations

from .scenario_source import revision_for_messages


EPISODE_BUNDLE_SCHEMA = "evolving-profile.scenario-episode-bundle.v1"
MAX_EPISODES = 64
DEFAULT_MAX_EPISODE_USER_MESSAGES = 4


def deterministic_size_boundaries(source: dict, *, max_user_messages: int = DEFAULT_MAX_EPISODE_USER_MESSAGES) -> list[str]:
    """Return safe user-message boundaries for long Sessions.

    This is a transport/coverage guard, not a semantic claim that the task
    changed. It never splits two user messages from the same host turn and
    leaves semantic boundary decisions to the model for all other positions.
    """
    if type(max_user_messages) is not int or max_user_messages < 1:
        raise ValueError("scenario_episode_size_limit_invalid")
    messages=source.get("messages") if isinstance(source,dict) else None
    if not isinstance(messages,list): raise ValueError("scenario_source_incomplete")
    user_seen=0; boundaries=[]; pending=False
    for index,row in enumerate(messages):
        if not isinstance(row,dict) or row.get("role")!="user": continue
        if pending:
            prior=messages[index-1] if index else {}
            if prior.get("turn_id")!=row.get("turn_id") or not prior.get("turn_id"):
                boundaries.append(row["evidence_id"]); pending=False; user_seen=0
        user_seen+=1
        if user_seen>=max_user_messages:
            pending=True
    return list(dict.fromkeys(boundaries))


def episode_id_for(session_id: str, start_message_id: str) -> str:
    session = str(session_id or "").strip()
    message = str(start_message_id or "").strip()
    if not session or not message:
        raise ValueError("scenario_episode_identity_invalid")
    return f"episode:{session}:{message}"


def partition_source(source: dict, episode_starts: list[str]) -> list[dict]:
    """Split a complete source at exact user-message IDs, without dropping rows."""
    if (not isinstance(source, dict) or source.get("status") != "complete"
            or not source.get("source_revision") or not source.get("thread_id")
            or not isinstance(source.get("messages"), list) or not source["messages"]):
        raise ValueError("scenario_source_incomplete")
    if not isinstance(episode_starts, list) or len(episode_starts) >= MAX_EPISODES:
        raise ValueError("scenario_episode_boundary_invalid")

    messages = source["messages"]
    if not isinstance(messages[0], dict) or messages[0].get("role") != "user":
        raise ValueError("scenario_episode_first_message_not_user")
    message_ids = [row.get("evidence_id") if isinstance(row, dict) else None for row in messages]
    if (any(not isinstance(value, str) or not value for value in message_ids)
            or len(set(message_ids)) != len(message_ids)):
        raise ValueError("scenario_source_message_ids_invalid")
    positions = {message_id: index for index, message_id in enumerate(message_ids)}
    boundary_positions = []
    for message_id in episode_starts:
        if not isinstance(message_id, str) or message_id not in positions:
            raise ValueError("scenario_episode_boundary_invalid")
        position = positions[message_id]
        if position <= 0 or messages[position].get("role") != "user":
            raise ValueError("scenario_episode_boundary_invalid")
        prior_message = messages[position - 1]
        prior_turn = prior_message.get("turn_id")
        current_turn = messages[position].get("turn_id")
        if (prior_message.get("role") == "user"
                and (not isinstance(prior_turn, str) or not prior_turn
                     or not isinstance(current_turn, str) or not current_turn)):
            raise ValueError("scenario_episode_turn_boundary_unverified")
        if (prior_turn is not None and current_turn is not None
                and prior_turn == current_turn):
            raise ValueError("scenario_episode_turn_split")
        boundary_positions.append(position)

    if (len(set(boundary_positions)) != len(boundary_positions)
            or boundary_positions != sorted(boundary_positions)):
        raise ValueError("scenario_episode_boundary_invalid")
    if len(boundary_positions) + 1 > MAX_EPISODES:
        raise ValueError("scenario_episode_limit_exceeded")

    positions_in_source = [0, *boundary_positions, len(messages)]
    episodes = []
    for index, (start, end) in enumerate(zip(positions_in_source, positions_in_source[1:])):
        segment = messages[start:end]
        user_messages = [row for row in segment if row.get("role") == "user"]
        if not user_messages:
            raise ValueError("scenario_episode_without_user_message")
        segment_revision = revision_for_messages(segment)
        episodes.append({
            "episode_id": episode_id_for(source["thread_id"], user_messages[0]["evidence_id"]),
            "parent_session_id": source["thread_id"],
            "parent_source_revision": source["source_revision"],
            "source_revision": segment_revision,
            "start_message_id": message_ids[start],
            "start_user_message_id": user_messages[0]["evidence_id"],
            "end_message_id": message_ids[end - 1],
            "message_ids": message_ids[start:end],
            "source_message_count": len(segment),
            "source_file_ids": sorted({str(row.get("source_path") or "") for row in segment
                                        if row.get("source_path")}),
            "source_offsets": [{"source_path": row.get("source_path"),
                                "byte_offset": row.get("byte_offset"),
                                "message_id": row["evidence_id"]} for row in segment],
            "_messages": segment,
        })
    return episodes


def validate_episode_bundle(source: dict, bundle: dict) -> dict:
    """Reject stale or structurally incomplete bundles before review/promotion."""
    if (not isinstance(bundle, dict) or bundle.get("schema") != EPISODE_BUNDLE_SCHEMA
            or bundle.get("status") != "source_linked_episode_draft"
            or bundle.get("thread_id") != source.get("thread_id")
            or bundle.get("parent_source_revision") != source.get("source_revision")
            or not isinstance(bundle.get("episodes"), list) or not bundle["episodes"]):
        raise ValueError("scenario_episode_bundle_stale_or_invalid")
    rows = bundle["episodes"]
    starts = [row.get("start_message_id") for row in rows[1:] if isinstance(row, dict)]
    if len(starts) != len(rows) - 1:
        raise ValueError("scenario_episode_bundle_invalid")
    expected = partition_source(source, starts)
    if len(rows) != len(expected):
        raise ValueError("scenario_episode_bundle_invalid")

    user_ids = [row["evidence_id"] for row in source["messages"] if row.get("role") == "user"]
    expected_decision_ids = user_ids[1:]
    decisions = bundle.get("boundary_decisions")
    if (not isinstance(decisions, list) or len(decisions) != len(expected_decision_ids)
            or [row.get("message_id") if isinstance(row, dict) else None for row in decisions]
            != expected_decision_ids):
        raise ValueError("scenario_episode_decision_coverage_invalid")
    normalized_decisions = []
    for row in decisions:
        decision, method = row.get("decision"), row.get("method")
        if (not isinstance(decision, str) or decision not in {"new_episode", "same_episode", "uncertain"}
                or not isinstance(method, str) or method not in {"model_boundary_review", "same_turn_join", "deterministic_size_boundary"}):
            raise ValueError("scenario_episode_decision_invalid")
        if decision == "uncertain":
            raise ValueError("scenario_episode_boundary_unresolved")
        if method in {"same_turn_join"} and decision != "same_episode":
            raise ValueError("scenario_episode_decision_invalid")
        if method == "deterministic_size_boundary" and decision != "new_episode":
            raise ValueError("scenario_episode_decision_invalid")
        normalized_decisions.append({"message_id": row["message_id"], "decision": decision,
                                     "method": method})
    proposed_starts = [row["message_id"] for row in normalized_decisions
                       if row["decision"] == "new_episode"]
    if proposed_starts != starts:
        raise ValueError("scenario_episode_decision_coverage_invalid")
    if bundle.get("unresolved_boundary_ids") != []:
        raise ValueError("scenario_episode_boundary_unresolved")

    from .scenario_model import validate_session_draft

    normalized = []
    for row, partition in zip(rows, expected):
        if not isinstance(row, dict):
            raise ValueError("scenario_episode_bundle_invalid")
        for key in ("episode_id", "start_message_id", "start_user_message_id", "end_message_id",
                    "message_ids", "source_message_count", "source_revision"):
            if row.get(key) != partition[key]:
                raise ValueError("scenario_episode_bundle_coverage_invalid")
        draft = row.get("draft")
        episode_source = {**source, "messages": partition["_messages"],
                          "source_revision": partition["source_revision"]}
        if (not isinstance(draft, dict)
                or draft.get("context_id") != "session:" + source["thread_id"]
                or draft.get("status") != "source_linked_draft"
                or draft.get("source_files") != source.get("source_files")
                or draft.get("source_message_count") != len(partition["_messages"])):
            raise ValueError("scenario_episode_bundle_coverage_invalid")
        rebuilt = validate_session_draft(episode_source, draft,
                                         model=str((draft or {}).get("summary_model") or "unknown"))
        if rebuilt.get("schema") != "evolving-profile.scenario-draft.v3":
            raise ValueError("scenario_episode_draft_v3_required")
        normalized_compact = " ".join(str(rebuilt["summaries"].get("compact") or "").split())
        normalized_full = " ".join(str(rebuilt["summaries"].get("full") or "").split())
        if normalized_compact == normalized_full:
            raise ValueError("scenario_promotion_layers_not_distinct")
        title = rebuilt["state"]["subject"]["text"][:120]
        if row.get("title") is not None and row.get("title") != title:
            raise ValueError("scenario_episode_title_mismatch")
        if (row.get("title_authority") is not None
                and row.get("title_authority") != "navigation_label_not_verified_fact"):
            raise ValueError("scenario_episode_title_mismatch")
        persisted_partition = {key: value for key, value in partition.items() if key != "_messages"}
        normalized.append({**persisted_partition, "draft": draft,
                           "title": title,
                           "title_authority": "navigation_label_not_verified_fact"})
    return {**bundle, "boundary_decisions": normalized_decisions, "episodes": normalized}


def revalidate_persisted_episode_source(source: dict, session_row: dict,
                                        episode_id: str) -> dict:
    """Revalidate the full stored partition before returning a selected episode."""
    if source.get("status") != "complete" or not source.get("messages"):
        return {"status": "episode_source_unavailable"}
    episodes = session_row.get("episodes") if isinstance(session_row, dict) else None
    if not isinstance(episodes, list) or not episodes:
        return {"status": "episode_not_found"}
    if source.get("thread_id") != session_row.get("session_id"):
        return {"status": "stale_source_changed"}
    if session_row.get("raw_source_files") and source.get("source_files") != session_row.get("raw_source_files"):
        return {"status": "stale_source_changed"}

    ids = [row.get("evidence_id") for row in source["messages"]]
    if (any(not isinstance(message_id, str) or not message_id for message_id in ids)
            or len(set(ids)) != len(ids)):
        return {"status": "stale_source_changed"}
    positions = {message_id: index for index, message_id in enumerate(ids)}
    selected_index = next((index for index, row in enumerate(episodes)
                           if isinstance(row, dict) and row.get("episode_id") == episode_id), None)
    if selected_index is None:
        return {"status": "episode_not_found"}
    stored_ids = []
    expected_start = 0
    for row in episodes:
        if (not isinstance(row, dict) or row.get("parent_session_id") != source["thread_id"]
                or row.get("parent_source_revision") != session_row.get("source_revision")):
            return {"status": "stale_source_changed"}
        message_ids = row.get("message_ids")
        if (not isinstance(message_ids, list) or not message_ids
                or any(mid not in positions for mid in message_ids)):
            return {"status": "stale_source_changed"}
        start, end = positions[message_ids[0]], positions[message_ids[-1]]
        if (start != expected_start or end - start + 1 != len(message_ids)
                or ids[start:end + 1] != message_ids
                or row.get("start_message_id") != message_ids[0]
                or row.get("end_message_id") != message_ids[-1]
                or revision_for_messages(source["messages"][start:end + 1]) != row.get("source_revision")):
            return {"status": "stale_source_changed"}
        expected_start = end + 1
        stored_ids.extend(message_ids)
    if ids[:len(stored_ids)] != stored_ids:
        return {"status": "stale_source_changed"}

    parent_changed = source.get("source_revision") != session_row.get("source_revision")
    has_appended_messages = len(ids) > len(stored_ids)
    if has_appended_messages and not parent_changed:
        return {"status": "stale_source_changed"}
    if has_appended_messages and selected_index == len(episodes) - 1:
        return {"status": "stale_source_changed"}
    selected = episodes[selected_index]
    return {"status": "span_current_parent_revision_changed" if parent_changed else "current",
            "episode": selected, "parent_source_revision_changed": parent_changed}
