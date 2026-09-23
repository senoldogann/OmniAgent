"""OAuth döngüsü, Keychain ve eksik bağlantı önkoşulları."""
import json
from pathlib import Path
from urllib.request import urlopen

import pytest

import outlook_auth
from integration_runtime import IntegrationRuntime, InteractionRequired
from outlook_auth import OutlookAuth


@pytest.mark.asyncio
async def test_missing_client_id_without_ui(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("OMNI_OUTLOOK_CLIENT_ID", raising=False)
    auth = OutlookAuth(tmp_path)
    with pytest.raises(InteractionRequired, match="uygulama kimliği"):
        await auth.token(IntegrationRuntime(lambda e: None, lambda: False))
    await auth.close()


@pytest.mark.asyncio
async def test_browser_oauth_code_and_token_never_enter_events(tmp_path: Path, monkeypatch):
    saved = {}
    events = []
    prompts = []
    class Keyring:
        def get_password(self, service, key):
            return saved.get((service, key))
        def set_password(self, service, key, value):
            saved[service, key] = value
    class Cache:
        has_state_changed = True
        def deserialize(self, value):
            pass
        def serialize(self):
            return "PRIVATE_TOKEN_CACHE"
    class App:
        def __init__(self, client_id, **kwargs):
            self.accounts = []
        def get_accounts(self):
            return self.accounts
        def initiate_auth_code_flow(self, scopes, redirect_uri, prompt):
            assert scopes == ["Mail.ReadWrite"]
            return {"state": "private-state", "auth_uri": redirect_uri + "/?code=PRIVATE_CODE&state=private-state"}
        def acquire_token_by_auth_code_flow(self, flow, response):
            assert response["code"] == "PRIVATE_CODE"
            self.accounts = [{"home_account_id": "personal-account", "local_account_id": "oid"}]
            return {"access_token": "PRIVATE_TOKEN", "id_token_claims": {"oid": "oid"}}
        def acquire_token_silent(self, *args, **kwargs):
            return {"access_token": "PRIVATE_TOKEN"}
    async def answer(title, fields):
        prompts.append(title)
        return {"client_id": "00000000-0000-0000-0000-000000000001"}
    def open_browser(url):
        with urlopen(url, timeout=2) as result:
            assert result.status == 200
        return True
    import keyring.backends.macOS
    monkeypatch.setattr(keyring.backends.macOS, "Keyring", Keyring)
    monkeypatch.setattr(outlook_auth.msal, "SerializableTokenCache", Cache)
    monkeypatch.setattr(outlook_auth.msal, "PublicClientApplication", App)
    monkeypatch.setattr(outlook_auth.webbrowser, "open", open_browser)
    monkeypatch.delenv("OMNI_OUTLOOK_CLIENT_ID", raising=False)
    auth = OutlookAuth(tmp_path)
    task = IntegrationRuntime(events.append, lambda: False, answer)
    try:
        assert await auth.token(task) == "PRIVATE_TOKEN"
        assert await auth.token(task) == "PRIVATE_TOKEN"
        assert len(prompts) == 1
        assert auth.status() == "ready"
        assert saved
        assert "PRIVATE" not in json.dumps(events)
        assert "PRIVATE" not in (tmp_path / "outlook.json").read_text()
    finally:
        await auth.close()
