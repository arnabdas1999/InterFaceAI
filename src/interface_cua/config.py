"""Paths, environment, and default limits. One place so the CLI, tests, and demos agree."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]


def _load_env() -> None:
    load_dotenv(ROOT / ".env", override=False)


@dataclass(frozen=True)
class Limits:
    max_steps: int = 25
    wall_clock_s: int = 300
    max_invalid_decisions: int = 2
    max_no_progress: int = 3
    max_run_tokens: int = 400_000
    step_timeout_ms: int = 10_000
    slow_threshold_ms: int = 1_500
    claim_sla_s: int = 600
    human_active_max_s: int = 900
    heartbeat_s: int = 30
    approval_valid_s: int = 300


@dataclass(frozen=True)
class Settings:
    root: Path = ROOT
    runs_dir: Path = ROOT / "runs"
    capabilities_dir: Path = ROOT / "capabilities"
    apps_dir: Path = ROOT / "apps"
    config_dir: Path = ROOT / "config"
    evidence_dir: Path = ROOT / "evidence"
    state_dir: Path = ROOT / ".cua"
    model: str = "claude-sonnet-5"
    operator_port: int = 8766
    limits: Limits = field(default_factory=Limits)

    @property
    def db_path(self) -> Path:
        return self.state_dir / "audit.sqlite"


def load_settings() -> Settings:
    _load_env()
    limits = Limits(max_run_tokens=int(os.environ.get("CUA_MAX_RUN_TOKENS", Limits.max_run_tokens)))
    return Settings(
        model=os.environ.get("CUA_MODEL", "claude-sonnet-5"),
        operator_port=int(os.environ.get("CUA_OPERATOR_PORT", "8766")),
        limits=limits,
    )


def hmac_key(settings: Settings) -> bytes:
    """Key for correlation hashes in logs. Env first, else a per-machine random key (git-ignored)."""
    _load_env()
    env = os.environ.get("CUA_HMAC_KEY")
    if env:
        return env.encode()
    path = settings.state_dir / "hmac.key"
    if not path.exists():
        settings.state_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_hex(32))
    return path.read_text().strip().encode()
