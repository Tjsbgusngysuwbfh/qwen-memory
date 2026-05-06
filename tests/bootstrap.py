"""Test bootstrap helpers for script-style regression suites."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def bootstrap() -> None:
    root = Path(__file__).resolve().parents[1]
    src = root / "src"
    pkg_dir = src / "qwen_memory"

    if str(src) not in sys.path:
        sys.path.insert(0, str(src))

    for name in [
        "qwen_memory",
        "qwen_memory.budget",
        "qwen_memory.db",
        "qwen_memory.mem",
        "qwen_memory.semantic",
        "qwen_memory.store",
        "qwen_memory.trigger_router",
        "budget",
        "db",
        "mem",
        "semantic",
        "store",
        "trigger_router",
    ]:
        sys.modules.pop(name, None)

    pkg_spec = importlib.util.spec_from_file_location(
        "qwen_memory",
        pkg_dir / "__init__.py",
        submodule_search_locations=[str(pkg_dir)],
    )
    assert pkg_spec and pkg_spec.loader
    pkg_module = importlib.util.module_from_spec(pkg_spec)
    sys.modules["qwen_memory"] = pkg_module
    pkg_spec.loader.exec_module(pkg_module)

    aliases = ["budget", "db", "mem", "semantic", "store", "trigger_router"]
    for alias in aliases:
        full_name = f"qwen_memory.{alias}"
        module_spec = importlib.util.spec_from_file_location(full_name, pkg_dir / f"{alias}.py")
        assert module_spec and module_spec.loader
        module = importlib.util.module_from_spec(module_spec)
        sys.modules[full_name] = module
        module_spec.loader.exec_module(module)
        sys.modules[alias] = module
