"""시나리오 YAML의 형식 정의.

사람이 쓴 시나리오와 AI가 만든 시나리오가 같은 형식을 따릅니다. 형식이 하나라서
러너도 하나, 리뷰 기준도 하나입니다.

예시:
    id: SC-MSG-001
    title: 메시지를 보내면 순번이 1부터 매겨진다
    tags: [smoke, messaging]
    steps:
      - name: alice 생성
        request: {method: POST, path: /v1/users, json: {name: alice}}
        expect: {status: 201}
        save: {alice: $.id}
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: str = Field(description="{변수} 치환 가능. 예: /v1/chats/{chat}/messages")
    headers: dict[str, str] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    json_body: Any | None = Field(default=None, alias="json")


class Expect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: int
    json_subset: Any | None = Field(
        default=None,
        alias="json",
        description="응답 JSON에 이 값들이 포함되어 있어야 함(부분 일치)",
    )
    lengths: dict[str, int] = Field(default_factory=dict, description="JSONPath → 배열 길이")
    contract: bool = Field(default=True, description="OpenAPI 응답 스키마 검증 여부")


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    request: Request
    expect: Expect
    save: dict[str, str] = Field(default_factory=dict, description="변수명 → JSONPath($.a.b, $.items[0].id)")


Tag = Literal["smoke", "regression", "negative", "user", "chat", "messaging", "read"]


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    tags: list[Tag] = Field(default_factory=list, description="pyproject.toml에 등록된 마커만 허용")
    origin: Literal["human", "ai"] = "human"
    steps: list[Step] = Field(min_length=1)


def load_scenarios(root: Path) -> list[tuple[Path, Scenario]]:
    """root 아래 *.yaml을 모두 읽는다. '_'로 시작하는 폴더(검토 대기)는 건너뛴다."""
    found = []
    for path in sorted(root.rglob("*.yaml")):
        if any(part.startswith("_") for part in path.relative_to(root).parts):
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        found.append((path, Scenario.model_validate(data)))
    return found
