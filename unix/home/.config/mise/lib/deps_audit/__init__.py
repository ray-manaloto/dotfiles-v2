"""Schema-first cross-OS dependency and toolchain auditor (`deps:audit`)."""

from deps_audit.auditor import audit_dependencies, resolve_deps_audit_state_dir
from deps_audit.cli import main

__all__ = ["audit_dependencies", "main", "resolve_deps_audit_state_dir"]
