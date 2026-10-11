"""Regression tests for compact, recoverable recall audit traces.

These tests protect the storage boundary: detailed retrieval traces belong in a
content-addressed cold archive, while hot audit JSON keeps the actual returned
results plus an immutable archive receipt.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

from .audit_module_test_context import isolated_audit_loader, isolated_audit_modules


@isolated_audit_loader
def _archive_module():
    module_path = Path(__file__).parents[1] / "evolving_profile_api" / "engine" / "audit_trace_archive.py"
    spec = importlib.util.spec_from_file_location("_audit_trace_archive_under_test", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@isolated_audit_loader
def _audit_module():
    """Load the logger with its DB boundaries replaced by a real minimal contract."""
    package_root = Path(__file__).parents[1] / "evolving_profile_api"
    engine_root = package_root / "engine"
    engine_package = types.ModuleType("evolving_profile_api.engine")
    engine_package.__path__ = [str(engine_root)]
    sys.modules[engine_package.__name__] = engine_package

    models = types.ModuleType("evolving_profile_api.models")
    models.RequestContext = object
    sys.modules[models.__name__] = models

    db_utils = types.ModuleType("evolving_profile_api.engine.db_utils")

    @contextlib.asynccontextmanager
    async def acquire_with_retry(pool, max_retries=1):
        yield pool.connection

    db_utils.acquire_with_retry = acquire_with_retry
    sys.modules[db_utils.__name__] = db_utils

    schema = types.ModuleType("evolving_profile_api.engine.schema")
    schema.fq_table_explicit = lambda table, schema_name: f'"{schema_name}".{table}'
    sys.modules[schema.__name__] = schema

    module_path = engine_root / "audit.py"
    spec = importlib.util.spec_from_file_location("evolving_profile_api.engine.audit", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_standalone_loaders_leave_real_api_modules_and_parent_attributes_unchanged():
    """Fake DB dependencies must not replace the next test's runtime imports."""
    before = {name:module for name,module in sys.modules.items()
              if name == 'evolving_profile_api' or name.startswith('evolving_profile_api.')}
    parents = {name:dict(vars(module)) for name,module in before.items() if isinstance(module,types.ModuleType)}
    private_before = sys.modules.get('_audit_trace_archive_under_test')
    _archive_module()
    _audit_module()
    after = {name:module for name,module in sys.modules.items()
             if name == 'evolving_profile_api' or name.startswith('evolving_profile_api.')}
    assert after == before
    assert sys.modules.get('_audit_trace_archive_under_test') is private_before
    for name,attributes in parents.items():
        assert vars(before[name]) == attributes


def test_loader_context_restores_runtime_parent_links_even_when_loading_raises():
    package = sys.modules['evolving_profile_api']
    engine = sys.modules['evolving_profile_api.engine']
    package_before = dict(vars(package))
    engine_before = dict(vars(engine))
    runtime_name = 'evolving_profile_api.engine.runtime'
    runtime_before = sys.modules.get(runtime_name)
    with pytest.raises(RuntimeError, match='loader failed'):
        with isolated_audit_modules():
            runtime = types.ModuleType(runtime_name)
            sys.modules[runtime_name] = runtime
            engine.runtime = runtime
            package.engine = runtime
            raise RuntimeError('loader failed')
    assert sys.modules.get(runtime_name) is runtime_before
    assert vars(package) == package_before
    assert vars(engine) == engine_before


def test_archive_round_trip_preserves_the_complete_trace(tmp_path):
    """Changing bytes, ordering, or the object path must break this recovery contract."""
    RecallTraceArchive = _archive_module().RecallTraceArchive

    trace = {
        "retrieval_results": [{"node_id": "n-1", "content": "source evidence", "score": 0.91}],
        "rrf_merged": [{"node_id": "n-1", "score": 0.42}],
    }

    archive = RecallTraceArchive(tmp_path)
    receipt = archive.archive_trace(trace)

    assert receipt.sha256
    assert receipt.bytes > 0
    assert archive.read_trace(receipt) == trace


def test_large_recall_trace_is_gzip_stored_but_hashes_uncompressed_evidence(tmp_path):
    """A cold archive that merely moves repeated JSON cannot reduce total audit storage."""
    RecallTraceArchive = _archive_module().RecallTraceArchive
    trace = {"retrieval_results": [{"node_id": str(index), "content": "repeated candidate body " * 200} for index in range(80)]}

    receipt = RecallTraceArchive(tmp_path).archive_trace(trace)

    assert receipt.encoding == "gzip"
    assert receipt.stored_bytes < receipt.bytes
    assert receipt.object_path.endswith(".json.gz")
    assert RecallTraceArchive(tmp_path).read_trace(receipt) == trace


