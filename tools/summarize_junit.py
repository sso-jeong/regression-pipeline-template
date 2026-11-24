"""샤드별 JUnit XML을 모아 마크다운 요약을 만든다.

GitHub Actions에서는 $GITHUB_STEP_SUMMARY에, Jenkins에서는 파일로 남긴다.

    python tools/summarize_junit.py reports/*.xml > summary.md
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path


def main(paths: list[str]) -> int:
    totals = defaultdict(int)
    failures = []
    per_file = []
    for p in paths:
        root = ET.parse(p).getroot()
        suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
        counts = defaultdict(int)
        for suite in suites:
            for case in suite.findall("testcase"):
                name = f"{case.get('classname', '')}::{case.get('name')}"
                if case.find("failure") is not None or case.find("error") is not None:
                    node = case.find("failure") if case.find("failure") is not None else case.find("error")
                    counts["failed"] += 1
                    failures.append((Path(p).stem, name, (node.get("message") or "").splitlines()[:1]))
                elif case.find("skipped") is not None:
                    counts["skipped"] += 1
                else:
                    counts["passed"] += 1
        per_file.append((Path(p).stem, counts))
        for k, v in counts.items():
            totals[k] += v

    total = sum(totals.values())
    icon = "✅" if totals["failed"] == 0 else "❌"
    print(f"## {icon} 회귀 테스트 결과: {totals['passed']}/{total} 통과\n")
    print("| 샤드 | 통과 | 실패 | 건너뜀 |\n| --- | ---: | ---: | ---: |")
    for name, c in per_file:
        print(f"| {name} | {c['passed']} | {c['failed']} | {c['skipped']} |")
    if failures:
        print("\n### 실패 목록\n")
        for shard, name, msg in failures:
            first = msg[0][:200] if msg else ""
            print(f"- `{name}` ({shard})  \n  {first}")
    return 1 if totals["failed"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]) if len(sys.argv) > 1 else 0)
