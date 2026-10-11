"""Small, replaceable current-task projection; never a long-term user fact."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from lib.content import extract_task_user_request


class TaskStateStore:
    def __init__(self, root: Path, max_age_seconds: int = 86400):
        self.root = Path(root)
        self.max_age_seconds = max_age_seconds

    def _key(self, session_id: str) -> str:
        return hashlib.sha256(str(session_id).encode()).hexdigest()

    def _path(self, session_id: str) -> Path:
        return self.root / f"{self._key(session_id)}.json"

    @contextmanager
    def _lock(self, session_id: str, *, exclusive: bool):
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self.root / f"{self._key(session_id)}.lock"
        with lock_path.open("a+") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _load_unlocked(self, session_id: str) -> dict | None:
        try:
            value = json.loads(self._path(session_id).read_text(encoding="utf-8"))
            expires = float(value.get("expires_at_epoch") or 0)
            if expires:
                return None if time.time() >= expires else value
            if time.time() - float(value.get("updated_at_epoch") or 0) > self.max_age_seconds:
                return None
            return value
        except (OSError, ValueError, TypeError):
            return None

    def load(self, session_id: str) -> dict | None:
        with self._lock(session_id, exclusive=False):
            return self._load_unlocked(session_id)

    def _write_unlocked(self, session_id: str, value: dict) -> dict:
        target = self._path(session_id)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.root,
                prefix=target.name + ".", suffix=".tmp", delete=False,
            ) as stream:
                temporary_path = Path(stream.name)
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, target)
            directory_fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            return value
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _fresh_metadata(self, previous: dict | None, update_reason: str) -> dict:
        now = time.time()
        expires = now + self.max_age_seconds
        return {
            "schema": "evolving-profile.task-state.v2",
            "version": int((previous or {}).get("version") or 0) + 1,
            "update_reason": str(update_reason or "unspecified")[:160],
            "updated_at_epoch": now,
            "expires_at_epoch": expires,
            "expires_at": datetime.fromtimestamp(expires, timezone.utc).isoformat(),
            "authority": "working_projection_current_prompt_wins",
        }

    def record(self, session_id: str, prompt: str, turn_id: str | None,
               hook_invocation_id: str | None, *, continuation: bool,
               update_reason: str = "prompt_ingress") -> dict:
        with self._lock(session_id, exclusive=True):
            existing = self._load_unlocked(session_id)
            current_message = " ".join(extract_task_user_request(str(prompt or "")).split())
            if not current_message:
                # A host context message is not a new mission or continuation.
                # Keep the existing task's version, identity and expiry intact.
                return existing or {}
            previous = existing if continuation else None
            if previous:
                prior_objective = extract_task_user_request(str(previous.get('current_objective') or ''))
                previous = {**previous, 'current_objective': prior_objective} if prior_objective else None
            objective = (previous or {}).get("current_objective") or current_message
            context = objective if previous and objective != current_message else None
            value = {
                **(previous or {}),
                **self._fresh_metadata(existing, update_reason),
                "session_id": session_id,
                "turn_id": turn_id,
                "hook_invocation_id": hook_invocation_id,
                "current_objective": objective,
                "current_message": current_message,
                "continuation": bool(previous and continuation),
                "continuation_context": context,
                "source": "current_prompt" if not previous else "prior_active_task",
            }
            return self._write_unlocked(session_id, value)

    def update(self, session_id: str, patch: dict, turn_id: str | None = None,
               hook_invocation_id: str | None = None, *, expected_version: int | None = None,
               update_reason: str = "agent_update") -> dict:
        with self._lock(session_id, exclusive=True):
            previous = self._load_unlocked(session_id) or {
                "schema": "evolving-profile.task-state.v2", "session_id": session_id,
            }
            current_version = int(previous.get("version") or 0)
            if expected_version is not None and int(expected_version) != current_version:
                raise ValueError(
                    f"stale_task_state_version: expected {expected_version}, current {current_version}"
                )
            allowed = (
                "current_objective", "current_message", "constraints", "completed",
                "unresolved", "objects", "source_versions",
            )
            value = {**previous, **{key: patch[key] for key in allowed if key in patch}}
            value.update(
                self._fresh_metadata(previous, update_reason),
                turn_id=turn_id or previous.get("turn_id"),
                hook_invocation_id=hook_invocation_id or previous.get("hook_invocation_id"),
                source="agent_explicit_task_update",
            )
            return self._write_unlocked(session_id, value)
