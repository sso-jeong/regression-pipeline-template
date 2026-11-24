"""데모용 메시징 Platform API (테스트 대상 서버).

실제 서비스를 흉내 낸 작은 인메모리 서버입니다. 사용자, 채팅방, 메시지 전송,
읽음 처리, 안 읽은 수를 제공합니다.

INJECT_BUGS 환경변수로 회귀 버그를 일부러 심을 수 있습니다. 파이프라인이 실제로
회귀를 잡아내는지 보여 주기 위한 장치입니다.
    INJECT_BUGS=pagination,idempotency,unread
"""

from __future__ import annotations

import os
import threading
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

BUGS = {b.strip() for b in os.getenv("INJECT_BUGS", "").split(",") if b.strip()}

MAX_TEXT_LEN = 1000


# ---------- 모델 ----------


class Error(BaseModel):
    detail: str


class UserCreate(BaseModel):
    name: str = Field(min_length=1, max_length=50)


class User(BaseModel):
    id: str
    name: str


class ChatCreate(BaseModel):
    member_ids: list[str] = Field(min_length=2, max_length=100)
    title: str | None = Field(default=None, max_length=100)


class Chat(BaseModel):
    id: str
    title: str | None
    member_ids: list[str]
    created_at: datetime


class MessageCreate(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT_LEN)


class Message(BaseModel):
    id: str
    chat_id: str
    sender_id: str
    seq: int = Field(ge=1, description="채팅방 안에서 1부터 1씩 증가하는 순번")
    text: str
    created_at: datetime


class MessagePage(BaseModel):
    items: list[Message]
    next_after_seq: int | None = Field(description="다음 페이지 커서. 마지막 페이지면 null")


class ReadRequest(BaseModel):
    up_to_seq: int = Field(ge=0)


class ReadState(BaseModel):
    chat_id: str
    user_id: str
    last_read_seq: int


class Unread(BaseModel):
    chat_id: str
    user_id: str
    unread_count: int = Field(ge=0)


class Health(BaseModel):
    status: str
    injected_bugs: list[str]


# ---------- 저장소 ----------


class Store:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.users: dict[str, User] = {}
        self.chats: dict[str, Chat] = {}
        self.messages: dict[str, list[Message]] = {}
        self.idempotency: dict[tuple, Message] = {}
        self.read_seq: dict[tuple, int] = {}


store = Store()

app = FastAPI(
    title="Messaging Platform API (demo)",
    version="1.0.0",
    description="회귀 파이프라인 데모용 메시징 API",
)

E400 = {400: {"model": Error}}
E403 = {403: {"model": Error}}
E404 = {404: {"model": Error}}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _get_chat(chat_id: str) -> Chat:
    chat = store.chats.get(chat_id)
    if chat is None:
        raise HTTPException(404, "chat not found")
    return chat


def _require_member(chat: Chat, user_id: str) -> None:
    if user_id not in chat.member_ids:
        raise HTTPException(403, "user is not a member of this chat")


# ---------- 엔드포인트 ----------


@app.get("/health", response_model=Health)
def health() -> Health:
    return Health(status="ok", injected_bugs=sorted(BUGS))


@app.post("/v1/users", response_model=User, status_code=201)
def create_user(body: UserCreate) -> User:
    user = User(id=f"u_{uuid.uuid4().hex[:10]}", name=body.name)
    with store.lock:
        store.users[user.id] = user
    return user


@app.get("/v1/users/{user_id}", response_model=User, responses=E404)
def get_user(user_id: str) -> User:
    user = store.users.get(user_id)
    if user is None:
        raise HTTPException(404, "user not found")
    return user


