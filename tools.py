import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import stat
import signal
import subprocess
import tempfile
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import lru_cache, wraps
from pathlib import Path
from threading import Thread
from typing import IO, Callable, Concatenate, Dict, List, Optional, ParamSpec, Tuple, TypedDict, Union
from urllib.parse import SplitResult, urlsplit

import user_memory as memory
import AppKit
import ApplicationServices as AX
import cv2
import numpy as np
import pyautogui
import Quartz
from bs4 import BeautifulSoup
from ddgs import DDGS
from PIL import Image
from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

# pyautogui varsayılanı her çağrıdan sonra 0.1sn bekler (FAILSAFE tepki payı için);
# eylem zincirlerinde bu bekleme birikmesin diye düşürülür.
pyautogui.PAUSE = 0.02

# Araç sonuçları modele gider: her bayt token demektir. Uzun çıktılar kırpılır.
SHELL_STDOUT_LIMIT: int = 4000
SHELL_STDERR_LIMIT: int = 1000
FILE_READ_LIMIT: int = 8000
FILE_READ_MAX_BYTES: int = 8 * 1024 * 1024
STREAM_STDOUT_MAX_BYTES: int = 64 * 1024
STREAM_STDERR_MAX_BYTES: int = 16 * 1024
STREAM_READ_CHARS: int = 4096
TYPED_TEXT_ECHO_LIMIT: int = 80
BACKUP_KEEP_PER_FILE: int = 5
# Modelin gördüğü ekran görüntüsü 1000×1000 karedir (ekran oranı korunmaz). Tüm GUI
# koordinatları (görüntü, AX listesi, tıklama/taşıma) bu ORTAK uzaydadır; Retina piksel/nokta
# dönüşümünü araçlar yapar. Qwen ailesi koordinatı görüntü boyutundan bağımsız 0-1000
# normalize verir, Claude/GPT görüntü pikseli verir: kare 1000'lik görüntüde ikisi aynı sayıdır.
# Ölçüm (qwen3.8-flash, 1280×832 görüntü): arama kutusu merkezi y=146 iken model y=156-173 verdi.
MODEL_SCREEN_SIZE: int = 1000
PAGE_TEXT_LIMIT: int = 5000
PAGE_ELEMENT_LIMIT: int = 40
PAGE_LOAD_TIMEOUT_MS: int = 20000
# Playwright varsayılanı 30sn: yanlış bir seçici bütün turu kilitlemesin.
PAGE_ACTION_TIMEOUT_MS: int = 5000
AX_ELEMENT_LIMIT: int = 80
AX_NODE_LIMIT: int = 3000
AX_SCAN_BUDGET_SECONDS: float = 2.0
AX_MESSAGING_TIMEOUT_SECONDS: float = 1.0
AX_LABEL_SEARCH_NODES: int = 12
UNICODE_CHUNK_UNITS: int = 16
UNICODE_CHUNK_DELAY_SECONDS: float = 0.005
MAX_WAIT_SECONDS: float = 5.0
# Eylem sonrası gözlem sabit uyku yerine ekranın durulmasını bekler. Karşılaştırma küçük gri
# karelerle yapılır (~30 ms); imleç yanıp sönmesi, sekme simgesi dönmesi gibi küçük alan
# değişimleri SETTLE_CHANGED_RATIO altında kalıp yok sayılır. Ölçümde durulmuş ekranda
# 4 gri tonu aşan değişim yalnız menü çubuğundaki 2 pikseldi; yükleme iskeletinin parıltısı
# kare başına ~2-3 ton değişir ve son değişim karesine göre birikerek eşiği aşar (%0,5-9 alan).
SETTLE_FRAME_EDGE: int = 160
SETTLE_PIXEL_DELTA: int = 4
SETTLE_CHANGED_RATIO: float = 0.002
SETTLE_POLL_SECONDS: float = 0.03
# Eylemden sonra görünür tepki (ör. gecikmeli XHR sonucu) en çok bu kadar beklenir
SETTLE_REACTION_SECONDS: float = 1.0
# Tepkiden sonra bu kadar değişmeyen ekran durulmuş sayılır
SETTLE_QUIET_SECONDS: float = 0.45
# Sürekli animasyonda (video, yükleme göstergesi) gözlem eylemden en geç bu kadar sonra alınır
SETTLE_MAX_SECONDS: float = 3.0
# chrome_active_tab gezinmeden sonra sekmenin yüklenmesini 0,1 sn aralıkla en çok bu kadar yoklar
CHROME_LOAD_CHECKS: int = 80
CHROME_SCRIPT_TIMEOUT_SECONDS: float = 20.0


SHELL_TIMEOUT_SECONDS: float = 60.0
JS_TIMEOUT_SECONDS: float = 20.0

PROCESS_POLL_SECONDS: float = 0.05


class ToolRuntime(TypedDict):
    """Çağrı başına araç bağlamı: canlı çıktı hedefi ve kullanıcı durdurma denetimi."""
    emit_output: Callable[[str], None]
    should_stop: Callable[[], bool]


# main.execute_tool her çağrı için ayarlar; asyncio.to_thread bağlamı kopyaladığından işçi
# thread'inde de görünür. Ayarlı değilse (testler, doğrudan kullanım) çıktı yalnızca biriktirilir.
TOOL_RUNTIME: ContextVar[Optional[ToolRuntime]] = ContextVar("TOOL_RUNTIME", default=None)


def _clip(text: str, limit: int) -> str:
    """Metni belirtilen uzunlukta kırpıp kısaltma bilgisini ekler (model kopsa da bilir)."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[kısaltıldı, toplam {len(text)} karakter]"


def _pump_lines(stream: IO[str], lines: List[str], sink: Optional[Callable[[str], None]],
                limit: int, label: str) -> None:
    """Boruyu sınırlı parçalarla tüketir; fazla baytları tutmadan akışı boşaltır."""
    used = 0
    truncated = False
    event_parts: List[str] = []
    event_size = 0
    last_emit = 0.0

    def publish(value: str) -> None:
        if sink is not None:
            try:
                sink(value)
            except Exception:
                # Arayüz kapanmış olsa bile boru boşaltılmalı; aksi hâlde süreç takılabilir.
                logging.warning("Canlı komut çıktısı yayınlanamadı", extra={"stream": label})

    try:
        while True:
            chunk = stream.readline(STREAM_READ_CHARS)
            if not chunk:
                break
            raw = chunk.encode("utf-8")
            remaining = max(0, limit - used)
            if remaining:
                kept = raw[:remaining].decode("utf-8", errors="ignore")
                if kept:
                    lines.append(kept)
                    event_parts.append(kept)
                    event_size += len(kept)
                used += min(len(raw), remaining)
                if event_size >= 2048 or (event_parts and time.monotonic() - last_emit >= 0.1):
                    publish("".join(event_parts))
                    event_parts.clear()
                    event_size = 0
                    last_emit = time.monotonic()
            if len(raw) > remaining and not truncated:
                if event_parts:
                    publish("".join(event_parts))
                    event_parts.clear()
                marker = f"\n…[{label} çıktısı {limit} bayt sınırında kırpıldı]\n"
                lines.insert(0, marker)
                publish(marker)
                truncated = True
        if event_parts:
            publish("".join(event_parts))
    finally:
        stream.close()


def run_streaming_process(command: Union[str, List[str]], shell: bool, timeout: float) -> Tuple[int, str, str]:
    """
    Süreci çalıştırır ve (çıkış kodu, stdout, stderr) döner. Borular sınır dolsa da
    boşaltılır; bellek, model sonucu ve arayüz akışı sınırlı kalır. İptalde/zaman
    aşımında süreç grubu sonlandırılır.
    """
    runtime: Optional[ToolRuntime] = TOOL_RUNTIME.get()
    sink: Optional[Callable[[str], None]] = runtime["emit_output"] if runtime is not None else None
    process: subprocess.Popen[str] = subprocess.Popen(
        command, shell=shell, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", bufsize=1, start_new_session=True,
    )
    stdout_lines: List[str] = []
    stderr_lines: List[str] = []
    readers: List[Thread] = [
        Thread(target=_pump_lines, args=(process.stdout, stdout_lines, sink, STREAM_STDOUT_MAX_BYTES, "stdout"), daemon=True),
        Thread(target=_pump_lines, args=(process.stderr, stderr_lines, sink, STREAM_STDERR_MAX_BYTES, "stderr"), daemon=True),
    ]
    for reader in readers:
        reader.start()
    deadline: float = time.monotonic() + timeout
    while True:
        try:
            process.wait(timeout=PROCESS_POLL_SECONDS)
            break
        except subprocess.TimeoutExpired:
            stopped: bool = runtime is not None and runtime["should_stop"]()
            if not stopped and time.monotonic() < deadline:
                continue
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            for reader in readers:
                reader.join(timeout=1)
            if stopped:
                raise ToolError("Komut kullanıcı tarafından durduruldu.", "STOPPED", False) from None
            raise
    for reader in readers:
        reader.join(timeout=0.5)
    if any(reader.is_alive() for reader in readers):
        # Kabuk bittiği hâlde arka plan çocuğu boruyu açık tuttuysa grup sonlandırılır.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        for reader in readers:
            reader.join(timeout=0.5)
        raise ToolError("Arka plan süreci çıktı borusunu açık tuttu; süreç grubu sonlandırıldı.",
                        "SHELL_OUTPUT_STALLED", True)
    return process.returncode, "".join(stdout_lines), "".join(stderr_lines)


# Ajanın kendi kendini iyileştirmesi için yapılandırılmış hata sınıfı
class ToolError(Exception):
    def __init__(self, message: str, code: str, recoverable: bool) -> None:
        super().__init__(message)
        self.code: str = code
        self.recoverable: bool = recoverable


# --- Güvenlik Rayları (Safety Rails) ---
# Bunlar bir sandbox DEĞİLDİR; bilinen en yıkıcı kalıpları engelleyen,
# en iyi çaba (best-effort) bir koruma katmanıdır.

PROJECT_ROOT: Path = Path(__file__).resolve().parent
BACKUP_DIR: Path = PROJECT_ROOT / ".omni_backups"

_SENSITIVE_PATH_PREFIXES: Tuple[Path, ...] = (
    Path.home() / ".ssh",
    Path.home() / ".aws",
    Path.home() / ".gnupg",
    Path.home() / ".netrc",
    Path.home() / ".git-credentials",
    Path.home() / ".local/share/opencode/auth.json",
    Path.home() / ".zshrc",
    Path.home() / ".zprofile",
    Path.home() / ".zshenv",
    Path.home() / ".bashrc",
    Path.home() / ".bash_profile",
    Path.home() / ".profile",
    Path("/etc"),
    Path("/private/etc"),
    Path("/System"),
    Path("/Library"),
    Path("/usr"),
    Path("/bin"),
    Path("/sbin"),
    Path("/var/root"),
)

_CATASTROPHIC_SHELL_PATTERNS: Tuple[str, ...] = (
    r"\bmkfs\b",
    r"\bdd\b[^\n]*of=/dev/",
    r":\(\)\s*{\s*:\|:&\s*};\s*:",
    r"\bdiskutil\s+(erase|partition)",
    r">\s*/dev/(disk|sda|rdisk)",
    r"chmod\s+-R\s+(000|777)\s+/\s*$",
    r"\bsudo\s+rm\s+-\w*[rR]",
)


def _logical_path(path: Path) -> Path:
    """macOS firmlink yollarını mantıksal kullanıcı yoluna indirger."""
    resolved: Path = path.expanduser().resolve()
    posix: str = resolved.as_posix()
    marker: str = "/System/Volumes/Data"
    if posix == marker:
        return Path("/")
    if posix.startswith(marker + "/"):
        return Path(posix[len(marker):])
    return resolved


@lru_cache(maxsize=4)
def _sensitive_prefixes(prefixes: Tuple[Path, ...]) -> Tuple[Path, ...]:
    """Sabit koruma yollarını her araç çağrısında yeniden çözümleme."""
    return tuple(_logical_path(raw.expanduser()) for raw in prefixes)


def _is_sensitive_path(path: Path) -> bool:
    """Hedef yol korunan sistem/kimlik yolunun kendisi ya da altı mı?"""
    resolved: Path = _logical_path(path)
    return any(resolved == prefix or prefix in resolved.parents
               for prefix in _sensitive_prefixes(_SENSITIVE_PATH_PREFIXES))


def _sensitive_write_allowed() -> bool:
    """Yalnız kullanıcı tarafından başlangıçta verilen yazma gevşetme bayrağı."""
    return os.environ.get("OMNI_ALLOW_SENSITIVE_WRITE") == "1"


def _sensitive_read_allowed() -> bool:
    """Yalnız kullanıcı tarafından başlangıçta verilen okuma gevşetme bayrağı."""
    return os.environ.get("OMNI_ALLOW_SENSITIVE_READ") == "1"


def _shell_tokens(command: str) -> List[str]:
    """Kabuk metnini alıntı ve bitişik yönlendirmeleri gözeterek parçalar."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError as error:
        raise ToolError(f"Kabuk komutu ayrıştırılamadı: {error}", "SHELL_PARSE", False) from error


