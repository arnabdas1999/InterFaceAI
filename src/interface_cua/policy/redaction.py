"""Redaction applied at construction time for logs/artifacts/evidence, and masking for model egress.

Two renderings of the same rule set:
- model egress: ``⟦input:member_id⟧`` (reference-preserving, no value), ``⟦masked:money⟧``
- persisted evidence: ``⟦input:member_id#3fa91c⟧`` (keyed-hash suffix for correlation across events)
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Iterable
from typing import Any

from interface_cua.domain.profiles import SensitiveFieldMap

SENSITIVE_KEYS = re.compile(
    r"(?i)^(password|pwd|pass|secret|token|access_token|refresh_token|authorization|cookie|set-cookie|api[_-]?key|session|sessionid|jsessionid)$"
)
_BEARER = re.compile(r"(?i)bearer\s+[a-z0-9._\-]+")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"\b\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b")
_SESSION_Q = re.compile(r"(?i)([;?&](?:jsessionid|sid|sessionid|token)=)[^&#\s\"']+")


class Redactor:
    def __init__(
        self,
        key: bytes,
        *,
        sensitive_map: SensitiveFieldMap | None = None,
        inputs: dict[str, tuple[str, str]] | None = None,
        secrets: Iterable[str] = (),
        extra_values: dict[str, str] | None = None,
    ) -> None:
        """``inputs``: name -> (value, sensitivity). ``extra_values``: literal -> kind (e.g. names
        read from label-mapped cells) discovered at run time."""
        self._key = key
        self._patterns = [(re.compile(p.pattern), p.kind) for p in (sensitive_map.patterns if sensitive_map else [])]
        self._inputs: dict[str, tuple[str, str]] = {}
        self._secrets = [s for s in secrets if s]
        self._extra: dict[str, str] = {}
        for name, (value, sens) in (inputs or {}).items():
            self.add_input(name, value, sens)
        for value, kind in (extra_values or {}).items():
            self.add_value(value, kind)

    # --- registration ------------------------------------------------------------
    def add_input(self, name: str, value: str, sensitivity: str) -> None:
        if value and sensitivity != "public":
            self._inputs[name] = (value, sensitivity)

    def add_value(self, value: str, kind: str) -> None:
        value = value.strip()
        if len(value) >= 3:
            self._extra[value] = kind

    def add_secret(self, value: str) -> None:
        if value:
            self._secrets.append(value)

    def secret_values(self) -> list[str]:
        """For screenshot masking only (never logged)."""
        return list(self._secrets)

    def hash8(self, value: str) -> str:
        return hmac.new(self._key, value.encode(), hashlib.sha256).hexdigest()[:8]

    # --- text ---------------------------------------------------------------------
    def _apply(self, text: str, *, for_model: bool) -> str:
        if not text:
            return text
        for s in self._secrets:
            text = text.replace(s, "⟦secret⟧")
        text = _BEARER.sub("⟦secret⟧", text)
        text = _SESSION_Q.sub(r"\1⟦session⟧", text)
        # Longest values first so "12345" inside "8830012345" is handled by the account pattern.
        for kind_value, kind in sorted(self._extra.items(), key=lambda kv: -len(kv[0])):
            text = text.replace(kind_value, f"⟦masked:{kind}⟧")
        for pattern, kind in self._patterns:
            text = pattern.sub(f"⟦masked:{kind}⟧", text)
        for name, (value, _sens) in sorted(self._inputs.items(), key=lambda kv: -len(kv[1][0])):
            token = f"⟦input:{name}⟧" if for_model else f"⟦input:{name}#{self.hash8(value)}⟧"
            text = re.sub(rf"(?<![\w]){re.escape(value)}(?![\w])", token, text)
        text = _EMAIL.sub("⟦masked:email⟧", text)
        text = _PHONE.sub("⟦masked:phone⟧", text)
        return text

    def for_model(self, text: str) -> str:
        return self._apply(text, for_model=True)

    def text(self, text: str) -> str:
        return self._apply(text, for_model=False)

    def input_ref(self, value: str) -> str | None:
        for name, (v, _) in self._inputs.items():
            if v == value:
                return name
        return None

    # --- structures ---------------------------------------------------------------
    def obj(self, data: Any) -> Any:
        if isinstance(data, dict):
            out: dict[str, Any] = {}
            for k, v in data.items():
                if isinstance(k, str) and SENSITIVE_KEYS.match(k):
                    out[k] = "⟦redacted⟧"
                else:
                    out[k] = self.obj(v)
            return out
        if isinstance(data, list | tuple):
            return [self.obj(v) for v in data]
        if isinstance(data, str):
            return self.text(data)
        return data

    def output_value(self, value: Any, sensitivity: str) -> Any:
        """Persisted copy of an extracted output: shape kept, sensitive leaves replaced by a keyed hash."""
        if sensitivity in {"public"}:
            return value
        if isinstance(value, dict):
            return {k: (v if k in {"currency"} else f"⟦{sensitivity}#{self.hash8(str(v))}⟧") for k, v in value.items()}
        return f"⟦{sensitivity}#{self.hash8(str(value))}⟧"
