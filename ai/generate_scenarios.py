"""OpenAPI 스펙과 코드 변경분을 Claude에게 주고 테스트 시나리오 초안을 받는다.

흐름
  1. 스펙, 변경 diff, 이미 있는 시나리오 목록을 프롬프트로 묶는다
  2. Claude가 구조화 출력(JSON 스키마 고정)으로 시나리오를 돌려준다
  3. 하네스 형식(Scenario)으로 바꾸고 검증한다. 형식이 틀리면 버린다
  4. 로컬 서버에 실제로 돌려 본다
       통과 → scenarios/generated/        (리뷰 후 머지하면 회귀에 편입)
       실패 → scenarios/generated/_needs_review/  (버그인지, 시나리오 오류인지 사람이 판단)
  5. PR 본문으로 쓸 요약 마크다운을 남긴다

AI는 초안만 만들고, 회귀에 넣을지는 사람이 PR 리뷰로 정한다.

실행 방식(--engine)
  api          Anthropic SDK로 Claude API 호출. ANTHROPIC_API_KEY 필요. CI용
  claude-code  로컬 Claude Code CLI(`claude -p`)로 호출. Claude 구독만으로 실행 가능
  (기본값: ANTHROPIC_API_KEY가 있으면 api, 없으면 claude-code)

    python ai/generate_scenarios.py --count 5 --diff-base origin/main
    python ai/generate_scenarios.py --engine claude-code --count 5
    python ai/generate_scenarios.py --dry-run    # 호출 없이 프롬프트만 출력
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import yaml
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness.contract import Contract  # noqa: E402
from harness.runner import Runner, ScenarioFailure  # noqa: E402
from harness.scenario import Scenario, load_scenarios  # noqa: E402
from harness.server import target_server  # noqa: E402

MODEL = "claude-opus-5-5"
TAGS = ["smoke", "regression", "negative", "user", "chat", "messaging", "read"]

# 구조화 출력 스키마. strict 모드는 임의 키를 가진 객체를 못 쓰므로
# JSON 본문과 부분 일치 기대값은 JSON 문자열로 받고, 헤더·저장은 배열로 받는다.
_KV = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "value": {"type": "string"}},
    "required": ["name", "value"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "scenarios": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "SC-AI-<영역>-<번호>"},
                    "title": {"type": "string", "description": "검증하려는 동작을 한국어 한 문장으로"},
                    "risk": {"type": "string", "description": "이 시나리오가 막으려는 결함, 한 문장"},
                    "tags": {"type": "array", "items": {"type": "string", "enum": TAGS}},
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
                                "path": {"type": "string"},
                                "headers": {"type": "array", "items": _KV},
                                "query": {"type": "array", "items": _KV},
                                "body_json": {
                                    "type": "string",
                                    "description": "요청 본문 JSON 문자열. 없으면 빈 문자열",
                                },
                                "expect_status": {"type": "integer"},
                                "expect_json": {
                                    "type": "string",
                                    "description": "부분 일치 JSON 문자열. 없으면 빈 문자열",
                                },
                                "save": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {"var": {"type": "string"}, "path": {"type": "string"}},
                                        "required": ["var", "path"],
                                        "additionalProperties": False,
                                    },
                                },
                            },
                            "required": [
                                "name",
                                "method",
                                "path",
                                "headers",
                                "query",
                                "body_json",
                                "expect_status",
                                "expect_json",
                                "save",
                            ],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["id", "title", "risk", "tags", "steps"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["scenarios"],
    "additionalProperties": False,
}

SYSTEM = """당신은 메시징 Platform 서버의 QA 엔지니어입니다. OpenAPI 스펙과 코드 변경분을 읽고,
API 호출만으로 수행할 수 있는 회귀 테스트 시나리오를 설계합니다.

