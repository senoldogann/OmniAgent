"""Göreve göre küçük araç kümesi seçen kalıcı entegrasyon kataloğu."""
import asyncio
import hashlib
import os
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, NotRequired, Optional, TypedDict
from urllib.parse import urlparse

import httpx
from jsonschema import Draft202012Validator

from integration_runtime import IntegrationRuntime, data_root, read_json, save_json


class ToolEntry(TypedDict):
    schema: Dict[str, Any]
    execute: Callable[..., Awaitable[Any]]
    readonly: bool
    capability: str
    # Uzak aracın okunur adı (MCP yerel adları özetlenmiş kimliktir); onay penceresinde gösterilir
    label: NotRequired[str]
    # Para hareketi yapan araç: her çağrı kullanıcı onayı ister (approval.py)
    financial: NotRequired[bool]


class Capability(TypedDict, total=False):
    id: str
    kind: str
    title: str
    aliases: List[str]
    source: str
    version: str
    operations: List[str]
    batch: bool
    trusted: bool
    connection: str
    transport: str
    url: str
    command: str
    args: List[str]
    package: Dict[str, Any]
    skill_path: str
    skill_url: str
    token_key: str
    readonly_tools: List[str]
    operation_tools: Dict[str, List[str]]
    observed: Dict[str, Any]
    # Katalog kaydı finansal hizmetse (banka, ödeme, borsa) salt okunur olmayan tüm araçları onay ister
    financial: bool


def function_schema(name: str, description: str, properties: Dict[str, Any],
                    required: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties,
                           "required": list(properties) if required is None else required,
                           "additionalProperties": False}}}


DISCOVERY_SCHEMA = function_schema(
    "discover_capabilities",
    "Harici hizmet için hazır API/MCP/skill bulur; query=catalog yerel envanter, query=skills.sh:outlook yalnız skills.sh araması. Yerel dosya işlerinde kullanma.",
    {"query": {"type": "string", "description": "Kısa hizmet adı: outlook, github vb."},
     "operations": {"type": "array", "items": {"type": "string"},
                    "description": "Gereken işlemler; boş liste tüm uygun işlemleri seçer."},
     "allow_online": {"type": "boolean", "description": "Yeni/toplu işte kısa çevrimiçi keşfe izin ver."}},
)

OUTLOOK: Capability = {
    "id": "outlook", "kind": "api", "title": "Outlook / Hotmail (Microsoft Graph)",
    "aliases": ["outlook", "hotmail", "microsoft mail"], "source": "https://graph.microsoft.com",
    "version": "1", "operations": ["list", "search", "clean", "move", "restore"],
    "batch": True, "trusted": True, "connection": "setup_required",
}


def local_skill_entries() -> List[Capability]:
    """Kurulu yerel skill dizinlerini bir kez dizinler; içerik ancak seçilince okunur."""
    configured = os.environ.get("OMNI_SKILLS_DIRS")
    roots = ([Path(item).expanduser() for item in configured.split(os.pathsep) if item]
             if configured is not None else
             [Path.home() / ".agents/skills", Path.home() / ".codex/skills"])
    found: List[Capability] = []
    names: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*/SKILL.md")):
            try:
                resolved = path.resolve(strict=True)
                if (path.is_symlink() or not resolved.is_relative_to(root.resolve())
                        or not resolved.is_file() or resolved.stat().st_size > 64000):
                    continue
                content = resolved.read_bytes()
            except OSError:
                continue
            name = path.parent.name
            if name.casefold() in names:
                continue
            names.add(name.casefold())
            digest = hashlib.sha256(content).hexdigest()[:16]
            found.append({
                "id": "skill:" + name, "kind": "skill", "title": name.replace("-", " "),
                "aliases": [name, name.replace("-", " "), name.replace("_", " ")],
                "source": str(resolved), "skill_path": str(resolved), "version": digest,
                "operations": [], "batch": False, "trusted": True, "connection": "ready",
            })
    return found


def matches(entry: Capability, query: str) -> bool:
    query = query.casefold().strip()
    words = [entry["id"], entry.get("title", "")] + entry.get("aliases", [])
    if len(query) < 3:
        return any(query == word.casefold() for word in words if word)
    return any(query in word.casefold() or word.casefold() in query for word in words if word)


