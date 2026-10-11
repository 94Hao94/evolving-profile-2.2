"""Incrementally review changed observations with the live Evolving Profile runtime."""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys

from mcp_runtime import load_repository
from observation_rebuild import atomic, fetch_inputs, observation_fingerprint


STATE_ROOT = Path(os.environ.get("EVOLVING_PROFILE_STATE_ROOT", str(Path.home() / ".evolving-profile")))
ROOT = STATE_ROOT / "guidance-v1"
LOCK = ROOT / "worker.lock"
STATE = ROOT / "worker-state.json"
CONFIG = ROOT / "guidance-v1.json"
SOURCE_ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "worker-runs"


def changed_observation_ids(current: dict[str, str], state: dict) -> list[str]:
    """Return IDs unseen or changed since the last successful fingerprint run.

    A legacy state only contains IDs. It forms a one-time baseline: only IDs
    absent from that baseline are processed, then every current ID receives a
    content fingerprint after a successful run.
    """
    fingerprints = state.get("seen_observation_fingerprints")
    if isinstance(fingerprints, dict) and fingerprints:
        return sorted(identifier for identifier, fingerprint in current.items() if fingerprints.get(identifier) != fingerprint)
    seen = {str(value) for value in state.get("seen_observation_ids") or []}
    return sorted(identifier for identifier in current if identifier not in seen)


def _run(script: str, *args: str, env: dict[str, str]) -> None:
    subprocess.run([sys.executable, str(SOURCE_ROOT / script), *args], check=True, env=env)


def last_completed_summary(previous: dict) -> dict | None:
    if isinstance(previous.get("last_completed"), dict):
        return previous["last_completed"]
    reports = sorted(RUNS.glob("*/observation-publication/publication-report.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not reports:
        return None
    try:
        report = json.loads(reports[0].read_text())
        run_dir = reports[0].parents[1]
        ids = json.loads((run_dir / "observation-ids.json").read_text())
        return {"run_dir": str(run_dir), "processed": len(ids),
                "publication": {key: report.get(key) for key in ("candidate_count", "active_published", "held_count", "failed")},
                "active_guidance": previous.get("active_guidance"), "active_models": previous.get("active_models"),
                "completed_at": report.get("completed_at")}
    except (OSError, ValueError, TypeError):
        return None


def prewarm_semantic_cache(env: dict[str, str]) -> dict:
    """Best-effort derived-vector refresh; it cannot block preference publication."""
    try:
        completed=subprocess.run([sys.executable,str(SOURCE_ROOT/'prewarm_semantic_cache.py')],check=True,env=env,capture_output=True,text=True,timeout=45)
        return json.loads(completed.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as exc:
        return {'status':'degraded','reason':type(exc).__name__}


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+") as lock:
        try: fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            atomic(STATE, {"status": "already_running", "at": dt.datetime.now(dt.timezone.utc).isoformat(), "llm_calls": 0}); return
        current_rows = fetch_inputs()
        current = {str(row["id"]): observation_fingerprint(row) for row in current_rows}
        previous = json.loads(STATE.read_text()) if STATE.exists() else {}
        changed = changed_observation_ids(current, previous)
        now = dt.datetime.now(dt.timezone.utc)
        if not changed:
            last_completed = last_completed_summary(previous)
            env = dict(os.environ, PYTHONPATH=str(SOURCE_ROOT), EVOLVING_PROFILE_STATE_ROOT=str(STATE_ROOT))
            semantic_cache = prewarm_semantic_cache(env)
            atomic(STATE, {"status": "idle_up_to_date", "at": now.isoformat(), "seen_observation_fingerprints": current,
                           "seen_observation_ids": sorted(current), "pending": 0, "llm_calls": 0,
                           "source_total": len(current), "last_successful_run": previous.get("last_successful_run"),
                           "last_completed": last_completed, "semantic_cache": semantic_cache}); return
        run_dir = RUNS / now.strftime("%Y%m%dT%H%M%SZ")
        observation_dir, publication_dir, model_dir = run_dir / "observation-rebuild", run_dir / "observation-publication", run_dir / "model-rebuild"
        run_dir.mkdir(parents=True, exist_ok=True)
        ids_path = run_dir / "observation-ids.json"; ids_path.write_text(json.dumps(changed), encoding="utf-8")
        env = dict(os.environ, PYTHONPATH=str(SOURCE_ROOT), EVOLVING_PROFILE_STATE_ROOT=str(STATE_ROOT))
        atomic(STATE, {"status": "rebuilding", "at": now.isoformat(), "pending": len(changed), "source_total": len(current),
                       "changed_observation_ids": changed, "run_dir": str(run_dir), "llm_calls": "bounded_to_changed_observations"})
        _run("observation_rebuild.py", str(observation_dir), "--ids-json", str(ids_path), env=env)
        _run("publish_observation_guidance.py", str(observation_dir / "observation-dispositions.json"), str(observation_dir / "source-map.json"), str(CONFIG), str(publication_dir), env=env)
        publication = json.loads((publication_dir / "publication-report.json").read_text())
        if publication.get("active_published"):
            _run("rebuild_models.py", str(CONFIG), str(model_dir), env=env)
        repo = load_repository(CONFIG)
        semantic_cache = prewarm_semantic_cache(env)
        completed = {"run_dir": str(run_dir), "processed": len(changed),
                     "publication": {key: publication.get(key) for key in ("candidate_count", "active_published", "held_count", "failed")},
                     "active_guidance": len(repo.active_units()), "active_models": len(repo.active_models()),
                     "completed_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        atomic(STATE, {"status": "completed", "at": completed["completed_at"], "seen_observation_fingerprints": current,
                       "seen_observation_ids": sorted(current), "pending": 0, "source_total": len(current), "processed": len(changed),
                       "run_dir": str(run_dir), "publication": completed["publication"],
                       "active_guidance": len(repo.active_units()), "active_models": len(repo.active_models()),
                       "last_successful_run": now.isoformat(), "last_completed": completed, "semantic_cache": semantic_cache,
                       "llm_calls": "recorded_in_run_reports"})


if __name__ == "__main__": main()
