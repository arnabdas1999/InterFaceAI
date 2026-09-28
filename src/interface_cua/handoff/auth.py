"""Operator authentication and authorization for the operator API.

Operators, their roles, and the tenants they may serve are declared in ``config/operators.json``
(reviewed, committed). Bearer tokens are random; only their SHA-256 hashes are stored, per machine, in
the git-ignored state directory. Identity always comes from the token, never from a request body.

Production replaces this with SSO (OIDC) mapping identity-provider groups to the same roles and tenant
scopes; the checks in the API do not change.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Role = Literal["operator", "supervisor"]


@dataclass(frozen=True)
class Operator:
    id: str
    roles: frozenset[str]
    tenants: frozenset[str]

    def can_serve(self, tenant_id: str | None) -> bool:
        return "*" in self.tenants or (tenant_id is not None and tenant_id in self.tenants)


class AuthError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class OperatorDirectory:
    def __init__(self, config_path: Path, token_store: Path) -> None:
        self.config_path = config_path
        self.token_store = token_store

    def operators(self) -> dict[str, Operator]:
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        return {o["id"]: Operator(id=o["id"], roles=frozenset(o["roles"]), tenants=frozenset(o["tenants"])) for o in data["operators"]}

    def _tokens(self) -> dict[str, list[str]]:
        if not self.token_store.exists():
            return {}
        data: dict[str, list[str]] = json.loads(self.token_store.read_text(encoding="utf-8"))
        return data

    def create_token(self, operator_id: str) -> str:
        if operator_id not in self.operators():
            raise KeyError(f"unknown operator {operator_id} (declare it in {self.config_path.name})")
        token = "cuaop_" + secrets.token_urlsafe(24)
        tokens = self._tokens()
        tokens.setdefault(operator_id, []).append(_hash(token))
        self.token_store.parent.mkdir(parents=True, exist_ok=True)
        self.token_store.write_text(json.dumps(tokens, indent=2), encoding="utf-8")
        return token

    def revoke(self, operator_id: str) -> None:
        tokens = self._tokens()
        tokens.pop(operator_id, None)
        self.token_store.write_text(json.dumps(tokens, indent=2), encoding="utf-8")

    def authenticate(self, authorization: str | None) -> Operator:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise AuthError(401, "missing bearer token")
        digest = _hash(authorization.split(" ", 1)[1].strip())
        for op_id, hashes in self._tokens().items():
            if any(hmac.compare_digest(digest, h) for h in hashes):
                op = self.operators().get(op_id)
                if op is None:
                    break
                return op
        raise AuthError(401, "invalid or revoked token")

    @staticmethod
    def require(op: Operator, role: Role, tenant_id: str | None) -> None:
        if role not in op.roles:
            raise AuthError(403, f"{op.id} lacks the {role} role")
        if not op.can_serve(tenant_id):
            raise AuthError(403, f"{op.id} is not authorized for tenant {tenant_id}")
