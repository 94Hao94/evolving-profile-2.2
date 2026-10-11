#!/usr/bin/env python3
"""Private-stdin protocol and detached persisted manual recovery runner."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

from lib.memory_recovery import RecoveryEngine, RecoveryError, read_json


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-root',default=os.environ.get('EVOLVING_PROFILE_STATE_ROOT',str(Path.home()/'.evolving-profile')))
    parser.add_argument('--run-job',metavar='UUID')
    args=parser.parse_args(argv)
    try:
        engine=RecoveryEngine(args.state_root)
        # Production roots come from trusted configuration, never caller JSON.
        if engine.state_root==Path.home()/'.evolving-profile':
            engine.config.setdefault('sessionRoot',str(Path.home()/'.codex/sessions'))
        if args.run_job:
            result=engine.run_job(args.run_job)
            while result['job'] and result['job']['status'] in {'running','queued'}:
                time.sleep(2)
                result=engine.run_job(args.run_job)
        else:
            payload=sys.stdin.read(1024*1024+1)
            if len(payload)>1024*1024: raise RecoveryError('request_too_large')
            try: request=json.loads(payload)
            except (ValueError,TypeError): raise RecoveryError('invalid_json') from None
            result=engine.dispatch(request)
        print(json.dumps(result,ensure_ascii=False),flush=True)
        return 0
    except RecoveryError as error:
        print(json.dumps({'error':{'code':str(error)}}),flush=True)
        return 2
    except Exception:
        print(json.dumps({'error':{'code':'recovery_unavailable'}}),flush=True)
        return 1


if __name__=='__main__': raise SystemExit(main())
