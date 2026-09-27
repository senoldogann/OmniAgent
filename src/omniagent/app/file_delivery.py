"""Açık yerel dosya hedefleri için görev içi teslim sözleşmesi ve son durum kanıtı."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
from typing import Any, Literal, Optional, Tuple, TypedDict

from omniagent.app.policy import _action_scope, _english_method_command
from omniagent.app.types import ToolCallDraft, ToolResult
from omniagent.tools.types import FILE_READ_MAX_BYTES


MOVE_VERIFY_MAX_BYTES: int = 64 * 1024 * 1024
FileKind = Literal["missing", "file", "symlink", "directory", "unknown"]
Operation = Literal["delete", "move", "edit", "unsupported"]


class FileState(TypedDict):
    """`lstat` ile görülen nesne ve küçük, gizli içeriği taşımayan özeti."""

    kind: FileKind
    digest: Optional[str]
    link_target: Optional[str]


class FileContract(TypedDict):
    """Görev başında sabitlenen dosya hedefi ve ilk durum."""

    kind: Operation
    sources: Tuple[Path, ...]
    before: Tuple[FileState, ...]
    destination: Optional[Path]
    move_argument: Optional[Path]
    destination_before: Optional[FileState]
    reason: str


class FileReceipt(TypedDict):
    """Tam araç çağrısından çıkarılan kısa ve kalıcı olmayan işlem kanıtı."""

    kind: Literal["delete", "move", "edit"]
    sources: Tuple[Path, ...]
    destination: Optional[Path]


_PATH_TOKEN = re.compile(
    r"`(?P<backtick>[^`\r\n]+)`|\"(?P<double>[^\"\r\n]+)\"|"
    r"'(?P<single>[^'\r\n]+)'|"
    r"(?P<bare>(?:/|\.{1,2}/)[^\s,;!?`'\"<>]+|"
    r"[\w.-]+(?:/[\w.-]+)*\.[A-Za-z][A-Za-z0-9_]{0,7})",
    re.UNICODE,
)
_DELETE_VERB = re.compile(r"\b(?:sil|kaldır|delete|remove)\b", re.IGNORECASE)
_MOVE_VERB = re.compile(r"\b(?:taşı|move)\b", re.IGNORECASE)
_EDIT_VERB = re.compile(r"\b(?:düzenle|değiştir|edit|update)\b", re.IGNORECASE)
_FILE_NOUN = re.compile(r"\b(?:dosya\w*|file\w*)\b", re.IGNORECASE)
_REFERENCE_REMOVAL = re.compile(
    r"\bfrom\s+(?:the\s+)?(?:README|list|document)\b|"
    r"\bREADME\b.{0,60}\b(?:içindeki|içinden)\b",
    re.IGNORECASE,
)
_DELETE_PATH_START = re.compile(
    r"\s*:?[\s(\[{]*(?:(?:the\s+)?file\s+at\s+)?", re.IGNORECASE,
)
_MOVE_CONNECTOR = re.compile(r"\s*(?:to\b|->|(?:konumuna|yoluna)\b)\s*", re.IGNORECASE)
_LIST_CONNECTOR = re.compile(r"\s*(?:,|\bve\b|\band\b)\s*", re.IGNORECASE)


def _normal_path(raw: str, cwd: Path) -> Optional[Path]:
    """Yolu leksik olarak çöz; URL, genişleme ve belirsiz özel token'ı reddet."""
    if not raw or raw.startswith("~") or "://" in raw or any(char in raw for char in "\x00\r\n$*?`"):
        return None
    return Path(os.path.abspath(str(cwd / raw)))


def _path_at(text: str, position: int, cwd: Path) -> Optional[Tuple[Path, int]]:
    match = _PATH_TOKEN.match(text, position)
    if match is None:
        return None
    raw = next((part for part in match.groups() if part is not None), "")
    if match.group("bare") is not None:
        # Cümle noktasını dosya adından ayır; ikisi de varsa hedef belirsizdir.
        variants = [raw]
        while variants[-1] and variants[-1][-1] in ".)]}:":
            variants.append(variants[-1][:-1])
        paths = [_normal_path(item, cwd) for item in variants if item]
        existing = [path for path in paths if path is not None and os.path.lexists(path)]
        if len(set(existing)) > 1:
            return None
        selected = existing[0] if existing else paths[-1]
    else:
        selected = _normal_path(raw, cwd)
    return (selected, match.end()) if selected is not None else None


