"""Codex ve OpenCode oturumlarını OmniAgent model turuna bağlar."""
import asyncio
import base64
import json
import os
import signal
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


class CliModelError(RuntimeError):
    """Yerel model istemcisinin anlaşılır hatası."""


SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "content": {"type": "string"},
        "tool_calls": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "arguments": {"type": "string"}},
                "required": ["name", "arguments"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["content", "tool_calls"],
    "additionalProperties": False,
}
TIMEOUT_SECONDS: float = 45.0


def _prompt(messages: List[Dict[str, Any]], schemas: List[Dict[str, Any]], directory: Path) -> tuple[str, List[Path]]:
    """Çok kipli bağlamı korur; görselleri yalnız tur süresince geçici dosyada tutar."""
    images: List[Path] = []
    records: List[Dict[str, Any]] = []
    for entry in messages:
        content: Any = entry.get("content", "")
        parts: List[str] = []
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    parts.append(str(part.get("text", "")))
                elif part.get("type") == "image_url":
                    image: Any = part.get("image_url", {})
                    url: str = str(image.get("url", "")) if isinstance(image, dict) else ""
                    if url.startswith("data:image/") and ";base64," in url:
                        header, encoded = url.split(";base64,", 1)
                        path: Path = directory / f"image-{len(images) + 1}{'.png' if 'png' in header else '.jpg'}"
                        try:
                            path.write_bytes(base64.b64decode(encoded, validate=True))
                        except (OSError, ValueError):
                            continue
                        images.append(path)
                        parts.append(f"[Görsel {len(images)} eklendi]")
        else:
            parts.append(str(content))
        record: Dict[str, Any] = {"role": entry.get("role", ""), "content": "\n".join(parts)}
        for key in ("tool_calls", "tool_call_id"):
            if entry.get(key):
                record[key] = entry[key]
        records.append(record)
    history: str = json.dumps(records, ensure_ascii=False, separators=(",", ":"))
    if len(history) > 110_000:
        history = json.dumps(records[:1] + records[-12:], ensure_ascii=False, separators=(",", ":"))
    functions: List[Dict[str, Any]] = [entry["function"] for entry in schemas if "function" in entry]
    instructions: str = (
        "Sen OmniAgent'ın yalnız model karar katmanısın. Kendi araçlarını çağırma. "
        "Eylemleri yalnız aşağıdaki OmniAgent araçları için JSON tool_calls alanına yaz; "
        "OmniAgent bunları kendi denetimiyle çalıştırır. Yalnız bir JSON nesnesi döndür: "
        '{"content":"yanıt veya STATE","tool_calls":[{"name":"araç","arguments":"{\\"alan\\":\\"değer\\"}"}]}. '
        "arguments geçerli JSON nesnesinin metni olmalı. Görev bittiyse tool_calls boş olmalı. "
        "Markdown kod bloğu ekleme. SYSTEM kaydını ana kural, tool sonuçlarını gözlem say.\n"
    )
    prompt: str = (
        instructions + "\nİZİNLİ ARAÇLAR:\n"
        + json.dumps(functions, ensure_ascii=False, separators=(",", ":"))
        + "\nKONUŞMA:\n" + history
    )
    if len(prompt) > 120_000:
        raise CliModelError("CLI model bağlamı 120.000 karakteri aşıyor.")
    return prompt, images


def _events(provider: str, stdout: bytes) -> tuple[str, Dict[str, int]]:
    """JSONL yanıtındaki metni ve token kullanımını ayırır."""
    chunks: List[str] = []
    usage: Dict[str, int] = {"prompt_tokens": 0, "cached_tokens": 0, "completion_tokens": 0}
    errors: List[str] = []
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        try:
            event: Dict[str, Any] = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind: str = str(event.get("type", ""))
        if kind == "error":
            detail: Any = event.get("error", event.get("message", ""))
            if isinstance(detail, dict):
                data: Any = detail.get("data", {})
                detail = data.get("message", detail.get("message", detail)) if isinstance(data, dict) else detail
            errors.append(str(detail)[:300])
        if provider == "codex":
            item: Any = event.get("item", {})
            if kind == "item.completed" and isinstance(item, dict) and item.get("type") == "agent_message":
                chunks.append(str(item.get("text", "")))
            if kind == "turn.completed":
                data = event.get("usage", {})
                usage = {"prompt_tokens": int(data.get("input_tokens", 0)),
                         "cached_tokens": int(data.get("cached_input_tokens", 0)),
                         "completion_tokens": int(data.get("output_tokens", 0))}
            if kind == "turn.failed":
                errors.append(str(event.get("error", {}).get("message", "Codex turu başarısız"))[:300])
        else:
            part: Any = event.get("part", {})
            if kind == "text" and isinstance(part, dict):
                chunks.append(str(part.get("text", "")))
            if kind == "step_finish" and isinstance(part, dict):
                data = part.get("tokens", {})
                cached: int = int(data.get("cache", {}).get("read", 0))
                usage["prompt_tokens"] += int(data.get("input", 0)) + cached
                usage["cached_tokens"] += cached
                usage["completion_tokens"] += int(data.get("output", 0))
    if errors:
        raise CliModelError("; ".join(errors)[:400])
    return "".join(chunks), usage


