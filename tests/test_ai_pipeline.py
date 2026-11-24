"""AI 시나리오 생성기의 후처리(형식 변환, 실행 검증, 분류)를 API 호출 없이 검증한다.

tests/fixtures/ai_response.json은 Claude 구조화 출력과 같은 형식의 고정 응답이다.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

from harness.scenario import Scenario

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "ai_response.json"


def test_generator_sorts_drafts_into_accepted_review_and_rejected(tmp_path):
    out = tmp_path / "generated"
    report = tmp_path / "report.md"
    subprocess.run(
        [
            sys.executable,
            "ai/generate_scenarios.py",
            "--from-json",
            str(FIXTURE),
            "--out",
            str(out),
            "--report",
            str(report),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )

    accepted = sorted(p.stem for p in out.glob("*.yaml"))
    review = sorted(p.stem for p in (out / "_needs_review").glob("*.yaml"))
    assert accepted == ["SC-FX-MSG-001", "SC-FX-READ-001"]
    assert review == ["SC-FX-MSG-002"], "서버가 공백 이름을 허용하므로 사람 검토로 가야 한다"
    assert "버림 | SC-FX-BAD-001" in report.read_text(encoding="utf-8")

    for path in out.rglob("*.yaml"):
        sc = Scenario.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        assert sc.origin == "ai"
