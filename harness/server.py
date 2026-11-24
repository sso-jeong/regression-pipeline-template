"""테스트 대상 서버를 띄우거나, 이미 떠 있는 서버에 붙는다.

BASE_URL이 있으면 그 서버를 그대로 씁니다(스테이징 등 원격 환경).
없으면 로컬에서 uvicorn을 빈 포트로 띄우고, 끝나면 내립니다.
"""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator

import httpx


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def target_server(inject_bugs: str | None = None) -> Iterator[str]:
    base = os.getenv("BASE_URL")
    if base:
        yield base.rstrip("/")
        return

    port = _free_port()
    env = dict(os.environ)
    if inject_bugs is not None:
        env["INJECT_BUGS"] = inject_bugs
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning"],
        env=env,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 15
        while True:
            try:
                if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if proc.poll() is not None or time.time() > deadline:
                raise RuntimeError("테스트 대상 서버가 뜨지 않았습니다")
            time.sleep(0.1)
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=10)
