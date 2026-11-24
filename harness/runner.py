"""시나리오 실행기.

스텝마다 요청을 보내고, 상태코드, 부분 일치, 배열 길이, OpenAPI 계약을 검사합니다.
앞 스텝 응답에서 값을 저장해 뒤 스텝의 경로, 헤더, 본문에 넣을 수 있습니다.
실패하면 어느 스텝에서 무엇이 달랐는지와 요청·응답 원문을 함께 보여 줍니다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from harness.contract import Contract
from harness.scenario import Scenario, Step

_VAR = re.compile(r"\{(\w+)\}")
_PATH_TOKEN = re.compile(r"\.([A-Za-z_]\w*)|\[(\d+)\]")


class ScenarioFailure(AssertionError):
    pass


def json_path(data: Any, path: str) -> Any:
    """$.a.b[0].c 형태만 지원하는 작은 JSONPath."""
    if not path.startswith("$"):
        raise ValueError(f"JSONPath는 $로 시작해야 합니다: {path}")
    cur = data
    for key, idx in _PATH_TOKEN.findall(path[1:]):
        cur = cur[key] if key else cur[int(idx)]
    return cur


def substitute(value: Any, vars: dict[str, Any]) -> Any:
    if isinstance(value, str):
        whole = _VAR.fullmatch(value)
        if whole and whole.group(1) in vars:
            return vars[whole.group(1)]  # 타입 유지 (숫자 등)
        return _VAR.sub(lambda m: str(vars.get(m.group(1), m.group(0))), value)
    if isinstance(value, list):
        return [substitute(v, vars) for v in value]
    if isinstance(value, dict):
        return {k: substitute(v, vars) for k, v in value.items()}
    return value


def subset_diff(expected: Any, actual: Any, where: str = "$") -> list[str]:
    """expected가 actual 안에 포함되는지 비교하고 다른 곳을 돌려준다."""
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [f"{where}: object 기대, 실제 {type(actual).__name__}"]
        diffs = []
        for k, v in expected.items():
            if k not in actual:
                diffs.append(f"{where}.{k}: 키 없음")
            else:
                diffs += subset_diff(v, actual[k], f"{where}.{k}")
        return diffs
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(expected) != len(actual):
            return [f"{where}: 배열 {expected!r} 기대, 실제 {actual!r}"]
        diffs = []
        for i, (e, a) in enumerate(zip(expected, actual, strict=False)):
            diffs += subset_diff(e, a, f"{where}[{i}]")
        return diffs
    return [] if expected == actual else [f"{where}: {expected!r} 기대, 실제 {actual!r}"]


@dataclass
class StepLog:
    name: str
    method: str
    url: str
    request_body: Any
    status: int
    response_body: Any


@dataclass
class Runner:
    client: httpx.Client
    contract: Contract | None = None
    vars: dict[str, Any] = field(default_factory=dict)
    log: list[StepLog] = field(default_factory=list)

    def run(self, scenario: Scenario) -> None:
        for i, step in enumerate(scenario.steps, start=1):
            try:
                self._run_step(step)
            except ScenarioFailure as e:
                raise ScenarioFailure(
                    f"[{scenario.id}] {i}번째 스텝 '{step.name}' 실패\n{e}\n\n{self.transcript()}"
                ) from None

    def _run_step(self, step: Step) -> None:
        req = step.request
        path = substitute(req.path, self.vars)
        headers = {k: str(v) for k, v in substitute(req.headers, self.vars).items()}
        params = substitute(req.params, self.vars)
        body = substitute(req.json_body, self.vars)

        resp = self.client.request(
            req.method, path, headers=headers, params=params or None, json=body if req.json_body is not None else None
        )
        try:
            data = resp.json()
        except ValueError:
            data = resp.text
        self.log.append(StepLog(step.name, req.method, str(resp.request.url), body, resp.status_code, data))

        exp = step.expect
        problems: list[str] = []
        if resp.status_code != exp.status:
            problems.append(f"상태코드: {exp.status} 기대, 실제 {resp.status_code}")
        if exp.json_subset is not None:
            problems += subset_diff(substitute(exp.json_subset, self.vars), data)
        for jp, n in exp.lengths.items():
            try:
                actual = len(json_path(data, jp))
            except (KeyError, IndexError, TypeError):
                problems.append(f"{jp}: 경로 없음")
                continue
            if actual != n:
                problems.append(f"{jp} 길이: {n} 기대, 실제 {actual}")
        if exp.contract and self.contract is not None:
            try:
                self.contract.check(req.method, resp.request.url.path, resp.status_code, data)
            except AssertionError as e:
                problems.append(f"계약 위반: {e}")
        if problems:
            raise ScenarioFailure("\n".join(f"  - {p}" for p in problems))

        for var, jp in step.save.items():
            try:
                self.vars[var] = json_path(data, jp)
            except (KeyError, IndexError, TypeError):
                raise ScenarioFailure(f"  - 저장 실패: {var} ← {jp} 경로가 응답에 없음") from None

    def transcript(self) -> str:
        lines = ["--- 실행 기록 ---"]
        for s in self.log:
            lines.append(f"> {s.method} {s.url}  ({s.name})")
            if s.request_body is not None:
                lines.append(f"  body: {json.dumps(s.request_body, ensure_ascii=False)}")
            lines.append(f"< {s.status} {json.dumps(s.response_body, ensure_ascii=False)[:500]}")
        return "\n".join(lines)
