"""Incremental raw-message locators; no model calls or memory publication.

Uses a private sidecar, never changes the capture queue or scenario index.
Raw line hashes are rechecked at readback. Assistant claims stay claims.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import sqlite3
import uuid
from pathlib import Path
from .content import extract_user_request, is_synthetic_codex_user_message


def read_evidence(session_id, root, *, turn_id=None, max_chars=12000, cache_root=None):
    sid = str(uuid.UUID(str(session_id)))
    root = Path(root).expanduser()
    cache = Path(cache_root or Path.home() / ".evolving-profile/audit/raw-session-locators")
    paths = sorted(root.rglob(f"rollout-*-{sid}.jsonl")) if root.is_dir() else []
    archive = root.parent / "archived_sessions"
    if archive.is_dir():
        paths += sorted(archive.rglob(f"rollout-*-{sid}.jsonl"))
    base = {"source_type": "codex_thread_history", "claims_independently_verified": False,
            "boundary": "original_message_readback_not_artifact_or_external_fact_verification"}
    if not paths:
        return {**base, "status": "source_missing", "source": {}, "coverage": {"matched_messages": 0}}
    cache.mkdir(parents=True, exist_ok=True)
    cache.chmod(0o700)
    cache_db = cache / (sid + '.sqlite')
    cache_db.touch(exist_ok=True)
    cache_db.chmod(0o600)
    with (cache / (sid + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with sqlite3.connect(cache_db, timeout=10) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS checkpoint(path TEXT PRIMARY KEY, offset INTEGER, turn_id TEXT, inode INTEGER);
                CREATE TABLE IF NOT EXISTS message(path TEXT, offset INTEGER, hash TEXT, role TEXT, turn_id TEXT, text TEXT, at TEXT, PRIMARY KEY(path,offset));
            """)
            for path in paths:
                st = path.stat()
                prior = db.execute("SELECT offset,turn_id,inode FROM checkpoint WHERE path=?", (str(path),)).fetchone()
                start, turn = (prior[0], prior[1]) if prior else (0, "")
                if prior and (st.st_ino != prior[2] or st.st_size < start):
                    db.execute("DELETE FROM message WHERE path=?", (str(path),)); start, turn = 0, ""
                with path.open("rb") as stream:
                    stream.seek(start)
                    while True:
                        offset = stream.tell(); raw = stream.readline()
                        if not raw or not raw.endswith(b"\n"):
                            break
                        try:
                            row = json.loads(raw)
                        except (ValueError, UnicodeError):
                            start = stream.tell(); continue
                        payload = row.get("payload") or {}
                        if row.get("type") == "turn_context":
                            turn = str(payload.get("turn_id") or turn)
                        elif row.get("type") == "response_item" and payload.get("type") == "message":
                            role = payload.get("role")
                            if role in {"user", "assistant"} and (role == "user" or payload.get("phase") in {"final", "final_answer"}):
                                text = "\n".join(c.get("text", "") for c in payload.get("content", []) if isinstance(c, dict))
                                meaningful = role == 'assistant' or (bool(extract_user_request(text)) and not is_synthetic_codex_user_message(text) and not text.lstrip().startswith('<heartbeat>'))
                                if text.strip() and meaningful:
                                    db.execute("INSERT OR REPLACE INTO message VALUES(?,?,?,?,?,?,?)", (str(path), offset, hashlib.sha256(raw).hexdigest(), role, turn, text, row.get("timestamp")))
                        start = stream.tell()
                db.execute("INSERT OR REPLACE INTO checkpoint VALUES(?,?,?,?)", (str(path), start, turn, st.st_ino))
            db.commit()
            marks = ",".join("?" for _ in paths)
            clause = f"path IN ({marks})"; args = [str(p) for p in paths]
            if turn_id:
                clause += " AND turn_id=?"; args.append(str(turn_id))
            total = db.execute("SELECT count(*) FROM message WHERE " + clause, args).fetchone()[0]
            selected = db.execute("SELECT path,offset,hash,role,turn_id,text,at FROM message WHERE " + clause + " ORDER BY at DESC,path,offset DESC LIMIT 24", args).fetchall()
            text, locators, used, clipped = [], [], 0, False
            for path, offset, digest, role, turn, body, at in reversed(selected):
                with Path(path).open("rb") as stream:
                    stream.seek(offset); raw = stream.readline()
                if hashlib.sha256(raw).hexdigest() != digest:
                    db.execute('DELETE FROM message WHERE path=?', (path,))
                    db.execute('DELETE FROM checkpoint WHERE path=?', (path,))
                    db.commit()
                    return {**base, "status": "source_changed", "source": {}, "coverage": {"matched_messages": total}}
                remaining = max(0, int(max_chars) - used)
                if not remaining: break
                full_excerpt = f"[role: {role}; turn: {turn}; at: {at}]\n{body}"
                excerpt = full_excerpt[:remaining]
                clipped = clipped or len(excerpt) < len(full_excerpt)
                text.append(excerpt); used += len(excerpt)
                locators.append({"source_path": path, "byte_offset": offset, "raw_line_sha256": digest, "role": role, "turn_id": turn, "at": at})
            return {**base, "status": "source_read" if text else "source_empty",
                    "source": {"text": "\n\n".join(text), "locators": locators, "session_id": sid, "turn_id": turn_id, "verbatim_message_excerpts": True},
                    "coverage": {"matched_messages": total, "returned_messages": len(locators), "partial": clipped or total > len(locators)}}