@app.post("/v1/chats", response_model=Chat, status_code=201, responses={**E400, **E404})
def create_chat(body: ChatCreate) -> Chat:
    members = list(dict.fromkeys(body.member_ids))  # 순서 유지 중복 제거
    if len(members) < 2:
        raise HTTPException(400, "a chat needs at least 2 distinct members")
    unknown = [m for m in members if m not in store.users]
    if unknown:
        raise HTTPException(404, f"unknown users: {', '.join(unknown)}")
    chat = Chat(id=f"c_{uuid.uuid4().hex[:10]}", title=body.title, member_ids=members, created_at=_now())
    with store.lock:
        store.chats[chat.id] = chat
        store.messages[chat.id] = []
    return chat


@app.get("/v1/chats/{chat_id}", response_model=Chat, responses=E404)
def get_chat(chat_id: str) -> Chat:
    return _get_chat(chat_id)


@app.post(
    "/v1/chats/{chat_id}/messages",
    response_model=Message,
    status_code=201,
    responses={**E403, **E404},
)
def send_message(
    chat_id: str,
    body: MessageCreate,
    x_user_id: str = Header(..., description="보내는 사용자 ID"),
    idempotency_key: str | None = Header(default=None, description="같은 키로 재전송하면 같은 메시지를 돌려줌"),
) -> Message:
    chat = _get_chat(chat_id)
    _require_member(chat, x_user_id)
    with store.lock:
        key = (chat_id, x_user_id, idempotency_key)
        if idempotency_key and "idempotency" not in BUGS and key in store.idempotency:
            return store.idempotency[key]
        msgs = store.messages[chat_id]
        msg = Message(
            id=f"m_{uuid.uuid4().hex[:10]}",
            chat_id=chat_id,
            sender_id=x_user_id,
            seq=len(msgs) + 1,
            text=body.text,
            created_at=_now(),
        )
        msgs.append(msg)
        if idempotency_key:
            store.idempotency[key] = msg
        # 보낸 사람은 자기 메시지를 읽은 것으로 본다
        store.read_seq[(chat_id, x_user_id)] = msg.seq
    return msg


@app.get(
    "/v1/chats/{chat_id}/messages",
    response_model=MessagePage,
    responses={**E403, **E404},
)
def list_messages(
    chat_id: str,
    x_user_id: str = Header(...),
    after_seq: int = Query(0, ge=0, description="이 순번 다음부터 조회"),
    limit: int = Query(20, ge=1, le=100),
) -> MessagePage:
    chat = _get_chat(chat_id)
    _require_member(chat, x_user_id)
    msgs = store.messages[chat_id]
    start = after_seq if "pagination" not in BUGS else after_seq + 1  # 주입 버그: 한 건씩 건너뜀
    items = msgs[start : start + limit]
    has_more = start + limit < len(msgs)
    return MessagePage(items=items, next_after_seq=items[-1].seq if items and has_more else None)


@app.post(
    "/v1/chats/{chat_id}/read",
    response_model=ReadState,
    responses={**E400, **E403, **E404},
)
def mark_read(chat_id: str, body: ReadRequest, x_user_id: str = Header(...)) -> ReadState:
    chat = _get_chat(chat_id)
    _require_member(chat, x_user_id)
    with store.lock:
        last = len(store.messages[chat_id])
        if body.up_to_seq > last:
            raise HTTPException(400, f"up_to_seq {body.up_to_seq} exceeds last seq {last}")
        current = store.read_seq.get((chat_id, x_user_id), 0)
        new = max(current, body.up_to_seq)  # 읽음 위치는 뒤로 가지 않는다
        store.read_seq[(chat_id, x_user_id)] = new
    return ReadState(chat_id=chat_id, user_id=x_user_id, last_read_seq=new)


@app.get("/v1/chats/{chat_id}/unread", response_model=Unread, responses={**E403, **E404})
def unread(chat_id: str, x_user_id: str = Header(...)) -> Unread:
    chat = _get_chat(chat_id)
    _require_member(chat, x_user_id)
    total = len(store.messages[chat_id])
    read = store.read_seq.get((chat_id, x_user_id), 0)
    count = total - read
    if "unread" in BUGS:
        count = total  # 주입 버그: 읽음 처리를 무시
    return Unread(chat_id=chat_id, user_id=x_user_id, unread_count=count)
