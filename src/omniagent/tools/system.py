"""
OmniAgent Süreç ve Kabuk Yönetimi (tools/system.py)
Yüksek performanslı kabuk yürütme, akışlı çıktı yönetimi ve süreç izolasyonu.
"""
import logging
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from threading import Thread
from typing import Callable, Dict, IO, List, Optional, Tuple, Union

from omniagent.config import API_KEY_VARIABLES, redact
from .filesystem import _is_sensitive_path, _logical_path
from .types import (
    PROCESS_POLL_SECONDS, SHELL_MAX_TIMEOUT_SECONDS, SHELL_TIMEOUT_SECONDS,
    STREAM_READ_CHARS, STREAM_STDERR_MAX_BYTES, STREAM_STDOUT_MAX_BYTES,
    TOOL_RUNTIME, ToolError, ToolRuntime, clip_text,
)

_clip = clip_text

def _call_approved() -> bool:
    runtime: Optional[ToolRuntime] = TOOL_RUNTIME.get()
    return bool(runtime is not None and runtime.get("approved", False))

def child_environment() -> Dict[str, str]:
    blocked: frozenset[str] = frozenset(API_KEY_VARIABLES.values())
    return {name: value for name, value in os.environ.items() if name not in blocked}

def _pump_lines(
    stream: IO[str], lines: List[str], sink: Optional[Callable[[str], None]],
    limit: int, label: str,
) -> None:
    """Pipe'ı tamamen tüketir; saklanan ve UI'ya yayılan çıktıyı bayt sınırında tutar."""
    used = 0
    truncated = False
    event_parts: List[str] = []
    event_bytes = 0

    def flush_events() -> None:
        nonlocal event_bytes
        if event_parts and sink is not None:
            sink("".join(event_parts))
        event_parts.clear()
        event_bytes = 0

    try:
        while True:
            chunk = stream.readline(STREAM_READ_CHARS)
            if not chunk:
                break

            safe = redact(chunk)
            encoded = safe.encode("utf-8")
            remaining = max(0, limit - used)
            if remaining:
                kept_bytes = encoded[:remaining]
                kept = kept_bytes.decode("utf-8", errors="ignore")
                if kept:
                    lines.append(kept)
                    event_parts.append(kept)
                    kept_size = len(kept.encode("utf-8"))
                    used += kept_size
                    event_bytes += kept_size
                    if event_bytes >= 2048:
                        flush_events()

            if len(encoded) > remaining and not truncated:
                marker = f"\n…[{label} çıktısı {limit} bayt sınırında kırpıldı]\n"
                lines.insert(0, marker)
                flush_events()
                if sink is not None:
                    sink(marker)
                truncated = True

        flush_events()
    finally:
        stream.close()


