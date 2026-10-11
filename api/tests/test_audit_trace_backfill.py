"""Tests for non-destructive archival of legacy recall audit traces."""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

from .audit_module_test_context import isolated_audit_loader


@isolated_audit_loader
def _module():
    package_root = Path(__file__).parents[1] / "evolving_profile_api"
    engine_root = package_root / "engine"
    root_package = types.ModuleType("evolving_profile_api")
    root_package.__path__ = [str(package_root)]
    sys.modules[root_package.__name__] = root_package
    engine_package = types.ModuleType("evolving_profile_api.engine")
    engine_package.__path__ = [str(engine_root)]
    sys.modules[engine_package.__name__] = engine_package
    archive_spec = importlib.util.spec_from_file_location(
        "evolving_profile_api.engine.audit_trace_archive", engine_root / "audit_trace_archive.py"
    )
    assert archive_spec and archive_spec.loader
    archive_module = importlib.util.module_from_spec(archive_spec)
    sys.modules[archive_spec.name] = archive_module
    archive_spec.loader.exec_module(archive_module)

    spec = importlib.util.spec_from_file_location(
        "evolving_profile_api.engine.audit_trace_backfill", engine_root / "audit_trace_backfill.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[archive_spec.name] = archive_module
    spec.loader.exec_module(module)
    return module, archive_module


def test_backfill_loader_preserves_real_package_version_schema_and_parent_links():
    """Returning a standalone helper must not leave fake public packages cached."""
    before = {name:module for name,module in sys.modules.items()
              if name == 'evolving_profile_api' or name.startswith('evolving_profile_api.')}
    parents = {name:dict(vars(module)) for name,module in before.items() if isinstance(module,types.ModuleType)}
    version = before['evolving_profile_api'].__version__
    _module()
    after = {name:module for name,module in sys.modules.items()
             if name == 'evolving_profile_api' or name.startswith('evolving_profile_api.')}
    assert after == before
    assert after['evolving_profile_api'].__version__ == version
    for name,attributes in parents.items():
        assert vars(before[name]) == attributes


def test_backfill_writes_verifiable_manifest_without_mutating_legacy_response(tmp_path):
    """Removing the trace from the source row here would violate the non-destructive migration contract."""
    module, archive_module = _module()
    response = {"results": [{"id": "memory-1"}], "trace": {"visits": [{"node_id": "memory-1"}]}}
    original = json.loads(json.dumps(response))
    archive = archive_module.RecallTraceArchive(tmp_path / "archive")

    manifest = module.backfill_trace("legacy-audit-id", response, archive, tmp_path / "manifests")

    assert response == original
    assert manifest["audit_id"] == "legacy-audit-id"
    assert manifest["status"] == "archived"
    assert archive.read_trace(manifest["trace_archive"]) == original["trace"]
    assert (tmp_path / "manifests" / "legacy-audit-id.json").exists()
