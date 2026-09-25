"""Güvenilir MCP/skill kayıtlarını ayrı süreç ve yeniden kullanılan bağlantıyla açar."""
import asyncio
import hashlib
import json
import os
import re
import shutil
import signal
import sys
import time
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from omniagent.approval import financial_tool_name
from .capabilities import Capability, ToolEntry
from omniagent.config import API_KEY_VARIABLES
from .runtime import IntegrationRuntime, InteractionRequired, read_json, save_json


async def run_install(command: List[str], runtime: IntegrationRuntime, timeout: float) -> None:
    """Kabuk kullanmaz; iptalde kurulum süreç grubunu sonlandırır."""
    runtime.check()
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        env={key: value for key, value in os.environ.items() if key not in API_KEY_VARIABLES.values()},
        start_new_session=True)
    try:
        await runtime.wait(process.wait(), timeout=timeout)
        if process.returncode:
            raise RuntimeError(f"Entegrasyon kurulumu başarısız (çıkış {process.returncode}).")
    finally:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                await asyncio.wait_for(process.wait(), 1)
            except (ProcessLookupError, asyncio.TimeoutError):
                if process.returncode is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await process.wait()


async def install_package(root: Path, entry: Capability, runtime: IntegrationRuntime) -> List[str]:
    package = entry.get("package", {})
    if not entry.get("trusted") or not entry.get("source") or not package:
        raise ValueError("Kurulum için incelenmiş kaynak ve paket kaydı gerekiyor.")
    name, version = package.get("name", ""), package.get("version", "")
    if not re.fullmatch(r"[a-zA-Z0-9@][a-zA-Z0-9._/@-]*", name):
        raise ValueError("Geçersiz paket adı.")
    if not re.fullmatch(r"\d+\.\d+(?:\.\d+)?[a-zA-Z0-9.+-]*", version):
        raise ValueError("Sabit paket sürümü gerekiyor; latest veya sürüm aralığı kullanılamaz.")
    digest = hashlib.sha256(json.dumps(package, sort_keys=True).encode()).hexdigest()[:20]
    destination = root / "packages" / digest
    marker = destination / "installed.json"
    ecosystem = package.get("ecosystem")
    if ecosystem == "python":
        module = package.get("module", "")
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_.]*", module):
            raise ValueError("Python giriş modülü gerekiyor.")
        executable = destination / "venv/bin/python"
        command = [str(executable), "-m", module]
    elif ecosystem == "node":
        binary = package.get("bin", "")
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", binary):
            raise ValueError("Node giriş komutu gerekiyor.")
        executable = destination / "node_modules/.bin" / binary
        command = [str(executable)]
    else:
        raise ValueError("Paket türü python veya node olmalı.")
    if read_json(marker, None) == package and executable.exists():
        return command + entry.get("args", [])
    if entry["id"] in runtime.install_attempts:
        raise RuntimeError("Bu görevde kurulum zaten denendi; yeniden denenmedi.")
    runtime.install_attempts.add(entry["id"])
    destination.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    runtime.status("install", f"{entry['title']}: sabit sürüm kuruluyor")
    try:
        if ecosystem == "python":
            uv = shutil.which("uv")
            if uv:
                await run_install([uv, "venv", "--python", sys.executable, str(destination / "venv")],
                                  runtime, 60 - (time.monotonic() - start))
                await run_install([uv, "pip", "install", "--python", str(executable),
                                   "--index-url", "https://pypi.org/simple", name + "==" + version],
                                  runtime, 60 - (time.monotonic() - start))
            else:
                await run_install([sys.executable, "-m", "venv", str(destination / "venv")],
                                  runtime, 60 - (time.monotonic() - start))
                await run_install([str(executable), "-m", "pip", "install",
                                   "--index-url", "https://pypi.org/simple", name + "==" + version],
                                  runtime, 60 - (time.monotonic() - start))
        else:
            npm = shutil.which("npm")
            if not npm:
                raise RuntimeError("Node entegrasyonu için npm gerekiyor.")
            await run_install([npm, "install", "--prefix", str(destination), "--ignore-scripts",
                               "--no-audit", "--no-fund", "--registry", "https://registry.npmjs.org",
                               name + "@" + version], runtime, 60 - (time.monotonic() - start))
        if not executable.exists():
            raise RuntimeError("Paket kuruldu ancak giriş komutu bulunamadı.")
        save_json(marker, package)
        return command + entry.get("args", [])
    finally:
        runtime.metrics["install_seconds"] += time.monotonic() - start


