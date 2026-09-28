"""Shared fixtures. Integration tests drive the real local target through the real browser adapter."""

from __future__ import annotations

import dataclasses
import json
import shutil
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

from interface_cua.config import Limits, Settings, load_settings
from interface_cua.handoff.auth import OperatorDirectory
from target_app import admin
from tests.helpers import TEST_OPERATORS, TOKENS

TARGET_URL = "http://127.0.0.1:8765"


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


@pytest.fixture(scope="session")
def target() -> Iterator[str]:
    """The synthetic target on the tenant-a port (reuses a server you already started)."""
    running = None
    if not _port_open(8765):
        from target_app.server import start_in_thread

        running = start_in_thread(8765)
    admin.reset(TARGET_URL)
    yield TARGET_URL
    if running:
        running.stop()


@pytest.fixture(autouse=True)
def _clean_target(request: pytest.FixtureRequest) -> None:
    if "target" in request.fixturenames:
        admin.reset(TARGET_URL)


@pytest.fixture(scope="session")
def settings(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    base = tmp_path_factory.mktemp("cua")
    config = base / "config"
    shutil.copytree(load_settings().config_dir, config)
    (config / "operators.json").write_text(json.dumps({"operators": TEST_OPERATORS}), encoding="utf-8")
    s = dataclasses.replace(
        load_settings(),
        runs_dir=base / "runs",
        capabilities_dir=base / "capabilities",
        state_dir=base / "state",
        config_dir=config,
        operator_port=8796,
        limits=Limits(claim_sla_s=60, human_active_max_s=60, heartbeat_s=30),
    )
    directory = OperatorDirectory(config / "operators.json", s.state_dir / "operator_tokens.json")
    for op in TEST_OPERATORS:
        TOKENS[op["id"]] = directory.create_token(op["id"])
    return s


def run_dir(settings: Settings, run_id: str) -> Path:
    return settings.runs_dir / run_id
