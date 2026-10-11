#!/usr/bin/env python3
"""Direct deterministic Context backfill by the current agent.

This does not claim semantic model generation: it preserves each native seed
and publishes bounded Context tiers with explicit provenance and status.
"""
import argparse, json, os, sys
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib.context_summary import update_context_index, reproject_context_row
from lib.context_pipeline import write_progress

parser = argparse.ArgumentParser()
parser.add_argument('--index', default=os.path.expanduser('~/.evolving-profile/context/context-index.json'))
parser.add_argument('--queue', default=os.path.expanduser('~/.evolving-profile/context/context-pipeline.json'))
parser.add_argument('--progress', default=os.path.expanduser('~/.evolving-profile/context/context-pipeline-progress.json'))
args = parser.parse_args()
updated = datetime.now(timezone.utc).isoformat()
success = 0
def project_latest(index):
    global success
    rows = list(index.get('sessions') or []) + list(index.get('projects') or [])
    for row in rows:
        context_type = row.get('context_type')
        seed = str((row.get('summary') or {}).get('full') or (row.get('summary') or {}).get('standard') or '')
        if context_type not in ('session', 'project') or not seed:
            continue
        row.update(reproject_context_row(row))
        row['processed_at'] = updated
        success += 1
    return index
update_context_index(args.index, project_latest)
queue = json.loads(open(args.queue, encoding='utf-8').read())
for job in queue.get('jobs') or []:
    job['status'] = 'projection_complete_review_pending'
    job['completed_by'] = 'deterministic_source_projection'
    job['completed_at'] = updated
queue['status'] = 'review_pending'
progress = {'schema': 'evolving-profile.context-progress.v1', 'status': 'review_pending', 'phase': 'deterministic_source_projection', 'total': len(queue.get('jobs') or []), 'queued': 0, 'running': 0, 'retrying': 0, 'succeeded': 0, 'failed': 0, 'review_pending': success, 'updated_at': updated}
write_progress(args.progress, progress)
open(args.queue, 'w', encoding='utf-8').write(json.dumps(queue, ensure_ascii=False, indent=2) + '\n')
print(json.dumps({'status': 'review_pending', 'projected': success, 'progress': progress}, ensure_ascii=False))
