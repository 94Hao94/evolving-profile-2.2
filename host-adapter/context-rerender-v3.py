#!/usr/bin/env python3
"""Re-render a source-linked V3 state after renderer changes without model recall."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "guidance"))

from lib.scenario_model import fingerprint_draft, validate_session_draft
from lib.scenario_source import read_session_source
from lib.scenario_state_v3 import validate_state_draft
from observation_rebuild import atomic


def rebuild_rerender_draft(source: dict, draft: dict) -> dict:
    if draft.get("source_revision") != source.get("source_revision"):
        raise ValueError("scenario_source_revision_mismatch")
    rebuilt = validate_state_draft(source,draft.get('state'),model=str(draft.get('summary_model') or 'unknown'),
                                   user_claim_protocol=draft.get('state_claim_protocol'))
    for key in ("selection_coverage", "source_chunk_char_limit"):
        if key in draft:
            rebuilt[key] = draft[key]
    return validate_session_draft(source, rebuilt, model=str(draft.get("summary_model") or "unknown"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--session-root", default=str(Path.home() / ".codex/sessions"))
    parser.add_argument("--draft-dir", default=str(Path.home() / ".evolving-profile/context/pilot-drafts-v3"))
    parser.add_argument("--max-chars", type=int, default=500000)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    source = read_session_source(args.session_id, args.session_root, max_chars=args.max_chars)
    if source["status"] != "complete":
        raise ValueError("scenario_source_incomplete")
    root = Path(args.draft_dir).expanduser()
    target = root / (source["thread_id"] + ".json")
    draft = json.loads(target.read_text(encoding="utf-8"))
    if draft.get("schema") != "evolving-profile.scenario-draft.v3":
        raise ValueError("scenario_v3_draft_required")
    if draft.get("source_revision") != source["source_revision"]:
        raise ValueError("scenario_source_revision_mismatch")
    marker = root / ".attempts" / (source["thread_id"] + ".json")
    if not args.dry_run:
        atomic(marker, {"status": "pending", "source_revision": source["source_revision"],
                        "method": "revalidated_previous_state"})
    rebuilt = rebuild_rerender_draft(source, draft)
    before_hash = fingerprint_draft(draft)
    after_hash = fingerprint_draft(rebuilt)
    receipt = {"schema": "evolving-profile.scenario-rerender.v1", "method": "revalidated_previous_state",
               "session_id": source["thread_id"], "source_revision": source["source_revision"],
               "prior_draft_sha256": before_hash, "draft_sha256": after_hash,
               "status": "eligible" if args.dry_run else "rerendered", "dry_run": args.dry_run}
    if not args.dry_run:
        backup = root / ".history" / f"{source['thread_id']}-{before_hash}.json"
        backup.parent.mkdir(parents=True, exist_ok=True)
        if not backup.exists():
            shutil.copy2(target, backup)
        atomic(target, rebuilt)
        atomic(marker, {"status": "succeeded", "source_revision": source["source_revision"],
                        "draft_sha256": after_hash, "method": "revalidated_previous_state",
                        "prior_draft_sha256": before_hash})
        receipt["backup"] = str(backup)
    print(json.dumps(receipt, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