class MCPConnection:
    """AnyIO taşıma bağlamını açıldığı görevde kapatan kalıcı bağlantı."""
    def __init__(self, entry: Capability, command: Optional[List[str]], headers: Dict[str, str]) -> None:
        self.entry = entry
        self.command = command
        self.headers = headers
        self.ready: asyncio.Future[ClientSession] = asyncio.get_running_loop().create_future()
        self.stop = asyncio.Event()
        self.task = asyncio.create_task(self._serve())

    async def _serve(self) -> None:
        try:
            async with AsyncExitStack() as stack:
                if self.entry.get("transport") == "stdio":
                    if not self.command:
                        raise RuntimeError("stdio MCP bağlantısı için komut hazırlanmadı.")
                    reader, writer = await stack.enter_async_context(stdio_client(
                        StdioServerParameters(command=self.command[0], args=self.command[1:])))
                elif self.entry.get("transport") == "streamable_http":
                    client = await stack.enter_async_context(httpx.AsyncClient(
                        headers=self.headers, timeout=httpx.Timeout(30, read=60), follow_redirects=False))
                    reader, writer, _ = await stack.enter_async_context(streamable_http_client(
                        self.entry["url"], http_client=client))
                else:
                    raise ValueError("Desteklenen MCP taşımaları: stdio, streamable_http")
                session = await stack.enter_async_context(ClientSession(
                    reader, writer, read_timeout_seconds=timedelta(seconds=30)))
                await session.initialize()
                self.ready.set_result(session)
                await self.stop.wait()
        except BaseException as error:
            if not self.ready.done():
                self.ready.set_exception(RuntimeError(f"MCP bağlantısı kurulamadı ({type(error).__name__})."))
            elif not isinstance(error, asyncio.CancelledError):
                # Arka plan görevinin hatası sonraki çağrıda bağlantı durumuyla anlaşılır.
                pass

    async def close(self) -> None:
        self.stop.set()
        if not self.ready.done():
            self.task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(self.task), 3)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