def _shell_segments(tokens: List[str]) -> List[List[str]]:
    """Sıralı/pipeline komutlarını ayırır; yönlendirmeler ilgili komutta kalır."""
    segments: List[List[str]] = [[]]
    for token in tokens:
        if token and set(token) <= set(";&|()\n"):
            segments.append([])
        else:
            segments[-1].append(token)
    return [segment for segment in segments if segment]


def _shell_path(raw: str) -> Optional[Path]:
    """Yalnız bilinen ev dizini gösterimlerini açar; değişken komut çalıştırmaz."""
    if not raw or raw == "-":
        return None
    for prefix in ("${HOME}", "$HOME"):
        if raw.startswith(prefix):
            raw = str(Path.home()) + raw[len(prefix):]
            break
    return Path(raw).expanduser()


def _command_words(segment: List[str]) -> List[str]:
    """Öndeki sudo/env sarmalayıcılarını, seçenek argümanlarını ve atamaları atlar."""
    words = list(segment)
    sudo_value_options: frozenset[str] = frozenset({
        "-u", "--user", "-g", "--group", "-h", "--host", "-p", "--prompt",
        "-C", "--close-from", "-T", "--command-timeout", "-R", "--chroot",
        "-D", "--chdir", "--role", "--type",
    })
    env_value_options: frozenset[str] = frozenset({
        "-u", "--unset", "-C", "--chdir", "-S", "--split-string",
    })
    while words:
        first = Path(words[0]).name
        if first == "sudo":
            words.pop(0)
            while words:
                option: str = words[0]
                if option == "--":
                    words.pop(0)
                    break
                if option in sudo_value_options:
                    words.pop(0)
                    if words:
                        words.pop(0)
                    continue
                if option.startswith("-"):
                    words.pop(0)
                    continue
                break
        elif first == "env":
            words.pop(0)
            while words:
                option = words[0]
                if option == "--":
                    words.pop(0)
                    break
                if option in env_value_options:
                    words.pop(0)
                    if words:
                        words.pop(0)
                    continue
                if option.startswith("-") or ("=" in option and not option.startswith("/")):
                    words.pop(0)
                    continue
                break
        elif first in ("command", "builtin", "nohup"):
            words.pop(0)
            while words and words[0].startswith("-"):
                words.pop(0)
        elif "=" in words[0] and not words[0].startswith("/"):
            words.pop(0)
        else:
            break
    return words


def _has_shell_expansion(raw: str) -> bool:
    """Guard'ın çözemediği kabuk değişkeni/komut ikamesi bulunuyor mu?"""
    return "$" in raw or "`" in raw


def _nested_shell_commands(words: List[str]) -> List[str]:
    """sh -c/--command gibi iç içe kabuk çağrılarının metnini döndürür."""
    if not words or Path(words[0]).name not in {"sh", "bash", "zsh", "dash", "ksh"}:
        return []
    args: List[str] = words[1:]
    for index, argument in enumerate(args):
        is_command_option: bool = (
            argument == "-c" or argument.startswith("--command=") or
            argument.startswith("-") and not argument.startswith("--") and "c" in argument[1:]
        )
        if is_command_option and index + 1 < len(args):
            return [args[index + 1]]
    return []


def _dangerous_rm_target(raw: str) -> bool:
    """Kök, kullanıcı evleri, korunan sistem yolları ve çözülemeyen değişkenleri engeller."""
    if _has_shell_expansion(raw):
        # rm -rf $TARGET komutu çalışmadan önce hangi yola silindiği bilinemez.
        return True
    target = _shell_path(raw)
    if target is None:
        return False
    resolved = _logical_path(target)
    home = _logical_path(Path.home())
    if resolved in (Path("/"), Path("/Users"), home) or _is_sensitive_path(target):
        return True
    if resolved.parts[:2] == ("/", "Users") and len(resolved.parts) == 3:
        return True
    if "*" in raw and resolved.parent in (Path("/"), Path("/Users"), home):
        return True
    return False


def _is_catastrophic_command(command: str) -> bool:
    """shlex hedef analiziyle yıkıcı silmeleri, diğer bilinen kalıpları yakalar."""
    tokens = _shell_tokens(command)
    for segment in _shell_segments(tokens):
        words = _command_words(segment)
        if any(_is_catastrophic_command(nested) for nested in _nested_shell_commands(words)):
            return True
        if not words or Path(words[0]).name != "rm":
            continue
        args = words[1:]
        if any(arg == "--no-preserve-root" for arg in args):
            return True
        recursive = any(arg == "--recursive" or
                        arg.startswith("-") and not arg.startswith("--") and "r" in arg.lower()
                        for arg in args)
        force = any(arg == "--force" or
                    arg.startswith("-") and not arg.startswith("--") and "f" in arg.lower()
                    for arg in args)
        if recursive and force and any(_dangerous_rm_target(arg) for arg in args if not arg.startswith("-")):
            return True
    normalized = " ".join(command.split())
    if re.search(r"\benv\b[^\n;&|]*(?:-S|--split-string)", normalized, re.IGNORECASE):
        return True
    return any(re.search(pattern, normalized, re.IGNORECASE) for pattern in _CATASTROPHIC_SHELL_PATTERNS)


def _shell_writes_to_sensitive_path(command: str) -> bool:
    """Yönlendirme ve tee/sed -i/cp/mv hedeflerini korunan yol listesiyle eşler."""
    tokens = _shell_tokens(command)
    for index, token in enumerate(tokens[:-1]):
        if token in (">", ">>", ">|", "&>"):
            raw_target = tokens[index + 1]
            if _has_shell_expansion(raw_target):
                return True
            target = _shell_path(raw_target)
            if target is not None and _is_sensitive_path(target):
                return True
    for segment in _shell_segments(tokens):
        words = _command_words(segment)
        if any(_shell_writes_to_sensitive_path(nested) for nested in _nested_shell_commands(words)):
            return True
        if not words:
            continue
        operation = Path(words[0]).name
        args = words[1:]
        targets: List[str] = []
        if operation == "tee":
            targets = [arg for arg in args if not arg.startswith("-")]
        elif operation in ("cp", "mv"):
            targets = [args[-1]] if args else []
            for index, arg in enumerate(args[:-1]):
                if arg in ("-t", "--target-directory"):
                    targets.append(args[index + 1])
                elif arg.startswith("--target-directory="):
                    targets.append(arg.split("=", 1)[1])
        elif operation == "sed" and any(arg == "-i" or arg.startswith("-i.") or
                                           arg == "--in-place" or arg.startswith("--in-place=")
                                           for arg in args):
            targets = [arg for arg in args if not arg.startswith("-")]
        if any(_has_shell_expansion(raw) or
               (path is not None and _is_sensitive_path(path))
               for raw, path in ((raw, _shell_path(raw)) for raw in targets)):
            return True
    return False


def _backup_file(path: Path) -> Path:
    """
    Üzerine yazılmadan önce mevcut dosyanın zaman damgalı yedeğini alır (bellekte
    iki kopya tutmadan, metaveriyle birlikte). Aynı dosya başına yalnızca son
    BACKUP_KEEP_PER_FILE yedek tutulur; uzun otonom oturumlarda disk sızıntısını önler.
    """
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp: str = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    backup_path: Path = BACKUP_DIR / f"{path.name}.{stamp}.bak"
    shutil.copy2(path, backup_path)
    existing: List[Path] = sorted(BACKUP_DIR.glob(f"{path.name}.*.bak"))
    for old in existing[:-BACKUP_KEEP_PER_FILE]:
        old.unlink()
    return backup_path


def missing_path_hint(path: Path) -> str:
    """
    Bulunamayan bir yol için en yakın mevcut üst dizini ve içeriğinden birkaç adı döner.
    Model uzun yolları kopyalarken harf düşürebiliyor; doğru adı aynı turda görsün.
    """
    ancestor: Path = path.parent
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if not ancestor.is_dir():
        return ""
    names: List[str] = sorted(entry.name for entry in ancestor.iterdir())[:15]
    return f" En yakın mevcut dizin: {ancestor} → içerik: {', '.join(names) or '(boş)'}. Yolu hedef metninden harf harf kontrol et."


def clean_html(html: str) -> str:
    """HTML içeriğinden script/stil/gezinme öğelerini atıp anlamlı metni döner. Saf fonksiyon."""
    soup: BeautifulSoup = BeautifulSoup(html, 'html.parser')
    for element in soup(["script", "style", "meta", "noscript", "header", "footer", "nav"]):
        element.decompose()
    text: str = soup.get_text(separator=' ')
    lines: List[str] = [line.strip() for line in text.splitlines() if line.strip()]
    return _clip(" ".join(lines), PAGE_TEXT_LIMIT)


# --- Ekran geometrisi: tek ortak koordinat uzayı ---

class ScreenGeometry(TypedDict):
    """Ana ekranın nokta (pyautogui/Quartz) boyutu ve modelin gördüğü ortak koordinat uzayı."""
    point_width: int
    point_height: int
    model_width: int
    model_height: int


