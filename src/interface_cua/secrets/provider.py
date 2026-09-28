"""Secret provider. Secrets are wrapped so they cannot be printed, logged, or serialized."""

from __future__ import annotations

import os
from typing import Protocol

from dotenv import load_dotenv

from interface_cua.config import ROOT


class SecretValue:
    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        """The only way to get the plaintext; call sites are the login routine only."""
        return self._value

    def __repr__(self) -> str:
        return "SecretValue(***)"

    __str__ = __repr__

    def __reduce__(self) -> str | tuple[object, ...]:
        raise TypeError("SecretValue cannot be pickled or serialized")

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SecretValue) and other._value == self._value

    def __hash__(self) -> int:
        return hash(("secret", len(self._value)))


class SecretProvider(Protocol):
    def get(self, ref: str, field: str) -> SecretValue: ...


class EnvSecretProvider:
    """``synthcore/operator`` + ``password`` -> ``CUA_SECRET_SYNTHCORE_OPERATOR_PASSWORD``.

    A vault-backed provider implements the same protocol.
    """

    def __init__(self) -> None:
        load_dotenv(ROOT / ".env", override=False)

    def get(self, ref: str, field: str) -> SecretValue:
        key = "CUA_SECRET_" + (ref.replace("/", "_") + "_" + field).upper().replace("-", "_")
        value = os.environ.get(key)
        if value is None:
            raise KeyError(f"secret {ref}#{field} not configured (env {key})")
        return SecretValue(value)

    def all_values(self, ref: str) -> list[str]:
        """Plaintexts for the redactor's deny-list (never logged themselves)."""
        out = []
        for f in ("username", "password"):
            try:
                out.append(self.get(ref, f).reveal())
            except KeyError:
                pass
        return out
