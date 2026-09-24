"""Graph üzerinden filtreli okuma, hesap kuralları ve toplu posta taşıma."""
import asyncio
import hashlib
import time
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict
from urllib.parse import quote, urlparse

import httpx

from capabilities import ToolEntry, function_schema
from integration_runtime import IntegrationRuntime, InteractionRequired, read_json, save_json
from outlook_auth import OutlookAuth

GRAPH = "https://graph.microsoft.com/v1.0"
IMMUTABLE = {"Prefer": 'IdType="ImmutableId"'}
FIELDS = "id,subject,from,receivedDateTime,parentFolderId,flag,bodyPreview"


class GraphError(Exception):
    """Model çıktısına token veya ham HTTP isteği taşımayan Graph hatası."""
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class MailRule(TypedDict):
    folder: str
    senders: List[str]
    older_than_days: Optional[int]
    exclude_senders: List[str]
    keep_flagged: bool
    semantic: str


def retry_after(headers: Any) -> float:
    raw = headers.get("Retry-After", headers.get("retry-after", "1"))
    try:
        return max(0.0, float(raw))
    except (ValueError, TypeError):
        try:
            return max(0.0, (parsedate_to_datetime(raw) - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError):
            return 1.0


def parse_rule(values: Dict[str, Any]) -> MailRule:
    def addresses(value: Any) -> List[str]:
        return [part.strip().casefold() for part in str(value or "").split(",") if part.strip()]
    days = values.get("older_than_days")
    age = None if days in (None, "") else int(days)
    if age is not None and age < 0:
        raise ValueError("Gün sayısı negatif olamaz.")
    rule: MailRule = {"folder": str(values.get("folder") or "inbox").strip(),
                      "senders": addresses(values.get("senders")),
                      "older_than_days": age,
                      "exclude_senders": addresses(values.get("exclude_senders")),
                      "keep_flagged": bool(values.get("keep_flagged", True)),
                      "semantic": str(values.get("semantic") or "").strip()}
    if rule["folder"].casefold() == "deleteditems":
        raise ValueError("Çöp Kutusu temizleme kaynağı olamaz.")
    if not rule["senders"] and age is None and not rule["semantic"]:
        raise ValueError("Gönderen, yaş veya anlam ölçütlerinden en az biri gerekiyor.")
    return rule


def mail_filter(rule: MailRule, now: datetime) -> str:
    clauses: List[str] = []
    if rule["older_than_days"] is not None:
        cutoff = now - timedelta(days=rule["older_than_days"])
        clauses.append("receivedDateTime lt " + cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"))
    if rule["senders"]:
        clauses.append("(" + " or ".join("from/emailAddress/address eq '" + s.replace("'", "''") + "'"
                                         for s in rule["senders"]) + ")")
    return " and ".join(clauses)


def eligible(message: Dict[str, Any], rule: MailRule, now: datetime) -> bool:
    sender = message.get("from", {}).get("emailAddress", {}).get("address", "").casefold()
    if sender in rule["exclude_senders"] or (rule["senders"] and sender not in rule["senders"]):
        return False
    if rule["keep_flagged"] and message.get("flag", {}).get("flagStatus") == "flagged":
        return False
    if rule["older_than_days"] is not None:
        try:
            received = datetime.fromisoformat(message["receivedDateTime"].replace("Z", "+00:00"))
            if received >= now - timedelta(days=rule["older_than_days"]):
                return False
        except (KeyError, ValueError, TypeError):
            return False
    return bool(message.get("id") and message.get("parentFolderId"))


class OutlookAdapter:
    """Graph dış sistem konnektörü; modelden alınan ID'leri gözlenen adaylarla sınırlar."""
    def __init__(self, root: Path, http: httpx.AsyncClient, auth: Optional[Any] = None) -> None:
        self.root = root
        self.http = http
        self.auth = auth or OutlookAuth(root)
        self.pending: Dict[str, Dict[str, Any]] = {}
        self.lock = asyncio.Lock()

    def connection_status(self) -> str:
        return self.auth.status()

    def account_root(self) -> Path:
        if not self.auth.account_id:
            raise InteractionRequired("Önce Outlook hesabına bağlanın.")
        digest = hashlib.sha256(self.auth.account_id.encode()).hexdigest()[:24]
        return self.root / "mail" / digest

    async def request(self, runtime: IntegrationRuntime, method: str, path: str,
                      *, body: Optional[Dict[str, Any]] = None,
                      params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = path if path.startswith("https://") else GRAPH + path
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != "graph.microsoft.com" or not parsed.path.startswith("/v1.0/"):
            raise ValueError("Graph dışındaki adrese hesap tokenı gönderilemez.")
        refreshed = False
        token = await self.auth.token(runtime)
        retries = 0
        while True:
            runtime.check()
            started = time.monotonic()
            runtime.metrics["network_requests"] += 1
            try:
                response = await runtime.wait(self.http.request(
                    method, url, headers={"Authorization": "Bearer " + token, **IMMUTABLE},
                    json=body, params=params, timeout=15))
            except (httpx.TransportError, TimeoutError):
                if method != "GET" or retries >= 2:
                    raise GraphError(0, "Graph ağ sonucu belirsiz.") from None
                retries += 1
                await runtime.delay(0.25 * retries)
                continue
            finally:
                runtime.metrics["network_seconds"] += time.monotonic() - started
            if response.status_code == 401 and not refreshed:
                refreshed = True
                token = await self.auth.token(runtime, force_refresh=True)
                continue
            if response.status_code == 403:
                raise GraphError(403, "Microsoft Graph erişimi reddetti; Mail.ReadWrite iznini kontrol edin.")
            if response.status_code == 429 and method == "GET" and retries < 2:
                retries += 1
                await runtime.delay(retry_after(response.headers))
                continue
            if response.status_code >= 500 and method == "GET" and retries < 2:
                retries += 1
                await runtime.delay(0.25 * retries)
                continue
            if response.status_code >= 400:
                if response.status_code == 401:
                    self.auth.ready = False
                raise GraphError(response.status_code, f"Graph işlemi başarısız: HTTP {response.status_code}.")
            return response.json() if response.content else {}

    async def rule(self, runtime: IntegrationRuntime, reset: bool = False) -> MailRule:
        path = self.account_root() / "rule.json"
        saved = read_json(path, None)
        if saved and not reset:
            return saved
        values = await runtime.ask("Hangi e-postalar gereksiz? Bu kural hesabınız için saklanacak.", {
            "folder": {"type": "string", "label": "Kaynak klasör (inbox veya junkemail)", "default": "inbox"},
            "senders": {"type": "string", "label": "Silinecek gönderenler (virgülle; boş = tümü)", "default": ""},
            "older_than_days": {"type": "string", "label": "Kaç günden eski? (boş = yaş sınırı yok)", "default": ""},
            "exclude_senders": {"type": "string", "label": "Korunacak gönderenler (virgülle)", "default": ""},
            "keep_flagged": {"type": "boolean", "label": "Bayraklı mesajları koru", "default": True},
            "semantic": {"type": "string", "label": "Anlamsal ölçüt (isteğe bağlı; ör. yalnız reklamlar)", "default": ""},
            "_help": "Girilen ölçütler birlikte uygulanır. Eşleşenler Çöp Kutusu'na taşınır; kalıcı silme yapılmaz."},
            None)
        result = parse_rule(values)
        save_json(path, result)
        return result

    async def messages(self, runtime: IntegrationRuntime, rule: MailRule) -> List[Dict[str, Any]]:
        now = datetime.now(timezone.utc)
        path = "/me/mailFolders/" + quote(rule["folder"], safe="") + "/messages"
        params: Optional[Dict[str, Any]] = {"$select": FIELDS, "$top": 100}
        expression = mail_filter(rule, now)
        if expression:
            params["$filter"] = expression
        result: List[Dict[str, Any]] = []
        seen: set[str] = set()
        pages: set[str] = set()
        while path:
            runtime.check()
            if path in pages:
                raise GraphError(0, "Graph sayfalama döngüsü tespit edildi.")
            pages.add(path)
            data = await self.request(runtime, "GET", path, params=params)
            for message in data.get("value", []):
                if message.get("id") not in seen:
                    seen.add(message.get("id", ""))
                    result.append(message)
            path = data.get("@odata.nextLink", "")
            params = None
        return result

    async def reconcile(self, runtime: IntegrationRuntime, row: Dict[str, Any]) -> str:
        try:
            current = await self.request(runtime, "GET", "/me/messages/" + quote(row["id"], safe=""),
                                         params={"$select": "id,parentFolderId"})
        except GraphError:
            return "unknown"
        if current.get("parentFolderId") == row["destination"]:
            row["new_id"] = current["id"]
            return "moved"
        return "unchanged" if current.get("parentFolderId") == row["source"] else "unknown"

    async def recover(self, runtime: IntegrationRuntime) -> None:
        """Önceki kesintide yanıtı kaybolan yazmaları yeniden göndermeden uzlaştırır."""
        for path in sorted((self.account_root() / "operations").glob("*.json")):
            journal = read_json(path, {})
            dirty = False
            for row in journal.get("rows", []):
                if row["status"] in ("moving", "unknown"):
                    state = await self.reconcile(runtime, row)
                    row["status"] = state
                    dirty = True
                    if state == "unknown":
                        save_json(path, journal)
                        raise GraphError(0, "Önceki taşımanın sonucu belirsiz; aynı mesajlar yeniden işlenmedi.")
            if dirty:
                save_json(path, journal)

    async def move(self, runtime: IntegrationRuntime, messages: List[Dict[str, Any]],
                   destination: str = "deleteditems") -> Dict[str, Any]:
        runtime.check()
        folder = await self.request(runtime, "GET", "/me/mailFolders/" + quote(destination, safe=""),
                                    params={"$select": "id"})
        destination_id = folder["id"]
        operation_id = uuid.uuid4().hex
        path = self.account_root() / "operations" / (operation_id + ".json")
        rows = [{"id": m["id"], "source": m["parentFolderId"], "destination": destination_id,
                 "new_id": "", "status": "pending"} for m in messages]
        journal = {"id": operation_id, "rows": rows}
        save_json(path, journal)
        for offset in range(0, len(rows), 20):
            runtime.check()
            group = rows[offset:offset + 20]
            retry = 0
            while group:
                runtime.check()
                for row in group:
                    row["status"] = "moving"
                save_json(path, journal)
                requests = [{"id": str(i), "method": "POST",
                             "url": "/me/messages/" + quote(row["id"], safe="") + "/move",
                             "headers": {"Content-Type": "application/json", **IMMUTABLE},
                             "body": {"destinationId": destination_id}} for i, row in enumerate(group)]
                try:
                    result = await self.request(runtime, "POST", "/$batch", body={"requests": requests})
                    responses = {str(r["id"]): r for r in result.get("responses", [])}
                except GraphError as error:
                    if error.status in (401, 403):
                        for row in group:
                            row["status"] = "failed"
                        save_json(path, journal)
                        raise
                    responses = {}
                again: List[Dict[str, Any]] = []
                wait_seconds = 0.0
                for i, row in enumerate(group):
                    response = responses.get(str(i), {})
                    status = response.get("status", 0)
                    if 200 <= status < 300 and response.get("body", {}).get("id"):
                        row.update(status="moved", new_id=response["body"]["id"])
                    elif status == 429 and retry < 2:
                        row["status"] = "pending"
                        again.append(row)
                        wait_seconds = max(wait_seconds, retry_after(response.get("headers", {})))
                    elif status == 401 and retry < 1:
                        await self.auth.token(runtime, force_refresh=True)
                        row["status"] = "pending"
                        again.append(row)
                    elif status == 0 or status >= 500:
                        row["status"] = await self.reconcile(runtime, row)
                        if row["status"] == "unchanged" and retry < 1:
                            again.append(row)
                    else:
                        row["status"] = "failed"
                save_json(path, journal)
                completed = sum(r["status"] == "moved" for r in rows)
                runtime.status("progress", f"{completed}/{len(rows)} ileti taşındı", completed, len(rows))
                group = again
                retry += 1
                if group and wait_seconds:
                    await runtime.delay(wait_seconds)
        moved = sum(row["status"] == "moved" for row in rows)
        failed = len(rows) - moved
        runtime.metrics["operations_ok"] += moved
        runtime.metrics["operations_failed"] += failed
        return {"operation_id": operation_id, "moved": moved, "failed": failed,
                "unknown": sum(row["status"] == "unknown" for row in rows)}

    def selection(self, runtime: IntegrationRuntime, messages: List[Dict[str, Any]], rule: MailRule,
                  skipped: int = 0) -> Dict[str, Any]:
        key = uuid.uuid4().hex
        self.pending[key] = {"runtime": runtime, "account": self.auth.account_id,
                             "messages": messages, "rule": rule, "skipped": skipped}
        chunk = messages[:100]
        return {"status": "classification_required", "selection_id": key, "criterion": rule["semantic"],
                "remaining": max(0, len(messages) - 100), "skipped": skipped,
                "candidates": [{"id": m["id"], "subject": m.get("subject", ""),
                                "sender": m.get("from", {}).get("emailAddress", {}).get("address", ""),
                                "preview": m.get("bodyPreview", "")[:240]} for m in chunk],
                "instruction": "Kullanıcı ölçütünü karşılayan kesin adayların ID'lerini toplu seç. Belirsizleri atla."}

    async def clean(self, runtime: IntegrationRuntime, reset_rule: bool = False) -> Dict[str, Any]:
        async with self.lock:
            await self.auth.token(runtime)
            runtime.status("connected", "Outlook bağlantısı hazır")
            await self.recover(runtime)
            rule = await self.rule(runtime, reset_rule)
            messages = await self.messages(runtime, rule)
            now = datetime.now(timezone.utc)
            selected = [m for m in messages if eligible(m, rule, now)]
            skipped = len(messages) - len(selected)
            if rule["semantic"]:
                return self.selection(runtime, selected, rule, skipped)
            if not selected:
                return {"moved": 0, "failed": 0, "skipped": skipped}
            result = await self.move(runtime, selected)
            return {**result, "skipped": skipped}

    async def apply_selection(self, runtime: IntegrationRuntime, selection_id: str,
                              message_ids: List[str]) -> Dict[str, Any]:
        async with self.lock:
            selection = self.pending.get(selection_id)
            if not selection or selection["runtime"] is not runtime or selection["account"] != self.auth.account_id:
                raise ValueError("Seçim bu göreve/hesaba ait değil veya süresi dolmuş.")
            current = selection["messages"][:100]
            available = {m["id"]: m for m in current}
            if not set(message_ids) <= set(available):
                raise ValueError("Yalnızca bu seçimde gösterilmiş mesajlar taşınabilir.")
            self.pending.pop(selection_id)
            await self.recover(runtime)
            selected = [available[key] for key in dict.fromkeys(message_ids)]
            result = await self.move(runtime, selected) if selected else {"moved": 0, "failed": 0}
            result["skipped"] = len(current) - len(selected) + selection["skipped"]
            remaining = selection["messages"][100:]
            if remaining:
                result["next"] = self.selection(runtime, remaining, selection["rule"])
            return result

    async def search(self, runtime: IntegrationRuntime, senders: List[str], older_than_days: Optional[int],
                     folder: str) -> Dict[str, Any]:
        await self.auth.token(runtime)
        rule: MailRule = {"folder": folder, "senders": [s.casefold() for s in senders],
                          "older_than_days": older_than_days, "exclude_senders": [],
                          "keep_flagged": True, "semantic": "Görevde istenen mesajlar"}
        messages = await self.messages(runtime, rule)
        selected = [m for m in messages if eligible(m, rule, datetime.now(timezone.utc))]
        return self.selection(runtime, selected, rule)

    async def restore(self, runtime: IntegrationRuntime, operation_id: str) -> Dict[str, Any]:
        async with self.lock:
            if len(operation_id) != 32 or any(c not in "0123456789abcdef" for c in operation_id):
                raise ValueError("Geçersiz işlem kimliği.")
            await self.auth.token(runtime)
            await self.recover(runtime)
            path = self.account_root() / "operations" / (operation_id + ".json")
            journal = read_json(path, None)
            if journal is None:
                raise ValueError("Bu hesaba ait işlem bulunamadı.")
            total = 0
            for source in {r["source"] for r in journal["rows"] if r["status"] == "moved"}:
                selected = []
                original = []
                for row in journal["rows"]:
                    if row["status"] == "moved" and row["source"] == source:
                        current = await self.request(runtime, "GET", "/me/messages/" + quote(row["new_id"], safe=""),
                                                     params={"$select": "id,parentFolderId"})
                        if current["parentFolderId"] == row["source"]:
                            row["status"] = "restored"
                        elif current["parentFolderId"] == row["destination"]:
                            selected.append(current)
                            original.append(row)
                if selected:
                    result = await self.move(runtime, selected, source)
                    restored = read_json(self.account_root() / "operations" / (result["operation_id"] + ".json"), {})
                    for before, after in zip(original, restored["rows"], strict=True):
                        if after["status"] == "moved":
                            before["status"] = "restored"
                            total += 1
                save_json(path, journal)
            return {"restored": total}

    def tools(self, runtime: IntegrationRuntime, operations: List[str]) -> Dict[str, ToolEntry]:
        async def clean(reset_rule: bool) -> Any:
            return await self.clean(runtime, reset_rule)
        async def search(senders: List[str], older_than_days: Optional[int], folder: str) -> Any:
            return await self.search(runtime, senders, older_than_days, folder)
        async def apply(selection_id: str, message_ids: List[str]) -> Any:
            return await self.apply_selection(runtime, selection_id, message_ids)
        async def restore(operation_id: str) -> Any:
            return await self.restore(runtime, operation_id)
        definitions = [
            ("outlook_clean", "Kuralı bir kez kullanıcıya sorar, saklar, eşleşenleri toplu Çöp Kutusu'na taşır.",
             {"reset_rule": {"type": "boolean"}}, clean, False, {"clean"}),
            ("outlook_search", "Postaları filtreli getirir. Dönen adaylar toplu seçilebilir.",
             {"senders": {"type": "array", "items": {"type": "string"}},
              "older_than_days": {"type": ["integer", "null"], "minimum": 0},
              "folder": {"type": "string"}}, search, True, {"list", "search", "move"}),
            ("outlook_apply_selection", "Bu görevde gösterilen kesin adayları toplu taşır; belirsizleri seçme.",
             {"selection_id": {"type": "string"}, "message_ids": {"type": "array", "items": {"type": "string"}}},
             apply, False, {"clean", "move"}),
            ("outlook_restore", "Bu hesaba ait önceki taşıma işlemini kaynak klasörlere geri yükler.",
             {"operation_id": {"type": "string"}}, restore, False, {"restore"}),
        ]
        return {name: {"schema": function_schema(name, description, properties),
                       "execute": fn, "readonly": readonly, "capability": "outlook"}
                for name, description, properties, fn, readonly, supported in definitions
                if not operations or set(operations) & supported}

    def release(self, runtime: IntegrationRuntime) -> None:
        self.pending = {key: value for key, value in self.pending.items() if value["runtime"] is not runtime}

    async def close(self) -> None:
        await self.auth.close()

