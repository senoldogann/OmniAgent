"""Private, bounded source receipts. Tool data never grants execution permissions.

The JSON payload is portable across report queues; this store is the authoritative
copy for explicit recovery. Delivery is pending until an adapter confirms it.
"""
from __future__ import annotations

import json
import os
import re
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, NotRequired, TypedDict
from uuid import UUID, uuid4

from omniagent import config
from omniagent.core.observation_filter import (
    SENSITIVE_PLACEHOLDER, TYPED_ECHO_LIMIT, TYPED_TEXT_TOOLS, is_sensitive_key,
    mask_sensitive_text, mask_typed_arguments, typed_texts,
)

VERSION = 1
MAX_RUN_BYTES = 2 * 1024 * 1024
MAX_OBSERVATION_BYTES = 256 * 1024
MAX_SOURCE_REFERENCE_BYTES = 8192
# Reserve metadata room for overflow receipts without evicting earlier evidence.
_METADATA_RESERVE = 16384
_CROP = re.compile(r"…\[kısaltıldı, toplam \d+ karakter\]|\[(?:truncated|output truncated)\]|output.{0,20}truncated|…\[[^\]\n]*kırpıldı\]|…liste[^\n]*kısaltıldı", re.I)


class RequestContract(TypedDict):
    subject: str
    route: Literal["chat", "investigate", "task"]
    required_fields: list[str]
    needs_observation: bool


class SourceObservation(TypedDict):
    tool: str
    source_type: str
    source_reference: str
    observed_at: str
    text: str
    ok: bool
    status: str
    complete: bool
    completeness: str
    artifact_path: NotRequired[str]


class EvidenceBundle(TypedDict):
    version: int
    run_id: str
    contract: RequestContract
    created_at: str
    observations: list[SourceObservation]
    complete: bool
    limitations: list[str]
    delivery_status: Literal["pending", "unknown", "delivered"]
    delivered_at: str | None
    omitted_observations: int


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sanitize_text(text: str) -> str:
    """Mask the entire result before clipping, persistence or model presentation."""
    return mask_sensitive_text(config.redact(text))


def sanitize_presentation_arguments(tool: str, arguments: str) -> str:
    """A JSON-valid presentation copy; execution and argument tags keep originals."""
    try:
        parsed = json.loads(mask_typed_arguments(tool, arguments))
        def clean(value):
            if isinstance(value, dict):
                return {key: SENSITIVE_PLACEHOLDER if is_sensitive_key(key) else clean(item)
                        for key, item in value.items()}
            if isinstance(value, list):
                return [clean(item) for item in value]
            return sanitize_text(value) if isinstance(value, str) else value
        cleaned = clean(parsed)
        # Preserve exact formatting for ordinary calls and provider regressions.
        if cleaned == json.loads(arguments):
            return arguments
        return json.dumps(cleaned, ensure_ascii=False)
    except (ValueError, TypeError, RecursionError):
        # Incomplete/invalid JSON may contain a partial secret that patterns cannot identify.
        return json.dumps({"presentation": SENSITIVE_PLACEHOLDER})


def sanitize_tool_text(tool: str, arguments: str, text: str) -> str:
    """Mask tool text, including arbitrary echoes of that call's typed input."""
    if tool in TYPED_TEXT_TOOLS:
        try:
            parsed = json.loads(arguments)
            values = typed_texts(tool, parsed) if isinstance(parsed, dict) else []
        except (ValueError, RecursionError):
            values = []
        for value in sorted(set(values), key=len, reverse=True):
            if value:
                # Standard GUI tools echo at most 80 chars; errors may echo the full value.
                for echo in (value, value[:TYPED_ECHO_LIMIT]):
                    text = text.replace(echo, SENSITIVE_PLACEHOLDER)
                    text = text.replace(json.dumps(echo, ensure_ascii=False)[1:-1], SENSITIVE_PLACEHOLDER)
    return sanitize_text(text)


def validate_run_id(run_id: str) -> str:
    try:
        valid = str(UUID(run_id))
    except (ValueError, AttributeError, TypeError) as error:
        raise ValueError("Invalid evidence run UUID") from error
    if valid != run_id:
        raise ValueError("Evidence run UUID must be canonical")
    return valid


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Evidence timestamps require a timezone")
    return parsed


