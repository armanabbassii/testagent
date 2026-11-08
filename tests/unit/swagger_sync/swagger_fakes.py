"""
tests/unit/swagger_sync/swagger_fakes.py — سندها و نشستِ HTTP ِ ساختگی (بدونِ شبکه)
"""

from __future__ import annotations

import json


def spec(operations: int, prefix: str = "/admin", title: str = "Podium") -> dict:
    """سندِ OpenAPI با تعدادِ مشخصِ operation."""
    return {
        "openapi": "3.0.1",
        "info": {"title": title, "version": "1.0"},
        "servers": [{"url": "http://podium.test/api"}],
        "paths": {
            f"{prefix}/op{n}": {
                "get": {
                    "operationId": f"op{n}",
                    "summary": f"op{n}",
                    "responses": {"200": {"description": "OK"}},
                }
            }
            for n in range(operations)
        },
    }


def raw(document) -> bytes:
    return document if isinstance(document, bytes) else json.dumps(document).encode()


class Response:
    def __init__(self, status: int = 200, content: bytes = b"", reason: str = "OK", headers=None):
        self.status_code = status
        self.content = content
        self.reason = reason
        self.headers = headers or {}


def ok(document) -> Response:
    return Response(200, raw(document))


class FakeSession:
    """هر get یک پاسخ یا استثنا از صف (یا از نگاشتِ url → صف) برمی‌گرداند."""

    def __init__(self, *outcomes, by_url: dict | None = None):
        self.outcomes = list(outcomes)
        self.by_url = {url: list(items) for url, items in (by_url or {}).items()}
        self.calls: list[dict] = []

    def get(self, url, timeout=None, headers=None):
        self.calls.append({"url": url, "timeout": timeout, "headers": headers})
        queue = self.by_url.get(url, self.outcomes)
        if not queue:
            raise AssertionError(f"unexpected request to {url}")
        outcome = queue.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
