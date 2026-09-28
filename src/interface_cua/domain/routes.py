"""Route patterns and URL canonicalization.

Artifacts never contain concrete URLs. They contain route patterns such as
``/members/:member_id/accounts/*`` that are matched against the path of a canonicalized URL.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlsplit

# Query parameters that carry session state in legacy apps; always dropped.
SESSION_PARAMS = frozenset({"jsessionid", "sid", "sessionid", "phpsessid", "token", "auth"})


@dataclass(frozen=True)
class CanonicalUrl:
    origin: str
    path: str
    query: str  # only allowlisted parameters, sorted

    def __str__(self) -> str:
        return f"{self.origin}{self.path}" + (f"?{self.query}" if self.query else "")

    @property
    def route_path(self) -> str:
        return self.path


def canonicalize(url: str, *, keep_params: frozenset[str] = frozenset()) -> CanonicalUrl:
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}" if parts.scheme else ""
    path = parts.path.split(";", 1)[0] or "/"  # strip ;jsessionid=... path params
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in SESSION_PARAMS and k in keep_params]
    return CanonicalUrl(origin=origin, path=path, query=urlencode(sorted(kept)))


@lru_cache(maxsize=512)
def _compile(pattern: str) -> re.Pattern[str]:
    out = ["^"]
    for seg in pattern.strip("/").split("/"):
        out.append("/")
        if seg == "**":
            out[-1] = "(?:/.*)?"
        elif seg == "*":
            out.append("[^/]+")
        elif seg.startswith(":"):
            out.append(f"(?P<{seg[1:]}>[^/]+)")
        else:
            out.append(re.escape(seg))
    if pattern.strip("/") == "":
        return re.compile("^/$")
    out.append("/?$")
    return re.compile("".join(out))


def match_route(pattern: str, path: str) -> dict[str, str] | None:
    """Return captured params if ``path`` matches ``pattern`` (``:name``, ``*``, trailing ``**``)."""
    m = _compile(pattern).match(path)
    return None if m is None else {k: v for k, v in m.groupdict().items() if v is not None}


def route_allowed(path: str, patterns: list[str]) -> bool:
    return any(match_route(p, path) is not None for p in patterns)


def generalize_path(path: str, known_values: dict[str, str]) -> str:
    """Turn a concrete path into a route pattern, replacing known input values with ``:name``.

    ``/members/12345/accounts`` with ``{"member_id": "12345"}`` -> ``/members/:member_id/accounts``.
    Remaining purely numeric segments become ``:id`` so no concrete identifier leaks into an artifact.
    """
    by_value = {v: k for k, v in known_values.items() if v}
    segs = []
    for seg in path.strip("/").split("/"):
        if seg in by_value:
            segs.append(f":{by_value[seg]}")
        elif seg.isdigit():
            segs.append(":id")
        else:
            segs.append(seg)
    return "/" + "/".join(segs)
