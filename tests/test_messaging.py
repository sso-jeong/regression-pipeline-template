"""YAML로 표현하기 어려운 검증은 코드로 쓴다: 반복, 동시성, 페이지 전체 순회."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest


@pytest.mark.smoke
def test_pagination_returns_every_message_exactly_once(api):
    alice, bob = api.user("alice"), api.user("bob")
    chat = api.chat(alice, bob)
    for i in range(1, 26):
        api.send(chat, alice if i % 2 else bob, f"m{i}")

    seen, cursor = [], 0
    while True:
        page = api.call(
            "GET", f"/v1/chats/{chat}/messages", params={"after_seq": cursor, "limit": 7}, headers={"X-User-Id": bob}
        ).json()
        seen += [m["seq"] for m in page["items"]]
        if page["next_after_seq"] is None:
            break
        cursor = page["next_after_seq"]

    assert seen == list(range(1, 26)), "페이지를 끝까지 넘겼을 때 누락이나 중복이 없어야 한다"


@pytest.mark.smoke
def test_idempotent_resend_does_not_duplicate(api):
    alice, bob = api.user("alice"), api.user("bob")
    chat = api.chat(alice, bob)

    first = api.send(chat, alice, "결제 완료", key="pay-123").json()
    retry = api.send(chat, alice, "결제 완료", key="pay-123").json()

    assert retry["id"] == first["id"]
    page = api.call("GET", f"/v1/chats/{chat}/messages", headers={"X-User-Id": bob}).json()
    assert len(page["items"]) == 1, "네트워크 재시도로 같은 메시지가 두 번 저장되면 안 된다"


def test_concurrent_sends_get_unique_contiguous_seq(api):
    users = [api.user(f"u{i}") for i in range(5)]
    chat = api.chat(*users)

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda i: api.send(chat, users[i % 5], f"c{i}").json()["seq"], range(50)))

    assert sorted(results) == list(range(1, 51)), "동시 전송에서도 순번이 겹치거나 비면 안 된다"


def test_unread_count_follows_read_position(api):
    alice, bob = api.user("alice"), api.user("bob")
    chat = api.chat(alice, bob)
    for i in range(5):
        api.send(chat, alice, f"m{i}")

    def unread(user):
        return api.call("GET", f"/v1/chats/{chat}/unread", headers={"X-User-Id": user}).json()["unread_count"]

    assert unread(bob) == 5
    assert unread(alice) == 0, "보낸 사람에게는 자기 메시지가 안 읽음으로 잡히면 안 된다"

    api.call("POST", f"/v1/chats/{chat}/read", json={"up_to_seq": 3}, headers={"X-User-Id": bob})
    assert unread(bob) == 2

    # 읽음 위치는 뒤로 가지 않는다
    state = api.call("POST", f"/v1/chats/{chat}/read", json={"up_to_seq": 1}, headers={"X-User-Id": bob}).json()
    assert state["last_read_seq"] == 3
    assert unread(bob) == 2


@pytest.mark.parametrize("text, status", [("", 422), ("a" * 1000, 201), ("a" * 1001, 422)])
def test_message_text_length_boundary(api, text, status):
    alice, bob = api.user("alice"), api.user("bob")
    chat = api.chat(alice, bob)
    resp = api.call("POST", f"/v1/chats/{chat}/messages", json={"text": text}, headers={"X-User-Id": alice})
    assert resp.status_code == status
