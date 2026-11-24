#!/usr/bin/env bash
# 데모 녹화용 스크립트. 장면마다 Enter를 눌러 다음으로 넘어간다.
#   ./demo.sh            전체
#   SKIP_AI=1 ./demo.sh  AI 생성 장면 건너뜀 (구독 사용량 아끼기)
set -u
cd "$(dirname "$0")"
PY=.venv/bin/python
PYTEST=.venv/bin/pytest

scene() {
  printf '\n\033[1;36m━━ %s ━━\033[0m\n' "$1"
  [ -n "${2:-}" ] && printf '\033[2m%s\033[0m\n' "$2"
  read -r -p "▶ Enter" _
}
run() { printf '\033[1;33m$ %s\033[0m\n' "$*"; "$@"; }

scene "1. 전체 회귀" "메시징 API를 시나리오 YAML + 코드 테스트 + OpenAPI 계약으로 검증"
run $PYTEST -q

scene "2. 회귀 훈련: 읽음 처리 버그를 심는다" "INJECT_BUGS=unread → 서버가 읽음 위치를 무시"
printf '\033[1;33m$ INJECT_BUGS=unread pytest -k READ\033[0m\n'
INJECT_BUGS=unread $PYTEST -q -x tests/test_scenarios.py -k READ 2>&1 | sed -n '/ScenarioFailure:/,/실행 기록/p' | sed 's/^E *//'

if [ -z "${SKIP_AI:-}" ]; then
  scene "3. Claude로 시나리오 초안 생성" "스펙 + 기존 시나리오 → 초안 → 로컬 서버에서 실행해 통과/검토/버림 분류"
  run $PY ai/generate_scenarios.py --engine claude-code --count 5 --out /tmp/demo-generated
fi

scene "4. AI 시나리오만 회귀 훈련에 돌려 본다" "AI가 다루지 않은 영역이 드러난다"
for bug in pagination idempotency unread; do
  printf '\n\033[1m[INJECT_BUGS=%s]\033[0m ' "$bug"
  INJECT_BUGS=$bug $PYTEST -q -m ai_generated 2>&1 | tail -1
done

scene "5. v1: 같은 API를 Postman 컬렉션 + Newman으로" "실무에서 운영하던 Postman → Newman → Jenkins 구조"
.venv/bin/uvicorn app.main:app --port 8000 --log-level warning &
SERVER=$!
sleep 1.5
run npx -y newman@6.2.1 run postman/messaging-regression.postman_collection.json --env-var baseUrl=http://127.0.0.1:8000
kill $SERVER

printf '\n\033[1;32m끝\033[0m\n'