def test_legacy_identity_manifest_does_not_block_a_new_gzip_receipt(tmp_path):
    """Changing storage encoding must not collide with an older receipt for the same logical trace."""
    module = _archive_module()
    trace = {"retrieval_results": [{"content": "repeated candidate " * 300} for _ in range(80)]}
    payload = module.canonical_trace_bytes(trace)
    digest = __import__("hashlib").sha256(payload).hexdigest()
    legacy_receipt = {
        "schema_version": 1,
        "sha256": digest,
        "bytes": len(payload),
        "object_path": f"objects/{digest[:2]}/{digest}.json",
    }
    legacy_object = tmp_path / legacy_receipt["object_path"]
    legacy_object.parent.mkdir(parents=True)
    legacy_object.write_bytes(payload)
    legacy_manifest = tmp_path / "manifests" / f"{digest}.json"
    legacy_manifest.parent.mkdir()
    legacy_manifest.write_text(json.dumps(legacy_receipt), encoding="utf-8")

    archive = module.RecallTraceArchive(tmp_path)
    assert archive.read_trace(legacy_receipt) == trace
    receipt = archive.archive_trace(trace)

    assert receipt.encoding == "gzip"
    assert (tmp_path / "manifests" / f"{digest}.gzip.json").exists()


def test_compact_recall_response_moves_only_trace_to_cold_archive(tmp_path):
    """Dropping results or retaining the full trace in hot JSON would lose either observability or space."""
    module = _archive_module()
    archive = module.RecallTraceArchive(tmp_path)
    response = {
        "results": [{"id": "memory-1", "content": "returned to the agent", "score": 0.99}],
        "trace": {"retrieval_results": [{"node_id": "memory-1", "content": "source evidence"}]},
        "query": "what was decided",
    }

    hot_response, metadata = module.compact_recall_response(response, archive)

    assert "trace" not in hot_response
    assert hot_response["results"] == response["results"]
    assert hot_response["query"] == "what was decided"
    assert metadata["recall_trace_archive"]["status"] == "archived"
    assert archive.read_trace(hot_response["trace_archive"]) == response["trace"]


def test_compact_recall_response_keeps_hot_trace_when_archive_write_fails():
    """A cold-store outage must degrade to a larger row, never an audit evidence gap."""
    module = _archive_module()

    class FailingArchive:
        def archive_trace(self, trace):
            raise OSError("archive volume unavailable")

    response = {"results": [{"id": "memory-1"}], "trace": {"visits": [{"node_id": "memory-1"}]}}

    hot_response, metadata = module.compact_recall_response(response, FailingArchive())

    assert hot_response == response
    assert metadata["recall_trace_archive"]["status"] == "failed"


def test_audit_logger_writes_compact_recall_json_and_recoverable_receipt(tmp_path):
    """Removing logger compaction would put the full trace back into PostgreSQL JSONB."""
    archive_module = _archive_module()
    audit_module = _audit_module()
    captured: list[tuple[str, tuple[object, ...]]] = []

    class Connection:
        async def execute(self, sql, *args):
            captured.append((sql, args))

    class Pool:
        connection = Connection()

    response = {
        "results": [{"id": "memory-1", "content": "the final returned item"}],
        "trace": {"retrieval_results": [{"node_id": "memory-1", "content": "large repeated candidate"}]},
    }

    async def write():
        logger = audit_module.AuditLogger(
            pool_getter=lambda: Pool(),
            schema_getter=lambda: "public",
            enabled=True,
            allowed_actions=[],
            trace_archive=archive_module.RecallTraceArchive(tmp_path),
        )
        await logger._safe_log(
            audit_module.AuditEntry(
                action="recall",
                transport="http",
                started_at=datetime.now(timezone.utc),
                response=response,
            )
        )

    asyncio.run(write())

    assert len(captured) == 1
    stored_response = json.loads(captured[0][1][7])
    stored_metadata = json.loads(captured[0][1][8])
    assert "trace" not in stored_response
    assert stored_response["results"] == response["results"]
    assert stored_metadata["recall_trace_archive"]["status"] == "archived"
    archive = archive_module.RecallTraceArchive(tmp_path)
    assert archive.read_trace(stored_response["trace_archive"]) == response["trace"]
