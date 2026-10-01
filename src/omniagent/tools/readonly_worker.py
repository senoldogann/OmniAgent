"""Owned subprocess for the existing DDGS search, with a search-only JSON protocol."""
from __future__ import annotations

from contextlib import contextmanager, redirect_stderr, redirect_stdout
import json
import os
import subprocess
import sys
from typing import Iterator

from ddgs import DDGS

from .process import run_preemptible_process
from .system import child_environment
from .types import ToolError
from .web import search_web

WORKER_FLAG = "--internal-readonly-worker"
REQUEST_LIMIT = 16 * 1024
QUERY_LIMIT = 8 * 1024
RESPONSE_LIMIT = 1024 * 1024


def worker_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, WORKER_FLAG]
    return [sys.executable, "-m", "omniagent.tools.readonly_worker", WORKER_FLAG]


def _arguments(value: object) -> dict:
    if not isinstance(value, dict) or not {"query"} <= value.keys() <= {"query", "category", "freshness_days"}:
        raise ValueError("invalid search arguments")
    query = value["query"]
    category = value.get("category", "auto")
    freshness = value.get("freshness_days")
    if (not isinstance(query, str) or not query.strip() or len(query.encode("utf-8")) > QUERY_LIMIT
            or not isinstance(category, str) or len(category.encode("utf-8")) > 32
            or category.strip().casefold() not in {"auto", "text", "news"}
            or (freshness is not None and (type(freshness) is not int or not 1 <= freshness <= 30))):
        raise ValueError("invalid search arguments")
    return {"query": query, "category": category, "freshness_days": freshness}


def _json_object(raw: bytes) -> dict:
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result
    def finite_constants(value):
        raise ValueError("non-finite JSON")
    value = json.loads(raw, object_pairs_hook=unique_keys, parse_constant=finite_constants)
    if not isinstance(value, dict):
        raise ValueError("expected object")
    return value


def search_in_worker(query: str, category: str = "auto", freshness_days: int | None = None) -> str:
    try:
        arguments = _arguments({"query": query, "category": category, "freshness_days": freshness_days})
    except (ValueError, UnicodeError):
        raise ToolError("Geçersiz web arama argümanları.", "INVALID_ARGUMENTS", False) from None
    request = json.dumps(arguments, ensure_ascii=False).encode("utf-8")
    if len(request) > REQUEST_LIMIT:
        raise ToolError("Web arama isteği boyut sınırını aştı.", "INVALID_ARGUMENTS", False)
    try:
        result = run_preemptible_process(worker_command(), env=child_environment(),
                                        input=request, timeout=30, max_output_bytes=RESPONSE_LIMIT)
        if result.returncode != 0 or len(result.stdout) > RESPONSE_LIMIT:
            raise ValueError("invalid worker completion")
        response = _json_object(result.stdout)
        if response.get("ok") is True and set(response) == {"ok", "result"} and isinstance(response["result"], str):
            return response["result"]
        if (response.get("ok") is False and set(response) == {"ok", "code", "recoverable"}
                and isinstance(response["code"], str)
                and response["code"] in {"INVALID_ARGUMENTS", "WEB_SEARCH_FAILED", "WEB_SEARCH_EMPTY", "OUTPUT_TOO_LARGE"}
                and type(response["recoverable"]) is bool):
            raise ToolError("Web araması tamamlanamadı.", response["code"], response["recoverable"])
        raise ValueError("invalid worker response")
    except (OSError, ValueError, RecursionError, subprocess.TimeoutExpired):
        # Never return child stdout/stderr or a command containing private input.
        raise ToolError("Web arama işçisi tamamlanamadı.", "WEB_SEARCH_FAILED", True) from None


@contextmanager
def _quiet_diagnostics() -> Iterator[None]:
    # Preserve only the private protocol writer. Suppress both Python prints and
    # native/client writes to stdout/stderr, including windowed bundles with None streams.
    with open(os.devnull, "w", encoding="utf-8") as sink:
        saved = [os.dup(1), os.dup(2)]
        try:
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
            with redirect_stdout(sink), redirect_stderr(sink):
                yield
        finally:
            for descriptor, duplicate in zip((1, 2), saved):
                os.dup2(duplicate, descriptor)
                os.close(duplicate)


def main() -> None:
    # Own descriptors explicitly: PyInstaller console=False replaces sys streams
    # with None, while the parent still supplies actual protocol pipes.
    with os.fdopen(os.dup(0), "rb") as reader, os.fdopen(os.dup(1), "wb") as writer:
        response = {"ok": False, "code": "INVALID_ARGUMENTS", "recoverable": False}
        with _quiet_diagnostics():
            try:
                request = reader.read(REQUEST_LIMIT + 1)
                if len(request) > REQUEST_LIMIT:
                    raise ValueError("request too large")
                arguments = _arguments(_json_object(request))
            except (ValueError, UnicodeError, RecursionError):
                pass
            else:
                try:
                    result = search_web(**arguments, client_factory=DDGS)
                    response = {"ok": True, "result": result}
                except ToolError as error:
                    code = error.code if error.code in {"WEB_SEARCH_EMPTY", "INVALID_ARGUMENTS"} else "WEB_SEARCH_FAILED"
                    response = {"ok": False, "code": code, "recoverable": error.recoverable}
                except Exception:
                    response = {"ok": False, "code": "WEB_SEARCH_FAILED", "recoverable": True}
            try:
                encoded = json.dumps(response, ensure_ascii=False).encode("utf-8")
            except (ValueError, UnicodeError):
                encoded = b'{"ok":false,"code":"WEB_SEARCH_FAILED","recoverable":true}'
            if len(encoded) > RESPONSE_LIMIT:
                encoded = b'{"ok":false,"code":"OUTPUT_TOO_LARGE","recoverable":false}'
            writer.write(encoded)
            writer.flush()


if __name__ == "__main__":
    main()
