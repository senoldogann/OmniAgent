"""skills.sh kaynaklı GitHub skill dizinlerini sabit commit ile yerel depoya kurar."""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

import httpx

from integration_runtime import IntegrationRuntime, read_json, save_json


_SKILL_SOURCE = re.compile(
    r"^(?:https://skills\.sh/)?(?P<owner>[A-Za-z0-9_.-]+)/"
    r"(?P<repo>[A-Za-z0-9_.-]+)/(?P<skill>[A-Za-z0-9_.-]+)/?$"
)
_MAX_FILES = 40
_MAX_TOTAL_BYTES = 2_000_000
_MAX_SKILL_BYTES = 64_000


def parse_skill_source(source: str) -> Tuple[str, str, str, str]:
    """Yalnız üç parçalı skills.sh kimliğini kabul eder; belirsiz depo yolu kullanmaz."""
    match = _SKILL_SOURCE.fullmatch(source.strip())
    if match is None or any(".." in match.group(part) for part in ("owner", "repo", "skill")):
        raise ValueError("Kaynak https://skills.sh/<sahip>/<depo>/<skill> biçiminde olmalı.")
    owner, repo, skill = match.group("owner", "repo", "skill")
    return owner, repo, skill, f"https://skills.sh/{owner}/{repo}/{skill}"


async def _get(http: httpx.AsyncClient, url: str, runtime: IntegrationRuntime) -> httpx.Response:
    runtime.check()
    started = time.monotonic()
    runtime.metrics["network_requests"] += 1
    try:
        response = await runtime.wait(http.get(url, headers={"Accept": "application/vnd.github+json"}),
                                      timeout=12)
        response.raise_for_status()
        return response
    finally:
        runtime.metrics["network_seconds"] += time.monotonic() - started


def _skill_files(tree: List[Dict[str, Any]], skill: str) -> List[Dict[str, Any]]:
    """Aynı adlı SKILL.md dosyasını seçip yalnız bulunduğu dizini indirir."""
    matches = [
        item for item in tree
        if item.get("type") == "blob" and item.get("path", "").endswith("/SKILL.md")
        and Path(str(item["path"])).parent.name == skill
    ]
    if len(matches) != 1:
        raise ValueError("Depoda bu ad için tek bir SKILL.md bulunmalı.")
    prefix = str(Path(str(matches[0]["path"])).parent) + "/"
    files = [
        item for item in tree
        if item.get("type") == "blob" and str(item.get("path", "")).startswith(prefix)
    ]
    for item in files:
        relative = str(item["path"]).removeprefix(prefix)
        parts = relative.split("/")
        if not relative or any(part in ("", ".", "..") for part in parts):
            raise ValueError("Skill dizinindeki güvensiz dosya yolu reddedildi.")
    if len(files) > _MAX_FILES:
        raise ValueError("Skill dizini dosya sayısı sınırını aşıyor.")
    total = sum(int(item.get("size", 0)) for item in files)
    if total > _MAX_TOTAL_BYTES or int(matches[0].get("size", 0)) > _MAX_SKILL_BYTES:
        raise ValueError("Skill dizini boyut sınırını aşıyor.")
    if any(item.get("mode") == "120000" or int(item.get("size", 0)) < 0 for item in files):
        raise ValueError("Skill dizinindeki bağlantı veya geçersiz dosya reddedildi.")
    return files


async def install_skill(
    root: Path, source: str, http: httpx.AsyncClient, runtime: IntegrationRuntime,
) -> Dict[str, Any]:
    """Kaynağı sabit SHA ile indirir; hiçbir kurulum betiğini çalıştırmaz."""
    owner, repo, skill, canonical = parse_skill_source(source)
    target = root / "skills" / skill
    manifest = target / "INSTALL.json"
    if target.exists():
        saved = read_json(manifest, {})
        if isinstance(saved, dict) and saved.get("source") == canonical and (target / "SKILL.md").is_file():
            return {"id": f"skill:{skill}", "source": canonical, "commit": saved.get("commit", ""),
                    "reused": True, "path": str(target / "SKILL.md")}
        raise ValueError("Bu adla farklı bir skill zaten kurulu; mevcut dosyalar korunuyor.")
    if canonical in runtime.install_attempts:
        raise RuntimeError("Bu skill bu görevde zaten denendi.")
    runtime.install_attempts.add(canonical)
    started = time.monotonic()
    runtime.status("install", f"{skill}: kaynak ve dosyalar doğrulanıyor")
    staging: Path | None = None
    try:
        api = f"https://api.github.com/repos/{owner}/{repo}"
        commit_response = await _get(http, api + "/commits/HEAD", runtime)
        commit = str(commit_response.json().get("sha", ""))
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError("GitHub commit kimliği doğrulanamadı.")
        tree_response = await _get(http, api + f"/git/trees/{commit}?recursive=1", runtime)
        payload = tree_response.json()
        if payload.get("truncated") or not isinstance(payload.get("tree"), list):
            raise ValueError("Depo ağacı eksik; güvenli skill yolu belirlenemedi.")
        files = _skill_files(payload["tree"], skill)
        prefix = str(Path(next(item["path"] for item in files if item["path"].endswith("/SKILL.md"))).parent) + "/"
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".skill-install-", dir=target.parent))
        semaphore = asyncio.Semaphore(4)

        async def download(item: Dict[str, Any]) -> Tuple[str, bytes]:
            async with semaphore:
                remote_path = str(item["path"])
                url = f"https://raw.githubusercontent.com/{owner}/{repo}/{commit}/{quote(remote_path)}"
                response = await _get(http, url, runtime)
                body = response.content
                if len(body) != int(item.get("size", -1)):
                    raise ValueError("Skill dosyası GitHub boyutuyla uyuşmuyor.")
                return remote_path.removeprefix(prefix), body

        downloaded = await runtime.wait(asyncio.gather(*(download(item) for item in files)), timeout=50)
        skill_text = next(body for name, body in downloaded if name == "SKILL.md")
        if len(skill_text) > _MAX_SKILL_BYTES:
            raise ValueError("SKILL.md boyut sınırını aşıyor.")
        skill_text.decode("utf-8")
        for relative, body in downloaded:
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("wb") as output:
                output.write(body)
                output.flush()
                os.fsync(output.fileno())
            destination.chmod(0o600)
        save_json(staging / "INSTALL.json", {
            "source": canonical, "repository": f"https://github.com/{owner}/{repo}",
            "commit": commit, "directory": prefix.rstrip("/"),
            "sha256": hashlib.sha256(skill_text).hexdigest(), "files": len(downloaded),
        })
        runtime.check()
        if target.exists():
            raise ValueError("Skill kurulurken aynı ad başka işlem tarafından oluşturuldu.")
        os.replace(staging, target)
        staging = None
        runtime.status("connected", f"{skill}: kalıcı skill hazır")
        return {"id": f"skill:{skill}", "source": canonical, "commit": commit,
                "reused": False, "path": str(target / "SKILL.md"), "files": len(downloaded)}
    finally:
        runtime.metrics["install_seconds"] += time.monotonic() - started
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
