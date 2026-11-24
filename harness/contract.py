"""OpenAPI 스펙 기준 응답 계약 검증.

모든 응답을 스펙의 (메서드, 경로, 상태코드) 스키마와 대조합니다.
스펙에 없는 상태코드가 오면 그것도 실패로 봅니다. 문서에 없는 응답은 계약 위반입니다.
"""

from __future__ import annotations

import json
import re
from functools import cached_property
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


class ContractViolation(AssertionError):
    pass


class Contract:
    def __init__(self, spec_path: Path) -> None:
        self.spec = json.loads(spec_path.read_text(encoding="utf-8"))

    @cached_property
    def _routes(self) -> list[tuple[re.Pattern, str]]:
        routes = []
        for template in self.spec["paths"]:
            pattern = "^" + re.sub(r"\{[^/]+\}", r"[^/]+", template) + "$"
            routes.append((re.compile(pattern), template))
        # 고정 경로가 변수 경로보다 먼저 매칭되도록 변수 개수 오름차순
        return sorted(routes, key=lambda r: r[1].count("{"))

    def template_for(self, path: str) -> str | None:
        for pattern, template in self._routes:
            if pattern.match(path):
                return template
        return None

    def check(self, method: str, path: str, status: int, body: Any) -> None:
        template = self.template_for(path)
        if template is None:
            raise ContractViolation(f"스펙에 없는 경로: {method} {path}")
        operation = self.spec["paths"][template].get(method.lower())
        if operation is None:
            raise ContractViolation(f"스펙에 없는 메서드: {method} {template}")
        response = operation["responses"].get(str(status))
        if response is None:
            documented = ", ".join(operation["responses"])
            raise ContractViolation(f"문서화되지 않은 상태코드 {status}: {method} {template} (스펙: {documented})")
        schema = response.get("content", {}).get("application/json", {}).get("schema")
        if schema is None:
            return
        root = {**schema, "components": self.spec.get("components", {})}
        errors = sorted(Draft202012Validator(root).iter_errors(body), key=lambda e: list(e.path))
        if errors:
            lines = [f"  - {'/'.join(map(str, e.path)) or '(root)'}: {e.message}" for e in errors[:5]]
            raise ContractViolation(f"응답 스키마 불일치: {method} {template} {status}\n" + "\n".join(lines))
