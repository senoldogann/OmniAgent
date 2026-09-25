"""
OmniAgent Araç Seti (tools/__init__.py)
Modüler araç yapısının ana Facade giriş noktası.
Tüm alt modüllerden (types, system, filesystem, screen, gui_input, browser) gelen
fonksiyonları, tipleri ve Toolbox sınıfını geriye dönük tam uyumlulukla dışa aktarır.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Callable, Concatenate, Dict, IO, List, NotRequired, Optional, ParamSpec, Tuple, TypedDict, Union
from urllib.parse import SplitResult, urlsplit

import AppKit
import cv2
from ddgs import DDGS
import numpy as np
from PIL import Image
import pyautogui
import Quartz
import types as _py_types

from omniagent.memory import user as memory
from omniagent.platform.macos import screen_text as st
from omniagent.config import redact
from omniagent.core import state as sm
from omniagent.integrations.runtime import CURRENT_RUNTIME
from omniagent.approval import APPROVAL_TIMEOUT_SECONDS, approval_granted

from . import browser, filesystem, gui_input, screen, system, types as tool_types

from .types import (
    AX_ELEMENT_LIMIT, AX_LABEL_SEARCH_NODES, AX_MESSAGING_TIMEOUT_SECONDS,
    AX_NODE_LIMIT, AX_SCAN_BUDGET_SECONDS, BACKUP_KEEP_PER_FILE,
    CHROME_LOAD_CHECKS, CHROME_SCRIPT_TIMEOUT_SECONDS,
    FETCH_ERROR_BODY_LIMIT, FILE_READ_LIMIT, FILE_READ_MAX_BYTES,
    HISTORY_RESULT_LIMIT, JS_TIMEOUT_SECONDS,
    MAX_WAIT_SECONDS, MODEL_SCREEN_SIZE,
    PAGE_ACTION_TIMEOUT_MS, PAGE_ELEMENT_LIMIT,
    PAGE_LOAD_TIMEOUT_MS, READ_EDGE_UNITS,
    READ_FIRST_STEP_SHARE, READ_GAP_MARKER,
    READ_MAX_PAGES, READ_STEP_SHARE,
    READ_TEXT_LIMIT, READ_TOP_ATTEMPTS,
    SCREEN_SETTINGS_URL, SCREENSHOT_MAX_EDGE,
    SCROLL_CHUNK_DELAY_SECONDS, SCROLL_CHUNK_POINTS,
    SCROLL_DIFF_EDGE, SCROLL_MAX_EVENTS,
    SCROLL_MOVED_RATIO, SCROLL_PIXEL_DELTA,
    SCROLL_SETTLE_MAX_SECONDS, SCROLL_SETTLE_QUIET_SECONDS,
    SETTLE_CHANGED_RATIO, SETTLE_FRAME_EDGE,
    SETTLE_MAX_SECONDS, SETTLE_PIXEL_DELTA,
    SETTLE_POLL_SECONDS, SETTLE_QUIET_SECONDS,
    SETTLE_REACTION_SECONDS, SHELL_MAX_TIMEOUT_SECONDS,
    SHELL_STDERR_LIMIT, SHELL_STDOUT_LIMIT,
    SHELL_TIMEOUT_SECONDS, STREAM_READ_CHARS,
    STREAM_STDERR_MAX_BYTES, STREAM_STDOUT_MAX_BYTES,
    TEXT_CANDIDATE_LIMIT, TIMEOUT_OUTPUT_TAIL,
    TOOL_RUNTIME, TYPED_TEXT_ECHO_LIMIT,
    UNICODE_CHUNK_DELAY_SECONDS, UNICODE_CHUNK_UNITS,
    ActionStep, AXElement, BrowserAction,
    PendingInput, ScreenGeometry, ToolError,
    ToolRuntime, clip_text,
)

from .system import (
    _call_approved, _clip, _command_words, _dangerous_rm_target,
    _has_shell_expansion, _is_catastrophic_command, _nested_shell_commands,
    _pump_lines, _shell_path, _shell_segments, _shell_tokens,
    _shell_writes_to_sensitive_path, child_environment,
    output_tail, parent_process_name, resolve_shell_timeout,
    run_streaming_process, shell_command_words,
)

from .filesystem import (
    _backup_file, _is_sensitive_path, _logical_path,
    _sensitive_prefixes, _sensitive_read_allowed, _sensitive_write_allowed,
    clean_html, edit_file_content, missing_path_hint,
    read_file_content, read_full_file, write_file_content,
)

from .screen import (
    BUNDLE_APP_NAMES, _bounds_intersect, _display_image, _draw_scaled,
    _front_app_window_image, _main_display_image, _raise_if_stopped,
    _request_screen_capture_once, _require_screen_capture,
    _SCREEN_CAPTURE_REQUESTED, active_display_ids, current_geometry,
    frame_change_ratio, geometry_for_display_id, geometry_for_display_index,
    grab_app_window_frame, grab_model_frame, gray_frame,
    model_space_size, model_to_points, parse_point, points_to_model,
    rgb_frame, screen_capture_granted, screen_capture_help,
    screen_capture_owner, screenshot_size, settle_app_frame,
    settle_display_frame, settle_frame, wait_for_screen_settle,
    click_model_point, move_model_point, post_scroll, changed_region,
)

from .gui_input import (
    CUA, _AX_ACTIONABLE_ROLES, _AX_SCAN_ATTRIBUTES, _AX_TEXT_INPUT_ROLES,
    _KEY_ALIASES, _app_pid, _ax_attribute, _ax_descendant_text,
    _ax_point, _ax_present, _ax_short_text, _ax_size,
    _bundle_name, _check_in_model_space, _post_unicode_chunk,
    _require_accessibility, _run_action_step, _visible_app_owners,
    format_ax_listing, press_key_spec,
    scan_ax_elements, type_unicode_text, unicode_chunks,
)

from .web import search_web

from .browser import (
    _PAGE_ELEMENTS_SCRIPT, HeadlessBrowserSession,
    browse_page_actions, fetch_raw_content,
    normalize_browser_key, run_chrome_active_tab,
)

from playwright.async_api import Browser, BrowserContext, Page, Playwright

_P = ParamSpec("_P")

def _screen_input(method: Callable[Concatenate["Toolbox", _P], str]) -> Callable[Concatenate["Toolbox", _P], str]:
    from functools import wraps
    @wraps(method)
    def recorded(self: "Toolbox", *args: _P.args, **kwargs: _P.kwargs) -> str:
        baseline: Optional[np.ndarray] = self._settle_frame() if screen_capture_granted() else None
        try:
            return method(self, *args, **kwargs)
        finally:
            if baseline is not None:
                self._pending_input = {"at": time.monotonic(), "baseline": baseline}
    return recorded

class Toolbox:
    def __init__(
        self,
        memory_file: Optional[str] = None,
        allow_memory_mutation: bool = False,
        history_file: Optional[str] = None,
    ) -> None:
        self._headless_browser: HeadlessBrowserSession = HeadlessBrowserSession()
        self.cua: CUA = CUA()
        self._browser_lock: asyncio.Lock = self._headless_browser._lock
        self._pending_input: Optional[PendingInput] = None
        self._memory_file: Optional[str] = memory_file
        self._allow_memory_mutation: bool = allow_memory_mutation
        self._history_file: Optional[str] = history_file
        self._chrome_applescript_available: Optional[bool] = None
        self._screen_scope_app: Optional[str] = None
        self._visual_geometry: Optional[ScreenGeometry] = None
        self._visual_display_id: Optional[int] = None
        self._task_js: Dict[str, str] = {}

    @property
    def playwright_instance(self) -> Optional[Playwright]:
        return self._headless_browser.playwright_instance

    @playwright_instance.setter
    def playwright_instance(self, value: Optional[Playwright]) -> None:
        self._headless_browser.playwright_instance = value

    @property
    def browser(self) -> Optional[Browser]:
        return self._headless_browser.browser

    @browser.setter
    def browser(self, value: Optional[Browser]) -> None:
        self._headless_browser.browser = value

    @property
    def browser_context(self) -> Optional[BrowserContext]:
        return self._headless_browser.browser_context

    @browser_context.setter
    def browser_context(self, value: Optional[BrowserContext]) -> None:
        self._headless_browser.browser_context = value

    @property
    def page(self) -> Optional[Page]:
        return self._headless_browser.page

    @page.setter
    def page(self, value: Optional[Page]) -> None:
        self._headless_browser.page = value

    def _settle_frame(self) -> np.ndarray:
        """Etkin görsel kapsamın küçük gri karesini döner."""
        if self._screen_scope_app is not None:
            return settle_app_frame(self._screen_scope_app)
        if self._visual_display_id is not None:
            return settle_display_frame(self._visual_display_id)
        return settle_frame()

    def _input_geometry(self) -> ScreenGeometry:
        """Girdiyi modelin son gördüğü ekran/pencere geometrisine bağlar."""
        if self._visual_geometry is not None:
            if self._visual_display_id is not None:
                current_geometry_for_display = geometry_for_display_id(self._visual_display_id)
                if current_geometry_for_display != self._visual_geometry:
                    raise ToolError(
                        "Ekran düzeni değişti; tıklamadan önce yeniden görüntü al.",
                        "DISPLAY_GEOMETRY_CHANGED",
                        True,
                    )
            return self._visual_geometry
        return current_geometry()

    @property
    def memory_mutation_allowed(self) -> bool:
        return self._allow_memory_mutation

    def user_memory(
        self,
        action: str,
        key: Optional[str] = None,
        value: Optional[str] = None,
        query: Optional[str] = None,
        category: Optional[str] = None,
    ) -> str:
        """Kalıcı kullanıcı tercihlerini ve geçmiş görev özetlerini yönetir."""
        normalized_action = action.strip().casefold()
        if normalized_action == "history":
            return self._task_history(query or "")
        if not self._memory_file:
            raise ToolError("Bu görev için kalıcı kullanıcı hafızası etkin değil.", "MEMORY_UNAVAILABLE", False)
        if normalized_action in {"remember", "forget"} and not (
            self._allow_memory_mutation or _call_approved()
        ):
            raise ToolError(
                "Bu görev kalıcı hafıza değiştirme yetkisiyle başlatılmadı ve kullanıcı onayı alınmadı.",
                "MEMORY_MUTATION_NOT_ALLOWED",
                False,
            )
        try:
            state = memory.load_memory(self._memory_file)
            if normalized_action == "remember":
                if not key or value is None:
                    raise ValueError("remember için key ve value zorunludur.")
                updated = memory.remember_preference(
                    state, key, value, category or "preference", memory.utc_timestamp()
                )
                memory.save_memory(self._memory_file, updated)
                return json.dumps(
                    {"ok": True, "action": normalized_action, "record": updated["preferences"][-1]},
                    ensure_ascii=False,
                )
            if normalized_action == "recall":
                records = memory.search_preferences(state, query or "")
                return json.dumps({"ok": True, "preferences": records}, ensure_ascii=False)
            if normalized_action == "forget":
                if not key:
                    raise ValueError("forget için key zorunludur.")
                updated, removed = memory.forget_preference(state, key)
                memory.save_memory(self._memory_file, updated)
                return json.dumps(
                    {"ok": True, "action": normalized_action, "removed": removed},
                    ensure_ascii=False,
                )
            raise ValueError("action remember, recall, forget veya history olmalı.")
        except ValueError as error:
            raise ToolError(
                f"Kullanıcı hafızası işlemi reddedildi: {error}", "MEMORY_INVALID", False
            ) from error
        except OSError as error:
            raise ToolError(
                f"Kullanıcı hafızasına erişilemedi: {error}", "MEMORY_IO", True
            ) from error

    def _task_history(self, query: str) -> str:
        if not self._history_file:
            raise ToolError("Bu görev için görev geçmişi etkin değil.", "MEMORY_UNAVAILABLE", False)
        try:
            state = sm.load_state(self._history_file)
        except (OSError, ValueError) as error:
            raise ToolError(f"Görev geçmişi okunamadı: {error}", "MEMORY_IO", True) from error
        return redact(
            json.dumps(
                {"ok": True, "episodes": sm.search_episodes(state, query, HISTORY_RESULT_LIMIT)},
                ensure_ascii=False,
            )
        )

    async def ask_user(self, question: str, kind: str) -> str:
        """Görev sırasında UI/Telegram kanalından kullanıcı girdisi bekler."""
        runtime = CURRENT_RUNTIME.get()
        if runtime is None or runtime.answer is None:
            raise ToolError(
                "Etkileşimli kullanıcı kanalı yok (CLI/benchmark); soruyu final yanıtında sor.",
                "INPUT_REQUIRED",
                False,
            )
        text = " ".join(str(question).split())
        if not text:
            raise ToolError("Soru boş olamaz.", "INVALID_QUESTION", False)
        if kind == "confirm":
            fields: Dict[str, object] = {
                "onay": {"type": "boolean", "label": "Onaylıyorum", "default": False}
            }
        elif kind == "text":
            fields = {"yanit": {"type": "string", "label": "Yanıtınız", "default": ""}}
        else:
            raise ToolError(
                f"kind confirm veya text olmalı; alınan: {kind!r}", "INVALID_QUESTION", False
            )
        try:
            answer = await runtime.ask(redact(_clip(text, 1500)), fields, APPROVAL_TIMEOUT_SECONDS)
        except TimeoutError as error:
            raise ToolError(
                f"Kullanıcı {APPROVAL_TIMEOUT_SECONDS / 60:.0f} dakika içinde yanıt vermedi; "
                "bekleyen soruyu final yanıtında bildir.",
                "INPUT_TIMEOUT",
                False,
            ) from error
        if kind == "confirm":
            return (
                "Kullanıcı ONAYLADI."
                if approval_granted(answer.get("onay"))
                else "Kullanıcı ONAYLAMADI: bu eylemi yapma; kalan işi buna göre sürdür veya durumu raporla."
            )
        reply = str(answer.get("yanit", "")).strip()
        return f"Kullanıcı yanıtı: {reply}" if reply else "Kullanıcı boş yanıt verdi."

    async def _get_page(self) -> Page:
        return await self._headless_browser.get_page()

    def execute_shell(
        self, command: str, use_sudo: bool = False, timeout_seconds: Optional[int] = None
    ) -> str:
        """Sistem kabuğunda komut çalıştırır; çıktı ve süre sınırları host tarafından uygulanır."""
        if not command.strip():
            raise ToolError("Boş kabuk komutu çalıştırılamaz.", "EMPTY_COMMAND", False)
        limit = resolve_shell_timeout(timeout_seconds)
        if _is_catastrophic_command(command):
            raise ToolError(
                f"Bilinen yıkıcı komut kalıbıyla eşleşti, çalıştırma engellendi: {command}",
                "CATASTROPHIC_COMMAND_BLOCKED",
                False,
            )
        if _shell_writes_to_sensitive_path(command) and not _sensitive_write_allowed():
            raise ToolError(
                f"Komut korunan bir sistem/kimlik yoluna yönlendirme yapıyor, engellendi: {command}.",
                "SENSITIVE_PATH_BLOCKED",
                False,
            )
        full_command: Union[str, List[str]] = (
            ["sudo", "-n", "/bin/sh", "-c", command] if use_sudo else command
        )
        returncode, stdout, stderr = run_streaming_process(full_command, not use_sudo, limit)
        if returncode != 0:
            raise ToolError(
                f"Kabuk komutu başarısız: çıkış={returncode}, "
                f"stdout={_clip(stdout, 1000)}, stderr={_clip(stderr, 1000)}",
                "SHELL_EXIT",
                True,
            )
        return (
            f"STDOUT: {_clip(stdout, SHELL_STDOUT_LIMIT)}\n"
            f"STDERR: {_clip(stderr, SHELL_STDERR_LIMIT)}\nÇıkış Kodu: {returncode}"
        )

    def process_list(self) -> str:
        result = subprocess.run(
            ["ps", "-eo", "pid,ppid,user,%cpu,%mem,comm"],
            env=child_environment(),
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            raise ToolError(
                f"Süreç listesi alınamadı: çıkış={result.returncode}, stderr={result.stderr.strip()}",
                "SHELL_EXIT",
                True,
            )
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        if lines and lines[0].lstrip().startswith("PID"):
            lines = lines[1:]

        def cpu_key(line: str) -> float:
            parts = line.split()
            try:
                return float(parts[3]) if len(parts) > 3 else 0.0
            except ValueError:
                return 0.0

        top = sorted(lines, key=cpu_key, reverse=True)[:15]
        return f"Toplam süreç sayısı: {len(lines)}\nEn ağır 15 süreç (CPU'ya göre):\n" + "\n".join(top)

    def take_screenshot(self, filename: str, display_index: Optional[int] = None) -> str:
        """Etkin pencere veya ekran kapsamını model koordinatlarıyla kaydeder."""
        target = Path(filename).expanduser()
        settle_note = ""
        if self._pending_input is not None:
            _require_screen_capture()
            waited = wait_for_screen_settle(
                self._pending_input["baseline"],
                self._pending_input["at"],
                self._settle_frame,
            )
            self._pending_input = None
            settle_note = f" Son eylemden sonra ekranın durulması {waited:.1f}sn beklendi."

        if self._screen_scope_app is not None:
            frame, geometry = grab_app_window_frame(self._screen_scope_app)
            display_id = None
        else:
            if display_index is not None:
                display_id, geometry = geometry_for_display_index(display_index)
                self._visual_display_id = display_id
            elif self._visual_display_id is not None:
                display_id = self._visual_display_id
                geometry = geometry_for_display_id(display_id)
            else:
                display_id = None
                geometry = current_geometry()
            frame = grab_model_frame(geometry, display_id)

        self._visual_geometry = geometry
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            frame.save(target)
        except ValueError as error:
            raise ToolError(
                f"Görüntü biçimi dosya uzantısından anlaşılamadı: {filename} (.png veya .jpg kullan)",
                "INVALID_IMAGE_PATH",
                False,
            ) from error
        scope_label = (
            f"{self._screen_scope_app} penceresi — "
            if self._screen_scope_app is not None
            else f"Ekran {display_index} — " if display_index is not None else ""
        )
        return (
            f"{scope_label}Ekran görüntüsü {target} dosyasına kaydedildi "
            f"({geometry['model_width']}×{geometry['model_height']}; koordinatlar aynı uzaydadır)."
            + settle_note
        )

    def capture_photo(self) -> str:
        desktop = Path.home() / "Desktop"
        if not desktop.is_dir():
            raise ToolError(f"Masaüstü dizini bulunamadı: {desktop}", "MISSING_DIRECTORY", False)
        target = desktop / f"fotograf-{datetime.now().strftime('%Y-%m-%d-%H%M%S-%f')}.jpg"
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise ToolError(
                "Doğrudan kamera çekimi için ffmpeg bulunamadı; Photo Booth kullanılabilir.",
                "FFMPEG_MISSING",
                True,
            )
        temporary: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=".omni_camera_", suffix=target.suffix, dir=target.parent, delete=False
            ) as pending:
                temporary = Path(pending.name)
            command = [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "avfoundation",
                "-i", "default:none", "-frames:v", "1", "-update", "1", "-y", str(temporary),
            ]
            returncode, stdout, stderr = run_streaming_process(command, False, 30.0)
            if returncode != 0:
                raise ToolError(
                    f"Kamera çekimi başarısız (çıkış {returncode}): {_clip(stderr or stdout, 600)}",
                    "CAMERA_CAPTURE_FAILED",
                    True,
                )
            if temporary.stat().st_size == 0:
                raise ToolError("Kamera boş dosya üretti.", "CAMERA_EMPTY", True)
            try:
                with Image.open(temporary) as image:
                    image.verify()
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
        if not 0 <= confidence <= 1:
            raise ToolError(f"Geçersiz güven eşiği: {confidence}", "INVALID_CONFIDENCE", False)
        template = cv2.imread(str(Path(template_path).expanduser()), cv2.IMREAD_GRAYSCALE)
        if template is None:
            raise ToolError(
                f"Şablon dosyası bulunamadı veya okunamadı: {template_path}",
                "TEMPLATE_MISSING",
                False,
            )
        geometry = self._input_geometry()
        if self._screen_scope_app is not None:
            frame, geometry = grab_app_window_frame(self._screen_scope_app)
            screen_gray = cv2.cvtColor(np.array(frame), cv2.COLOR_RGB2GRAY)
            region = screen_gray
            x0 = y0 = 0
        else:
            screen_gray = cv2.cvtColor(
                np.array(grab_model_frame(geometry, self._visual_display_id)),
                cv2.COLOR_RGB2GRAY,
            )
            left, top, width, height = self.cua.window_bounds(app_name)
            x0, y0 = points_to_model(left, top, geometry)
            x1, y1 = points_to_model(left + width, top + height, geometry)
            x0, y0 = max(x0, 0), max(y0, 0)
            x1, y1 = min(x1, screen_gray.shape[1]), min(y1, screen_gray.shape[0])
            if x1 <= x0 or y1 <= y0:
                raise ToolError(f"Pencere ekran dışında: {app_name}", "WINDOW_OFFSCREEN", True)
            region = screen_gray[y0:y1, x0:x1]
        if template.shape[0] > region.shape[0] or template.shape[1] > region.shape[1]:
            raise ToolError("Şablon pencereden büyük.", "TEMPLATE_TOO_LARGE", False)
        scores = cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED)
        _, best, _, location = cv2.minMaxLoc(scores)
        if best < confidence:
            raise ToolError(
                f"Hedef bulunamadı: en yüksek güven={best:.2f} (eşik {confidence}).",
                "TARGET_MISSING",
                True,
            )
        center_x = x0 + location[0] + template.shape[1] // 2
        center_y = y0 + location[1] + template.shape[0] // 2
        return click_model_point(center_x, center_y, "left", geometry) + f" (şablon güveni {best:.2f})"

    @_screen_input
    def smart_click(
        self,
        app_name: str,
        element_id: Optional[int],
        template_path: Optional[str],
        confidence: float,
    ) -> str:
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
        detail = "; ".join(failures) if failures else "hiç yöntem verilmedi"
        raise ToolError(
            f"Tüm tıklama yöntemleri başarısız oldu: {app_name}. Katman hataları: {detail}",
            "CLICK_FAILED",
            True,
        )

    def web_search(
        self,
        query: str,
        category: str = "auto",
        freshness_days: Optional[int] = None,
    ) -> str:
        """Web/haber aramasını ayrık arama katmanına yönlendirir."""
        return search_web(
            query,
            category,
            freshness_days,
            client_factory=DDGS,
        )

    async def browse_url(self, url: Optional[str], actions: List[BrowserAction]) -> str:
        page = await self._get_page()
        return await browse_page_actions(page, url, actions)

    def fetch_raw(self, url: str) -> str:
        return fetch_raw_content(url)

    @_screen_input
    def chrome_active_tab(self, url: Optional[str]) -> str:
        result, available = run_chrome_active_tab(url, self._chrome_applescript_available)
        self._chrome_applescript_available = available
        self._screen_scope_app = "Google Chrome"
        self._visual_display_id = None
        self._visual_geometry = None
        return result

    @_screen_input
    def cua_click_point(self, point: List[int]) -> str:
        _require_accessibility()
        x, y = parse_point(point)
        return click_model_point(x, y, "left", self._input_geometry())

    @_screen_input
    def cua_type_text(self, text: str) -> str:
        _require_accessibility()
        type_unicode_text(text)
        return f"Yazıldı ({len(text)} karakter): {_clip(text, TYPED_TEXT_ECHO_LIMIT)}"

    @_screen_input
    def cua_press_key(self, key: str) -> str:
        _require_accessibility()
        return press_key_spec(key)

    @_screen_input
    def cua_submit_text(self, point: List[int], text: str) -> str:
        _require_accessibility()
        x, y = parse_point(point)
        clicked = click_model_point(x, y, "left", self._input_geometry())
        press_key_spec("cmd+a")
        type_unicode_text(text)
        press_key_spec("enter")
        return (
            f"{clicked} Alana yazıldı ({len(text)} karakter): "
            f"{_clip(text, TYPED_TEXT_ECHO_LIMIT)}; Enter'a basıldı."
        )

    @_screen_input
    def cua_fill_field(self, point: List[int], text: str) -> str:
        _require_accessibility()
        x, y = parse_point(point)
        clicked = click_model_point(x, y, "left", self._input_geometry())
        press_key_spec("cmd+a")
        type_unicode_text(text)
        return f"{clicked} Alan dolduruldu ({len(text)} karakter): {_clip(text, TYPED_TEXT_ECHO_LIMIT)}"

    def _scope_image(self, image_option: int) -> Tuple[object, ScreenGeometry]:
        """Etkin görsel kapsamın ham görüntüsünü ve geometrisini döner."""
        _require_screen_capture()
        if self._screen_scope_app is not None:
            return _front_app_window_image(
                self._screen_scope_app, Quartz.kCGWindowImageBoundsIgnoreFraming
            )
        if self._visual_display_id is not None:
            geometry = geometry_for_display_id(self._visual_display_id)
            return _display_image(self._visual_display_id, image_option), geometry
        return _main_display_image(image_option), current_geometry()

    def _scope_gray(self) -> np.ndarray:
        image, _geometry = self._scope_image(Quartz.kCGWindowImageNominalResolution)
        return gray_frame(image, SCROLL_DIFF_EDGE)

    def _settled_gray(self, reference: np.ndarray) -> np.ndarray:
        started = time.monotonic()
        last_change = started
        previous = reference
        while True:
            _raise_if_stopped()
            frame = self._scope_gray()
            now = time.monotonic()
            if frame_change_ratio(previous, frame, SETTLE_PIXEL_DELTA) > SETTLE_CHANGED_RATIO:
                last_change = now
                previous = frame
            if (
                now - last_change >= SCROLL_SETTLE_QUIET_SECONDS
                or now - started >= SCROLL_SETTLE_MAX_SECONDS
            ):
                return frame
            time.sleep(SETTLE_POLL_SECONDS)

    def _scroll_to_start(self, geometry: ScreenGeometry) -> None:
        for _attempt in range(READ_TOP_ATTEMPTS):
            before = self._scope_gray()
            post_scroll(0.0, -5.0 * geometry["point_height"])
            after = self._settled_gray(before)
            if frame_change_ratio(before, after, SCROLL_PIXEL_DELTA) < SCROLL_MOVED_RATIO:
                return

    def _screen_text(self) -> Tuple[List[st.TextLine], ScreenGeometry]:
        image, geometry = self._scope_image(Quartz.kCGWindowImageDefault)
        try:
            return st.recognize_text(image), geometry
        except st.TextRecognitionError as error:
            raise ToolError(str(error), "OCR_FAILED", True) from error

    @_screen_input
    def cua_click_text(self, text: str, near: Optional[List[int]]) -> str:
        _require_accessibility()
        if not isinstance(text, str) or not text.strip():
            raise ToolError("Tıklanacak metin boş olamaz.", "INVALID_TEXT", False)
        near_point = parse_point(near) if near is not None else None
        lines, geometry = self._screen_text()
        matches = st.find_text_matches(lines, text)
        chosen, tied = st.select_text_match(matches, near_point)
        if tied:
            candidates = " · ".join(
                f"{index}. {match['line_text']!r} @{st.box_center(match['box'])}"
                for index, match in enumerate(tied[:TEXT_CANDIDATE_LIMIT], start=1)
            )
            raise ToolError(
                f"{text!r} ekranda {len(tied)} yerde görünüyor; tıklanmadı. "
                f"Hedeflediğin adayın yakınındaki noktayı near=[x, y] ile ver: {candidates}",
                "TEXT_AMBIGUOUS",
                True,
            )
        if chosen is None:
            similar = st.similar_texts(lines, text, TEXT_CANDIDATE_LIMIT)
            note = f" Benzer görünür metinler: {' · '.join(similar)}." if similar else ""
            raise ToolError(
                f"{text!r} ekranda bulunamadı (OCR, {len(lines)} satır).{note} "
                "Metin ekran dışındaysa önce cua_scroll ile kaydır; metni olmayan hedefte noktaya tıkla.",
                "TEXT_NOT_FOUND",
                True,
            )
        x, y = st.box_center(chosen["box"])
        clicked = click_model_point(x, y, "left", geometry)
        warning = ""
        if not st.same_text(chosen["line_text"], text):
            warning = (
                f" DİKKAT: birebir eşleşme yok; tıklanan satır {chosen['line_text']!r}, "
                f"aranan {text!r}. Hedef bu değilse kaydırıp tam metinle tekrar dene."
            )
        return f"{clicked} Metin: {chosen['line_text']!r}.{warning}"

    @_screen_input
    def cua_scroll(self, point: List[int], direction: str, amount: int) -> str:
        _require_accessibility()
        x, y = parse_point(point)
        if direction not in ("up", "down", "left", "right"):
            raise ToolError(
                f"direction up/down/left/right olmalı; alınan: {direction!r}",
                "INVALID_SCROLL",
                False,
            )
        if isinstance(amount, bool) or not isinstance(amount, int) or not 1 <= amount <= MODEL_SCREEN_SIZE:
            raise ToolError(
                f"amount 1-{MODEL_SCREEN_SIZE} arası tamsayı olmalı; alınan: {amount!r}",
                "INVALID_SCROLL",
                False,
            )
        geometry = self._input_geometry()
        move_model_point(x, y, geometry)
        vertical = direction in ("up", "down")
        sign = 1.0 if direction in ("down", "right") else -1.0
        distance = (
            sign
            * amount
            * (geometry["point_height"] if vertical else geometry["point_width"])
            / MODEL_SCREEN_SIZE
        )
        before = self._scope_gray()
        post_scroll(0.0 if vertical else distance, distance if vertical else 0.0)
        after = self._settled_gray(before)
        anchor = (
            min(after.shape[1] - 1, x * after.shape[1] // MODEL_SCREEN_SIZE),
            min(after.shape[0] - 1, y * after.shape[0] // MODEL_SCREEN_SIZE),
        )
        region = changed_region(before, after, anchor, SCROLL_MOVED_RATIO)
        if region is None:
            return (
                f"({x}, {y}) noktasında {direction} kaydırma denendi; içerik KAYMADI: "
                "bu yönde içerik bitti (sayfa/panel sonu) veya burası kaydırılabilir bir alan değil."
            )
        left = region[0] * MODEL_SCREEN_SIZE // after.shape[1]
        top = region[1] * MODEL_SCREEN_SIZE // after.shape[0]
        right = region[2] * MODEL_SCREEN_SIZE // after.shape[1]
        bottom = region[3] * MODEL_SCREEN_SIZE // after.shape[0]
        return (
            f"({x}, {y}) noktasında {direction} yönünde {amount} birim kaydırıldı; içerik kaydı "
            f"(değişen bölge ({left},{top})-({right},{bottom})). "
            "Sona gelindiğini ancak kaymayan bir kaydırma kanıtlar."
        )

    @_screen_input
    def cua_read_scrollable(self, point: List[int], max_pages: int) -> str:
        """İmlecin altındaki kaydırılabilir bölgeyi baştan sona OCR ile okur."""
        _require_accessibility()
        x, y = parse_point(point)
        if (
            isinstance(max_pages, bool)
            or not isinstance(max_pages, int)
            or not 1 <= max_pages <= READ_MAX_PAGES
        ):
            raise ToolError(
                f"max_pages 1-{READ_MAX_PAGES} arası tamsayı olmalı; alınan: {max_pages!r}",
                "INVALID_SCROLL",
                False,
            )
        geometry = self._input_geometry()
        move_model_point(x, y, geometry)
        self._scroll_to_start(geometry)
        first_lines, _first_geometry = self._screen_text()

        before = self._scope_gray()
        first_step_points = READ_FIRST_STEP_SHARE * geometry["point_height"]
        post_scroll(0.0, first_step_points)
        after = self._settled_gray(before)
        anchor = (
            min(after.shape[1] - 1, x * after.shape[1] // MODEL_SCREEN_SIZE),
            min(after.shape[0] - 1, y * after.shape[0] // MODEL_SCREEN_SIZE),
        )
        bounds = changed_region(before, after, anchor, SCROLL_MOVED_RATIO)
        if bounds is None:
            body = "\n".join(line["text"] for line in first_lines)
            return _clip(
                "Bölge kaydırılamadı: içerik zaten tamamen görünüyor ya da bu nokta "
                f"kaydırılabilir bir alanda değil. Ekrandaki tüm metin ({len(first_lines)} satır):\n{body}",
                READ_TEXT_LIMIT,
            )

        height, width = after.shape[:2]
        region: st.TextBox = {
            "left": bounds[0] * MODEL_SCREEN_SIZE / width,
            "top": bounds[1] * MODEL_SCREEN_SIZE / height,
            "width": (bounds[2] - bounds[0]) * MODEL_SCREEN_SIZE / width,
            "height": (bounds[3] - bounds[1]) * MODEL_SCREEN_SIZE / height,
        }
        text_lines, cut_tail = st.split_bottom_cut(
            st.lines_within(first_lines, region), region, READ_EDGE_UNITS
        )

        # Bölgeyi bulmak için yapılan ilk küçük kaydırmanın metnini de birleştir.
        # Böylece ilk tam kaydırma adımı arada satır atlamaz.
        probe_lines, _probe_geometry = self._screen_text()
        probe_page, cut_tail = st.split_bottom_cut(
            st.without_top_cut(
                st.lines_within(probe_lines, region), region, READ_EDGE_UNITS
            ),
            region,
            READ_EDGE_UNITS,
        )
        text_lines, _probe_overlap = st.merge_page_lines(text_lines, probe_page)

        step_points = (
            READ_STEP_SHARE
            * region["height"]
            * geometry["point_height"]
            / MODEL_SCREEN_SIZE
        )
        scrolled_points = first_step_points
        scrolls = 1
        gaps = 0
        reached_end = False

        while scrolls < max_pages:
            before = self._scope_gray()
            post_scroll(0.0, step_points)
            after = self._settled_gray(before)
            scrolled_points += step_points

            bounds_now = changed_region(before, after, anchor, SCROLL_MOVED_RATIO)
            if bounds_now is None:
                reached_end = True
                break

            top_px = max(0, round(region["top"] * height / MODEL_SCREEN_SIZE))
            bottom_px = min(
                height,
                round((region["top"] + region["height"]) * height / MODEL_SCREEN_SIZE),
            )
            left_px = max(0, round(region["left"] * width / MODEL_SCREEN_SIZE))
            right_px = min(
                width,
                round((region["left"] + region["width"]) * width / MODEL_SCREEN_SIZE),
            )
            if (
                bottom_px > top_px
                and right_px > left_px
                and frame_change_ratio(
                    before[top_px:bottom_px, left_px:right_px],
                    after[top_px:bottom_px, left_px:right_px],
                    SCROLL_PIXEL_DELTA,
                )
                < SCROLL_MOVED_RATIO
            ):
                reached_end = True
                break

            scrolls += 1
            page_lines, _page_geometry = self._screen_text()
            page, cut_tail = st.split_bottom_cut(
                st.without_top_cut(
                    st.lines_within(page_lines, region), region, READ_EDGE_UNITS
                ),
                region,
                READ_EDGE_UNITS,
            )
            merged, overlap = st.merge_page_lines(text_lines, page)
            if overlap == 0 and text_lines and page:
                gaps += 1
                merged = text_lines + [READ_GAP_MARKER] + page
                step_points /= 2
            text_lines = merged

        if cut_tail:
            text_lines, _tail_overlap = st.merge_page_lines(text_lines, cut_tail)

        restore_before = self._scope_gray()
        post_scroll(0.0, -(scrolled_points + geometry["point_height"]))
        self._settled_gray(restore_before)

        end_note = (
            "sona ulaşıldı (son kaydırmada içerik kaymadı)"
            if reached_end
            else f"SONA ULAŞILMADI: {max_pages} sayfa sınırı doldu, devamı var"
        )
        center_x, center_y = st.box_center(region)
        gap_note = (
            f" {gaps} yerde örtüşme bulunamadı ({READ_GAP_MARKER}): "
            "arada satır atlanmış olabilir."
            if gaps
            else ""
        )
        header = (
            f"Okunan bölge merkezi ({center_x},{center_y}), {scrolls} kaydırma, "
            f"{len(text_lines)} satır; {end_note}.{gap_note} Panel yeniden başına döndürüldü.\n"
        )
        return _clip(header + "\n".join(text_lines), READ_TEXT_LIMIT)

    @_screen_input
    def cua_get_app(self, app_name: str) -> str:
        return self.cua.get_app(app_name)

    def cua_get_ax_state(self, app_name: str) -> str:
        return self.cua.list_elements(app_name, self._input_geometry())

    @_screen_input
    def cua_click(self, app_name: str, element_id: int) -> str:
        return self.cua.click_element(app_name, element_id)

    @_screen_input
    def run_action_sequence(self, steps: List[ActionStep]) -> str:
        if isinstance(steps, str):
            try:
                steps = json.loads(steps)
            except json.JSONDecodeError as error:
                raise ToolError(
                    "steps bir JSON nesne listesi olmalı.",
                    "INVALID_ACTION_PARAMS",
                    False,
                ) from error
        if not isinstance(steps, list):
            raise ToolError("steps bir eylem nesnesi listesi olmalı.", "INVALID_ACTION_PARAMS", False)
        _require_accessibility()
        geometry = self._input_geometry()
        executed: List[str] = []
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                raise ToolError(
                    f"Eylem {index} nesne olmalı; alınan tür: {type(step).__name__}. "
                    f"Tamamlanan adımlar: {executed}",
                    "INVALID_ACTION_PARAMS",
                    False,
                )
            try:
                executed.append(_run_action_step(step, geometry))
            except (KeyError, TypeError, ValueError) as error:
                raise ToolError(
                    f"Eylem {index} ({step.get('action')}) geçersiz parametrelerle başarısız: {error}. "
                    f"Tamamlanan adımlar: {executed}",
                    "INVALID_ACTION_PARAMS",
                    False,
                ) from error
            except ToolError as error:
                raise ToolError(
                    f"Eylem {index} başarısız: {error}. Tamamlanan adımlar: {executed}",
                    error.code,
                    error.recoverable,
                ) from error
        return "Eylem dizisi tamamlandı:\n" + "\n".join(executed)

    def execute_js(self, code: str) -> str:
        if not isinstance(code, str):
            raise ToolError("JavaScript kodu metin olmalı.", "JS_INVALID", False)
        script: str = code
        label: Optional[str] = None
        argument: Optional[str] = None
        if code.startswith("// omni:save "):
            header, separator, script = code.partition("\n")
            label = header[len("// omni:save "):].strip()
            if (
                not separator
                or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", label)
                or not script.strip()
                or len(script.encode("utf-8")) > 16000
            ):
                raise ToolError("Geçici araç adı/kodu geçersiz (en çok 16 KB).", "JS_INVALID", False)
            if label not in self._task_js and len(self._task_js) >= 5:
                raise ToolError("Bir görevde en çok 5 geçici araç tutulur.", "JS_LIMIT", False)
        elif code.startswith("// omni:run "):
            header, separator, payload = code.partition("\n")
            name = header[len("// omni:run "):].strip()
            if name not in self._task_js:
                raise ToolError(f"Bu görevde '{name}' adlı geçici araç yok.", "JS_UNKNOWN", False)
            script = self._task_js[name]
            if separator and payload.strip():
                if len(payload.encode("utf-8")) > 4000:
                    raise ToolError("Geçici araç girdisi 4 KB sınırını aşıyor.", "JS_INVALID", False)
                try:
                    parsed = json.loads(payload)
                except json.JSONDecodeError as error:
                    raise ToolError(f"Geçici araç girdisi JSON olmalı: {error}", "JS_INVALID", False) from error
                argument = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
        temp_path: Optional[Path] = None
        input_path: Optional[Path] = None
        preload_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".js", prefix="omni_", encoding="utf-8", delete=False,
            ) as source:
                temp_path = Path(source.name)
                source.write(script)
            command = ["node", str(temp_path)]
            if argument is not None:
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".json", prefix="omni_input_", encoding="utf-8", delete=False,
                ) as input_file:
                    input_path = Path(input_file.name)
                    input_file.write(argument)
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".js", prefix="omni_preload_", encoding="utf-8", delete=False,
                ) as preload_file:
                    preload_path = Path(preload_file.name)
                    preload_file.write("process.argv[2] = require('fs').readFileSync(process.argv[2], 'utf8');\n")
                command = ["node", "--require", str(preload_path), str(temp_path), str(input_path)]
            try:
                returncode, stdout, stderr = run_streaming_process(command, False, JS_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired as error:
                raise ToolError(f"JS {JS_TIMEOUT_SECONDS:.0f} saniyede tamamlanmadı.", "JS_TIMEOUT", True) from error
            if returncode != 0:
                raise ToolError(
                    f"JS çalıştırma başarısız: çıkış={returncode}, stderr={_clip(stderr.strip(), 1000)}",
                    "JS_EXIT",
                    True,
                )
            if label is not None:
                self._task_js[label] = script
            note = f"Geçici araç '{label}' kaydedildi.\n" if label is not None else ""
            return note + f"STDOUT: {_clip(stdout, SHELL_STDOUT_LIMIT)}\nSTDERR: {_clip(stderr, SHELL_STDERR_LIMIT)}\nÇıkış Kodu: 0"
        finally:
            for path in (temp_path, input_path, preload_path):
                if path is not None and path.exists():
                    path.unlink()

    def write_file(self, path: str, content: str) -> str:
        """Dosyaya tam içeriği atomik yazar."""
        return write_file_content(path, content, self._read_full)

    def edit_file(self, path: str, old_text: str, new_text: str) -> str:
        """Benzersiz metni değiştirir; tam içeriği write_file ile güvenle yazar."""
        return edit_file_content(path, old_text, new_text, self._read_full, self.write_file)

    def read_file(self, path: str) -> str:
        """Dosya okur ve model için kısaltılmış içeriği döner."""
        return read_file_content(path, self._read_full)

    def _read_full(self, path: str) -> str:
        """Düzenli UTF-8 dosyasını boyut sınırıyla okur."""
        return read_full_file(path)

    async def close_browser(self) -> None:
        """Tarayıcı kaynaklarını temizler."""
        await self._headless_browser.close()


class _ToolsModule(_py_types.ModuleType):
    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        for submod in (browser, filesystem, gui_input, screen, system, tool_types):
            if hasattr(submod, name):
                try:
                    setattr(submod, name, value)
                except Exception:
                    pass


sys.modules[__name__].__class__ = _ToolsModule
