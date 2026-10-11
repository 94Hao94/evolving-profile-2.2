"""Independent source review and publication of observation-derived guidance."""

from __future__ import annotations

import datetime as dt
import json
import re
from hashlib import sha256

ROLE = re.compile(
    r"^\[role:\s*(user|assistant|tool|system|developer)\]\s*$|^\[(user|assistant|tool|system|developer):end\]\s*$",
    re.M,
)
QUOTED = re.compile(r'“[^”]*”|「[^」]*」|『[^』]*』|"[^"\n]*"|`[^`]*`|(?m:^\s*>.*$)')
AUTHORITY_FIELDS = {
    "source_role",
    "source_quote",
    "source_message_index",
    "source_start",
    "source_end",
    "independent_user_evidence",
    "evidence_group_id",
    "source_content_sha256",
}


def _integer(value):
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"0|[1-9][0-9]*", value):
        return int(value)
    raise ValueError("source_authority_invalid_integer")


def _true(value):
    return value is True or (isinstance(value, str) and value == "true")


def _body(message):
    value = message.get("content", message.get("text", ""))
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            str(part.get("text") or part.get("output_text") or "")
            for part in value
            if isinstance(part, dict)
            and part.get("type") not in ("tool_use", "tool_result")
        )
    return ""


def _record(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise ValueError("source_authority_invalid_record")
    if not isinstance(value, dict):
        raise ValueError("source_authority_invalid_record")
    return value


def _origin_group(record, role, original_digest):
    origin = {
        key: record[key]
        for key in (
            "transcript_path",
            "byte_offset",
            "raw_line_sha256",
            "session_id",
            "turn_id",
            "message_id",
        )
        if record.get(key) is not None
    }
    return sha256(
        (
            json.dumps(origin, ensure_ascii=False, sort_keys=True)
            + "\0"
            + role
            + "\0"
            + original_digest
        ).encode()
    ).hexdigest()


def _original_witness(message, body):
    """Independently check the extraction contract using original source bytes."""
    record = _record(message.get("source_record") or {})
    if "write_policy" in message:
        policy = message["write_policy"]
        if (
            not isinstance(policy, dict)
            or type(policy.get("version")) is not int
            or policy["version"] != 1
            or policy.get("knowledge_allowed") is not True
        ):
            raise ValueError("source_authority_knowledge_write_not_allowed")
    digest = sha256(body.encode()).hexdigest()
    frame = message.get("source_fragment")
    if frame is not None:
        if (
            not isinstance(frame, dict)
            or type(frame.get("version")) is not int
            or frame["version"] != 1
            or frame.get("role") != "user"
        ):
            raise ValueError("source_authority_invalid_fragment_role")
        base, end, total = (
            frame.get(key) for key in ("start", "end", "original_length")
        )
        if (
            any(type(v) is not int for v in (base, end, total))
            or not 0 <= base < end <= total
            or end - base != len(body)
        ):
            raise ValueError("source_authority_invalid_fragment_span")
        payload = {
            key: value for key, value in frame.items() if key != "witness_sha256"
        }
        witness_hash = sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        original_digest = frame.get("original_content_sha256")
        if (
            frame.get("content_sha256") != digest
            or frame.get("witness_sha256") != witness_hash
        ):
            raise ValueError("source_authority_fragment_content_changed")
        if (
            record.get("original_message_start") != base
            or record.get("original_message_end") != end
            or record.get("original_message_length") != total
            or record.get("original_message_content_sha256") != original_digest
        ):
            raise ValueError("source_authority_original_fragment_mismatch")
        group = _origin_group(record, "user", str(original_digest))
        if frame.get("evidence_group_id") != group:
            raise ValueError("source_authority_original_group_mismatch")
        ranges = frame.get("quoted_ranges")
    else:
        base = _integer(record.get("original_message_start", 0))
        total = _integer(record.get("original_message_length", len(body)))
        original_digest = record.get("original_message_content_sha256") or digest
        if base + len(body) > total or record.get(
            "original_message_end", base + len(body)
        ) != base + len(body):
            raise ValueError("source_authority_original_span_mismatch")
        ranges = record.get("original_message_quoted_ranges")
        if base or total != len(body):
            if not record.get("original_message_content_sha256") or ranges is None:
                raise ValueError("source_authority_original_witness_missing")
        elif original_digest != digest:
            raise ValueError("source_authority_original_content_changed")
        if ranges is None:
            ranges = []
        group = _origin_group(record, "user", str(original_digest))
    if not isinstance(original_digest, str) or not re.fullmatch(
        "[0-9a-f]{64}", original_digest
    ):
        raise ValueError("source_authority_original_hash_invalid")
    if not isinstance(ranges, list) or any(
        not isinstance(r, list)
        or len(r) != 2
        or any(type(v) is not int for v in r)
        or not 0 <= r[0] <= r[1] <= total
        for r in ranges
    ):
        raise ValueError("source_authority_quote_scope_invalid")
    # Re-evaluate the selected quote, even when its parent fact is a true user wrapper.
    ranges = ranges + [
        [base + m.start(), base + m.end()] for m in QUOTED.finditer(body)
    ]
    return {
        "base": base,
        "original_length": total,
        "original_digest": original_digest,
        "content_digest": digest,
        "group": group,
        "quoted_ranges": ranges,
        "record": record,
    }


def _user_quote_witness(
    text: str, quote: str, metadata: dict | None = None
) -> dict | None:
    if not isinstance(quote, str) or not quote.strip():
        return None
    try:
        messages = json.loads(text)
    except (ValueError, TypeError):
        messages = None
    if isinstance(messages, list):
        matches = []
        wanted_index = None
        if metadata and AUTHORITY_FIELDS.intersection(metadata):
            try:
                wanted_index = _integer(metadata.get("source_message_index"))
            except ValueError:
                return None
        for index, message in enumerate(messages):
            if wanted_index is not None and index != wanted_index:
                continue
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            body = _body(message)
            if not body:
                continue
            try:
                origin = _original_witness(message, body)
                lower, upper = 0, len(body)
                if metadata and AUTHORITY_FIELDS.intersection(metadata):
                    if (
                        not _true(metadata.get("independent_user_evidence"))
                        or metadata.get("source_role") != "user"
                    ):
                        return None
                    if _integer(metadata.get("source_message_index")) != index:
                        continue
                    supported = metadata.get("source_quote")
                    if not isinstance(supported, str) or not supported:
                        return None
                    lower, upper = (
                        _integer(metadata.get(key)) - origin["base"]
                        for key in ("source_start", "source_end")
                    )
                    if (
                        not 0 <= lower < upper <= len(body)
                        or body[lower:upper] != supported
                        or quote not in supported
                    ):
                        return None
                    if (
                        metadata.get("source_content_sha256")
                        != origin["content_digest"]
                    ):
                        return None
                    if metadata.get("evidence_group_id") != origin["group"]:
                        return None
                    if (
                        metadata.get("source_record") is not None
                        and _record(metadata["source_record"]) != origin["record"]
                    ):
                        return None
                    if (
                        metadata.get("original_source_content_sha256")
                        != origin["original_digest"]
                    ):
                        return None
                    if (
                        _integer(metadata.get("original_source_length"))
                        != origin["original_length"]
                    ):
                        return None
                start = body.find(quote, lower, upper)
                if start < 0 or body.find(quote, start + 1, upper) >= 0:
                    continue
                absolute_start, absolute_end = (
                    origin["base"] + start,
                    origin["base"] + start + len(quote),
                )
                if any(
                    a <= absolute_start and absolute_end <= b
                    for a, b in origin["quoted_ranges"]
                ):
                    return None
                local_basis = (
                    "original_message_content"
                    if origin["base"] or message.get("source_fragment")
                    else "decoded_message_content"
                )
                matches.append(
                    {
                        "span_start": absolute_start,
                        "span_end": absolute_end,
                        "source_span_basis": local_basis,
                        "source_message_index": index,
                        "source_record": origin["record"],
                        "user_body": body,
                        "source_authority_verified": True,
                        "verified_evidence_group_id": origin["group"],
                    }
                )
            except (ValueError, TypeError, KeyError):
                return None
        return matches[0] if len(matches) == 1 else None
    opened = None
    for match in ROLE.finditer(text):
        if match.group(1):
            if opened is None:
                opened = (match.group(1), match.end(), match.start())
        elif opened:
            if opened[0] == match.group(2) == "user":
                start = text.find(quote, opened[1], match.start())
                if start >= 0:
                    record = {}
                    headers = list(
                        re.finditer(
                            r"^\[source_record:\s*(\{.*\})\]\s*$",
                            text[: opened[1]],
                            re.M,
                        )
                    )
                    if headers:
                        try:
                            if not text[headers[-1].end() : opened[2]].strip():
                                record = json.loads(headers[-1].group(1))
                        except ValueError:
                            pass
                    if record.get("role") not in (None, "user"):
                        return None
                    body = text[opened[1] : match.start()]
                    local_start = start - opened[1]
                    if any(
                        m.start() <= local_start and local_start + len(quote) <= m.end()
                        for m in QUOTED.finditer(body)
                    ):
                        return None
                    # Typed fact offsets belong to decoded messages, not raw marker bytes.
                    if metadata and AUTHORITY_FIELDS.intersection(metadata):
                        return None
                    return {
                        "span_start": start,
                        "span_end": start + len(quote),
                        "source_span_basis": "original_source_text",
                        "source_record": record,
                        "user_body": body,
                        "source_authority_verified": True,
                    }
            if opened[0] == match.group(2):
                opened = None
    return None


def quote_in_user_span(
    text: str, quote: str, metadata: dict | None = None
) -> tuple[int, int] | None:
    witness = _user_quote_witness(text, quote, metadata)
    return (witness["span_start"], witness["span_end"]) if witness else None


def _source_metadata(item: dict) -> dict:
    metadata = item.get("memory_metadata") or item.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            return {}
    return metadata if isinstance(metadata, dict) else {}


def _independent_origin(item: dict, witness: dict) -> str:
    metadata = _source_metadata(item)
    flag = metadata.get("independent_user_evidence")
    if ("independent_user_evidence" in metadata and not _true(flag)) or metadata.get(
        "source_role"
    ) not in (None, "user"):
        raise ValueError("source_not_independent_user_evidence")
    if metadata.get("statement_kind") in {
        "assistant_proposal",
        "assistant_claim",
        "assistant_statement",
        "source_repetition",
        "quoted_repetition",
        "hypothetical",
    }:
        raise ValueError("source_not_independent_user_evidence")
    record = witness.get("source_record") or metadata.get("source_record") or {}
    if (
        isinstance(record, dict)
        and record.get("session_id")
        and (record.get("message_id") or record.get("raw_line_sha256"))
    ):
        if witness.get("verified_evidence_group_id"):
            return "origin:" + witness["verified_evidence_group_id"]
        identity = {
            key: record.get(key)
            for key in ("session_id", "turn_id", "message_id", "raw_line_sha256")
        }
        return (
            "origin:"
            + sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        )
    # Unknown legacy transports cannot create extra votes for the same original
    # human span simply by being copied into a different document.
    raise ValueError("source_not_independent_user_evidence_unknown_origin")


def verified_evidence_ref(
    item: dict, bank_id: str, witness_ref: str
) -> tuple[dict, str]:
    metadata = _source_metadata(item)
    quote = item.get("quote")
    if not isinstance(quote, str) or not quote:
        raise ValueError("quote_source_authority_invalid_quote")
    if "independent_user_evidence" in metadata and not _true(
        metadata["independent_user_evidence"]
    ):
        raise ValueError("source_not_independent_user_evidence")
    if "independent_user_evidence" in metadata and (
        not isinstance(metadata.get("source_quote"), str)
        or quote not in metadata["source_quote"]
    ):
        raise ValueError("quote_source_authority_mismatch")
    text = item.get("source_text")
    if not isinstance(text, str) or not text:
        raise ValueError("quote_source_authority_original_missing")
    witness = _user_quote_witness(text, quote, metadata)
    if not witness:
        raise ValueError("quote_source_authority_not_in_complete_user_span")
    origin = _independent_origin(item, witness)
    digest = sha256(text.encode()).hexdigest()
    ref = {
        "bank_id": bank_id,
        "memory_id": item["memory_id"],
        "document_id": item["document_id"],
        "chunk_id": item.get("chunk_id"),
        "source_revision": item["memory_revision"],
        "source_sha256": digest,
        **{key: value for key, value in witness.items() if key != "user_body"},
        "evidence_group_id": origin,
        "quote": quote,
        "stored_role": "user",
        "origin": "user_direct",
        "statement_kind": "request",
        "event_at": item.get("event_at"),
        "stored_at": item.get("stored_at") or "unknown",
        "human_author_verified": False,
        "origin_witness_ref": witness_ref,
        "origin_witness_sha256": digest,
    }
    return ref, origin


def build_observation_proposal(
    classification: dict, evidence: list[dict], bank_id: str
) -> dict:
    if classification.get("disposition") != "guidance_candidate":
        raise ValueError("not_guidance_candidate")
    if len(evidence) < 2:
        raise ValueError("insufficient_independent_source_families")
    refs = []
    origins = set()
    for item in evidence:
        ref, origin = verified_evidence_ref(
            item,
            bank_id,
            "observation-rebuild:" + classification["id"] + ":" + item["memory_id"],
        )
        origins.add(origin)
        refs.append(ref)
    if len(origins) < 2:
        raise ValueError("insufficient_independent_original_user_sources")
    return {
        "proposal_id": "observation-guidance:" + classification["id"],
        "operation": "create",
        "target_id": "observation-guidance:" + classification["id"],
        "base_revision": None,
        "nature": "inferred_pattern",
        "primary_category": classification["primary_category"],
        "related_categories": classification.get("related_categories") or [],
        "text": classification["text"],
        "applies_when": classification.get("applies_when") or [],
        "exceptions": classification.get("exceptions") or [],
        "effect_on_action": classification.get("effect_on_action")
        or "在匹配范围内调整行动",
        "scope": {
            "user_id": "liuzhongyang",
            "agent_roles": [],
            "project_ids": [],
            "task_ids": [],
            "domains": [],
            "media": [],
        },
        "evidence_refs": refs,
        "source_family_ids": sorted(origins),
        "preference_kind": classification.get("preference_kind") or "behavior_pattern",
        "polarity": classification.get("polarity") or "neutral",
        "scope_level": classification.get("scope_level") or "domain",
        "validity_kind": classification.get("validity_kind") or "context_sensitive",
        "confidence_inputs": {
            "independent_original_user_origins": len(origins),
            "source_span_verified": True,
        },
        "support_count": len(origins),
        "contradiction_count": 0,
        "source_turn_ids": sorted(
            {
                str(ref.get("source_record", {}).get("turn_id"))
                for ref in refs
                if isinstance(ref.get("source_record"), dict)
                and ref["source_record"].get("turn_id")
            }
        ),
        "supersedes": classification.get("supersedes") or [],
        "superseded_by": [],
        "cross_cutting": bool(classification.get("cross_cutting", False)),
    }


def publication_review(
    proposal: dict,
    reviewer_model: str = "qwen3.7-plus-independent-source-review",
    *,
    semantic_review: dict | None = None,
    source_snapshots: list[dict] | None = None,
) -> dict:
    from schemas import validate_proposal

    if not isinstance(semantic_review, dict) or not (
        semantic_review.get("support") == "supported"
        and semantic_review.get("scope_ok") is True
        and semantic_review.get("conditions_preserved") is True
        and semantic_review.get("source_role_ok") is True
        and semantic_review.get("hypothetical_only") is False
        and semantic_review.get("conflicts") == []
    ):
        raise ValueError("independent_semantic_review_required")
    refs = proposal.get("evidence_refs") or []
    if any(ref.get("source_authority_verified") is not True for ref in refs):
        if not source_snapshots:
            raise ValueError("independent_source_contract_not_verified")
        checked = []
        for ref in refs:
            snapshot = next(
                (
                    item
                    for item in source_snapshots
                    if item.get("memory_id") == ref.get("memory_id")
                    and item.get("document_id") == ref.get("document_id")
                ),
                None,
            )
            if not snapshot or snapshot.get("memory_revision") != ref.get(
                "source_revision"
            ):
                raise ValueError("legacy_source_authority_snapshot_outdated")
            digest = sha256(str(snapshot.get("source_text") or "").encode()).hexdigest()
            if (
                ref.get("source_sha256") != digest
                or ref.get("origin_witness_sha256") != digest
            ):
                raise ValueError("legacy_source_authority_snapshot_changed")
            verified, origin = verified_evidence_ref(
                {**snapshot, "quote": ref.get("quote")},
                ref["bank_id"],
                ref["origin_witness_ref"],
            )
            checked.append((verified, origin))
        required = 2 if proposal.get("nature") == "inferred_pattern" else 1
        if len({origin for _, origin in checked}) < required:
            raise ValueError("insufficient_independent_original_user_sources")
    if validate_proposal(proposal)["status"] != "supported" or not refs:
        raise ValueError("independent_source_contract_not_verified")
    candidate_id = str(proposal.get("proposal_id", "")).removeprefix(
        "observation-guidance:"
    )
    if semantic_review.get("id") != candidate_id:
        raise ValueError("independent_semantic_review_candidate_mismatch")
    reviewed = {
        (ref.get("memory_id"), ref.get("quote"))
        for ref in semantic_review.get("evidence", [])
        if isinstance(ref, dict)
    }
    if any(
        (ref.get("memory_id"), ref.get("quote")) not in reviewed
        for ref in proposal["evidence_refs"]
    ):
        raise ValueError("independent_semantic_review_evidence_mismatch")
    return {
        "support": "supported",
        "scope_ok": True,
        "conditions_preserved": True,
        "source_role_ok": True,
        "hypothetical_only": False,
        "conflicts": [],
        "source_witness_hash": sha256(
            "\x1f".join(
                ref["origin_witness_sha256"] for ref in proposal["evidence_refs"]
            ).encode()
        ).hexdigest(),
        "reviewer_model": reviewer_model,
        "reviewed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "independent_semantic_review": semantic_review,
        "independent_semantic_review_sha256": sha256(
            json.dumps(semantic_review, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest(),
    }
