from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from harness.contract import Contract
from harness.server import target_server

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "specs" / "openapi.json"


@pytest.fixture(scope="session")
def base_url():
    with target_server() as url:
        yield url


@pytest.fixture(scope="session")
def contract() -> Contract:
    return Contract(SPEC)


@pytest.fixture
def client(base_url):
    with httpx.Client(base_url=base_url, timeout=10) as c:
        yield c


@pytest.fixture
def api(client, contract):
    """요청마다 OpenAPI 계약을 자동 검증하는 클라이언트."""

    class Api:
        def call(self, method: str, path: str, **kw) -> httpx.Response:
            resp = client.request(method, path, **kw)
            body = resp.json() if resp.content else None
            contract.check(method, resp.request.url.path, resp.status_code, body)
            return resp

        def user(self, name: str) -> str:
            return self.call("POST", "/v1/users", json={"name": name}).json()["id"]

        def chat(self, *members: str) -> str:
            return self.call("POST", "/v1/chats", json={"member_ids": list(members)}).json()["id"]

        def send(self, chat: str, sender: str, text: str, key: str | None = None) -> httpx.Response:
            headers = {"X-User-Id": sender}
            if key:
                headers["Idempotency-Key"] = key
            return self.call("POST", f"/v1/chats/{chat}/messages", json={"text": text}, headers=headers)

    return Api()
