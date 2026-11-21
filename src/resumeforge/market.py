"""Market-demand lookup and skill-name canonicalization."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

_PATH = Path(__file__).with_name("market_demand.json")


@lru_cache
def skills() -> dict:
    return json.loads(_PATH.read_text())["skills"]


@lru_cache
def _compiled() -> list[tuple[str, re.Pattern]]:
    return [(k, re.compile("|".join(v["patterns"]), re.I)) for k, v in skills().items()]


def match(text: str) -> set[str]:
    """Canonical skills whose patterns appear in text."""
    return {k for k, pat in _compiled() if pat.search(text or "")}


def canonical(name: str) -> str:
    """Map a free-form skill name to a canonical key when it matches a known skill."""
    n = name.strip().lower()
    if n in skills():
        return n
    for k, v in skills().items():
        if n == v["display"].lower():
            return k
    hits = match(name)
    return next(iter(hits)) if len(hits) == 1 else n


def demand(key: str) -> float | None:
    s = skills().get(key)
    return s["demand"] if s else None


def info(key: str) -> dict | None:
    return skills().get(key)
