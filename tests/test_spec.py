"""스펙 드리프트 검사: 서버 코드에서 뽑은 OpenAPI와 커밋된 specs/openapi.json이 같아야 한다.

API를 바꾸고 스펙을 갱신하지 않으면 여기서 실패한다. 계약 변경이 PR diff에
드러나야 리뷰어와 AI 시나리오 생성기가 그 변경을 볼 수 있다.
"""

from __future__ import annotations

import json

from app.main import app
from tests.conftest import SPEC


def test_committed_spec_matches_server():
    committed = json.loads(SPEC.read_text(encoding="utf-8"))
    assert app.openapi() == committed, "API가 바뀌었습니다. `python tools/export_openapi.py`로 스펙을 갱신하세요."
