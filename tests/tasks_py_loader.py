"""Helper to import scripts from `tasks/py/` in tests."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

TASKS_PY_DIR = Path(__file__).resolve().parent.parent / "tasks" / "py"


def load_module(name: str) -> ModuleType:
    path = TASKS_PY_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"tasks_py_{name}", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
