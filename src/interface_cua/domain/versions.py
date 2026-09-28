"""Version range checks shared by fingerprinting, compatibility, and override applicability."""

from __future__ import annotations

import re


def _parse(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def version_in_range(version: str, spec: str) -> bool:
    """``spec`` is a comma-separated list like ``>=4.2,<5.0``."""
    v = _parse(version)
    for part in spec.split(","):
        m = re.match(r"\s*(>=|<=|>|<|==)\s*([\d.]+)", part)
        if not m:
            continue
        op, bound = m.group(1), _parse(m.group(2))
        ok = {">=": v >= bound, "<=": v <= bound, ">": v > bound, "<": v < bound, "==": v == bound}[op]
        if not ok:
            return False
    return True
