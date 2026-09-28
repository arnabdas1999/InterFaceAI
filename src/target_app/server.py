"""Run the synthetic target in-process (tests, demos) or as a standalone server (CLI)."""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass

import uvicorn

from target_app.app import create_app


@dataclass
class RunningTarget:
    server: uvicorn.Server
    thread: threading.Thread
    url: str

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


def start_in_thread(port: int = 8765, tenant: str = "tenant-a") -> RunningTarget:
    app = create_app(tenant)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name=f"target-{tenant}")
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0 and server.started:
                return RunningTarget(server=server, thread=thread, url=f"http://127.0.0.1:{port}")
        time.sleep(0.05)
    raise RuntimeError(f"target app did not start on port {port}")


def serve(port: int = 8765, tenant: str = "tenant-a") -> None:
    uvicorn.run(create_app(tenant), host="127.0.0.1", port=port, log_level="info")
