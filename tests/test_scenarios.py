"""scenarios/ 아래 YAML 시나리오를 하나씩 pytest 테스트로 실행한다.

시나리오의 tags는 pytest 마커가 되므로 `pytest -m smoke`처럼 골라 돌릴 수 있다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.runner import Runner
from harness.scenario import load_scenarios

SCENARIO_DIR = Path(__file__).resolve().parent.parent / "scenarios"


def _params():
    for path, sc in load_scenarios(SCENARIO_DIR):
        marks = [getattr(pytest.mark, t.replace("-", "_")) for t in sc.tags]
        if sc.origin == "ai":
            marks.append(pytest.mark.ai_generated)
        yield pytest.param(sc, id=f"{sc.id}:{path.stem}", marks=marks)


@pytest.mark.parametrize("scenario", list(_params()))
def test_scenario(scenario, client, contract):
    Runner(client=client, contract=contract).run(scenario)