설계 기준
- 스펙에 적힌 동작만 기대값으로 씁니다. 스펙에 없는 동작을 추측해 단정하지 않습니다.
- 정상 흐름보다 결함이 숨기 쉬운 곳을 우선합니다: 권한(멤버가 아닌 사용자), 경계값(min/max 길이, 0, 상한),
  상태 전이(읽음 위치, 순번 증가), 재시도(Idempotency-Key), 페이지네이션 커서, 존재하지 않는 리소스.
- 변경 diff가 주어지면 바뀐 엔드포인트와 그 주변을 먼저 다룹니다.
- 이미 있는 시나리오와 같은 내용을 반복하지 않습니다.
- 각 시나리오는 자기 데이터를 직접 만듭니다(사용자·채팅방 생성부터). 다른 시나리오에 기대지 않습니다.

형식 규칙
- 앞 스텝 응답 값은 save로 변수에 저장하고, 뒤 스텝에서 {변수} 로 씁니다. path, 헤더 값, body_json 안의 문자열에 쓸 수 있습니다.
- body_json 안에서 숫자 변수를 쓰려면 문자열 전체를 "{변수}" 로 둡니다. 실행기가 원래 타입으로 바꿉니다.
- save의 path는 $.id, $.items[0].seq 같은 단순 JSONPath만 씁니다.
- expect_json은 응답에 반드시 들어 있어야 하는 필드만 부분 일치로 적습니다. id, created_at처럼 매번 바뀌는 값은 넣지 않습니다.
- 사용자 헤더는 X-User-Id, 재시도 키는 Idempotency-Key 입니다."""


def _git_diff(base: str) -> str:
    try:
        out = subprocess.run(
            ["git", "diff", base, "--", "app", "specs"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout
    except subprocess.CalledProcessError as e:
        sys.exit(f"git diff 실패: {e.stderr.strip()}")
    return out[:60_000]


def build_prompt(spec: dict, existing: list[Scenario], diff: str, count: int) -> str:
    existing_lines = "\n".join(f"- {s.id}: {s.title}" for s in existing) or "(없음)"
    parts = [
        f"<openapi_spec>\n{json.dumps(spec, ensure_ascii=False)}\n</openapi_spec>",
        f"<existing_scenarios>\n{existing_lines}\n</existing_scenarios>",
    ]
    if diff.strip():
        parts.append(f"<code_diff>\n{diff}\n</code_diff>")
    parts.append(
        f"새 시나리오를 {count}개 설계하세요. 각 시나리오의 risk에는 무엇이 깨지면 이 시나리오가 실패하는지 적으세요."
    )
    return "\n\n".join(parts)


def call_claude_api(prompt: str) -> tuple[list[dict], str]:
    import anthropic

    client = anthropic.Anthropic()
    try:
        resp = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM,
            thinking={"type": "adaptive"},
            output_config={"effort": "high", "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
            # 안전 분류기가 요청을 거절하면 서버가 다른 모델로 자동 재시도
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.AuthenticationError:
        sys.exit("ANTHROPIC_API_KEY가 없거나 잘못되었습니다.")
    except anthropic.RateLimitError:
        sys.exit("요청 한도 초과. 잠시 후 다시 실행하세요.")
    except anthropic.APIStatusError as e:
        sys.exit(f"API 오류 {e.status_code}: {e.message}")
    except anthropic.APIConnectionError:
        sys.exit("API에 연결하지 못했습니다.")

    if resp.stop_reason == "refusal":
        sys.exit(f"모델이 요청을 거절했습니다: {resp.stop_details}")
    if resp.stop_reason == "max_tokens":
        sys.exit("출력이 max_tokens에서 잘렸습니다. --count를 줄이세요.")
    text = next(b.text for b in resp.content if b.type == "text")
    usage = resp.usage
    print(f"[claude] {resp.model} 입력 {usage.input_tokens} / 출력 {usage.output_tokens} 토큰", file=sys.stderr)
    return json.loads(text)["scenarios"], resp.model


def _wait_with_progress(proc: subprocess.Popen, prompt: str) -> tuple[str, str]:
    """생성 중에 경과 시간을 한 줄로 갱신해 보여 준다. 화면이 멈춘 것처럼 보이지 않게."""
    import threading
    import time

    result: dict[str, str] = {}

    def communicate() -> None:
        result["out"], result["err"] = proc.communicate(prompt)

    worker = threading.Thread(target=communicate, daemon=True)
    worker.start()
    start, frames = time.time(), "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    tty = sys.stderr.isatty()
    i = 0
    while worker.is_alive():
        if tty:
            sys.stderr.write(
                f"\r{frames[i % len(frames)]} Claude가 시나리오를 설계하는 중... {time.time() - start:.0f}초"
            )
            sys.stderr.flush()
        i += 1
        worker.join(0.2)
    if tty:
        sys.stderr.write("\r" + " " * 50 + "\r")
    return result.get("out", ""), result.get("err", "")


def call_claude_code(prompt: str) -> tuple[list[dict], str]:
    """로컬 Claude Code를 헤드리스로 실행해 같은 스키마의 구조화 출력을 받는다.

    --safe-mode: 개인 CLAUDE.md·플러그인·훅을 빼고 깨끗한 상태로 실행
    --tools "": 파일·명령 도구 없이 텍스트 생성만
    """
    if shutil.which("claude") is None:
        sys.exit("claude CLI가 없습니다. Claude Code를 설치하거나 --engine api를 쓰세요.")
    cmd = [
        "claude",
        "-p",
        "--safe-mode",
        "--tools",
        "",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(OUTPUT_SCHEMA),
        "--system-prompt",
        SYSTEM,
    ]
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=ROOT
    )
    stdout, stderr = _wait_with_progress(proc, prompt)
    try:
        out = json.loads(stdout)
    except json.JSONDecodeError:
        sys.exit(f"Claude Code 실행 실패 (exit {proc.returncode}): {stderr.strip() or stdout[:500]}")
    if out.get("is_error") or out.get("structured_output") is None:
        sys.exit(
            f"Claude Code가 구조화 출력을 돌려주지 않았습니다: {out.get('subtype')} {str(out.get('result'))[:300]}"
        )
    model = next(iter(out.get("modelUsage", {})), "claude-code")
    usage = out.get("usage", {})
    input_total = sum(
        usage.get(k, 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    )
    print(
        f"[claude-code] {model} 입력 {input_total}"
        f" / 출력 {usage.get('output_tokens')} 토큰, {out.get('duration_ms', 0) / 1000:.0f}초",
        file=sys.stderr,
    )
    return out["structured_output"]["scenarios"], model


def _parse_json(raw: str, where: str) -> Any:
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"{where}: JSON이 아님 ({e})") from None


def to_scenario(item: dict) -> tuple[Scenario, str]:
    """AI 출력 한 건을 하네스 형식으로 바꾼다. 형식이 틀리면 ValueError."""
    steps = []
    for s in item["steps"]:
        expect: dict[str, Any] = {"status": s["expect_status"]}
        exp_json = _parse_json(s["expect_json"], f"{s['name']}.expect_json")
        if exp_json is not None:
            expect["json"] = exp_json
        request: dict[str, Any] = {"method": s["method"], "path": s["path"]}
        if s["headers"]:
            request["headers"] = {h["name"]: h["value"] for h in s["headers"]}
        if s["query"]:
            request["params"] = {q["name"]: q["value"] for q in s["query"]}
        body = _parse_json(s["body_json"], f"{s['name']}.body_json")
        if body is not None:
            request["json"] = body
        step: dict[str, Any] = {"name": s["name"], "request": request, "expect": expect}
        if s["save"]:
            step["save"] = {v["var"]: v["path"] for v in s["save"]}
        steps.append(step)
    data = {"id": item["id"], "title": item["title"], "tags": item["tags"], "origin": "ai", "steps": steps}
    try:
        return Scenario.model_validate(data), item["risk"]
    except ValidationError as e:
        raise ValueError(str(e)) from None


def to_yaml(sc: Scenario, header: str) -> str:
    data = sc.model_dump(by_alias=True, exclude_defaults=True)
    data["origin"] = "ai"
    body = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=120)
    comments = "".join(f"# {line}\n" for line in header.splitlines())
    return comments + body


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--count", type=int, default=5)
    ap.add_argument("--diff-base", help="이 ref와의 diff(app/, specs/)를 함께 준다. 예: origin/main")
    ap.add_argument("--from-json", type=Path, help="API 대신 저장해 둔 응답 JSON을 쓴다(재현·테스트용)")
    ap.add_argument("--out", type=Path, default=ROOT / "scenarios" / "generated")
    ap.add_argument("--report", type=Path, help="PR 본문용 마크다운 요약 파일")
    ap.add_argument("--dry-run", action="store_true", help="프롬프트만 출력")
    ap.add_argument(
        "--engine",
        choices=["api", "claude-code"],
        default="api" if os.getenv("ANTHROPIC_API_KEY") else "claude-code",
        help="api: Anthropic SDK (CI) / claude-code: 로컬 Claude Code CLI (구독)",
    )
    args = ap.parse_args()

    spec_path = ROOT / "specs" / "openapi.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    existing = [sc for _, sc in load_scenarios(ROOT / "scenarios")]
    diff = _git_diff(args.diff_base) if args.diff_base else ""
    prompt = build_prompt(spec, existing, diff, args.count)

    if args.dry_run:
        print(SYSTEM, "\n\n", prompt)
        return 0

    if args.from_json:
        items, model = json.loads(args.from_json.read_text())["scenarios"], "fixture"
    elif args.engine == "api":
        items, model = call_claude_api(prompt)
    else:
        items, model = call_claude_code(prompt)

    existing_ids = {s.id for s in existing}
    accepted, review, rejected = [], [], []
    contract = Contract(spec_path)
    with target_server() as url:
        for item in items:
            try:
                sc, risk = to_scenario(item)
            except ValueError as e:
                rejected.append((item.get("id", "?"), str(e).splitlines()[0]))
                continue
            if sc.id in existing_ids:
                rejected.append((sc.id, "이미 있는 ID"))
                continue
            with httpx.Client(base_url=url, timeout=10) as client:
                try:
                    Runner(client=client, contract=contract).run(sc)
                    accepted.append((sc, risk, None))
                except ScenarioFailure as e:
                    reason = str(e).split("\n\n--- 실행 기록")[0]
                    review.append((sc, risk, reason))

    args.out.mkdir(parents=True, exist_ok=True)
    for sc, risk, reason in accepted + review:
        target = args.out if reason is None else args.out / "_needs_review"
        target.mkdir(parents=True, exist_ok=True)
        header = f"AI 초안 ({model}, {args.engine}). 리뷰 후 머지.\n위험: {risk}"
        if reason:
            header += f"\n현재 서버에서 실패:\n{reason}"
        (target / f"{sc.id}.yaml").write_text(to_yaml(sc, header), encoding="utf-8")

    lines = [
        f"## AI 시나리오 초안 {len(items)}건",
        "",
        f"- 통과 {len(accepted)}건 → `scenarios/generated/`",
        f"- 실패 {len(review)}건 → `scenarios/generated/_needs_review/` (결함인지 시나리오 오류인지 확인 필요)",
        f"- 형식 오류로 버림 {len(rejected)}건",
        "",
        "| 결과 | ID | 제목 | 막으려는 결함 |",
        "| --- | --- | --- | --- |",
    ]
    lines += [f"| 통과 | {sc.id} | {sc.title} | {risk} |" for sc, risk, _ in accepted]
    lines += [f"| 검토 | {sc.id} | {sc.title} | {risk} |" for sc, risk, _ in review]
    lines += [f"| 버림 | {i} | {why} | |" for i, why in rejected]
    summary = "\n".join(lines)
    print(summary)
    if args.report:
        args.report.write_text(summary + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