def _paths_after_delete(text: str, cwd: Path) -> Tuple[Path, ...]:
    """`sil: yol ve yol` gibi bitişik yol listesini çıkar."""
    verb = _DELETE_VERB.search(text)
    if verb is None:
        return ()
    start = _DELETE_PATH_START.match(text, verb.end())
    if start is None:
        return ()
    first = _path_at(text, start.end(), cwd)
    if first is None:
        return ()
    paths = [first[0]]
    position = first[1]
    while True:
        connector = _LIST_CONNECTOR.match(text, position)
        if connector is None:
            break
        following = _path_at(text, connector.end(), cwd)
        if following is None:
            break
        paths.append(following[0])
        position = following[1]
    # Belge içindeki yol referansını gerçek dosya silmeye dönüştürme.
    if re.match(r"^[\s,]*from\s+(?!(?:(?:the|my|this|local)\s+)?"
                r"(?:disk|filesystem|computer|machine|mac|hard\s+drive)\b)",
                text[position:], re.IGNORECASE):
        return ()
    return tuple(dict.fromkeys(paths))


def _path_before_delete(text: str, cwd: Path) -> Tuple[Path, ...]:
    beginning = re.match(r"\s*", text)
    parsed = _path_at(text, beginning.end() if beginning is not None else 0, cwd)
    if parsed is None:
        return ()
    if re.match(r"\s*(?:(?:dosyasını|dosyayı|file)\s+)?"
                r"(?:sil|kaldır|delete|remove)\b", text[parsed[1]:], re.IGNORECASE):
        return (parsed[0],)
    return ()


def _delete_paths(goal: str, scope: str, cwd: Path) -> Tuple[Path, ...]:
    command = _english_method_command(goal)
    if command is not None:
        direct = _paths_after_delete(command, cwd)
        if direct:
            return direct
        if not re.fullmatch(
            r"(?:do\s+it|(?:sil|kaldır|delete|remove)\s+it)"
            r"(?:\s+(?:for\s+me|now))*[.!?]?", command, re.IGNORECASE,
        ):
            return ()
        question = goal[:goal.find("?")]
        return _paths_after_delete(question, cwd)
    return _paths_after_delete(scope, cwd) or _path_before_delete(scope, cwd)


def _move_paths(scope: str, cwd: Path) -> Optional[Tuple[Path, Path]]:
    verb = _MOVE_VERB.search(scope)
    if verb is None:
        return None
    start = re.match(r"\s*:?[\s(\[{]*", scope[verb.end():])
    position = verb.end() + (start.end() if start is not None else 0)
    source = _path_at(scope, position, cwd)
    if source is None:
        return None
    connector = _MOVE_CONNECTOR.match(scope, source[1])
    if connector is None:
        return None
    destination = _path_at(scope, connector.end(), cwd)
    return (source[0], destination[0]) if destination is not None else None


def _edit_path(scope: str, cwd: Path) -> Optional[Path]:
    verb = _EDIT_VERB.search(scope)
    if verb is None:
        return None
    after = re.match(r"\s*:?[\s(\[{]*", scope[verb.end():])
    parsed = _path_at(scope, verb.end() + (after.end() if after is not None else 0), cwd)
    if parsed is not None:
        return parsed[0]
    first = _path_at(scope, len(scope) - len(scope.lstrip()), cwd)
    if first is not None and first[1] < verb.start() and re.match(
        r"\s*(?:dosyasını|dosyayı|file)\s*$", scope[first[1]:verb.start()], re.IGNORECASE,
    ):
        return first[0]
    return None