async def _process(
    command: List[str], env: Dict[str, str], input_text: Optional[str],
    should_stop: Callable[[], bool],
) -> tuple[bytes, bytes, int, bool]:
    """Süreç grubunu Esc, iptal veya zaman aşımında temizleyip sonlandırır."""
    process = await asyncio.create_subprocess_exec(
        *command, stdin=asyncio.subprocess.PIPE if input_text is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=env, start_new_session=True,
    )
    operation = asyncio.create_task(process.communicate(
        input_text.encode("utf-8") if input_text is not None else None
    ))

    async def terminate() -> None:
        """wait_for'ın communicate görevini iptal etmeden süreç ağacını kapatır."""
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        done, _ = await asyncio.wait({operation}, timeout=2)
        if not done:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            done, _ = await asyncio.wait({operation}, timeout=2)
        if not done:
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
        if process.returncode is None:
            await process.wait()

    loop = asyncio.get_running_loop()
    deadline: float = loop.time() + TIMEOUT_SECONDS
    try:
        while not operation.done():
            if should_stop() or loop.time() >= deadline:
                stopped: bool = should_stop()
                await terminate()
                return b"", b"", int(process.returncode or 0), stopped
            await asyncio.wait({operation}, timeout=0.1)
        stdout, stderr = await operation
        return stdout, stderr, int(process.returncode or 0), False
    except BaseException:
        await terminate()
        raise


async def run_cli_model(
    provider: str, model: str, messages: List[Dict[str, Any]], schemas: List[Dict[str, Any]],
    emit: Callable[[Dict[str, Any]], None], should_stop: Callable[[], bool],
) -> Dict[str, Any]:
    """ChatGPT oturumlu Codex veya OpenCode ücretsiz modelinden tek karar turu alır."""
    if provider not in ("codex", "opencode"):
        raise CliModelError(f"Bilinmeyen CLI sağlayıcısı: {provider}")
    with tempfile.TemporaryDirectory(prefix="omni-model-") as temporary:
        directory: Path = Path(temporary)
        prompt, images = _prompt(messages, schemas, directory)
        env: Dict[str, str] = dict(os.environ)
        if provider == "codex":
            schema_file: Path = directory / "answer-schema.json"
            schema_file.write_text(json.dumps(SCHEMA), encoding="utf-8")
            command: List[str] = [
                "codex", "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral",
                "--skip-git-repo-check", "-s", "read-only", "-C", temporary,
                "-m", model, "--output-schema", str(schema_file), "--json",
            ]
            for path in images:
                command.extend(["-i", str(path)])
            command.append("-")
            input_text: Optional[str] = prompt
        else:
            config_file: Path = directory / "opencode.json"
            config_file.write_text(json.dumps(
                {"agent": {"build": {"permission": {"*": "deny"}}}}
            ), encoding="utf-8")
            env["OPENCODE_CONFIG"] = str(config_file)
            command = [
                "opencode", "run", "--pure", "--agent", "build", "--model",
                f"opencode/{model}", "--format", "json", "--dir", temporary,
            ]
            for path in images:
                command.extend(["-f", str(path)])
            input_text = prompt
        stdout, stderr, code, stopped = await _process(command, env, input_text, should_stop)
        if stopped:
            return {"content": "", "tool_calls": [], "finish_reason": "stopped",
                    "usage": {"prompt_tokens": 0, "cached_tokens": 0, "completion_tokens": 0}}
        if not stdout and not stderr:
            raise CliModelError(f"{provider} model isteği zaman aşımına uğradı.")
        answer_text, usage = _events(provider, stdout)
        if code != 0:
            raise CliModelError(
                f"{provider} istemcisi çıkış {code}: {stderr.decode('utf-8', errors='replace')[-300:]}"
            )
        value: str = answer_text.strip()
        if value.startswith(chr(96) * 3):
            lines: List[str] = value.splitlines()
            value = "\n".join(lines[1:-1]).strip() if len(lines) >= 3 else value
        try:
            answer: Any = json.loads(value)
        except json.JSONDecodeError as error:
            raise CliModelError(f"Model geçerli araç JSON'u döndürmedi: {value[:240]}") from error
        if not isinstance(answer, dict) or not isinstance(answer.get("content"), str) or not isinstance(answer.get("tool_calls"), list):
            raise CliModelError("Model yanıtında content/tool_calls alanları eksik.")
        calls: List[Dict[str, str]] = []
        for item in answer["tool_calls"]:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                raise CliModelError("Araç çağrısı geçersiz biçimde.")
            arguments: Any = item.get("arguments", "{}")
            if isinstance(arguments, dict):
                arguments = json.dumps(arguments, ensure_ascii=False)
            try:
                valid: bool = isinstance(arguments, str) and isinstance(json.loads(arguments), dict)
            except json.JSONDecodeError:
                valid = False
            if not valid:
                raise CliModelError("Araç argümanları JSON nesnesi değil.")
            calls.append({"id": f"cli-{uuid.uuid4().hex[:12]}", "name": item["name"], "arguments": arguments})
        content: str = answer["content"]
        if content:
            emit({"kind": "text_delta", "text": content})
        return {"content": content, "tool_calls": calls,
                "finish_reason": "tool_calls" if calls else "stop", "usage": usage}
