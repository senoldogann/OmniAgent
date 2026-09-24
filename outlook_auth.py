"""Microsoft kişisel hesap bağlantısı; tokenlar yalnız macOS Keychain'de."""
import asyncio
import os
import sys
import time
import uuid
import webbrowser
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

import msal
import requests

from integration_runtime import IntegrationRuntime, InteractionRequired, read_json, save_json

SCOPES = ["Mail.ReadWrite"]


class MSALTransport:
    """MSAL'in senkron HTTP çağrılarına sınırlı ağ süresi uygular."""
    def __init__(self) -> None:
        self.session = requests.Session()

    def get(self, url: str, **kwargs: Any) -> Any:
        return self.session.get(url, timeout=10, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        return self.session.post(url, timeout=10, **kwargs)


class OutlookAuth:
    """MSAL ve Keychain konnektörü; model token veya OAuth kodunu görmez."""
    def __init__(self, root: Path) -> None:
        self.root = root
        self.config: Dict[str, Any] = read_json(root / "outlook.json", {})
        self.cache = msal.SerializableTokenCache()
        self.app: Optional[msal.PublicClientApplication] = None
        self.keyring: Optional[Any] = None
        self.account_id = self.config.get("account_id", "")
        self.transport = MSALTransport()
        self.lock = asyncio.Lock()
        self.ready = False

    def status(self) -> str:
        if self.ready:
            return "ready"
        return "login_required" if self.config.get("client_id") or os.environ.get("OMNI_OUTLOOK_CLIENT_ID") else "setup_required"

    async def _setup(self, runtime: IntegrationRuntime) -> None:
        if self.app:
            return
        client_id = self.config.get("client_id") or os.environ.get("OMNI_OUTLOOK_CLIENT_ID")
        if not client_id:
            result = await runtime.ask(
                "Outlook bağlantısı: Microsoft uygulama kimliği",
                {"client_id": {"type": "string", "label": "Application (client) ID", "default": ""},
                 "_help": "Entra App registrations: kişisel Microsoft hesapları destekli uygulama oluşturun. "
                          "Mobile and desktop applications yönlendirmesi: http://localhost. "
                          "İstemci sırrı gerekmez. Microsoft Graph delegated Mail.ReadWrite kullanılır.",
                 "_url": "https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade"},
                None)
            client_id = result.get("client_id", "").strip()
        try:
            client_id = str(uuid.UUID(str(client_id)))
        except ValueError as error:
            raise InteractionRequired("Geçerli bir Microsoft Application (client) ID gerekiyor.") from error
        if sys.platform != "darwin":
            raise InteractionRequired("Outlook token deposu bu sürümde macOS Keychain gerektiriyor.")
        from keyring.backends.macOS import Keyring
        self.keyring = Keyring()
        cached = await runtime.wait(asyncio.to_thread(
            self.keyring.get_password, "OmniAgent.Outlook", client_id))
        if cached:
            self.cache.deserialize(cached)
        self.app = await runtime.wait(asyncio.to_thread(
            msal.PublicClientApplication, client_id, authority="https://login.microsoftonline.com/consumers",
            token_cache=self.cache, http_client=self.transport))
        self.config["client_id"] = client_id
        save_json(self.root / "outlook.json", self.config)

    async def _interactive(self, runtime: IntegrationRuntime) -> Dict[str, Any]:
        if runtime.answer is None:
            raise InteractionRequired("Microsoft hesabına giriş gerekiyor; arayüzde Outlook Bağlantısı'nı açın.")
        assert self.app is not None
        loop = asyncio.get_running_loop()
        callback: asyncio.Future[Dict[str, str]] = loop.create_future()
        flow: Dict[str, Any] = {}

        async def receive(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                line = await asyncio.wait_for(reader.readline(), 5)
                parts = line.decode("ascii", errors="replace").split()
                values = {k: v[0] for k, v in parse_qs(urlparse(parts[1]).query).items()} if len(parts) > 1 else {}
                valid = values.get("state") == flow.get("state") and bool(values.get("code") or values.get("error"))
                if valid and not callback.done():
                    callback.set_result(values)
                body = "Giriş alındı. OmniAgent'a dönebilirsiniz." if valid else "Geçersiz dönüş."
                encoded = body.encode("utf-8")
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\n"
                             + f"Content-Length: {len(encoded)}\r\nConnection: close\r\n\r\n".encode() + encoded)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        start = time.monotonic()
        runtime.status("waiting_user", "Microsoft girişi gerekiyor; açılan tarayıcıda giriş yapın.")
        server = await asyncio.start_server(receive, "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            flow = await runtime.wait(asyncio.to_thread(
                self.app.initiate_auth_code_flow, SCOPES, redirect_uri=f"http://localhost:{port}",
                prompt="select_account"))
            if not flow.get("auth_uri"):
                raise InteractionRequired("Microsoft oturum açma bağlantısı oluşturulamadı.")
            opened = await runtime.wait(asyncio.to_thread(webbrowser.open, flow["auth_uri"]))
            if not opened:
                raise InteractionRequired("Microsoft giriş sayfası açılamadı; varsayılan tarayıcıyı kontrol edin.")
            response = await runtime.wait(callback)
            return await runtime.wait(asyncio.to_thread(
                self.app.acquire_token_by_auth_code_flow, flow, response))
        finally:
            server.close()
            await server.wait_closed()
            runtime.metrics["user_wait_seconds"] += time.monotonic() - start
            runtime.status("resumed", "Microsoft giriş beklemesi tamamlandı")

    async def token(self, runtime: IntegrationRuntime, force_refresh: bool = False) -> str:
        async with self.lock:
            await self._setup(runtime)
            assert self.app is not None
            accounts = await runtime.wait(asyncio.to_thread(self.app.get_accounts))
            account = next((a for a in accounts if a["home_account_id"] == self.account_id), None)
            if account is None and len(accounts) == 1:
                account = accounts[0]
            result = await runtime.wait(asyncio.to_thread(
                self.app.acquire_token_silent, SCOPES, account=account, force_refresh=force_refresh)) if account else None
            if not result or "access_token" not in result:
                if force_refresh:
                    self.ready = False
                    raise InteractionRequired("Microsoft oturumu yenilenemedi; tekrar giriş gerekiyor.")
                result = await self._interactive(runtime)
            if "access_token" not in result:
                self.ready = False
                raise InteractionRequired("Microsoft girişi tamamlanamadı; uygulama kaydını ve izinleri kontrol edin.")
            accounts = await runtime.wait(asyncio.to_thread(self.app.get_accounts))
            # consumers yetkilisi yalnız kişisel hesapları kabul eder.
            claims = result.get("id_token_claims", {})
            oid = claims.get("oid")
            selected = next((a for a in accounts if a.get("local_account_id") == oid), None)
            selected = selected or account or (accounts[0] if len(accounts) == 1 else None)
            if selected is None:
                raise InteractionRequired("Hesap kimliği belirlenemedi; yeniden giriş gerekiyor.")
            self.account_id = selected["home_account_id"]
            self.config["account_id"] = self.account_id
            save_json(self.root / "outlook.json", self.config)
            if self.cache.has_state_changed:
                await runtime.wait(asyncio.to_thread(
                    self.keyring.set_password, "OmniAgent.Outlook", self.config["client_id"], self.cache.serialize()))
            self.ready = True
            return result["access_token"]

    async def close(self) -> None:
        self.transport.session.close()
