#!/usr/bin/env python3
"""Discover Session changes and process bounded background Scenario jobs."""
import argparse
import json
import os
import time
from pathlib import Path

from lib.context_incremental import ContextIncremental, reassess_source_jobs, summary_runtime_permission


def load_provider(path, settings_path=None):
    """Match EP recovery precedence: saved primary, then existing API profile."""
    config = {}
    profile = Path(path).expanduser()
    for line in profile.read_text().splitlines() if profile.is_file() else []:
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            config[key] = value
    settings = json.loads(Path(settings_path).read_text()) if settings_path and Path(settings_path).is_file() else {}
    if not isinstance(settings,dict): raise ValueError('ep_provider_configuration_unavailable')
    primary = (settings.get('providers') or {}).get('primary') or {}
    if not isinstance(primary,dict): raise ValueError('ep_provider_configuration_unavailable')
    for field,key in (('model','MODEL'),('base_url','BASE_URL'),('api_key','API_KEY')):
        name = 'EVOLVING_PROFILE_API_LLM_' + key
        config[name] = primary.get(field) or config.get(name) or config.get('HINDSIGHT_API_LLM_' + key)
        if not config[name]: raise ValueError('ep_provider_configuration_unavailable')
    return config


def recording_enabled(state_root):
    return summary_runtime_permission(state_root)['allowed']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-root', default=str(Path.home()/'.evolving-profile'))
    parser.add_argument('--session-root', default=str(Path.home()/'.codex/sessions'))
    parser.add_argument('--provider-env', help='Existing EP provider profile; no Codex model substitution')
    parser.add_argument('--bank-id', default=os.environ.get('EVOLVING_PROFILE_BANK_ID'), help='Explicit owning Bank; no inferred assignment')
    parser.add_argument('--session-id', action='append', help='Limit discovery and processing to exact Session IDs for scoped catch-up')
    parser.add_argument('--new-after', help='Only discover previously untracked Sessions created after this ISO time; existing jobs still update')
    parser.add_argument('--discover-only', action='store_true')
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--reassess-source', action='store_true', help='Read-only source qualification plan; no discovery or model calls')
    parser.add_argument('--apply-reassessment', action='store_true', help='Apply only a reviewed exact-session reassessment plan')
    parser.add_argument('--expected-plan-sha256', help='Required dry-run digest for reassessment apply')
    parser.add_argument('--process', action='store_true', help='Explicitly allow configured-provider background summaries')
    parser.add_argument('--watch', action='store_true')
    parser.add_argument('--scan-limit', type=int, default=128)
    parser.add_argument('--max-jobs', type=int, default=4)
    parser.add_argument('--interval', type=int, default=30)
    parser.add_argument('--debounce', type=int, default=30)
    args = parser.parse_args()
    if not 1 <= args.max_jobs <= 32 or not 1 <= args.interval <= 3600 or not 0 <= args.debounce <= 3600:
        parser.error('invalid worker bounds')
    if args.process and args.discover_only: parser.error('choose process or discover-only')
    if args.apply_reassessment and not args.reassess_source: parser.error('apply requires reassess-source')
    if args.reassess_source:
        if args.process or args.watch or args.status or args.discover_only:
            parser.error('reassessment cannot be combined with worker execution')
        try:
            receipt = reassess_source_jobs(args.state_root, args.session_root, session_ids=args.session_id,
                apply=args.apply_reassessment, expected_plan_sha256=args.expected_plan_sha256)
        except ValueError as error:
            parser.error(str(error))
        print(json.dumps(receipt, ensure_ascii=False), flush=True)
        return 0
    worker = ContextIncremental(args.state_root, args.session_root, debounce=args.debounce, bank_id=args.bank_id, session_ids=args.session_id, new_after=args.new_after)
    provider_path = args.provider_env or str(Path(args.state_root)/'profiles/evolving-profile-api.env')
    while True:
        if not args.status and not recording_enabled(args.state_root):
            print(json.dumps({'status':'disabled','model_calls':0}), flush=True)
            if not args.watch: return 0
            time.sleep(args.interval)
            continue
        discovery = {} if args.status else worker.discover(limit=args.scan_limit)
        results = []
        if args.process and not args.status:
            try: config = load_provider(provider_path,Path(args.state_root)/'config/runtime-settings.json')
            except (OSError, ValueError):
                print(json.dumps({'status':'waiting_provider_configuration', 'progress':worker.progress(), 'model_calls':0}))
                if not args.watch: return 2
            else:
                for _ in range(args.max_jobs):
                    result = worker.run_once(config)
                    results.append(result)
                    if result['status'] == 'idle': break
        print(json.dumps({'discovery':discovery, 'jobs':results, 'progress':worker.progress(),
                          'model_calls':0 if not args.process else 'background_processor_calls_not_counted'}, ensure_ascii=False), flush=True)
        if not args.watch: return 0
        time.sleep(args.interval)


if __name__ == '__main__':
    raise SystemExit(main())
