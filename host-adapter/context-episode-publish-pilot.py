#!/usr/bin/env python3
"""Publish a manually accepted episode directory to the local Context index."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from lib.context_pipeline import promote_episode_bundle
from lib.context_summary import read_context_index, update_context_index
from lib.scenario_model import (fingerprint_episode_bundle, fingerprint_episode_review,
                                validate_latest_episode_attempt, validate_latest_episode_review_attempt)
from lib.scenario_source import read_session_source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--session-root", default=str(Path.home() / ".codex/sessions"))
    parser.add_argument("--index", default=str(Path.home() / ".evolving-profile/context/context-index.json"))
    parser.add_argument("--draft-dir", default=str(Path.home() / ".evolving-profile/context/pilot-episode-drafts"))
    parser.add_argument("--review-dir", default=str(Path.home() / ".evolving-profile/context/pilot-episode-reviews"))
    parser.add_argument("--audit", required=True, help="Manual whole-session and episode-partition audit JSON")
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
    if source["status"] != "complete":
        raise ValueError("scenario_source_incomplete")

    draft_dir = Path(args.draft_dir).expanduser()
    review_dir = Path(args.review_dir).expanduser()
    bundle = json.loads((draft_dir / (args.session_id + ".json")).read_text(encoding="utf-8"))
    review = json.loads((review_dir / (args.session_id + ".json")).read_text(encoding="utf-8"))
    validate_latest_episode_attempt(draft_dir, args.session_id, bundle, source["source_revision"])
    validate_latest_episode_review_attempt(review_dir, args.session_id, review, source["source_revision"])
    audit = json.loads(Path(args.audit).expanduser().read_text(encoding="utf-8"))
    manual = next((item for item in (audit.get("manual_spot_check") or {}).get("items") or []
                   if item.get("context_id") == "session:" + args.session_id), None)
    if manual is None:
        raise ValueError("manual_review_missing")
    manual = {**manual, "reviewed_at": audit.get("audited_at")}
    published = promote_episode_bundle(rows[position], source, bundle, review, manual)
    receipt = {"schema": "evolving-profile.episode-promotion-receipt.v1", "session_id": args.session_id,
               "source_revision": source["source_revision"],
               "bundle_sha256": fingerprint_episode_bundle(bundle),
               "review_sha256": fingerprint_episode_review(review),
               "status": "eligible" if args.dry_run else "published",
               "episode_count": len(published.get("episodes") or []), "dry_run": args.dry_run}
    if not args.dry_run:
        backup = target.with_name(target.name + ".pre-episode-pilot-" +
                                  datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
        def replace(latest):
            current = list(latest.get('sessions') or [])
            selected = next((i for i, row in enumerate(current) if row.get('session_id') == args.session_id), None)
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