class MCPBridge:
    """Yalnız yerel katalogda güvenilir olarak kaydedilen MCP'leri çalıştırır."""
    def __init__(self, root: Path, http: httpx.AsyncClient) -> None:
        self.root = root
        self.http = http
        self.connections: Dict[str, MCPConnection] = {}
        self.locks: Dict[str, asyncio.Lock] = {}

    async def connect(self, entry: Capability, runtime: IntegrationRuntime) -> MCPConnection:
        if not entry.get("trusted") or not entry.get("version") or not entry.get("source"):
            raise ValueError("MCP kaynağı ve sürümü önce incelenmeli.")
        identity = {key: value for key, value in entry.items() if key not in ("observed", "connection")}
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        async with self.locks.setdefault(entry["id"], asyncio.Lock()):
            connection = self.connections.get(key)
            if connection and not connection.task.done():
                return connection
            command: Optional[List[str]] = None
            headers: Dict[str, str] = {}
            if entry.get("transport") == "stdio":
                command = (await install_package(self.root, entry, runtime) if entry.get("package")
                           else [entry["command"]] + entry.get("args", []))
            else:
                parsed = urlparse(entry.get("url", ""))
                if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost")):
                    raise ValueError("Uzak MCP HTTPS kullanmalı.")
                if entry.get("token_key"):
                    from keyring.backends.macOS import Keyring
                    token = await runtime.wait(asyncio.to_thread(
                        Keyring().get_password, "OmniAgent.MCP", entry["token_key"]))
                    if not token:
                        raise InteractionRequired("MCP bağlantısının Keychain erişim anahtarı eksik.")
                    headers["Authorization"] = "Bearer " + token
            connection = MCPConnection(entry, command, headers)
            self.connections[key] = connection
            try:
                await runtime.wait(asyncio.shield(connection.ready), timeout=10)
            except BaseException:
                await connection.close()
                self.connections.pop(key, None)
                raise
            runtime.status("connected", f"{entry['title']}: MCP bağlantısı hazır")
            return connection

    async def tools(self, entry: Capability, runtime: IntegrationRuntime,
                    operations: List[str]) -> Dict[str, ToolEntry]:
        connection = await self.connect(entry, runtime)
        session = connection.ready.result()
        advertised = []
        cursor = None
        while True:
            result = await runtime.wait(session.list_tools(cursor=cursor), timeout=10)
            advertised.extend(result.tools)
            cursor = result.nextCursor
            if not cursor:
                break
            if len(advertised) > 500:
                raise ValueError("MCP araç kataloğu 500 araç sınırını aştı.")
        requested = set(operations)
        mapping = entry.get("operation_tools", {})
        names = {name for operation in requested for name in mapping.get(operation, [operation])}
        selected = [tool for tool in advertised if not requested or tool.name in names]
        if len(selected) > 12:
            raise ValueError("Daha dar operations seçin. Kullanılabilir araçlar: " +
                             ", ".join(tool.name for tool in advertised)[:1600])
        prefix = hashlib.sha256(entry["id"].encode()).hexdigest()[:10]
        tools: Dict[str, ToolEntry] = {}
        for tool in selected:
            remote_name = tool.name
            local = "mcp_" + prefix + "_" + hashlib.sha256(remote_name.encode()).hexdigest()[:12]
            def bind(remote: str):
                async def invoke(**arguments: Any) -> Any:
                    runtime.check()
                    if connection.task.done():
                        raise RuntimeError("MCP bağlantısı kapandı; yeniden keşfet.")
                    started = time.monotonic()
                    runtime.metrics["network_requests"] += 1
                    try:
                        response = await runtime.wait(session.call_tool(remote, arguments), timeout=60)
                        if response.isError:
                            raise RuntimeError("MCP aracı hata döndürdü: " +
                                               " ".join(getattr(c, "text", "") for c in response.content)[:2000])
                        return response.model_dump(mode="json", exclude_none=True)
                    finally:
                        runtime.metrics["network_seconds"] += time.monotonic() - started
                return invoke
            tools[local] = {
                "schema": {"type": "function", "function": {"name": local,
                           "description": (remote_name + ": " + (tool.description or ""))[:1200],
                           "parameters": tool.inputSchema}},
                "execute": bind(remote_name), "readonly": remote_name in entry.get("readonly_tools", []),
                "capability": entry["id"], "label": remote_name,
                "financial": bool(entry.get("financial")) or financial_tool_name(remote_name),
            }
        if not tools:
            raise ValueError("İstenen işlemler bu MCP'de yok: " + ", ".join(t.name for t in advertised)[:1500])
        return tools

    async def close(self) -> None:
        await asyncio.gather(*(connection.close() for connection in self.connections.values()))
        self.connections.clear()


async def load_skill(entry: Capability, http: httpx.AsyncClient, runtime: IntegrationRuntime) -> str:
    """İncelenmiş yöntem metnini boyut sınırıyla okur; komut çalıştırmaz."""
    if not entry.get("trusted"):
        raise ValueError("Skill kaynağı önce incelenmeli.")
    if entry.get("skill_path"):
        path = Path(entry["skill_path"]).expanduser()
        if path.stat().st_size > 64000:
            raise ValueError("Skill dosyası 64 KB sınırını aşıyor.")
        text = await runtime.wait(asyncio.to_thread(path.read_text, encoding="utf-8"))
    else:
        url = entry.get("skill_url", "")
        if not url.startswith("https://") or not entry.get("version"):
            raise ValueError("Skill için sürümlenmiş HTTPS kaynağı gerekiyor.")
        response = await runtime.wait(http.get(url), timeout=min(8.0, runtime.discovery_remaining))
        response.raise_for_status()
        if len(response.content) > 64000:
            raise ValueError("Skill dosyası 64 KB sınırını aşıyor.")
        text = response.text
    return text[:12000]