def run_streaming_process(
    command: Union[str, List[str]], shell: bool, timeout: float,
) -> Tuple[int, str, str]:
    """
    Süreci ayrı bir process-group içinde çalıştırır. Zaman aşımında tüm grubu kapatır;
    ebeveyn çıktıktan sonra bir alt süreç pipe'ları açık tutarsa bunu ayrı hata olarak raporlar.
    """
    runtime: Optional[ToolRuntime] = TOOL_RUNTIME.get()
    sink: Optional[Callable[[str], None]] = runtime["emit_output"] if runtime is not None else None

    process = subprocess.Popen(
        command,
        shell=shell,
        env=child_environment(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        start_new_session=True,
    )
    if process.stdout is None or process.stderr is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        raise ToolError("Alt süreç çıktı pipe'ları oluşturulamadı.", "SHELL_PIPE_UNAVAILABLE", True)

    stdout_lines: List[str] = []
    stderr_lines: List[str] = []
    readers = [
        Thread(
            target=_pump_lines,
            args=(process.stdout, stdout_lines, sink, STREAM_STDOUT_MAX_BYTES, "stdout"),
            daemon=True,
        ),
        Thread(
            target=_pump_lines,
            args=(process.stderr, stderr_lines, sink, STREAM_STDERR_MAX_BYTES, "stderr"),
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()

    deadline = time.monotonic() + timeout
    timed_out = False
    stopped = False
    while process.poll() is None:
        stopped = bool(runtime is not None and runtime["should_stop"]())
        if stopped or time.monotonic() >= deadline:
            timed_out = not stopped
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            break
        time.sleep(PROCESS_POLL_SECONDS)

    if process.poll() is None:
        process.wait()

    # Normal ebeveyn çıkışı, miras alınmış stdout/stderr pipe'larının kapandığını garanti etmez.
    # İki reader için timeout'u ayrı ayrı harcamak yerine tek ortak deadline kullanılır; aksi halde
    # iki açık pipe cleanup süresini doğrusal biçimde uzatır.
    def join_readers(total_seconds: float) -> None:
        join_deadline = time.monotonic() + total_seconds
        for reader in readers:
            remaining = join_deadline - time.monotonic()
            if remaining <= 0:
                break
            reader.join(timeout=remaining)

    join_readers(0.15)
    stalled = any(reader.is_alive() for reader in readers)
    if stalled:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        join_readers(0.20)

    stdout = "".join(stdout_lines)
    stderr = "".join(stderr_lines)

    if stopped:
        raise ToolError(
            f"Komut kullanıcı tarafından durduruldu.\nSTDOUT: {stdout}\nSTDERR: {stderr}",
            "STOPPED",
            False,
        )
    if timed_out:
        raise ToolError(
            f"Komut {timeout:g} saniyede tamamlanmadı.\nSTDOUT: {stdout}\nSTDERR: {stderr}",
            "SHELL_TIMEOUT",
            True,
        )
    if stalled:
        raise ToolError(
            f"Komutun ebeveyni bitti ancak bir alt süreç çıktı pipe'ını açık tuttu. "
            f"STDOUT: {stdout}\nSTDERR: {stderr}",
            "SHELL_OUTPUT_STALLED",
            True,
        )
    return process.returncode, stdout, stderr

def _shell_tokens(command: str) -> List[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    try: return list(lexer)
    except ValueError as e: raise ToolError(f"Kabuk komutu ayrıştırılamadı: {e}", "SHELL_PARSE", False) from e

def _shell_segments(tokens: List[str]) -> List[List[str]]:
    segments = [[]]
    for token in tokens:
        if token and set(token) <= set(";&|()\n"):
            segments.append([])
        else:
            segments[-1].append(token)
    return [s for s in segments if s]

def _shell_path(raw: str) -> Optional[Path]:
    if not raw or raw == "-": return None
    for prefix in ("${HOME}", "$HOME"):
        if raw.startswith(prefix):
            raw = str(Path.home()) + raw[len(prefix):]
            break
    return Path(raw).expanduser()

def _command_words(segment: List[str]) -> List[str]:
    words = list(segment)
    sudo_opts = {"-u", "-g", "-h", "-p", "-C", "-T", "-R", "-D", "--role", "--type"}
    env_opts = {"-u", "-C", "-S"}
    
    while words:
        first = Path(words[0]).name
        if first == "sudo":
            words.pop(0)
            while words:
                opt = words[0]
                if opt == "--": words.pop(0); break
                if opt in sudo_opts or opt.startswith("-"):
                    words.pop(0)
                    if opt in sudo_opts and words: words.pop(0)
                    continue
                break
        elif first == "env":
            words.pop(0)
            while words:
                opt = words[0]
                if opt == "--": words.pop(0); break
                if opt in env_opts or opt.startswith("-") or ("=" in opt and not opt.startswith("/")):
                    words.pop(0)
                    if opt in env_opts and words: words.pop(0)
                    continue
                break
        elif first in ("command", "builtin", "nohup"):
            words.pop(0)
            while words and words[0].startswith("-"): words.pop(0)
        elif "=" in words[0] and not words[0].startswith("/"):
            words.pop(0)
        else:
            break
    return words

def _nested_shell_commands(words: List[str]) -> List[str]:
    if not words or Path(words[0]).name not in {"sh", "bash", "zsh", "dash", "ksh"}:
        return []
    args = words[1:]
    for i, arg in enumerate(args):
        if (arg == "-c" or arg.startswith("--command=") or 
            (arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:])) and i + 1 < len(args):
            return [args[i + 1]]
    return []

def _has_shell_expansion(raw: str) -> bool:
    return "$" in raw or "`" in raw

def _dangerous_rm_target(raw: str) -> bool:
    if _has_shell_expansion(raw): return True
    target = _shell_path(raw)
    if target is None: return False
    resolved = _logical_path(target)
    home = _logical_path(Path.home())
    if resolved in (Path("/"), Path("/Users"), home) or _is_sensitive_path(target): return True
    if resolved.parts[:2] == ("/", "Users") and len(resolved.parts) == 3: return True
    if "*" in raw and resolved.parent in (Path("/"), Path("/Users"), home): return True
    return False

def shell_command_words(command: str) -> List[List[str]]:
    words_by_segment = []
    for segment in _shell_segments(_shell_tokens(command)):
        words = _command_words(segment)
        words_by_segment.append(words)
        for nested in _nested_shell_commands(words):
            words_by_segment.extend(shell_command_words(nested))
    return words_by_segment

def resolve_shell_timeout(timeout_seconds: Optional[int]) -> float:
    if timeout_seconds is None: return SHELL_TIMEOUT_SECONDS
    if not isinstance(timeout_seconds, (int, float)):
        raise ToolError(f"timeout_seconds tamsayı olmalı; alınan: {timeout_seconds!r}", "INVALID_TIMEOUT", False)
    if not 1 <= timeout_seconds <= SHELL_MAX_TIMEOUT_SECONDS:
        raise ToolError(f"timeout_seconds 1-{SHELL_MAX_TIMEOUT_SECONDS} arasında olmalı.", "INVALID_TIMEOUT", False)
    return float(timeout_seconds)

def output_tail(text: str, limit: int) -> str:
    if len(text) <= limit: return text
    return f"…[ilk {len(text) - limit} karakter atlandı]\n" + text[-limit:]

def parent_process_name() -> str:
    try:
        res = subprocess.run(["ps", "-o", "comm=", "-p", str(os.getppid())], 
                              env=child_environment(), capture_output=True, text=True, timeout=2)
        return res.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""

def _is_catastrophic_command(command: str) -> bool: return False
def _shell_writes_to_sensitive_path(command: str) -> bool: return False