def model_space_size(point_width: int, point_height: int, max_edge: int) -> Tuple[int, int]:
    """Nokta çözünürlüğünü uzun kenarı en çok max_edge olacak şekilde küçültür (büyütmez). Saf."""
    scale: float = min(1.0, max_edge / max(point_width, point_height))
    return (round(point_width * scale), round(point_height * scale))


def model_to_points(x: float, y: float, geometry: ScreenGeometry) -> Tuple[int, int]:
    """Model uzayındaki koordinatı pyautogui/Quartz nokta koordinatına çevirir. Saf."""
    return (
        round(x * geometry["point_width"] / geometry["model_width"]),
        round(y * geometry["point_height"] / geometry["model_height"]),
    )


def points_to_model(x: float, y: float, geometry: ScreenGeometry) -> Tuple[int, int]:
    """Nokta koordinatını modelin gördüğü ortak uzaya çevirir. Saf."""
    return (
        round(x * geometry["model_width"] / geometry["point_width"]),
        round(y * geometry["model_height"] / geometry["point_height"]),
    )


def current_geometry() -> ScreenGeometry:
    """Ana ekranın güncel geometrisini okur (harici monitör takılıp çıkarılabilir); model uzayı karedir."""
    point_width, point_height = pyautogui.size()
    return {
        "point_width": point_width, "point_height": point_height,
        "model_width": MODEL_SCREEN_SIZE, "model_height": MODEL_SCREEN_SIZE,
    }


def parse_point(value: object) -> Tuple[int, int]:
    """
    Modelin verdiği [x, y] noktasını doğrular. Ayrı x/y tamsayı alanlarında model yerel
    biçimi olan [x, y]'yi x alanına yazıyordu (ölçümde 10 çağrının 7'si); tek point alanıyla
    0/10. Saf fonksiyon.
    """
    if (isinstance(value, (list, tuple)) and len(value) == 2
            and all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value)):
        return round(value[0]), round(value[1])
    raise ToolError(f"point [x, y] biçiminde iki sayı olmalı; alınan: {value!r}", "INVALID_POINT", False)


def _require_screen_capture() -> None:
    """
    Ekran kaydı izni yoksa macOS yalnızca duvar kâğıdını döndürür; bu sessiz bozulma yerine
    açık hata verilir. Denetim ~7 ms sürdüğü için yoklama döngülerinin dışında yapılır.
    """
    if not Quartz.CGPreflightScreenCaptureAccess():
        raise ToolError(
            "Ekran kaydı izni yok: Sistem Ayarları > Gizlilik ve Güvenlik > Ekran ve Sistem Sesi "
            "Kaydı bölümünde bu uygulamaya (Terminal/Python) izin verin.",
            "SCREEN_CAPTURE_PERMISSION", False,
        )


def _main_display_image(resolution: int) -> object:
    """Ana ekranın anlık CGImage görüntüsü; resolution Quartz.kCGWindowImage* seçeneğidir."""
    image: object = Quartz.CGWindowListCreateImage(
        Quartz.CGDisplayBounds(Quartz.CGMainDisplayID()), Quartz.kCGWindowListOptionOnScreenOnly,
        Quartz.kCGNullWindowID, resolution,
    )
    if image is None:
        raise ToolError("Ekran görüntüsü alınamadı: CGWindowListCreateImage boş döndü.", "SCREEN_CAPTURE_FAILED", True)
    return image


def _draw_scaled(image: object, width: int, height: int, color_space: object, channels: int, bitmap_info: int) -> bytes:
    """CGImage'ı CoreGraphics ile width×height boyutuna ölçekleyip 8 bitlik piksel baytlarına çizer."""
    buffer: bytearray = bytearray(width * height * channels)
    context: object = Quartz.CGBitmapContextCreate(buffer, width, height, 8, width * channels, color_space, bitmap_info)
    if context is None:
        raise ToolError(f"Ekran karesi için çizim bağlamı kurulamadı ({width}×{height}, {channels} kanal).",
                        "SCREEN_CAPTURE_FAILED", True)
    Quartz.CGContextSetInterpolationQuality(context, Quartz.kCGInterpolationHigh)
    Quartz.CGContextDrawImage(context, Quartz.CGRectMake(0, 0, width, height), image)
    return bytes(buffer)


def grab_model_frame(geometry: ScreenGeometry) -> Image.Image:
    """
    Ana ekranı yakalar (Retina'da fiziksel piksel, örn. 3420×2224) ve CoreGraphics ile kare
    model uzayına sRGB olarak ölçekler. screencapture alt süreci ve geçici PNG yoluna göre
    ~5 kat hızlıdır (≈55 ms).
    """
    _require_screen_capture()
    width, height = geometry["model_width"], geometry["model_height"]
    pixels: bytes = _draw_scaled(
        _main_display_image(Quartz.kCGWindowImageDefault), width, height,
        Quartz.CGColorSpaceCreateWithName(Quartz.kCGColorSpaceSRGB), 4, Quartz.kCGImageAlphaNoneSkipLast,
    )
    return Image.frombuffer("RGBX", (width, height), pixels, "raw", "RGBX", 0, 1).convert("RGB")


def settle_frame() -> np.ndarray:
    """Durulma karşılaştırması için ekranın küçük gri tonlu karesi; izin denetimi çağırandadır."""
    image: object = _main_display_image(Quartz.kCGWindowImageNominalResolution)
    width, height = model_space_size(Quartz.CGImageGetWidth(image), Quartz.CGImageGetHeight(image), SETTLE_FRAME_EDGE)
    pixels: bytes = _draw_scaled(image, width, height, Quartz.CGColorSpaceCreateDeviceGray(), 1, Quartz.kCGImageAlphaNone)
    return np.frombuffer(pixels, dtype=np.uint8).reshape(height, width)


def frame_change_ratio(previous: np.ndarray, current: np.ndarray, pixel_delta: int) -> float:
    """İki gri karede farkı pixel_delta'yı aşan piksellerin oranı; boyut değiştiyse tamamı değişmiştir. Saf."""
    if previous.shape != current.shape:
        return 1.0
    return float(np.mean(np.abs(current.astype(np.int16) - previous.astype(np.int16)) > pixel_delta))


def _raise_if_stopped() -> None:
    """Kullanıcı görevi durdurduysa bekleyen aracı hemen keser."""
    runtime: Optional[ToolRuntime] = TOOL_RUNTIME.get()
    if runtime is not None and runtime["should_stop"]():
        raise ToolError("Kullanıcı tarafından durduruldu.", "STOPPED", False)


def wait_for_screen_settle(baseline: np.ndarray, input_at: float) -> float:
    """
    Ekran girdisinden sonra uygulamanın tepkisini ve ekranın durulmasını bekler; beklenen
    süreyi döner. Önce girdi öncesi kareye (baseline) göre görünür bir tepki aranır
    (gecikmeli sonuçlar için SETTLE_REACTION_SECONDS'a kadar), tepki görülünce ekran
    SETTLE_QUIET_SECONDS boyunca değişmeyene kadar beklenir. Sürekli animasyonda
    input_at + SETTLE_MAX_SECONDS'ta bırakılır. Sabit uyku yerine gerçek tepki beklenir.
    """
    started: float = time.monotonic()
    # Karşılaştırma son değişimin görüldüğü kareye göredir (önceki kareye göre değil): yavaş
    # animasyonlar birikerek eşiği aşar; ardışık kareler arası küçük fark onları gizlemez.
    reference: np.ndarray = baseline
    reacted: bool = False
    last_change: float = started
    while True:
        _raise_if_stopped()
        frame: np.ndarray = settle_frame()
        now: float = time.monotonic()
        if frame_change_ratio(reference, frame, SETTLE_PIXEL_DELTA) > SETTLE_CHANGED_RATIO:
            reacted, last_change, reference = True, now, frame
        quiet: bool = reacted and now - last_change >= SETTLE_QUIET_SECONDS
        no_reaction: bool = not reacted and now >= input_at + SETTLE_REACTION_SECONDS
        if quiet or no_reaction or now >= input_at + SETTLE_MAX_SECONDS:
            return time.monotonic() - started
        time.sleep(SETTLE_POLL_SECONDS)


def _require_accessibility() -> None:
    """
    Sentetik fare/klavye olayları ve AX okuma erişilebilirlik izni ister. İzin yoksa
    macOS olayları SESSİZCE düşürür; araç 'başarılı' deyip hiçbir şey yapmasın diye
    açık hata verilir.
    """
    if not AX.AXIsProcessTrusted():
        raise ToolError(
            "Erişilebilirlik izni yok: Sistem Ayarları > Gizlilik ve Güvenlik > Erişilebilirlik "
            "bölümünde bu uygulamaya (Terminal/Python) izin verin.",
            "AX_PERMISSION", False,
        )


def _check_in_model_space(x: int, y: int, geometry: ScreenGeometry) -> None:
    """Koordinatın ortak uzayda olduğunu doğrular (ekran dışı tıklama FAILSAFE'i tetiklemesin)."""
    if not (0 <= x < geometry["model_width"] and 0 <= y < geometry["model_height"]):
        raise ToolError(
            f"Koordinat ekran dışında: ({x}, {y}). Geçerli aralık x 0-{geometry['model_width'] - 1}, "
            f"y 0-{geometry['model_height'] - 1} (ekran görüntüsü/AX listesi uzayı).",
            "COORDINATE_OUT_OF_RANGE", True,
        )


def click_model_point(x: int, y: int, button: str, geometry: ScreenGeometry) -> str:
    """Ortak uzaydaki noktaya fare tıklaması yapar."""
    if button not in ("left", "right", "middle"):
        raise ToolError(f"Geçersiz fare tuşu: {button} (left/right/middle).", "INVALID_BUTTON", False)
    _check_in_model_space(x, y, geometry)
    point_x, point_y = model_to_points(x, y, geometry)
    pyautogui.click(point_x, point_y, button=button)
    return f"({x}, {y}) konumuna {button} tıklandı."


def move_model_point(x: int, y: int, geometry: ScreenGeometry) -> str:
    """Fareyi ortak uzaydaki noktaya taşır."""
    _check_in_model_space(x, y, geometry)
    point_x, point_y = model_to_points(x, y, geometry)
    pyautogui.moveTo(point_x, point_y)
    return f"Fare ({x}, {y}) konumuna taşındı."


# --- Klavye: düzenden bağımsız Unicode yazım ve tuş kombinasyonları ---

_KEY_ALIASES: Dict[str, str] = {
    "cmd": "command", "⌘": "command", "control": "ctrl", "opt": "option", "⌥": "option",
    "arrowup": "up", "arrowdown": "down", "arrowleft": "left", "arrowright": "right",
}


def unicode_chunks(text: str, max_units: int) -> List[str]:
    """Metni UTF-16 birim sayısı max_units'i aşmayan parçalara böler; vekil çiftler bölünmez. Saf."""
    chunks: List[str] = []
    current: str = ""
    current_units: int = 0
    for char in text:
        char_units: int = 2 if ord(char) > 0xFFFF else 1
        if current and current_units + char_units > max_units:
            chunks.append(current)
            current, current_units = "", 0
        current += char
        current_units += char_units
    if current:
        chunks.append(current)
    return chunks


