"""Keep standalone audit-loader doubles out of the shared API import graph."""
import sys
import types
from contextlib import contextmanager
from functools import wraps


def _owned(name):
    return name == 'evolving_profile_api' or name.startswith('evolving_profile_api.') or name == '_audit_trace_archive_under_test'


@contextmanager
def isolated_audit_modules():
    before = {name: module for name, module in sys.modules.items() if _owned(name)}
    # importlib can attach freshly imported children to an existing parent even
    # when its sys.modules entry is restored. Restore the original namespaces
    # too, including lazy runtime exports, not just the registration mapping.
    parents = {module: dict(vars(module)) for module in before.values() if isinstance(module, types.ModuleType)}
    try:
        yield
    finally:
        for name in list(sys.modules):
            if _owned(name) and name not in before:
                sys.modules.pop(name, None)
        sys.modules.update(before)
        for module, attributes in parents.items():
            vars(module).clear()
            vars(module).update(attributes)


def isolated_audit_loader(load):
    @wraps(load)
    def wrapped(*args, **kwargs):
        with isolated_audit_modules():
            return load(*args, **kwargs)
    return wrapped