def new_evidence_bundle(contract: RequestContract, run_id: str | None = None) -> EvidenceBundle:
    return {"version": VERSION, "run_id": validate_run_id(run_id or str(uuid4())),
            "contract": contract, "created_at": utc_now(), "observations": [],
            "complete": True, "limitations": [], "delivery_status": "pending",
            "delivered_at": None, "omitted_observations": 0}


def mark_incomplete(bundle: EvidenceBundle, reason: str) -> None:
    bundle["complete"] = False
    if reason not in bundle["limitations"]:
        bundle["limitations"].append(sanitize_text(reason))


def _encoded(bundle: EvidenceBundle) -> bytes:
    return json.dumps(bundle, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _clip_bytes(text: str, maximum: int) -> str:
    return text.encode("utf-8")[:maximum].decode("utf-8", errors="ignore")


class EvidenceStore:
    """One selected state directory, no symlink following, atomic private writes."""
    def __init__(self, state_file: str | Path):
        self.directory = Path(state_file).expanduser().absolute().parent / "conversation_evidence"
        self.directory.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        with self._directory_fd() as fd:
            os.fchmod(fd, 0o700)

    def _directory_fd(self):
        from contextlib import contextmanager
        @contextmanager
        def opened():
            try:
                fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            except OSError as error:
                raise ValueError("Evidence directory is not a safe directory") from error
            try:
                yield fd
            finally:
                os.close(fd)
        return opened()

    def path_for(self, run_id: str) -> Path:
        return self.directory / f"{validate_run_id(run_id)}.json"

    def _artifact_name(self, run_id: str, index: int) -> str:
        return f"{validate_run_id(run_id)}-{index}.txt"

    def _read(self, name: str, maximum: int) -> bytes:
        with self._directory_fd() as directory_fd:
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
            except OSError as error:
                if isinstance(error, FileNotFoundError):
                    raise
                raise ValueError("Unsafe evidence file") from error
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
                    raise ValueError("Invalid evidence file size/type")
                return stream.read(maximum + 1)

    def _write(self, name: str, payload: bytes, *, create_only: bool = False) -> None:
        with self._directory_fd() as directory_fd:
            try:
                info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("Unsafe evidence destination")
            except FileNotFoundError:
                pass
            temporary = f".{uuid4()}.tmp"
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory_fd)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                if create_only:
                    os.link(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd, follow_symlinks=False)
                else:
                    os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                os.fsync(directory_fd)
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass

    def _validate(self, bundle: Any, run_id: str | None = None) -> EvidenceBundle:
        if not isinstance(bundle, dict) or type(bundle.get("version")) is not int or bundle["version"] != VERSION:
            raise ValueError("Unsupported evidence version")
        identity = validate_run_id(bundle.get("run_id"))
        if run_id is not None and identity != run_id:
            raise ValueError("Evidence run identity mismatch")
        try:
            _timestamp(bundle["created_at"])
            contract = bundle["contract"]
            if (not isinstance(contract["subject"], str) or contract["route"] not in ("chat", "investigate", "task")
                or type(contract["needs_observation"]) is not bool
                or not isinstance(contract["required_fields"], list)
                or not all(isinstance(field, str) for field in contract["required_fields"])):
                raise ValueError("Invalid evidence request contract")
            if (type(bundle["complete"]) is not bool or not isinstance(bundle["limitations"], list)
                or not all(isinstance(item, str) for item in bundle["limitations"])
                or bundle["delivery_status"] not in ("pending", "unknown", "delivered")
                or type(bundle["omitted_observations"]) is not int or bundle["omitted_observations"] < 0
                or not isinstance(bundle["observations"], list)):
                raise ValueError("Invalid evidence lifecycle")
            if bundle["delivered_at"] is not None:
                _timestamp(bundle["delivered_at"])
            if bundle["delivery_status"] == "delivered" and bundle["delivered_at"] is None:
                raise ValueError("Delivery requires confirmation time")
            for index, observation in enumerate(bundle["observations"]):
                _timestamp(observation["observed_at"])
                if (not all(isinstance(observation[field], str) for field in
                    ("tool", "source_type", "source_reference", "text", "status", "completeness"))
                    or type(observation["ok"]) is not bool or type(observation["complete"]) is not bool
                    or len(_encoded(observation)) > MAX_OBSERVATION_BYTES
                    or len(observation["source_reference"].encode()) > MAX_SOURCE_REFERENCE_BYTES):
                    raise ValueError("Invalid source observation")
                artifact = observation.get("artifact_path")
                if artifact is not None and artifact != str(self.directory / self._artifact_name(identity, index)):
                    raise ValueError("Invalid evidence artifact path")
        except (KeyError, TypeError, AttributeError) as error:
            raise ValueError("Malformed evidence payload") from error
        return bundle

    def _artifact_bytes(self, bundle: EvidenceBundle) -> int:
        total = 0
        for observation in bundle["observations"]:
            if observation.get("artifact_path"):
                data = self._read(Path(observation["artifact_path"]).name, MAX_RUN_BYTES)
                total += len(data)
        return total

    def save(self, bundle: EvidenceBundle, *, create_only: bool = False) -> None:
        self._validate(bundle)
        # Defence in depth for callers modifying a report before persistence.
        bundle["contract"]["subject"] = sanitize_text(bundle["contract"]["subject"])
        bundle["limitations"] = [sanitize_text(item) for item in bundle["limitations"]]
        for observation in bundle["observations"]:
            for field in ("text", "source_reference", "status"):
                observation[field] = sanitize_text(observation[field])
        payload = _encoded(bundle)
        if len(payload) + self._artifact_bytes(bundle) > MAX_RUN_BYTES:
            raise ValueError("Evidence run exceeds storage budget; continue with a narrower request")
        self._write(self.path_for(bundle["run_id"]).name, payload, create_only=create_only)

    def create(self, contract: RequestContract, run_id: str | None = None) -> EvidenceBundle:
        bundle = new_evidence_bundle(contract, run_id)
        self.save(bundle, create_only=True)
        return bundle

    def load(self, run_id: str) -> EvidenceBundle:
        validate_run_id(run_id)
        try:
            bundle = self._validate(json.loads(self._read(self.path_for(run_id).name, MAX_RUN_BYTES)), run_id)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Malformed evidence JSON") from error
        if len(_encoded(bundle)) + self._artifact_bytes(bundle) > MAX_RUN_BYTES:
            raise ValueError("Evidence run exceeds storage budget")
        return bundle

    def capture(self, bundle: EvidenceBundle, tool: str, result: Mapping[str, Any], *,
                source_reference: str = "", arguments: str = "") -> SourceObservation | None:
        """Capture actual returned text, including failures, before archive limits."""
        ok = bool(result.get("ok"))
        text = str(result.get("result", ""))
        if not ok:
            failure = f"{result.get('error_type', 'Error')}: {result.get('error', result.get('code', 'unavailable'))}"
            text = f"{failure}\n{text}".rstrip()
        text = sanitize_tool_text(tool, arguments, text)
        reference = sanitize_text(source_reference) if source_reference else sanitize_presentation_arguments(tool, arguments) if arguments else ""
        reference_cropped = len(reference.encode()) > MAX_SOURCE_REFERENCE_BYTES
        if reference_cropped:
            reference = _clip_bytes(reference, MAX_SOURCE_REFERENCE_BYTES)
            mark_incomplete(bundle, "Kaynak adresi veya işlem metni kayıt sınırını aştığı için eksik; daha dar bir istekle devam edin.")
        snippet = tool == "web_search"
        cropped = bool(_CROP.search(text)) or bool(result.get("truncated")) or result.get("complete") is False
        complete = not cropped and not snippet and not reference_cropped
        observation: SourceObservation = {
            "tool": tool, "source_type": "web" if tool.startswith("web_") else "tool",
            "source_reference": reference, "observed_at": utc_now(), "text": text,
            "ok": ok, "status": "ok" if ok else sanitize_text(str(result.get("code") or "error")),
            "complete": complete, "completeness": "snippet" if snippet else "cropped" if cropped else "full",
        }
        if not ok:
            mark_incomplete(bundle, "Bir kaynak işlemi başarısız oldu; hata ayrıntısı aşağıda korunuyor.")
        if not complete:
            mark_incomplete(bundle, "Kaynak bilgisi kısaltılmış veya arama özeti olduğu için eksik; kaynağı okuyarak ya da daha dar bir istekle devam edin.")
        artifact_payload = None
        if len(_encoded(observation)) > MAX_OBSERVATION_BYTES:
            # Include metadata and JSON escaping in the per-observation byte cap.
            def excerpt(kept: int) -> str:
                head = (kept * 3) // 4
                tail = kept - head
                marker = f"\n… [orta bölüm kırpıldı: {len(text) - kept} karakter] …\n"
                return text[:head] + marker + (text[-tail:] if tail else "")
            low, high = 0, len(text)
            while low < high:
                middle = (low + high + 1) // 2
                if len(_encoded({**observation, "text": excerpt(middle)})) <= MAX_OBSERVATION_BYTES - 512:
                    low = middle
                else:
                    high = middle - 1
            observation["text"] = excerpt(low)
            observation["complete"] = False
            observation["completeness"] = "bounded"
            mark_incomplete(bundle, "Kaynak metni kayıt sınırını aşıyor; okunabilir dosyadan inceleyin veya daha dar bir istekle devam edin.")
            artifact_payload = text.encode()
        # Budget checks include JSON escaping, all observations and existing artifacts.
        used_artifacts = self._artifact_bytes(bundle)
        if artifact_payload is not None:
            name = self._artifact_name(bundle["run_id"], len(bundle["observations"]))
            observation["artifact_path"] = str(self.directory / name)
            candidate = {**bundle, "observations": [*bundle["observations"], observation]}
            if len(_encoded(candidate)) + used_artifacts + len(artifact_payload) <= MAX_RUN_BYTES - _METADATA_RESERVE:
                self._write(name, artifact_payload)
            else:
                observation.pop("artifact_path")
                artifact_payload = None
                mark_incomplete(bundle, "Kaynağın tamamı kayıt bütçesini aşıyor; daha dar bir istekle devam edin.")
        candidate = {**bundle, "observations": [*bundle["observations"], observation]}
        if len(_encoded(candidate)) + used_artifacts + len(artifact_payload or b"") > MAX_RUN_BYTES - _METADATA_RESERVE:
            # Keep earlier receipts; a bounded counter explicitly records lost captures.
            bundle["omitted_observations"] += 1
            mark_incomplete(bundle, "Kaynak kayıt bütçesi doldu; ek gözlemler kaydedilemedi. Daha dar bir istekle devam edin.")
            self.save(bundle)
            return None
        bundle["observations"].append(observation)
        self.save(bundle)
        return observation

    def mark_delivered(self, run_id: str, *, delivered_at: str | None = None) -> EvidenceBundle:
        bundle = self.load(run_id)
        bundle["delivery_status"] = "delivered"
        bundle["delivered_at"] = delivered_at or utc_now()
        self.save(bundle)
        return bundle

    def cleanup(self, *, now: datetime | None = None, maximum: int = 128) -> int:
        """Bounded seven-day cleanup only after positive delivery confirmation."""
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=7)
        removed = 0
        with self._directory_fd() as fd:
            names = sorted(os.listdir(fd))
        checked = 0
        for name in names:
            if not name.endswith(".json"):
                continue
            checked += 1
            if checked > maximum:
                break
            try:
                bundle = self.load(name[:-5])
            except (OSError, ValueError):
                continue
            if bundle["delivery_status"] != "delivered" or _timestamp(bundle["delivered_at"]) >= cutoff:
                continue
            with self._directory_fd() as fd:
                for observation in bundle["observations"]:
                    if observation.get("artifact_path"):
                        os.unlink(Path(observation["artifact_path"]).name, dir_fd=fd)
                os.unlink(name, dir_fd=fd)
            removed += 1
        return removed