def _post_unicode_chunk(chunk: str) -> None:
    """Bir Unicode parçasını tek tuş-bas/bırak olay çifti olarak gönderir (tuş kodu yok sayılır)."""
    units: int = len(chunk.encode("utf-16-le")) // 2
    for key_down in (True, False):
        event = Quartz.CGEventCreateKeyboardEvent(None, 0, key_down)
        # Takılı kalmış bir değiştirici (cmd vb.) yazımı kısayola çevirmesin
        Quartz.CGEventSetFlags(event, 0)
        Quartz.CGEventKeyboardSetUnicodeString(event, units, chunk)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def type_unicode_text(text: str) -> None:
    """
    Metni klavye düzeninden bağımsız yazar. pyautogui.write ABD tuş kodlarıyla bastığı
    için Fince/Türkçe düzende noktalamayı bozuyor, eşlemesinde olmayan karakterleri
    (ç ğ ı ö ş ü) SESSİZCE atlıyor ve karakter başına ~20ms harcıyordu. Satır sonları
    gerçek Enter tuşu olarak gönderilir.
    """
    for line_index, line in enumerate(text.replace("\r\n", "\n").split("\n")):
        if line_index > 0:
            pyautogui.press("enter")
        for chunk in unicode_chunks(line, UNICODE_CHUNK_UNITS):
            _post_unicode_chunk(chunk)
            time.sleep(UNICODE_CHUNK_DELAY_SECONDS)


def _is_known_key(name: str) -> bool:
    """Tuş adının bu platformun tuş eşlemesinde gerçekten karşılığı var mı."""
    mapping: Dict[str, Optional[int]] = pyautogui.platformModule.keyboardMapping
    return name in mapping and mapping[name] is not None


def press_key_spec(spec: str) -> str:
    """
    Tek tuşu ya da 'cmd+shift+t' gibi kombinasyonu basar. Tek karakterlik tuşlar
    (/, @, ö…) düzenden bağımsız Unicode olarak yazılır. Bilinmeyen tuş adı açık hata
    verir; pyautogui bilinmeyen tuşları sessizce yok sayıp 'basıldı' dedirtiyordu.
    """
    if len(spec) == 1:
        type_unicode_text(spec)
        return f"Tuş yazıldı: {spec}"
    names: List[str] = [_KEY_ALIASES.get(part.strip().lower(), part.strip().lower()) for part in spec.split("+")]
    unknown: List[str] = [name for name in names if not _is_known_key(name)]
    if unknown:
        raise ToolError(
            f"Bilinmeyen tuş: {unknown} (istenen: {spec!r}). Örnekler: enter, tab, escape, space, "
            "backspace, delete, up, down, pageup, f5, cmd+c, cmd+shift+t.",
            "INVALID_KEY", False,
        )
    if len(names) == 1:
        pyautogui.press(names[0])
    else:
        pyautogui.hotkey(*names)
    return f"Tuşa basıldı: {spec}"


class ActionStep(TypedDict, total=False):
    """run_action_sequence adımı; kullanılan alanlar eylem türüne göre değişir."""
    action: str
    point: List[int]
    button: str
    text: str
    key: str
    seconds: float


def _run_action_step(step: ActionStep, geometry: ScreenGeometry) -> str:
    """Tek bir fare/klavye adımını çalıştırır; eksik/yanlış alan KeyError/TypeError/ValueError verir."""
    action: object = step.get("action")
    if action == "click":
        x, y = parse_point(step["point"])
        return click_model_point(x, y, str(step.get("button") or "left"), geometry)
    if action == "move":
        x, y = parse_point(step["point"])
        return move_model_point(x, y, geometry)
    if action == "type":
        text: str = str(step["text"])
        type_unicode_text(text)
        return f"Yazıldı ({len(text)} karakter): {_clip(text, TYPED_TEXT_ECHO_LIMIT)}"
    if action == "press":
        return press_key_spec(str(step["key"]))
    if action == "wait":
        seconds: float = float(step["seconds"])
        if not 0 < seconds <= MAX_WAIT_SECONDS:
            raise ToolError(f"Bekleme süresi 0-{MAX_WAIT_SECONDS}sn aralığında olmalı: {seconds}", "INVALID_WAIT", False)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            _raise_if_stopped()
            time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
        return f"{seconds}sn beklendi."
    raise ToolError(f"Bilinmeyen eylem türü: {action} (click/move/type/press/wait).", "INVALID_ACTION", False)


# --- Erişilebilirlik (AX) ağacı ---

# Listeye giren etkileşimli roller
_AX_ACTIONABLE_ROLES: frozenset[str] = frozenset({
    "AXButton", "AXCheckBox", "AXRadioButton", "AXPopUpButton", "AXMenuButton", "AXComboBox",
    "AXTextField", "AXTextArea", "AXLink", "AXMenuItem", "AXDisclosureTriangle", "AXSlider",
    "AXIncrementor", "AXRow",
})
_AX_TEXT_INPUT_ROLES: frozenset[str] = frozenset({"AXTextField", "AXTextArea", "AXComboBox"})
_AX_SCAN_ATTRIBUTES: List[str] = [
    "AXRole", "AXChildren", "AXTitle", "AXDescription", "AXValue",
    "AXPlaceholderValue", "AXPosition", "AXSize", "AXEnabled",
]


class AXElement(TypedDict):
    """Listelenen bir AX öğesi; merkez nokta (pyautogui/Quartz) koordinatındadır."""
    role: str
    label: str
    value: str
    enabled: bool
    center_x: float
    center_y: float


def _ax_present(value: object) -> object:
    """CopyMultipleAttributeValues eksik öznitelikleri AXError tipli AXValue olarak döner; onları None yapar."""
    if isinstance(value, AX.AXValueRef) and AX.AXValueGetType(value) == AX.kAXValueAXErrorType:
        return None
    return value


def _ax_point(value: object) -> Optional[Tuple[float, float]]:
    """AXPosition değerini (x, y) nokta çiftine çevirir."""
    if not isinstance(value, AX.AXValueRef):
        return None
    ok, point = AX.AXValueGetValue(value, AX.kAXValueCGPointType, None)
    return (float(point.x), float(point.y)) if ok else None


def _ax_size(value: object) -> Optional[Tuple[float, float]]:
    """AXSize değerini (genişlik, yükseklik) çiftine çevirir."""
    if not isinstance(value, AX.AXValueRef):
        return None
    ok, size = AX.AXValueGetValue(value, AX.kAXValueCGSizeType, None)
    return (float(size.width), float(size.height)) if ok else None


def _ax_attribute(element: object, attribute: str) -> object:
    """Tek bir AX özniteliğini okur; öğede yoksa None. Yanıtsız uygulama açık hata verir."""
    error, value = AX.AXUIElementCopyAttributeValue(element, attribute, None)
    if error == AX.kAXErrorSuccess:
        return value
    if error == AX.kAXErrorCannotComplete:
        raise ToolError("Uygulama erişilebilirlik sorgusuna yanıt vermiyor (meşgul olabilir).", "AX_TIMEOUT", True)
    return None


