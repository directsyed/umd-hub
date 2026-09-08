"""Source registry. Adapters are imported lazily by name so a missing or broken module
only disables that one source (the collector logs it and moves on)."""
from __future__ import annotations

import importlib
from types import ModuleType
from typing import Callable

# Collector order. Structured sources first; unstructured (feed) sources after;
# backlog last because it only reads the study repo.
SOURCE_ORDER = [
    "seed", "canvas_ics", "gradescope", "canvas_api",
    "email_imap", "piazza", "course_site", "backlog",
]


def load_module(name: str) -> ModuleType:
    if name not in SOURCE_ORDER:
        raise KeyError(f"unknown source {name!r}")
    return importlib.import_module(f".{name}", __package__)


def get_fetch(name: str) -> Callable:
    return load_module(name).fetch


def get_probe(name: str) -> Callable | None:
    try:
        return getattr(load_module(name), "probe", None)
    except Exception:
        return None
