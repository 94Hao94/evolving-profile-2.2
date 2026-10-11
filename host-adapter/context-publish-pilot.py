#!/usr/bin/env python3
"""Publish one independently checked Session draft, keeping the old index recoverable."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.context_pipeline import promote_session_draft
from lib.context_summary import read_context_index, update_context_index
from lib.scenario_model import fingerprint_draft, validate_latest_v3_attempt
from lib.scenario_source import read_session_source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--session-root", default=str(Path.home() / ".codex/sessions"))
    parser.add_argument("--index", default=str(Path.home() / ".evolving-profile/context/context-index.json"))
    parser.add_argument("--draft-dir", default=str(Path.home() / ".evolving-profile/context/pilot-drafts"))
    parser.add_argument("--review-dir", default=str(Path.home() / ".evolving-profile/context/pilot-reviews"))
    parser.add_argument("--audit", default=str(Path.home() / ".evolving-profile/context/context-quality-pilot-audit.json"))
    parser.add_argument("--max-chars", type=int, default=500000)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    target = Path(args.index).expanduser()
    index = read_context_index(target)
    rows = list(index.get("sessions") or [])
    position = next((i for i, row in enumerate(rows) if row.get("session_id") == args.session_id), None)
    if position is None:
        raise ValueError("session_not_in_context_index")
    source = read_session_source(args.session_id, args.session_root, max_chars=args.max_chars)
    identity = source["thread_id"]
    draft = json.loads((Path(args.draft_dir).expanduser() / (identity + ".json")).read_text(encoding="utf-8"))
    if draft.get("schema") == "evolving-profile.scenario-draft.v3":
        validate_latest_v3_attempt(args.draft_dir, identity, draft, source["source_revision"])
    review = json.loads((Path(args.review_dir).expanduser() / (identity + ".json")).read_text(encoding="utf-8"))
    audit = json.loads(Path(args.audit).expanduser().read_text(encoding="utf-8"))
    manual = next((item for item in (audit.get("manual_spot_check") or {}).get("items") or []
                   if item.get("context_id") == "session:" + identity), None)
    if manual is None:
        raise ValueError("manual_review_missing")
    manual = {**manual, "reviewed_at": audit.get("audited_at")}
    published = promote_session_draft(rows[position], source, draft, review, manual)
    receipt = {"schema": "evolving-profile.scenario-promotion-receipt.v1", "session_id": identity,
               "source_revision": source["source_revision"], "status": "eligible" if args.dry_run else "published",
               "draft_sha256": fingerprint_draft(draft),
               "review_scope": published["review_scope"], "dry_run": args.dry_run}
    if not args.dry_run:
        backup = target.with_name(target.name + ".pre-pilot-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
        def replace(latest):
            current = list(latest.get('sessions') or [])
            selected = next((i for i, row in enumerate(current) if row.get('session_id') == identity), None)
            if selected is None or current[selected] != rows[position]:
                raise RuntimeError('context_index_changed_during_promotion')
            shutil.copy2(target, backup)
            current[selected] = published
            return {**latest, 'sessions': current}
        update_context_index(target, replace)
        receipt["backup"] = str(backup)
    print(json.dumps(receipt, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
