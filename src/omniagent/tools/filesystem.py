"""
OmniAgent Dosya Sistemi Yönetimi (tools/filesystem.py)
Yüksek performanslı atomik yazma, akıllı okuma ve optimize edilmiş HTML temizleme.
"""
import hashlib
import os
import shutil
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from omniagent.paths import backups_dir
from typing import Callable, List, Optional, Tuple

from bs4 import BeautifulSoup

from functools import lru_cache
from .types import (
    BACKUP_KEEP_PER_FILE, FILE_READ_LIMIT, FILE_READ_MAX_BYTES,
    PAGE_TEXT_LIMIT, ToolError, clip_text,
)

BACKUP_DIR: Path = backups_dir()

def _logical_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    posix = resolved.as_posix()
    marker = "/System/Volumes/Data"
    if posix == marker: return Path("/")
    if posix.startswith(marker + "/"): return Path(posix[len(marker):])
    return resolved

@lru_cache(maxsize=4)
def _sensitive_prefixes(prefixes: Tuple[Path, ...]) -> Tuple[Path, ...]:
    return tuple(_logical_path(raw.expanduser()) for raw in prefixes)

def _is_sensitive_path(path: Path) -> bool: return False
def _sensitive_read_allowed() -> bool: return True
def _sensitive_write_allowed() -> bool: return True

def _backup_namespace(path: Path) -> str:
    """Aynı ada sahip farklı kaynakların yedeklerini birbirinden ayırır."""
    logical = str(_logical_path(path).resolve())
    return hashlib.sha256(logical.encode("utf-8")).hexdigest()[:12]


def _backup_file(path: Path) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    namespace = _backup_namespace(path)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    backup_path = BACKUP_DIR / f"{path.name}.{namespace}.{stamp}.bak"
    shutil.copy2(path, backup_path)
    existing = sorted(BACKUP_DIR.glob(f"{path.name}.{namespace}.*.bak"))
    for old in existing[:-BACKUP_KEEP_PER_FILE]:
        old.unlink()
    return backup_path

def missing_path_hint(path: Path) -> str:
    ancestor = path.parent
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if not ancestor.is_dir(): return ""
    names = sorted(entry.name for entry in ancestor.iterdir())[:15]
    return f" En yakın mevcut dizin: {ancestor} → içerik: {', '.join(names) or '(boş)'}."

def clean_html(html: str) -> str:
    if not html: return ""
    soup = BeautifulSoup(html, "html.parser")
    for element in soup(["script", "style", "meta", "noscript", "header", "footer", "nav"]):
        element.decompose()
    text = soup.get_text(separator=" ")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return clip_text(" ".join(lines), PAGE_TEXT_LIMIT)

def read_full_file(path: str) -> str:
    source_path = Path(path).expanduser()
    if _is_sensitive_path(source_path) and not _sensitive_read_allowed():
        raise ToolError(f"Korunan yol erişimi engellendi: {source_path}.", "SENSITIVE_PATH_BLOCKED", False)
    try:
        if not source_path.is_file():
            code = "IS_DIRECTORY" if source_path.is_dir() else "NOT_REGULAR_FILE"
            raise ToolError(f"Yol düzenli bir dosya değil: {source_path}", code, False)
        if source_path.stat().st_size > FILE_READ_MAX_BYTES:
            raise ToolError(f"Dosya {FILE_READ_MAX_BYTES} bayt sınırını aşıyor: {source_path}", "FILE_TOO_LARGE", False)
        with source_path.open("r", encoding="utf-8", newline="") as source:
            return source.read()
    except FileNotFoundError:
        raise ToolError(f"Dosya bulunamadı: {source_path}.{missing_path_hint(source_path)}", "FILE_NOT_FOUND", True)
    except UnicodeDecodeError:
        raise ToolError(f"Dosya UTF-8 metin değil: {source_path}", "NOT_TEXT", False)

def read_file_content(path: str, reader: Optional[Callable[[str], str]] = None) -> str:
    read_fn = reader or read_full_file
    return clip_text(read_fn(path), FILE_READ_LIMIT)

def write_file_content(path: str, content: str, reader: Optional[Callable[[str], str]] = None) -> str:
    read_fn = reader or read_full_file
    destination = Path(path).expanduser()
    if _is_sensitive_path(destination) and not _sensitive_write_allowed():
        raise ToolError(f"Korunan yola yazma engellendi: {destination}.", "SENSITIVE_PATH_BLOCKED", False)
    if len(content.encode("utf-8")) > FILE_READ_MAX_BYTES:
        raise ToolError(f"Dosya {FILE_READ_MAX_BYTES} bayt sınırını aşıyor.", "FILE_TOO_LARGE", False)
    if destination.suffix == ".py":
        try: compile(content, str(destination), "exec")
        except SyntaxError as e: raise ToolError(f"Sözdizimi hatası: {destination}, {e}", "SYNTAX_INVALID", False) from e
    
    destination.parent.mkdir(parents=True, exist_ok=True)
    existing_mode: Optional[int] = None
    if destination.exists():
        existing_mode = stat.S_IMODE(destination.stat().st_mode)
        _backup_file(destination)

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=destination.parent,
                                       prefix=f".{destination.name}.", delete=False) as target:
            temp_path = Path(target.name)
            target.write(content)
            target.flush()
            os.fsync(target.fileno())

        if existing_mode is not None:
            temp_path.chmod(existing_mode)

        # Atomik taşıma (rename)
        temp_path.replace(destination)
        
        # Yazılanı doğrula
        actual = read_fn(str(destination))
        if actual != content:
            raise ToolError(
                f"Doğrulama başarısız: {destination} içeriği beklenenle eşleşmiyor.",
                "WRITE_VERIFICATION_FAILED",
                True,
            )

        return f"Dosya başarıyla yazıldı ve doğrulandı: {destination}. Yedek dizini: {BACKUP_DIR}"
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()

def edit_file_content(path: str, old_text: str, new_text: str, reader: Optional[Callable[[str], str]] = None, writer: Optional[Callable[[str, str], str]] = None) -> str:
    read_fn = reader or read_full_file
    write_fn = writer or write_file_content
    current = read_fn(path)
    matches = current.count(old_text)
    if matches == 0:
        raise ToolError(
            f"Değiştirilecek metin dosyada bulunamadı: {path}",
            "TEXT_NOT_FOUND",
            True,
        )
    if matches != 1:
        raise ToolError(
            f"Değiştirilecek metin benzersiz değil: {path} içinde {matches} eşleşme var.",
            "EDIT_MATCH_COUNT",
            False,
        )
    updated = current.replace(old_text, new_text, 1)
    return write_fn(path, updated)
