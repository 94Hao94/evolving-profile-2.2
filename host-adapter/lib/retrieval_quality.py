"""Production import bridge for the Evolving Profile retrieval contract."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_SOURCE = Path('/Users/apple/Documents/Codex/2026-09-09/hind/work/guidance-v1/src/retrieval_quality.py')
_SPEC = importlib.util.spec_from_file_location('evolving_profile_retrieval_quality', _SOURCE)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f'cannot load retrieval quality source: {_SOURCE}')
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

build_retrieval_contract = _MODULE.build_retrieval_contract
evaluate_evidence_quality = _MODULE.evaluate_evidence_quality
project_record_index = _MODULE.project_record_index