def _snapshot(path: Path, max_bytes: int) -> FileState:
    try:
        before = path.lstat()
    except FileNotFoundError:
        return {"kind": "missing", "digest": None, "link_target": None}
    except OSError:
        return {"kind": "unknown", "digest": None, "link_target": None}
    if stat.S_ISLNK(before.st_mode):
        try:
            return {"kind": "symlink", "digest": None, "link_target": os.readlink(path)}
        except OSError:
            return {"kind": "unknown", "digest": None, "link_target": None}
    if stat.S_ISDIR(before.st_mode):
        return {"kind": "directory", "digest": None, "link_target": None}
    if stat.S_ISREG(before.st_mode) and max_bytes == 0:
        return {"kind": "file", "digest": None, "link_target": None}
    if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
        return {"kind": "unknown", "digest": None, "link_target": None}
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        after = path.lstat()
    except OSError:
        return {"kind": "unknown", "digest": None, "link_target": None}
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
    ):
        return {"kind": "unknown", "digest": None, "link_target": None}
    return {"kind": "file", "digest": digest.hexdigest(), "link_target": None}


def _unsupported(reason: str) -> FileContract:
    return {
        "kind": "unsupported", "sources": (), "before": (), "destination": None,
        "move_argument": None, "destination_before": None, "reason": reason,
    }


def capture_file_contract(goal: str, cwd: Path) -> Optional[FileContract]:
    """Görev başında yalnız açık yerel dosya niyetini bağla; belirsizi işaretle."""
    scope = _action_scope(goal)
    if not scope:
        return None
    if _REFERENCE_REMOVAL.search(goal) and _DELETE_VERB.search(scope):
        return _unsupported("belge içindeki yol referansının değişikliği doğrulanamadı")
    without_urls = re.sub(r"https?://\S+", " ", goal, flags=re.IGNORECASE)
    path_like = _PATH_TOKEN.search(without_urls) is not None
    file_noun = _FILE_NOUN.search(goal) is not None
    if not (path_like or file_noun):
        return None
    if _DELETE_VERB.search(scope):
        paths = _delete_paths(goal, scope, cwd)
        if not paths:
            return _unsupported("silinecek yerel dosya hedefi açıkça belirlenemedi")
        return {
            "kind": "delete", "sources": paths,
            "before": tuple(_snapshot(path, 0) for path in paths),
            "destination": None, "move_argument": None,
            "destination_before": None, "reason": "",
        }
    if _MOVE_VERB.search(scope):
        pair = _move_paths(scope, cwd)
        if pair is None:
            return _unsupported("taşınacak kaynak ve hedef yolu açıkça belirlenemedi")
        source, argument = pair
        target = argument / source.name if argument.is_dir() else argument
        return {
            "kind": "move", "sources": (source,),
            "before": (_snapshot(source, MOVE_VERIFY_MAX_BYTES),),
            "destination": target, "move_argument": argument,
            "destination_before": _snapshot(target, MOVE_VERIFY_MAX_BYTES), "reason": "",
        }
    if _EDIT_VERB.search(scope):
        target = _edit_path(scope, cwd)
        if target is None:
            return _unsupported("düzenlenecek yerel dosya yolu açıkça belirlenemedi")
        return {
            "kind": "edit", "sources": (target,),
            "before": (_snapshot(target, FILE_READ_MAX_BYTES),),
            "destination": None, "move_argument": None,
            "destination_before": None, "reason": "",
        }
    return None


def _shell_receipts(command: str, cwd: Path) -> Tuple[FileReceipt, ...]:
    """Yalnız doğrudan ve genişlemesiz rm/unlink/mv komutunu kanıt say."""
    if re.search(r"[;&|><$*?`\r\n]", command):
        return ()
    try:
        words = shlex.split(command)
    except ValueError:
        return ()
    if not words:
        return ()
    program = Path(words[0]).name
    if program not in {"rm", "unlink", "mv"}:
        return ()
    operands = [word for word in words[1:] if not word.startswith("-")]
    paths = tuple(_normal_path(word, cwd) for word in operands)
    if any(path is None for path in paths):
        return ()
    valid = tuple(path for path in paths if path is not None)
    if program == "rm" and valid:
        return ({"kind": "delete", "sources": valid, "destination": None},)
    if program == "unlink" and len(valid) == 1:
        return ({"kind": "delete", "sources": valid, "destination": None},)
    if program == "mv" and len(valid) == 2:
        return ({"kind": "move", "sources": (valid[0],), "destination": valid[1]},)
    return ()