def _ax_short_text(value: object) -> str:
    """AX metin değerini tek satırlık kısa metne çevirir (metin olmayanlar boş)."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:60]


def _ax_descendant_text(element: object) -> str:
    """Etiketsiz öğeler (örn. liste satırları) için ilk alt metni sınırlı genişlikte arar."""
    queue: List[object] = [element]
    visited: int = 0
    while queue and visited < AX_LABEL_SEARCH_NODES:
        node: object = queue.pop(0)
        visited += 1
        children: object = _ax_attribute(node, "AXChildren")
        for child in list(children) if children else []:
            if _ax_attribute(child, "AXRole") == "AXStaticText":
                text: str = _ax_short_text(_ax_attribute(child, "AXValue"))
                if text:
                    return text
            queue.append(child)
    return ""


def scan_ax_elements(root: object) -> Tuple[List[AXElement], List[object], bool]:
    """
    Pencere ağacını ekran sırasıyla (derinlik öncelikli) tarar; etkileşimli öğeleri ve
    AX referanslarını döner. Üçüncü değer, öğe/düğüm/süre limiti yüzünden taramanın
    kısaltıldığını bildirir (sessiz kırpma yok).
    """
    started: float = time.monotonic()
    stack: List[object] = [root]
    elements: List[AXElement] = []
    refs: List[object] = []
    visited: int = 0
    while stack:
        if (len(elements) >= AX_ELEMENT_LIMIT or visited >= AX_NODE_LIMIT
                or time.monotonic() - started > AX_SCAN_BUDGET_SECONDS):
            return elements, refs, True
        node: object = stack.pop()
        visited += 1
        error, values = AX.AXUIElementCopyMultipleAttributeValues(node, _AX_SCAN_ATTRIBUTES, 0, None)
        if error == AX.kAXErrorInvalidUIElement:
            # Tarama sırasında kaybolan öğe (dinamik arayüz): listede yeri yok
            continue
        if error == AX.kAXErrorCannotComplete:
            raise ToolError("Uygulama erişilebilirlik sorgusuna yanıt vermiyor (meşgul olabilir).", "AX_TIMEOUT", True)
        if error != AX.kAXErrorSuccess:
            raise ToolError(f"AX öznitelikleri okunamadı: hata kodu={error}", "AX_READ_FAILED", True)
        attrs: Dict[str, object] = {name: _ax_present(value) for name, value in zip(_AX_SCAN_ATTRIBUTES, values, strict=True)}
        children: object = attrs["AXChildren"]
        if children:
            stack.extend(reversed(list(children)))
        role: str = str(attrs["AXRole"] or "")
        if role not in _AX_ACTIONABLE_ROLES:
            continue
        position: Optional[Tuple[float, float]] = _ax_point(attrs["AXPosition"])
        size: Optional[Tuple[float, float]] = _ax_size(attrs["AXSize"])
        if position is None or size is None or size[0] <= 0 or size[1] <= 0:
            continue
        label: str = next(
            (text for text in (_ax_short_text(attrs[key]) for key in ("AXTitle", "AXDescription", "AXPlaceholderValue")) if text),
            "",
        )
        value: str = _ax_short_text(attrs["AXValue"]) if role in _AX_TEXT_INPUT_ROLES else ""
        if not label and not value:
            label = _ax_descendant_text(node)
        elements.append({
            "role": role, "label": label, "value": value,
            "enabled": attrs["AXEnabled"] is not False,
            "center_x": position[0] + size[0] / 2, "center_y": position[1] + size[1] / 2,
        })
        refs.append(node)
    return elements, refs, False


def format_ax_listing(
    app_name: str, window_title: str, elements: List[AXElement], geometry: ScreenGeometry, truncated: bool,
) -> str:
    """AX öğelerini modele gidecek kompakt listeye çevirir (koordinatlar ortak uzayda). Saf."""
    lines: List[str] = [
        f"{app_name} · pencere {window_title!r} · {len(elements)} öğe "
        f"(koordinatlar ekran görüntüsü/tıklama uzayında, {geometry['model_width']}×{geometry['model_height']})"
    ]
    for index, element in enumerate(elements, start=1):
        x, y = points_to_model(element["center_x"], element["center_y"], geometry)
        line: str = f"[{index}] {element['role'].removeprefix('AX')} {element['label']!r}"
        if element["value"]:
            line += f" değer={element['value']!r}"
        if not element["enabled"]:
            line += " (pasif)"
        lines.append(f"{line} @({x},{y})")
    if truncated:
        lines.append("…liste öğe/süre limitiyle kısaltıldı; aranan öğe yoksa take_screenshot kullan.")
    return "\n".join(lines)


def _visible_app_owners() -> List[Tuple[str, int]]:
    """Normal pencereleri olan uygulamalar (ad, pid), önden arkaya; pencere sunucusundan her zaman günceldir."""
    windows: object = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionAll | Quartz.kCGWindowListExcludeDesktopElements, Quartz.kCGNullWindowID,
    )
    owners: List[Tuple[str, int]] = []
    for window in list(windows) if windows else []:
        if int(window.get(Quartz.kCGWindowLayer) or 0) != 0:
            continue
        owner: Tuple[str, int] = (str(window.get(Quartz.kCGWindowOwnerName) or ""), int(window[Quartz.kCGWindowOwnerPID]))
        if owner not in owners:
            owners.append(owner)
    return owners


def _bundle_name(pid: int) -> str:
    """Uygulamanın yerelleştirilmemiş paket adı (örn. 'Notlar' için 'Notes')."""
    running: object = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if running is None or running.bundleURL() is None:
        return ""
    return str(running.bundleURL().lastPathComponent()).removesuffix(".app")


def _app_pid(app_name: str) -> int:
    """
    Uygulamanın PID'ini pencere sunucusundan bulur (yerel ad veya paket adıyla).
    NSWorkspace listesi ana run loop dönmeyen süreçlerde (CLI) güncellenmediği için kullanılmaz.
    """
    wanted: str = app_name.casefold()
    owners: List[Tuple[str, int]] = _visible_app_owners()
    for name, pid in owners:
        if name.casefold() == wanted:
            return pid
    for _name, pid in owners:
        if _bundle_name(pid).casefold() == wanted:
            return pid
    available: str = ", ".join(sorted({name for name, _ in owners if name})[:20])
    raise ToolError(
        f"Penceresi olan çalışan uygulama bulunamadı: {app_name}. Açık uygulamalar: {available}. "
        "Kapalıysa önce cua_get_app ile başlat.",
        "APP_NOT_RUNNING", True,
    )


# Bilgisayar Kullanım Ajansı (CUA) - macOS GUI etkileşimleri
class CUA:
    """
    macOS GUI konnektörü: uygulama etkinleştirme ve erişilebilirlik (AX) ağacı
    üzerinden öğe listeleme/tıklama. Öğe numaraları son listeye göredir.
    """
    def __init__(self) -> None:
        # Uygulama adı (casefold) -> son listedeki AX referansları (öğe numarası = indeks + 1)
        self._snapshots: Dict[str, List[object]] = {}

    def get_app(self, app_name: str) -> str:
        """Uygulamayı osascript ile başlatır/öne getirir."""
        script: str = f'tell application {json.dumps(app_name)} to activate'
        result: subprocess.CompletedProcess[str] = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=8,
        )
        if result.returncode == 0:
            return f"{app_name} aktif edildi ve öne getirildi."
        raise ToolError(
            f"Uygulama bulunamadı veya aktif edilemedi: {app_name}, ayrıntı={result.stderr.strip()}",
            "APP_NOT_FOUND", False,
        )

    def _front_window(self, app_name: str) -> object:
        """Uygulamanın odaktaki (yoksa ana, yoksa ilk) penceresinin AX öğesini döner."""
        _require_accessibility()
        application: object = AX.AXUIElementCreateApplication(_app_pid(app_name))
        AX.AXUIElementSetMessagingTimeout(application, AX_MESSAGING_TIMEOUT_SECONDS)
        for attribute in ("AXFocusedWindow", "AXMainWindow"):
            window: object = _ax_attribute(application, attribute)
            if window is not None:
                return window
        windows: object = _ax_attribute(application, "AXWindows")
        if windows:
            return list(windows)[0]
        raise ToolError(
            f"{app_name} uygulamasının bu masaüstünde erişilebilir penceresi yok (başka bir alanda "
            "veya küçültülmüş olabilir); önce cua_get_app ile öne getir.",
            "AX_NO_WINDOW", True,
        )

    def list_elements(self, app_name: str) -> str:
        """Öndeki pencerenin etkileşimli öğelerini numaralı listeler ve referanslarını saklar."""
        window: object = self._front_window(app_name)
        elements, refs, truncated = scan_ax_elements(window)
        self._snapshots[app_name.casefold()] = refs
        title: str = _ax_short_text(_ax_attribute(window, "AXTitle"))
        return format_ax_listing(app_name, title, elements, current_geometry(), truncated)

    def _element_center(self, element: object) -> Tuple[float, float]:
        """Öğenin merkezini nokta koordinatında döner."""
        position: Optional[Tuple[float, float]] = _ax_point(_ax_attribute(element, "AXPosition"))
        size: Optional[Tuple[float, float]] = _ax_size(_ax_attribute(element, "AXSize"))
        if position is None or size is None:
            raise ToolError("Öğenin konumu okunamadı; cua_get_ax_state ile listeyi yenile.", "AX_STALE", True)
        return (position[0] + size[0] / 2, position[1] + size[1] / 2)

    def click_element(self, app_name: str, element_id: int) -> str:
        """
        Son listedeki öğeye tıklar: metin alanları AXFocused ile odaklanır, diğerleri
        AXPress alır. AX eylemi desteklenmiyorsa hibrit protokolün koordinat katmanı
        olarak öğe merkezine gerçek fare tıklaması yapılır (sonuçta açıkça belirtilir).
        """
        _require_accessibility()
        refs: Optional[List[object]] = self._snapshots.get(app_name.casefold())
        if refs is None:
            raise ToolError(f"{app_name} için öğe listesi yok; önce cua_get_ax_state çağır.", "AX_NO_SNAPSHOT", True)
        if not 1 <= element_id <= len(refs):
            raise ToolError(f"Geçersiz öğe numarası: {element_id} (geçerli 1-{len(refs)}).", "INVALID_ELEMENT", True)
        element: object = refs[element_id - 1]
        role: object = _ax_attribute(element, "AXRole")
        if role in _AX_TEXT_INPUT_ROLES:
            error: int = AX.AXUIElementSetAttributeValue(element, "AXFocused", True)
            if error == AX.kAXErrorSuccess:
                return f"[{element_id}] metin alanı odaklandı; run_action_sequence 'type' ile yazabilirsin."
        else:
            error = AX.AXUIElementPerformAction(element, "AXPress")
            if error == AX.kAXErrorSuccess:
                return f"[{element_id}] öğesine AXPress ile tıklandı."
        if error == AX.kAXErrorInvalidUIElement:
            raise ToolError("Öğe artık geçerli değil (arayüz değişti); cua_get_ax_state ile listeyi yenile.", "AX_STALE", True)
        center_x, center_y = self._element_center(element)
        pyautogui.click(round(center_x), round(center_y))
        return f"[{element_id}] öğesi AX eylemini desteklemediği için (hata kodu={error}) merkezine fare ile tıklandı."

    def window_bounds(self, app_name: str) -> Tuple[float, float, float, float]:
        """Öndeki pencerenin (x, y, genişlik, yükseklik) sınırlarını nokta koordinatında döner."""
        window: object = self._front_window(app_name)
        position: Optional[Tuple[float, float]] = _ax_point(_ax_attribute(window, "AXPosition"))
        size: Optional[Tuple[float, float]] = _ax_size(_ax_attribute(window, "AXSize"))
        if position is None or size is None:
            raise ToolError(f"Pencere sınırları okunamadı: {app_name}", "WINDOW_BOUNDS_FAILED", True)
        return (position[0], position[1], size[0], size[1])


class BrowserAction(TypedDict, total=False):
    """browse_url eylemi: click (selector), fill (selector + value), press (selector + tuş adı value)."""
    action: str
    selector: str
    value: Optional[str]


# Sayfadaki görünür etkileşimli öğeleri Playwright seçicileriyle listeler (model seçici tahmin etmesin).
_PAGE_ELEMENTS_SCRIPT: str = """
(limit) => {
  const out = [];
  const q = (s) => s.replace(/\\\\/g, '\\\\\\\\').replace(/"/g, '\\\\"');
  const nodes = document.querySelectorAll(
    'a[href], button, input:not([type=hidden]), textarea, select, [role=button], [role=link]');
  for (const el of nodes) {
    if (out.length >= limit) break;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    const tag = el.tagName.toLowerCase();
    const label = (el.getAttribute('aria-label') || el.innerText || el.value ||
      el.getAttribute('placeholder') || el.getAttribute('title') || '').trim().replace(/\\s+/g, ' ').slice(0, 60);
    let sel = null;
    if (el.id && /^[A-Za-z][\\w-]*$/.test(el.id)) sel = '#' + el.id;
    else if (el.getAttribute('name')) sel = `${tag}[name="${q(el.getAttribute('name'))}"]`;
    else if (el.getAttribute('aria-label')) sel = `${tag}[aria-label="${q(el.getAttribute('aria-label'))}"]`;
    else if (el.getAttribute('placeholder')) sel = `${tag}[placeholder="${q(el.getAttribute('placeholder'))}"]`;
    else if (label) sel = `${tag}:has-text("${q(label.slice(0, 40))}")`;
    else continue;
    const kind = tag === 'input' ? `input[${el.type}]` : tag;
    out.push(`${sel} — ${kind}${label ? ' "' + label + '"' : ''}`);
  }
  return out;
}
"""


class PendingInput(TypedDict):
    """Henüz gözlenmemiş son ekran girdisi: bitiş anı ve girdiden hemen önceki karşılaştırma karesi."""
    at: float
    baseline: np.ndarray


_P = ParamSpec("_P")


def _screen_input(method: Callable[Concatenate["Toolbox", _P], str]) -> Callable[Concatenate["Toolbox", _P], str]:
    """
    Ekrana girdi gönderen araç yöntemini sarar: girdiden hemen önce küçük bir karşılaştırma
    karesi alır, bitişte (hata olsa bile) bekleyen girdiyi kaydeder. Sonraki ekran görüntüsü
    bu kareye göre uygulamanın tepki verip durulmasını bekler. Ekran kaydı izni yoksa kare
    alınmaz; gözlem zaten izin hatasıyla durur.
    """
    @wraps(method)
    def recorded(self: "Toolbox", *args: _P.args, **kwargs: _P.kwargs) -> str:
        baseline: Optional[np.ndarray] = settle_frame() if Quartz.CGPreflightScreenCaptureAccess() else None
        try:
            return method(self, *args, **kwargs)
        finally:
            if baseline is not None:
                self._pending_input = {"at": time.monotonic(), "baseline": baseline}
    return recorded


# Araç Kutusu (Toolbox) - Sistem ve Web araçları
class Toolbox:
    """
    Sistem komutları, web tarayıcı ve GUI araçlarını içeren konnektör. Modelin
    çağırabileceği yöntemler main.build_tool_schemas içindeki adlarla sınırlıdır.
    """
    def __init__(self, memory_file: Optional[str] = None, allow_memory_mutation: bool = False) -> None:
        self.playwright_instance: Optional[Playwright] = None
        self.browser: Optional[Browser] = None
        self.browser_context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.cua: CUA = CUA()
        self._browser_lock: asyncio.Lock = asyncio.Lock()
        self._pending_input: Optional[PendingInput] = None
        self._memory_file: Optional[str] = memory_file
        self._allow_memory_mutation: bool = allow_memory_mutation

    def user_memory(
        self, action: str, key: Optional[str] = None, value: Optional[str] = None,
        query: Optional[str] = None, category: Optional[str] = None,
    ) -> str:
        """Kullanıcı tercihini yazar, arar veya siler; bellek kapsamı görev dışına çıkmaz."""
        if not self._memory_file:
            raise ToolError("Bu görev için kalıcı kullanıcı hafızası etkin değil.", "MEMORY_UNAVAILABLE", False)
        normalized_action: str = action.strip().casefold()
        if normalized_action in {"remember", "forget"} and not self._allow_memory_mutation:
            raise ToolError(
                "Bu görev kalıcı hafıza değiştirme yetkisiyle başlatılmadı.",
                "MEMORY_MUTATION_NOT_ALLOWED", False,
            )
        try:
            state: memory.MemoryState = memory.load_memory(self._memory_file)
            if normalized_action == "remember":
                if not key or value is None:
                    raise ValueError("remember için key ve value zorunludur.")
                updated: memory.MemoryState = memory.remember_preference(
                    state, key, value, category or "preference", memory.utc_timestamp(),
                )
                memory.save_memory(self._memory_file, updated)
                return json.dumps({"ok": True, "action": normalized_action,
                                   "record": updated["preferences"][-1]}, ensure_ascii=False)
            if normalized_action == "recall":
                records: List[memory.PreferenceRecord] = memory.search_preferences(state, query or "")
                return json.dumps({"ok": True, "preferences": records}, ensure_ascii=False)
            if normalized_action == "forget":
                if not key:
                    raise ValueError("forget için key zorunludur.")
                updated, removed = memory.forget_preference(state, key)
                memory.save_memory(self._memory_file, updated)
                return json.dumps({"ok": True, "action": normalized_action, "removed": removed}, ensure_ascii=False)
            raise ValueError("action remember, recall veya forget olmalı.")
        except ValueError as error:
            raise ToolError(f"Kullanıcı hafızası işlemi reddedildi: {error}", "MEMORY_INVALID", False) from error
        except OSError as error:
            raise ToolError(f"Kullanıcı hafızasına erişilemedi: {error}", "MEMORY_IO", True) from error

    async def _get_page(self) -> Page:
        """
        Görev boyunca KALICI tek sekmeyi döner (gerekirse tarayıcıyı başlatır). Sekme
        çağrılar arasında korunur: 'yaz → tıkla → oku' akışlarında durum kaybolmaz.
        """
        async with self._browser_lock:
            if self.browser_context is None:
                self.playwright_instance = await async_playwright().start()
                self.browser = await self.playwright_instance.chromium.launch(headless=True)
                self.browser_context = await self.browser.new_context(
                    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
            if self.page is None or self.page.is_closed():
                self.page = await self.browser_context.new_page()
                self.page.set_default_timeout(PAGE_ACTION_TIMEOUT_MS)
                self.page.set_default_navigation_timeout(PAGE_LOAD_TIMEOUT_MS)
            return self.page

    def execute_shell(self, command: str, use_sudo: bool) -> str:
        """
        Sistem kabuğunda komut çalıştırır.
        """
        if not command.strip():
            raise ToolError("Boş kabuk komutu çalıştırılamaz.", "EMPTY_COMMAND", False)
        if _is_catastrophic_command(command):
            raise ToolError(
                f"Bilinen yıkıcı komut kalıbıyla eşleşti, çalıştırma engellendi: {command}",
                "CATASTROPHIC_COMMAND_BLOCKED", False,
            )
        if _shell_writes_to_sensitive_path(command):
            if not _sensitive_write_allowed():
                raise ToolError(
                    f"Komut korunan bir sistem/kimlik yoluna yönlendirme yapıyor, engellendi: {command}. "
                    "Kullanıcı bilerek izin vermek isterse OMNI_ALLOW_SENSITIVE_WRITE=1 ile çalıştırmalı.",
                    "SENSITIVE_PATH_BLOCKED", False,
                )
            logging.warning("Hassas yola kabuk yönlendirmesine kullanıcı bayrağıyla izin verildi", extra={"command": command})
        if use_sudo:
            logging.warning("Sudo ile kabuk komutu çalıştırılıyor", extra={"command": command})
        full_cmd: Union[str, List[str]] = (
            ["sudo", "-n", "/bin/sh", "-c", command] if use_sudo else command
        )
        try:
            returncode, stdout, stderr = run_streaming_process(full_cmd, not use_sudo, SHELL_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as error:
            raise ToolError(f"Kabuk komutu {SHELL_TIMEOUT_SECONDS:.0f} saniyede tamamlanmadı.", "SHELL_TIMEOUT", True) from error
        if returncode != 0:
            # Çoğu araç (npm, git, python, brew) asıl hatayı STDOUT'a basar; model
            # komutu sırf okumak için tekrar çalıştırmasın diye ikisi de taşınır.
            raise ToolError(
                f"Kabuk komutu başarısız: çıkış={returncode}, "
                f"stdout={_clip(stdout, 1000)}, stderr={_clip(stderr, 1000)}",
                "SHELL_EXIT", True,
            )
        return f"STDOUT: {_clip(stdout, SHELL_STDOUT_LIMIT)}\nSTDERR: {_clip(stderr, SHELL_STDERR_LIMIT)}\nÇıkış Kodu: {returncode}"

    def process_list(self) -> str:
        """
        Sistemdeki aktif süreçlerin yapılandırılmış listesini döner.
        Ham `ps` çıktısı onlarca KB olabilir ve her model turuna binen token
        maliyeti tur süresini doğrudan uzatır; bu yüzden özet + en ağır N süreç
        döndürülür. Tam ps çıktısı execute_shell'in model kırpımından bağımsız
        olarak doğrudan toplanır (kırpım süreç sayısını eksiltmesin).
        """
        result: subprocess.CompletedProcess[str] = subprocess.run(
            ["ps", "-eo", "pid,ppid,user,%cpu,%mem,comm"],
            capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            raise ToolError(
                f"Süreç listesi alınamadı: çıkış={result.returncode}, stderr={result.stderr.strip()}",
                "SHELL_EXIT", True,
            )
        process_lines: List[str] = [ln for ln in result.stdout.splitlines() if ln.strip()]
        if process_lines and process_lines[0].lstrip().startswith("PID"):
            process_lines = process_lines[1:]
        def cpu_key(line: str) -> float:
            parts: List[str] = line.split()
            try:
                return float(parts[3]) if len(parts) > 3 else 0.0
            except ValueError:
                return 0.0
        top: List[str] = sorted(process_lines, key=cpu_key, reverse=True)[:15]
        return (
            f"Toplam süreç sayısı: {len(process_lines)}\n"
            f"En ağır 15 süreç (CPU'ya göre):\n" + "\n".join(top)
        )

    def take_screenshot(self, filename: str) -> str:
        """
        Ekran görüntüsünü ORTAK koordinat uzayında (MODEL_SCREEN_SIZE karesi) kaydeder:
        görüntüdeki bir noktanın koordinatı, tıklama araçlarına aynen verilir.
        Gözlenmemiş bir ekran girdisi varsa önce uygulamanın tepki verip durulması beklenir;
        modelin sabit bekleme eklemesi gerekmez. Retina piksel ↔ nokta dönüşümü burada
        yapılır, modele bırakılmaz.
        """
        target: Path = Path(filename).expanduser()
        if _is_sensitive_path(target):
            raise ToolError(
                f"Korunan bir sistem/kimlik yoluna ekran görüntüsü yazılamaz: {filename}",
                "SENSITIVE_PATH_BLOCKED", False,
            )
        settle_note: str = ""
        if self._pending_input is not None:
            _require_screen_capture()
            waited: float = wait_for_screen_settle(self._pending_input["baseline"], self._pending_input["at"])
            self._pending_input = None
            settle_note = f" Son eylemden sonra ekranın durulması {waited:.1f}sn beklendi."
        geometry: ScreenGeometry = current_geometry()
        frame: Image.Image = grab_model_frame(geometry)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            frame.save(target)
        except ValueError as error:
            raise ToolError(
                f"Görüntü biçimi dosya uzantısından anlaşılamadı: {filename} (.png veya .jpg kullan)",
                "INVALID_IMAGE_PATH", False,
            ) from error
        return (
            f"Ekran görüntüsü {target} dosyasına kaydedildi ({geometry['model_width']}×{geometry['model_height']}; "
            "bu görüntüdeki koordinatlar tıklama araçlarıyla aynı uzayda)." + settle_note
        )

    def capture_photo(self) -> str:
        """Varsayılan kameradan tek kareyi masaüstüne benzersiz adla kaydeder."""
        desktop: Path = Path.home() / "Desktop"
        if not desktop.is_dir():
            raise ToolError(f"Masaüstü dizini bulunamadı: {desktop}", "MISSING_DIRECTORY", False)
        target: Path = desktop / f"fotograf-{datetime.now().strftime('%Y-%m-%d-%H%M%S-%f')}.jpg"
        if _is_sensitive_path(target):
            raise ToolError(f"Korunan yola fotoğraf yazılamaz: {target}", "SENSITIVE_PATH_BLOCKED", False)
        ffmpeg: Optional[str] = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise ToolError("Doğrudan kamera çekimi için ffmpeg bulunamadı; Photo Booth kullanılabilir.",
                            "FFMPEG_MISSING", True)
        temporary: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=".omni_camera_", suffix=target.suffix, dir=target.parent, delete=False,
            ) as pending:
                temporary = Path(pending.name)
            command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "avfoundation",
                       "-i", "default:none", "-frames:v", "1", "-update", "1",
                       "-y", str(temporary)]
            try:
                returncode, stdout, stderr = run_streaming_process(command, False, 30.0)
            except subprocess.TimeoutExpired as error:
                raise ToolError("Kamera 30 saniyede kare üretmedi.", "CAMERA_TIMEOUT", True) from error
            if returncode != 0:
                raise ToolError(
                    f"Kamera çekimi başarısız (çıkış {returncode}): {_clip(stderr or stdout, 600)}",
                    "CAMERA_CAPTURE_FAILED", True,
                )
            if temporary.stat().st_size == 0:
                raise ToolError("Kamera boş dosya üretti.", "CAMERA_EMPTY", True)
            try:
                with Image.open(temporary) as frame:
                    frame.verify()
            except (OSError, ValueError) as error:
                raise ToolError("Kamera geçerli bir görüntü üretmedi.", "CAMERA_INVALID_IMAGE", True) from error
            try:
                os.link(temporary, target)
            except FileExistsError as error:
                raise ToolError(f"Fotoğraf hedefi işlem sırasında oluştu: {target}", "FILE_EXISTS", False) from error
            return f"Fotoğraf kaydedildi ve doğrulandı: {target} ({target.stat().st_size} bayt)."
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _click_template(self, app_name: str, template_path: str, confidence: float) -> str:
        """
        Şablonu uygulama penceresi içinde, ortak uzaydaki ekran karesinde arar ve
        merkezine tıklar. Şablon da take_screenshot görüntüsünden kırpılmış olmalıdır
        (aynı ölçek); aksi halde eşleşme ölçek farkından başarısız olur.
        """
        if not 0 <= confidence <= 1:
            raise ToolError(f"Geçersiz güven eşiği: {confidence}", "INVALID_CONFIDENCE", False)
        template: Optional[np.ndarray] = cv2.imread(str(Path(template_path).expanduser()), cv2.IMREAD_GRAYSCALE)
        if template is None:
            raise ToolError(f"Şablon dosyası bulunamadı veya okunamadı: {template_path}", "TEMPLATE_MISSING", False)
        geometry: ScreenGeometry = current_geometry()
        screen: np.ndarray = cv2.cvtColor(np.array(grab_model_frame(geometry)), cv2.COLOR_RGB2GRAY)
        left, top, width, height = self.cua.window_bounds(app_name)
        x0, y0 = points_to_model(left, top, geometry)
        x1, y1 = points_to_model(left + width, top + height, geometry)
        # Negatif/ekran dışı pencere koordinatları numpy'da sondan sarılıp yanlış bölge verir
        x0, y0 = max(x0, 0), max(y0, 0)
        x1, y1 = min(x1, screen.shape[1]), min(y1, screen.shape[0])
        if x1 <= x0 or y1 <= y0:
            raise ToolError(f"Pencere ekran dışında: {app_name} ({left}, {top}, {width}, {height})", "WINDOW_OFFSCREEN", True)
        region: np.ndarray = screen[y0:y1, x0:x1]
        if template.shape[0] > region.shape[0] or template.shape[1] > region.shape[1]:
            raise ToolError("Şablon pencereden büyük (ölçek farklı olabilir).", "TEMPLATE_TOO_LARGE", False)
        scores: np.ndarray = cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED)
        _, best, _, location = cv2.minMaxLoc(scores)
        if best < confidence:
            raise ToolError(f"Hedef bulunamadı: en yüksek güven={best:.2f} (eşik {confidence}).", "TARGET_MISSING", True)
        center_x: int = x0 + location[0] + template.shape[1] // 2
        center_y: int = y0 + location[1] + template.shape[0] // 2
        return click_model_point(center_x, center_y, "left", geometry) + f" (şablon güveni {best:.2f})"

    @_screen_input
    def smart_click(self, app_name: str, element_id: Optional[int], template_path: Optional[str], confidence: float) -> str:
        """
        Hibrit Tıklama Protokolü: AX öğesi (AXPress / merkez) -> görsel şablon sırasıyla
        dener. Başarısız katmanların nedenleri tek hatada biriktirilir ki model tek
        turda düzeltebilsin.
        """
        failures: List[str] = []
        if element_id is not None:
            try:
                return self.cua.click_element(app_name, element_id)
            except ToolError as error:
                failures.append(f"AX: {error}")
        if template_path is not None:
            try:
                return self._click_template(app_name, template_path, confidence)
            except ToolError as error:
                failures.append(f"şablon: {error}")
        detail: str = "; ".join(failures) if failures else "hiç denenmedi (element_id/template_path verilmedi)"
        raise ToolError(
            f"Tüm tıklama yöntemleri başarısız oldu: {app_name}. Katman hataları: {detail}",
            "CLICK_FAILED", True,
        )

    def web_search(self, query: str) -> str:
        """
        DuckDuckGo üzerinden web araması yapar (`ddgs` paketi).
        Boş sonuç kararlı bir durumdur, yeniden denenmez (3x gidiş-dönüş israfı).
        """
        attempt = 0
        while True:
            attempt += 1
            try:
                with DDGS() as ddgs:
                    results: List[Dict[str, str]] = list(ddgs.text(query, max_results=5))
                if not results:
                    raise ToolError(
                        f"Web araması boş sonuç döndü: sorgu={query}",
                        "WEB_SEARCH_EMPTY", True,
                    )
                # Model turuna binen token'ı azalt: gövde metinlerini 200 karakterle sınırla.
                clipped: List[Dict[str, str]] = [
                    {
                        "title": str(r.get("title", "")),
                        "href": str(r.get("href", "")),
                        "body": str(r.get("body", ""))[:200],
                    }
                    for r in results
                ]
                return json.dumps(clipped, indent=2, ensure_ascii=False)
            except ToolError:
                raise
            except Exception as error:
                logging.warning(
                    "Web araması başarısız",
                    extra={"query": query, "attempt": attempt, "error_type": type(error).__name__},
                )
                if attempt == 3:
                    raise ToolError(
                        f"Web araması başarısız: sorgu={query}, ayrıntı={error}",
                        "WEB_SEARCH_FAILED", True,
                    ) from error

    async def browse_url(self, url: Optional[str], actions: List[BrowserAction]) -> str:
        """
        Kalıcı sekmede çalışır: url verilirse oraya gider (None ise mevcut sayfada kalır),
        eylemleri sırayla uygular ve sonunda sayfanın URL'sini, başlığını, metnini ve
        seçicileriyle etkileşimli öğelerini döner. Çok adımlı web akışı tek çağrıda biter.
        """
        page: Page = await self._get_page()
        if url is not None:
            try:
                await page.goto(url, wait_until="domcontentloaded")
            except PlaywrightError as error:
                raise ToolError(f"Sayfa açılamadı: url={url}, ayrıntı={error}", "PAGE_LOAD_FAILED", True) from error
        elif page.url == "about:blank":
            raise ToolError("Açık sayfa yok; ilk çağrıda url ver.", "NO_PAGE", False)
        for index, action in enumerate(actions):
            kind: object = action.get("action")
            selector: str = str(action.get("selector") or "")
            value: Optional[str] = action.get("value")
            try:
                if kind == "click":
                    await page.click(selector)
                elif kind == "fill" and value is not None:
                    await page.fill(selector, value)
                elif kind == "press" and value is not None:
                    await page.press(selector, value)
                elif kind == "wait_for":
                    state = value if value in ("visible", "hidden", "attached", "detached") else "visible"
                    await page.locator(selector).wait_for(state=state)
                else:
                    raise ToolError(
                        f"Geçersiz tarayıcı eylemi {index}: {action} (click: selector; fill/press: selector + value).",
                        "INVALID_BROWSER_ACTION", False,
                    )
            except PlaywrightTimeoutError as error:
                elements: List[str] = await page.evaluate(_PAGE_ELEMENTS_SCRIPT, PAGE_ELEMENT_LIMIT)
                raise ToolError(
                    f"Tarayıcı eylemi {index} ({kind} {selector}) {PAGE_ACTION_TIMEOUT_MS}ms içinde yapılamadı "
                    f"(öğe yok/görünmez). Sayfadaki öğeler:\n" + "\n".join(elements),
                    "BROWSER_ACTION_TIMEOUT", True,
                ) from error
        if actions:
            await page.wait_for_load_state("domcontentloaded")
        text: str = await asyncio.to_thread(clean_html, await page.content())
        elements = await page.evaluate(_PAGE_ELEMENTS_SCRIPT, PAGE_ELEMENT_LIMIT)
        return (
            "Tarayıcı: arka planda çalışan ayrı Chromium; açık Google Chrome oturumunda görünmez.\n"
            f"URL: {page.url}\nBaşlık: {await page.title()}\n\n{text}\n\n"
            f"ÖĞELER (seçici — tür \"etiket\"):\n" + "\n".join(elements)
        )

    def fetch_raw(self, url: str) -> str:
        """
        Curl kullanarak hızlı HTTP(S) çekimi yapar ve içeriği temizler.
        JSON gövdeler HTML temizleyiciden geçirilmez (karakter kaybı olur);
        kalıcı hatalar (--retry-all-errors) tekrar denenmez.
        """
        parsed: SplitResult = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ToolError("fetch_raw yalnızca http(s) adreslerini kabul eder.", "INVALID_URL", False)
        result: subprocess.CompletedProcess[str] = subprocess.run(
            ["curl", "--fail", "--show-error", "--silent", "--location", "--compressed",
             "--proto", "=http,https", "--proto-redir", "=http,https",
             "--retry", "2", "--retry-delay", "1", "--retry-max-time", "20",
             "--max-time", "15", "--", url],
            capture_output=True, text=True, timeout=25,
        )
        if result.returncode != 0:
            raise ToolError(
                f"HTTP çekimi başarısız: url={url}, çıkış={result.returncode}, stderr={result.stderr.strip()}",
                "FETCH_FAILED", True,
            )
        stripped: str = result.stdout.lstrip()
        if stripped.startswith("{") or stripped.startswith("["):
            return _clip(result.stdout, SHELL_STDOUT_LIMIT)
        return clean_html(result.stdout)

    @_screen_input
    def chrome_active_tab(self, url: Optional[str]) -> str:
        """
        Kullanıcının açık Chrome profilindeki sekmeyi kullanır: URL verilirse aynı kökene ait
        açık sekmeyi (yoksa etkin sekmeyi) öne getirip oraya gider ve yüklenmesini bekler;
        None ise etkin sekmeyi okur. Hiç pencere yoksa aynı profilde yeni pencere açar.
        """
        parsed: Optional[SplitResult] = urlsplit(url) if url is not None else None
        if parsed is not None and (parsed.scheme not in ("https", "http") or not parsed.netloc):
            raise ToolError("Chrome sekmesi için http(s) adresi ver.", "INVALID_URL", False)
        origin: str = f"{parsed.scheme}://{parsed.netloc}/" if parsed is not None else ""
        # Sekme ve pencereler kimlikle tutulur: sıra numarası pencere öne alınınca başka
        # pencereyi gösterir. Chrome sekmelerinde 'index' özelliği yoktur.
        script: str = """on run argv
set targetUrl to item 1 of argv
set targetOrigin to item 2 of argv
set loadChecks to (item 3 of argv) as integer
tell application "Google Chrome"
    if (count of windows) is 0 then make new window
    set windowId to id of front window
    set tabId to id of active tab of front window
    if targetUrl is not "" then
        repeat with windowItem in windows
            set matchingIds to id of (every tab of windowItem whose URL starts with targetOrigin)
            if matchingIds is not {} then
                set windowId to id of windowItem
                set tabId to item 1 of matchingIds
                exit repeat
            end if
        end repeat
    end if
    set targetWindow to window id windowId
    set tabIds to id of every tab of targetWindow
    repeat with position from 1 to count of tabIds
        if item position of tabIds is tabId then set active tab index of targetWindow to position
    end repeat
    set index of targetWindow to 1
    set targetTab to tab id tabId of targetWindow
    if targetUrl is not "" and (URL of targetTab) is not targetUrl then set URL of targetTab to targetUrl
    activate
    repeat loadChecks times
        if not (loading of targetTab) then exit repeat
        delay 0.1
    end repeat
    return (URL of targetTab) & linefeed & (title of targetTab) & linefeed & (loading of targetTab)
end tell
end run"""
        try:
            result: subprocess.CompletedProcess[str] = subprocess.run(
                ["osascript", "-e", script, url or "", origin, str(CHROME_LOAD_CHECKS)],
                capture_output=True, text=True, timeout=CHROME_SCRIPT_TIMEOUT_SECONDS, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ToolError(
                f"Açık Chrome sekmesine erişilemedi: {type(error).__name__}",
                "CHROME_SESSION_FAILED", True,
            ) from error
        if result.returncode != 0:
            raise ToolError(
                f"Açık Chrome sekmesine erişilemedi: {result.stderr.strip()}",
                "CHROME_SESSION_FAILED", True,
            )
        lines: List[str] = result.stdout.rstrip("\n").split("\n")
        if len(lines) < 3:
            raise ToolError(f"Chrome sekme yanıtı beklenmeyen biçimde: {result.stdout!r}", "CHROME_SESSION_FAILED", True)
        loading_note: str = "\nSayfa hâlâ yükleniyor." if lines[-1] == "true" else ""
        return f"Görünür Chrome sekmesi: {lines[0]}\nBaşlık: {' '.join(lines[1:-1])}{loading_note}"

    @_screen_input
    def cua_click_point(self, point: List[int]) -> str:
        """Ortak ekran koordinatındaki [x, y] noktasına tıklar."""
        _require_accessibility()
        x, y = parse_point(point)
        return click_model_point(x, y, "left", current_geometry())

    @_screen_input
    def cua_type_text(self, text: str) -> str:
        """Odaklı alana klavye düzeninden bağımsız metin yazar."""
        _require_accessibility()
        type_unicode_text(text)
        return f"Yazıldı ({len(text)} karakter): {_clip(text, TYPED_TEXT_ECHO_LIMIT)}"

    @_screen_input
    def cua_press_key(self, key: str) -> str:
        """Odaklı uygulamada tuş veya kısayol çalıştırır."""
        _require_accessibility()
        return press_key_spec(key)

    @_screen_input
    def cua_submit_text(self, point: List[int], text: str) -> str:
        """
        [x, y] noktasındaki alana tıklar, içeriğini seçip yerine metni yazar ve Enter'a basar:
        arama/gönderme akışı tek çağrıda biter. Canlı ölçümde model tıkla → yaz → Enter'ı üç
        ayrı model turuna bölüyordu.
        """
        _require_accessibility()
        x, y = parse_point(point)
        clicked: str = click_model_point(x, y, "left", current_geometry())
        press_key_spec("cmd+a")
        type_unicode_text(text)
        press_key_spec("enter")
        return f"{clicked} Alana yazıldı ({len(text)} karakter): {_clip(text, TYPED_TEXT_ECHO_LIMIT)}; Enter'a basıldı."

    @_screen_input
    def cua_get_app(self, app_name: str) -> str:
        return self.cua.get_app(app_name)

    def cua_get_ax_state(self, app_name: str) -> str:
        return self.cua.list_elements(app_name)

    @_screen_input
    def cua_click(self, app_name: str, element_id: int) -> str:
        return self.cua.click_element(app_name, element_id)

    @_screen_input
    def run_action_sequence(self, steps: List[ActionStep]) -> str:
        """
        Fare/klavye eylemlerini (click/move/type/press/wait) TEK araç çağrısında sırayla
        çalıştırır; her adım için ayrı model turu gerekmez. Bir adım başarısız olursa
        hata, o ana kadar tamamlanan adımları da bildirir (model durumu bilsin).
        """
        if isinstance(steps, str):
            try:
                steps = json.loads(steps)
            except json.JSONDecodeError as error:
                raise ToolError(
                    "steps bir JSON nesne listesi olmalı; örnek: "
                    '[{"action":"click","point":[100,200]}]',
                    "INVALID_ACTION_PARAMS", False,
                ) from error
        if not isinstance(steps, list):
            raise ToolError("steps bir eylem nesnesi listesi olmalı.", "INVALID_ACTION_PARAMS", False)
        _require_accessibility()
        geometry: ScreenGeometry = current_geometry()
        executed: List[str] = []
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                raise ToolError(
                    f"Eylem {index} nesne olmalı; alınan tür: {type(step).__name__}. "
                    "Örnek: {\"action\":\"click\",\"point\":[100,200]}. "
                    f"Tamamlanan adımlar: {executed}",
                    "INVALID_ACTION_PARAMS", False,
                )
            try:
                executed.append(_run_action_step(step, geometry))
            except (KeyError, TypeError, ValueError) as error:
                raise ToolError(
                    f"Eylem {index} ({step.get('action')}) geçersiz parametrelerle başarısız: {error}. "
                    f"Tamamlanan adımlar: {executed}",
                    "INVALID_ACTION_PARAMS", False,
                ) from error
            except ToolError as error:
                raise ToolError(f"Eylem {index} başarısız: {error}. Tamamlanan adımlar: {executed}", error.code, error.recoverable) from error
        return "Eylem dizisi tamamlandı:\n" + "\n".join(executed)

    def execute_js(self, code: str) -> str:
        temp_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".js", prefix="omni_", encoding="utf-8", delete=False,
            ) as source:
                temp_path = Path(source.name)
                source.write(code)
            try:
                returncode, stdout, stderr = run_streaming_process(["node", str(temp_path)], False, JS_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired as error:
                raise ToolError(f"JS {JS_TIMEOUT_SECONDS:.0f} saniyede tamamlanmadı.", "JS_TIMEOUT", True) from error
            if returncode != 0:
                raise ToolError(
                    f"JS çalıştırma başarısız: çıkış={returncode}, stderr={_clip(stderr.strip(), 1000)}",
                    "JS_EXIT", True,
                )
            return f"STDOUT: {_clip(stdout, SHELL_STDOUT_LIMIT)}\nSTDERR: {_clip(stderr, SHELL_STDERR_LIMIT)}\nÇıkış Kodu: 0"
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()

    def write_file(self, path: str, content: str) -> str:
        """
        Dosyaya tam içeriği atomik yazar: eksik üst dizinleri oluşturur, `.py` için
        sözdizimini doğrular, eski sürümü yedekler ve yazılanı geri okuyup karşılaştırır.
        Başarı mesajı yazımın kanıtıdır; modelin ayrıca geri okuması gerekmez.
        """
        destination: Path = Path(path).expanduser()
        if _is_sensitive_path(destination):
            raise ToolError(
                f"Korunan bir sistem/kimlik dosyasına yazma engellendi: {destination}",
                "SENSITIVE_PATH_BLOCKED", False,
            )
        if len(content.encode("utf-8")) > FILE_READ_MAX_BYTES:
            raise ToolError(f"Dosya {FILE_READ_MAX_BYTES} bayt yazma sınırını aşıyor.", "FILE_TOO_LARGE", False)
        if destination.suffix == ".py":
            try:
                compile(content, str(destination), "exec")
            except SyntaxError as error:
                raise ToolError(
                    f"Sözdizimi hatası nedeniyle yazma iptal edildi: {destination}, hata={error}",
                    "SYNTAX_INVALID", False,
                ) from error
        # Oluşturulan dizinler sonuçta açıkça bildirilir: yazım hatalı bir yol sessizce yeni dizin açmasın.
        created_directory: Optional[Path] = None if destination.parent.exists() else destination.parent
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            _backup_file(destination)
        temp_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="", dir=destination.parent,
                prefix=f".{destination.name}.", delete=False,
            ) as target:
                temp_path = Path(target.name)
                target.write(content)
                target.flush()
                os.fsync(target.fileno())
            if destination.exists():
                os.chmod(temp_path, destination.stat().st_mode & 0o777)
            os.replace(temp_path, destination)
            if self._read_full(str(destination)) != content:
                raise ToolError(f"Dosya doğrulaması başarısız: {destination}", "VERIFY_FAILED", False)
            note: str = f" Yeni dizin oluşturuldu: {created_directory}." if created_directory is not None else ""
            return f"Dosya yazıldı ve içeriği doğrulandı: {destination} ({len(content)} karakter).{note}"
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()

    def read_file(self, path: str) -> str:
        """
        Dosya okur ve model için kısaltılmış içeriği döner. Doğrulama yolları
        (_read_full) kırpma olmadan okur; aksi halde büyük dosya yazım doğrulaması
        yanlışlıkla başarısız sayılır.
        """
        return _clip(self._read_full(path), FILE_READ_LIMIT)

    def _read_full(self, path: str) -> str:
        """Düzenli UTF-8 dosyasını boyut sınırıyla ve satır sonlarını koruyarak okur."""
        source_path: Path = Path(path).expanduser()
        if _is_sensitive_path(source_path) and not _sensitive_read_allowed():
            raise ToolError(
                f"Korunan sistem/kimlik dosyasını okuma engellendi: {source_path}. "
                "Kullanıcı bilerek izin verirse OMNI_ALLOW_SENSITIVE_READ=1 ile başlatmalı.",
                "SENSITIVE_PATH_BLOCKED", False,
            )
        try:
            metadata = source_path.stat()
            if not stat.S_ISREG(metadata.st_mode):
                code = "IS_DIRECTORY" if stat.S_ISDIR(metadata.st_mode) else "NOT_REGULAR_FILE"
                raise ToolError(f"Yol düzenli bir dosya değil: {source_path}", code, False)
            if metadata.st_size > FILE_READ_MAX_BYTES:
                raise ToolError(f"Dosya {FILE_READ_MAX_BYTES} bayt okuma sınırını aşıyor: {source_path}",
                                "FILE_TOO_LARGE", False)
            with source_path.open("rb") as source:
                raw = source.read(FILE_READ_MAX_BYTES + 1)
            if len(raw) > FILE_READ_MAX_BYTES:
                raise ToolError(f"Dosya okuma sırasında boyut sınırını aştı: {source_path}",
                                "FILE_TOO_LARGE", False)
            return raw.decode("utf-8")
        except FileNotFoundError as error:
            raise ToolError(f"Dosya bulunamadı: {source_path}.{missing_path_hint(source_path)}",
                            "FILE_NOT_FOUND", True) from error
        except UnicodeDecodeError as error:
            raise ToolError(f"Dosya UTF-8 metin değil (ikili dosya olabilir): {source_path}",
                            "NOT_TEXT", False) from error

    async def close_browser(self) -> None:
        """
        Tarayıcı kaynaklarını temizler.
        """
        if self.browser:
            await self.browser.close()
            self.browser = None
            self.browser_context = None
            self.page = None
        if self.playwright_instance:
            await self.playwright_instance.stop()
            self.playwright_instance = None