def rank(entry: Capability) -> tuple:
    return (entry.get("kind") == "skill", entry.get("connection") != "ready",
            not entry.get("trusted", False), not entry.get("batch", False),
            entry.get("observed", {}).get("mean_seconds", 1000))


def validate_arguments(entry: ToolEntry, arguments: Any) -> None:
    Draft202012Validator(entry["schema"]["function"]["parameters"]).validate(arguments)


class CapabilityService:
    """HTTP/MCP bağlantılarını ve yerel kataloğu oturumlar arasında paylaşır."""
    def __init__(self, root: Optional[Path] = None, http: Optional[httpx.AsyncClient] = None) -> None:
        self.root = root or data_root()
        self.http = http or httpx.AsyncClient(timeout=8.0, follow_redirects=False)
        self._owns_http = http is None
        self.entries: List[Capability] = []
        self.refresh_local()
        self.cache: Dict[str, Any] = read_json(self.root / "discovery.json", {})
        self.stats: Dict[str, Any] = read_json(self.root / "observed.json", {})
        self.outlook: Optional[Any] = None
        self.mcp: Optional[Any] = None
        self.closed = False

    def refresh_local(self) -> None:
        """Yeni kurulan skill ve kayıtları uygulamayı yeniden başlatmadan görür."""
        catalog = read_json(self.root / "catalog.json", [])
        if not isinstance(catalog, list):
            raise ValueError("Entegrasyon kataloğu liste biçiminde olmalı.")
        self.entries = [dict(OUTLOOK)] + catalog + local_skill_entries()

    def local(self, query: str, operations: List[str]) -> List[Capability]:
        return sorted([dict(entry, observed=self.stats.get(entry["id"], {})) for entry in self.entries
                       if matches(entry, query) and
                       (not operations or not entry.get("operations") or
                        all(op in entry["operations"] for op in operations))], key=rank)

    async def discover(self, runtime: IntegrationRuntime, query: str, operations: List[str],
                       allow_online: bool) -> Dict[str, Any]:
        runtime.check()
        marketplace = re.match(r"(?i)^\s*skills\.sh(?:\s*[:/]\s*|\s+)(.+?)\s*$", query)
        marketplace_query = marketplace.group(1) if marketplace is not None else None
        if marketplace_query is None and matches(OUTLOOK, query):
            aliases = {"delete": "clean", "trash": "clean", "cleanup": "clean", "read": "list"}
            operations = [aliases.get(op.casefold(), op) for op in operations]
        start = time.monotonic()
        runtime.status("discovery", f"{query}: hazır bağlantılar aranıyor")
        try:
            if query.casefold().strip() in ("catalog", "skills", "yetenekler"):
                self.refresh_local()
                installed = sorted(self.entries, key=lambda entry: (entry.get("kind", ""), entry["id"]))
                return {
                    "selected": None,
                    "installed": [{"id": entry["id"], "kind": entry["kind"],
                                   "connection": entry.get("connection", "unknown")}
                                  for entry in installed[:80]],
                    "total": len(installed),
                    "reason": "Kurulu katalog; skill yalnız yöntem bilgisidir.",
                }
            entries = [] if marketplace_query is not None else self.local(query, operations)
            if not entries and marketplace_query is None:
                self.refresh_local()
                entries = self.local(query, operations)
            skill_guidance: List[str] = []
            for entry in entries:
                if entry.get("trusted"):
                    tools, note = await self.activate(entry, runtime, operations)
                    if entry.get("kind") == "skill":
                        # Skill yöntem bilgisidir; erişim veya çalıştırılabilir araç sayılmaz.
                        skill_guidance.append(note)
                        continue
                    runtime.selected.update(tools)
                    return {"selected": entry["id"], "reason": note, "tools": list(tools),
                            "source": entry.get("source"), "status": entry.get("connection")}
            review_entries = [entry for entry in entries if entry.get("kind") != "skill"]
            if review_entries:
                return {"selected": None, "candidates": review_entries, "status": "review_required",
                        "guidance": skill_guidance,
                        "reason": "Kaynak/sürüm/erişim incelemesi gerekiyor; otomatik çalıştırılmadı."}
            if not allow_online or runtime.discovery_remaining <= 0:
                return {"selected": None, "tools": [], "guidance": skill_guidance,
                        "reason": "\n".join(skill_guidance) if skill_guidance else
                                  "Hazır bağlantı yok; mevcut API/DOM/AX yolunu kullan."}
            key = query.casefold().strip()
            cached = self.cache.get(key)
            if cached and cached["expires"] > time.time():
                return {"candidates": cached["entries"], "cached": True, "selected": None,
                        "guidance": skill_guidance}
            network_start = time.monotonic()
            candidates: List[Capability] = []
            try:
                search = (self.skills_sh_search(marketplace_query, runtime)
                          if marketplace_query is not None else self.remote_search(query, runtime))
                candidates = await runtime.wait(search, timeout=runtime.discovery_remaining)
            except (TimeoutError, httpx.HTTPError):
                pass
            finally:
                runtime.discovery_remaining = max(0.0, runtime.discovery_remaining -
                                                   (time.monotonic() - network_start))
            self.cache[key] = {"expires": time.time() + (86400 if candidates else 900), "entries": candidates}
            save_json(self.root / "discovery.json", self.cache)
            return {"candidates": candidates, "selected": None, "guidance": skill_guidance,
                    "reason": "Bulunan kaynaklar inceleme gerektirir." if candidates else
                              "Keşif tamamlandı; uygun API/DOM/AX yoluyla devam et."}
        finally:
            runtime.metrics["discovery_seconds"] += time.monotonic() - start

    async def skills_sh_search(self, query: str, runtime: IntegrationRuntime) -> List[Capability]:
        """skills.sh CLI aramasını kullanır; bulunan skill yalnız inceleme adayıdır."""
        started = time.monotonic()
        runtime.metrics["network_requests"] += 1
        try:
            response = await self.http.get("https://skills.sh/api/search",
                                           params={"q": query, "limit": 5})
            response.raise_for_status()
            payload = response.json()
            items = payload.get("skills", []) if isinstance(payload, dict) else []
            result: List[Capability] = []
            for item in items[:5] if isinstance(items, list) else []:
                if not isinstance(item, dict):
                    continue
                slug = str(item.get("id", "")).strip()
                repository = str(item.get("source", "")).strip()
                if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", slug):
                    continue
                if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
                    continue
                result.append({
                    "id": "skills.sh:" + slug, "kind": "skill",
                    "title": str(item.get("name") or slug.rsplit("/", 1)[-1])[:120],
                    "source": "https://skills.sh/" + slug,
                    "version": "", "operations": [], "batch": False,
                    "trusted": False, "connection": "review_required",
                    "package": {"repository": "https://github.com/" + repository,
                                "skill": slug.rsplit("/", 1)[-1]},
                })
            return result
        finally:
            runtime.metrics["network_seconds"] += time.monotonic() - started

    async def registry_search(self, query: str, runtime: IntegrationRuntime) -> List[Capability]:
        """Registry verisi keşif bilgisidir; kurulum yetkisi vermez."""
        started = time.monotonic()
        runtime.metrics["network_requests"] += 1
        try:
            response = await self.http.get("https://registry.modelcontextprotocol.io/v0.1/servers",
                                           params={"search": query, "version": "latest", "limit": 5})
            response.raise_for_status()
            result: List[Capability] = []
            for item in response.json().get("servers", [])[:5]:
                server = item.get("server", {})
                result.append({"id": server.get("name", ""), "kind": "mcp",
                               "title": server.get("description", "")[:400],
                               "version": server.get("version", ""),
                               "source": server.get("repository", {}).get("url", ""),
                               "trusted": False, "operations": [],
                               "package": {"candidates": server.get("packages", [])},
                               "connection": "review_required"})
            return result
        finally:
            runtime.metrics["network_seconds"] += time.monotonic() - started

    async def remote_search(self, query: str, runtime: IntegrationRuntime) -> List[Capability]:
        """Resmî doküman ve registry aramasını aynı sekiz saniyelik bütçede yürütür."""
        sources = {"github": "https://docs.github.com", "slack": "https://api.slack.com",
                   "notion": "https://developers.notion.com", "microsoft": "https://learn.microsoft.com"}
        async def documentation() -> List[Capability]:
            url = sources.get(query.casefold().strip())
            if not url:
                return []
            started = time.monotonic()
            runtime.metrics["network_requests"] += 1
            try:
                response = await self.http.get(url)
                response.raise_for_status()
                from urllib.parse import urljoin
                links = re.findall(r'href=["\']([^"\']+)["\']', response.text[:250000])
                relevant = [urljoin(url, link) for link in links
                            if "SKILL.md" in link or "mcp" in link.casefold()]
                return [{"id": link, "kind": "skill" if "SKILL.md" in link else "documentation",
                         "title": query + " resmî kaynak bağlantısı", "source": url,
                         "skill_url": link if "SKILL.md" in link else "",
                         "trusted": False, "connection": "review_required", "version": ""}
                        for link in dict.fromkeys(relevant) if link.startswith("https://")][:5]
            finally:
                runtime.metrics["network_seconds"] += time.monotonic() - started
        results = await asyncio.gather(self.registry_search(query, runtime), documentation(), return_exceptions=True)
        candidates = [item for group in results if isinstance(group, list) for item in group]
        async def skill_at_repository(entry: Capability) -> Optional[Capability]:
            parsed = urlparse(entry.get("source", ""))
            parts = parsed.path.strip("/").removesuffix(".git").split("/")
            if parsed.netloc != "github.com" or len(parts) != 2:
                return None
            url = f"https://raw.githubusercontent.com/{parts[0]}/{parts[1]}/main/SKILL.md"
            started = time.monotonic()
            runtime.metrics["network_requests"] += 1
            try:
                response = await self.http.get(url)
                if response.status_code == 200:
                    return {"id": entry["id"] + "/skill", "kind": "skill", "title": entry["id"] + " yöntemi",
                            "source": entry["source"], "skill_url": url, "version": "",
                            "trusted": False, "connection": "review_required"}
            except httpx.HTTPError:
                return None
            finally:
                runtime.metrics["network_seconds"] += time.monotonic() - started
            return None
        skills = await asyncio.gather(*(skill_at_repository(entry) for entry in candidates[:3]))
        return candidates + [skill for skill in skills if skill is not None]

    async def activate(self, entry: Capability, runtime: IntegrationRuntime,
                       operations: List[str]) -> tuple[Dict[str, ToolEntry], str]:
        if entry["id"] == "outlook":
            from outlook import OutlookAdapter
            if self.outlook is None:
                self.outlook = OutlookAdapter(self.root, self.http)
            entry["connection"] = self.outlook.connection_status()
            return self.outlook.tools(runtime, operations), "Graph ile filtreli/toplu erişim; tarayıcı gezinmesi gerekmez."
        if entry["kind"] == "mcp":
            from mcp_bridge import MCPBridge
            if self.mcp is None:
                self.mcp = MCPBridge(self.root, self.http)
            return await self.mcp.tools(entry, runtime, operations), "MCP bağlantısı yeniden kullanılacak."
        if entry["kind"] == "skill":
            from mcp_bridge import load_skill
            guidance = await load_skill(entry, self.http, runtime)
            runtime.status("skill", f"{entry['title']}: görev yöntemi yüklendi")
            return {}, "Yardımcı yöntem bilgisi (erişim yetkisi sağlamaz):\n" + guidance
        raise ValueError("Bilinmeyen entegrasyon türü")

    def record(self, capability: str, seconds: float, ok: bool) -> None:
        previous = self.stats.get(capability, {"calls": 0, "successes": 0, "seconds": 0.0})
        current = {"calls": previous["calls"] + 1, "successes": previous["successes"] + int(ok),
                   "seconds": previous["seconds"] + seconds}
        current["mean_seconds"] = current["seconds"] / current["calls"]
        self.stats[capability] = current

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.stats:
            save_json(self.root / "observed.json", self.stats)
        if self.mcp:
            await self.mcp.close()
        if self.outlook:
            await self.outlook.close()
        if self._owns_http:
            await self.http.aclose()


def discovery_entry(service: CapabilityService, runtime: IntegrationRuntime) -> ToolEntry:
    async def discover(query: str, operations: List[str], allow_online: bool) -> Any:
        return await service.discover(runtime, query, operations, allow_online)
    return {"schema": DISCOVERY_SCHEMA, "execute": discover, "readonly": False, "capability": "catalog"}