def receipt_for_call(call: ToolCallDraft, result: ToolResult, cwd: Path) -> Tuple[FileReceipt, ...]:
    """Kırpılmamış çağrı ve başarılı sonuçtan kısa görev içi makbuz üret."""
    if not result.get("ok"):
        return ()
    try:
        arguments: Any = json.loads(call["arguments"] or "{}")
    except (TypeError, ValueError):
        return ()
    if not isinstance(arguments, dict):
        return ()
    if call["name"] == "execute_shell" and isinstance(arguments.get("command"), str):
        return _shell_receipts(arguments["command"], cwd)
    if call["name"] in {"write_file", "edit_file"} and isinstance(arguments.get("path"), str):
        path = _normal_path(arguments["path"], cwd)
        if path is not None:
            return ({"kind": "edit", "sources": (path,), "destination": None},)
    return ()


def file_delivery_gap(contract: FileContract, receipts: Tuple[FileReceipt, ...]) -> Optional[str]:
    """Model beyanından bağımsız olarak hedefin gerçekten teslim edilip edilmediğini ölç."""
    kind = contract["kind"]
    if kind == "unsupported":
        return contract["reason"]
    if kind == "delete":
        for path, initial in zip(contract["sources"], contract["before"], strict=True):
            if initial["kind"] == "missing":
                return f"silme hedefi görev başında zaten yoktu: {path}"
            if initial["kind"] == "unknown":
                return f"silme hedefinin başlangıç durumu okunamadı: {path}"
            if not any(receipt["kind"] == "delete" and path in receipt["sources"] for receipt in receipts):
                return f"hedefe yönelik silme komutu doğrulanmadı: {path}"
            if _snapshot(path, 0)["kind"] != "missing":
                return f"silme hedefi hâlâ mevcut: {path}"
        return None
    if kind == "move":
        source = contract["sources"][0]
        initial = contract["before"][0]
        destination = contract["destination"]
        argument = contract["move_argument"]
        if destination is None or argument is None:
            return "taşıma hedefi belirlenemedi"
        if initial["kind"] not in {"file", "symlink"}:
            return "taşınacak kaynağın başlangıç içeriği doğrulanamadı"
        prior_destination = contract["destination_before"]
        if prior_destination is None or prior_destination["kind"] != "missing":
            return "taşıma hedefi görev başında zaten mevcuttu"
        if not any(
            receipt["kind"] == "move" and receipt["sources"] == (source,)
            and receipt["destination"] == argument for receipt in receipts
        ):
            return "kaynak ve hedefe yönelik taşıma komutu doğrulanmadı"
        if _snapshot(source, 0)["kind"] != "missing":
            return "taşıma kaynağı hâlâ mevcut"
        final = _snapshot(destination, MOVE_VERIFY_MAX_BYTES)
        if final["kind"] != initial["kind"] or (
            final["digest"], final["link_target"]
        ) != (initial["digest"], initial["link_target"]):
            return "taşıma hedefi kaynakla eşleşmiyor"
        return None
    target = contract["sources"][0]
    initial = contract["before"][0]
    if initial["kind"] != "file" or initial["digest"] is None:
        return "düzenleme hedefinin başlangıç içeriği doğrulanamadı"
    if not any(receipt["kind"] == "edit" and target in receipt["sources"] for receipt in receipts):
        return "hedef dosyaya yönelik yazma/düzenleme aracı doğrulanmadı"
    final = _snapshot(target, FILE_READ_MAX_BYTES)
    if final["kind"] != "file" or final["digest"] is None:
        return "düzenleme hedefinin son içeriği doğrulanamadı"
    if final["digest"] == initial["digest"]:
        return "düzenleme hedefinin içeriği değişmedi"
    return None
