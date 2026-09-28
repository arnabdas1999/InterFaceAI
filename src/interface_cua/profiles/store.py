"""Loads app profiles, tenant profiles, and policy layers from the repo."""

from __future__ import annotations

import json
from pathlib import Path

from interface_cua.config import Settings
from interface_cua.domain.overrides import OverridePatch
from interface_cua.domain.policy import PolicyLayer
from interface_cua.domain.profiles import AppProfile, TenantProfile
from interface_cua.domain.routes import canonicalize


class ProfileStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def app_profile(self, ref: str) -> AppProfile:
        app_id, _, version = ref.partition("@")
        path = self.settings.apps_dir / app_id / "profile.json"
        profile = AppProfile.model_validate_json(path.read_text(encoding="utf-8"))
        if version and profile.version != version:
            raise ValueError(f"app profile {app_id} is {profile.version}, requested {version}")
        return profile

    def tenant(self, tenant_id: str) -> TenantProfile:
        path = self.settings.config_dir / "tenants" / f"{tenant_id}.json"
        if not path.exists():
            raise KeyError(f"unknown tenant {tenant_id}")
        return TenantProfile.model_validate_json(path.read_text(encoding="utf-8"))

    def overrides(self, tenant: TenantProfile) -> list[OverridePatch]:
        """Reviewed override patches referenced by the tenant profile (paths relative to config/)."""
        return [OverridePatch.model_validate(_read(self.settings.config_dir / ref)) for ref in tenant.override_refs]

    def global_policy(self) -> PolicyLayer:
        return PolicyLayer.model_validate(_read(self.settings.config_dir / "policy.global.json"))

    def tenant_policy(self, tenant: TenantProfile) -> PolicyLayer:
        """Tenant layer: pins the origin to the tenant's own instance, plus any tenant file."""
        origin = canonicalize(tenant.base_url).origin
        layer = PolicyLayer(id=f"tenant:{tenant.tenant_id}", allowed_origins=[origin])
        if tenant.policy_file:
            extra = PolicyLayer.model_validate(_read(self.settings.config_dir / tenant.policy_file))
            layer = extra.model_copy(update={"id": layer.id, "allowed_origins": [origin]})
        return layer


def _read(path: Path) -> dict[str, object]:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data
