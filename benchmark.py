"""
Hız + doğruluk benchmark'ı: deterministik beklenen çıktısı olan senaryoları gerçek modelle
koşturur; başarı oranı, medyan/en kötü süre, tur ve token (önbellek dahil) sayısını raporlar.
Her optimizasyon yalnızca süreyle değil, doğrulukla birlikte ölçülsün diye vardır.

Kullanım:
  .venv/bin/python benchmark.py --runs 3 --concurrency 3
  .venv/bin/python benchmark.py --runs 3 --backend opencode-think --only gun,paralel
  .venv/bin/python benchmark.py --runs 3 --json /tmp/omni_bench.json
"""
import argparse
import asyncio
import json
import random
import re
import statistics
import string
import tempfile
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import Callable, Dict, List, NotRequired, Optional, Tuple, TypedDict

from openai import AsyncOpenAI

from integration_runtime import IntegrationMetrics
from main import RunOptions, RunReport, close_model_clients, create_model_clients, run_agent_with_callback

CORE_SCENARIOS: Tuple[str, ...] = ("gun", "satir", "js", "satis", "paralel", "siralama", "json", "ceviri", "sadakat")
# Takip ve geçmişsiz negatif kontrol ayrı ölçülür; varsayılan başarı/hız paydasını bozmaz.
SCENARIO_NAMES: Tuple[str, ...] = CORE_SCENARIOS + ("takip", "takip_bos")


class Scenario(TypedDict):
    goal: str
    check: Callable[[str], Tuple[bool, str]]
    first_goal: NotRequired[str]


class RunResult(TypedDict):
    name: str
    ok: bool
    detail: str
    outcome: str
    elapsed_seconds: float
    turns: int
    tool_calls: int
    prompt_tokens: int
    cached_tokens: int
    completion_tokens: int
    backend: str
    integrations: IntegrationMetrics


class JsonHandler(BaseHTTPRequestHandler):
    """Yol sonundaki çalıştırma kimliğini içeren sabit JSON döner (fetch_raw senaryosu)."""

    def do_GET(self) -> None:
        run_id: str = self.path.rsplit("/", 1)[-1]
        body: bytes = json.dumps({"slideshow": {"author": f"Yours Truly {run_id}", "title": "Sample"}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def _read_stripped(path: Path) -> Optional[str]:
    """Dosya varsa kırpılmış içeriğini, yoksa None döner."""
    return path.read_text(encoding="utf-8").strip() if path.exists() else None


def build_scenario(name: str, run_dir: Path, run_id: str, port: int) -> Scenario:
    """Senaryonun hedef metnini ve sonuç denetimini kurar (gerekirse girdi dosyalarını hazırlar)."""
    if name == "gun":
        return {"goal": "2026-09-22 hangi gün? Tek satır 'GUN: <gün adı>' yaz.",
                "check": lambda o: (bool(re.search(r"sal[ıi]|tuesday", o.lower())), "")}
    if name == "satir":
        return {"goal": "execute_shell ile 'seq 1 2000' çalıştır ve çıktının satır sayısını tek satır 'SATIR: <n>' olarak yaz.",
                "check": lambda o: ("SATIR: 2000" in o, "")}
    if name == "js":
        return {"goal": "execute_js ile [3,1,4,1,5,9,2,6] dizisinin toplamını hesapla ve tek satır 'TOPLAM: <n>' yaz.",
                "check": lambda o: ("TOPLAM: 31" in o, "")}
    if name == "satis":
        directory: Path = run_dir / "satis"

        def check_satis(outcome: str) -> Tuple[bool, str]:
            content: Optional[str] = _read_stripped(directory / "satis.txt")
            lines: List[str] = content.split() if content else []
            return lines == ["elma,5", "armut,3", "cilek,8"] and "cilek,8" in outcome, f"dosya={lines}"
        return {"goal": f"{directory}/satis.txt dosyasına şu 3 satırı yaz: elma,5 / armut,3 / cilek,8 (her biri ayrı satırda). "
                        "Sonra en yüksek sayıya sahip satırı bul ve tek satır 'EN_YUKSEK: <satır>' yaz.",
                "check": check_satis}
    if name == "paralel":
        directory = run_dir / "okuma"
        directory.mkdir(parents=True)
        codes: List[str] = []
        paths: List[str] = []
        for index in range(1, 6):
            code: str = "KOD-" + "".join(random.choices(string.ascii_uppercase, k=4))
            body: str = "".join(f"dolgu satırı {n}: {'x' * 50}\n" for n in range(25)) + code + "\n"
            (directory / f"f{index}.txt").write_text(body, encoding="utf-8")
            codes.append(code)
            paths.append(str(directory / f"f{index}.txt"))
        expected: str = ",".join(codes)
        return {"goal": f"Şu 5 dosyayı oku: {', '.join(paths)}. Her dosyanın SON satırındaki kodu dosya sırasıyla "
                        "tek satır 'KODLAR: <k1>,<k2>,<k3>,<k4>,<k5>' olarak yaz.",
                "check": lambda o: (expected in o.replace(" ", ""), f"beklenen={expected}")}
    if name == "siralama":
        directory = run_dir / "sirala"

        def check_siralama(outcome: str) -> Tuple[bool, str]:
            raw: Optional[str] = _read_stripped(directory / "ham.txt")
            result: Optional[str] = _read_stripped(directory / "sonuc.txt")
            return raw == "3,1,2" and result == "1,2,3", f"ham={raw} sonuc={result}"
        return {"goal": f"{directory}/ham.txt dosyasına '3,1,2' yaz. Sonra bu sayıları küçükten büyüğe sıralayıp "
                        f"{directory}/sonuc.txt dosyasına aynı virgüllü biçimde yaz. Tek satır 'SONUC: <sonuc.txt içeriği>' yaz.",
                "check": check_siralama}
    if name == "json":
        return {"goal": f"fetch_raw ile http://127.0.0.1:{port}/json/{run_id} adresini çek, 'slideshow.author' değerini "
                        "tek satır 'AUTHOR: <değer>' olarak yaz.",
                "check": lambda o: (f"AUTHOR: Yours Truly {run_id}" in o, "")}
    if name == "ceviri":
        return {"goal": "'Merhaba dünya' ifadesini İngilizceye çevir ve tek satır 'EN: <çeviri>' yaz. Araç kullanma.",
                "check": lambda o: ("hello" in o.lower() and "world" in o.lower(), "")}
    if name == "sadakat":
        directory = run_dir / "sadakat"

        def check_sadakat(outcome: str) -> Tuple[bool, str]:
            content: Optional[str] = _read_stripped(directory / "not.txt")
            files: List[str] = sorted(p.name for p in directory.iterdir()) if directory.exists() else []
            return content == f"OMNI-{run_id}" and files == ["not.txt"], f"icerik={content} dosyalar={files}"
        return {"goal": f"{directory}/not.txt dosyasına tam olarak 'OMNI-{run_id}' yaz. Başka dosya oluşturma. Tek satır 'TAMAM' yaz.",
                "check": check_sadakat}
    if name in ("takip", "takip_bos"):
        directory = run_dir / "belgeler"
        directory.mkdir()
        largest = directory / f"veri-{run_id}.txt"
        largest.write_text(("uzun kayıt " + "x" * 80 + "\n") * 137, encoding="utf-8")
        (directory / "kisa.txt").write_text("kısa\n" * 7, encoding="utf-8")
        followup: Scenario = {
            "first_goal": f"{directory} dizinindeki boyutu en büyük dosyayı bul. Tam yolunu belirt.",
            "goal": "Onun satır sayısını söyle. Tek satır 'SATIR: <n>' yaz.",
            "check": lambda o: (bool(re.search(r"SATIR:\s*137\b", o)), "beklenen=137"),
        }
        return followup
    raise ValueError(f"Bilinmeyen senaryo: {name}")


async def run_one(
    name: str, root: Path, port: int, backend: Optional[str],
    clients: Dict[str, AsyncOpenAI], semaphore: asyncio.Semaphore,
) -> RunResult:
    """Bir senaryoyu tek kez koşturur ve sonucu denetler."""
    async with semaphore:
        run_id: str = uuid.uuid4().hex[:8]
        run_dir: Path = root / f"{name}-{run_id}"
        run_dir.mkdir(parents=True)
        scenario: Scenario = build_scenario(name, run_dir, run_id, port)
        options: RunOptions = {
            "requested_backend": backend, "should_stop": lambda: False,
            "state_file": str(run_dir / "memory.json"), "history": [],
        }
        first_ok = True
        if name in ("takip", "takip_bos"):
            first = await run_agent_with_callback(scenario["first_goal"], lambda event: None, options, clients)
            first_ok = first["success"] and f"veri-{run_id}.txt" in first["outcome"]
            if name == "takip":
                options["history"] = [first["exchange"]]
        # Takipte yalnızca ikinci mesajın maliyeti ölçülür; ilk mesaj her iki kolda aynıdır.
        started: float = time.monotonic()
        report: RunReport = await run_agent_with_callback(scenario["goal"], lambda event: None, options, clients)
        ok, detail = scenario["check"](report["outcome"])
        ok = ok and report["success"] and first_ok
        metrics = report["metrics"]
        result: RunResult = {
            "name": name, "ok": ok, "detail": detail, "outcome": report["outcome"][:300],
            "elapsed_seconds": round(time.monotonic() - started, 2), "turns": metrics["turns"],
            "tool_calls": metrics["tool_calls"], "prompt_tokens": metrics["prompt_tokens"],
            "cached_tokens": metrics["cached_tokens"], "completion_tokens": metrics["completion_tokens"],
            "backend": metrics["backend"],
            "integrations": metrics.get("integrations", {}),
        }
        print(f"{'✓' if ok else '✗'} {name:9s} {result['elapsed_seconds']:5.1f}s tur={result['turns']} "
              f"araç={result['tool_calls']} backend={result['backend']} | {report['outcome'][:70]!r} {detail[:80]}", flush=True)
        return result


def summarize(results: List[RunResult], names: List[str]) -> str:
    """Senaryo bazında başarı, medyan/maks süre, medyan tur ve toplam önbellek oranını raporlar. Saf."""
    lines: List[str] = [f"{'senaryo':9s} başarı  medyan   maks  tur"]
    for name in names:
        rows: List[RunResult] = [r for r in results if r["name"] == name]
        times: List[float] = [r["elapsed_seconds"] for r in rows]
        lines.append(
            f"{name:9s} {sum(r['ok'] for r in rows)}/{len(rows):<4d} {statistics.median(times):5.1f}s {max(times):5.1f}s "
            f"{statistics.median([r['turns'] for r in rows]):4.1f}"
        )
    all_times: List[float] = [r["elapsed_seconds"] for r in results]
    prompt: int = sum(r["prompt_tokens"] for r in results)
    cached: int = sum(r["cached_tokens"] for r in results)
    lines.append(
        f"TOPLAM başarı={sum(r['ok'] for r in results)}/{len(results)} medyan={statistics.median(all_times):.1f}s "
        f"ortalama={statistics.mean(all_times):.1f}s girdi={prompt} önbellek=%{100 * cached / prompt if prompt else 0:.0f} "
        f"çıktı={sum(r['completion_tokens'] for r in results)}"
    )
    measurements = [row.get("integrations", {}) for row in results]
    lines.append("Entegrasyon toplamları: " + " · ".join(
        f"{key}={sum(item.get(key, 0) for item in measurements):.3f}"
        for key in ("discovery_seconds", "install_seconds", "network_seconds", "wait_seconds",
                    "user_wait_seconds", "network_requests", "operations_ok", "operations_failed")))
    return "\n".join(lines)


async def main(runs: int, concurrency: int, backend: Optional[str], names: List[str], json_path: Optional[str]) -> None:
    server: HTTPServer = HTTPServer(("127.0.0.1", 0), JsonHandler)
    Thread(target=server.serve_forever, daemon=True).start()
    # Kısa, okunur kök: senaryolar yol kopyalama sadakatini de ölçer; /var/folders/... gibi
    # uzun rastgele yollar ölçümü temel çizgiye göre haksız biçimde zorlaştırır.
    root: Path = Path(tempfile.mkdtemp(prefix="omni_bench_", dir="/tmp"))
    # Sadakat denetimi: hiçbir senaryo ev dizinine dosya istemez; yeni dosya = istenmeyen yan etki
    home_before: set = {entry.name for entry in Path.home().iterdir()}
    clients: Dict[str, AsyncOpenAI] = create_model_clients()
    semaphore: asyncio.Semaphore = asyncio.Semaphore(concurrency)
    try:
        results: List[RunResult] = list(await asyncio.gather(*(
            run_one(name, root, server.server_port, backend, clients, semaphore)
            for name in names for _ in range(runs)
        )))
    finally:
        await close_model_clients(clients)
        server.shutdown()
    print("\n=== ÖZET ===\n" + summarize(results, names))
    unexpected: List[str] = sorted(
        entry.name for entry in Path.home().iterdir()
        if entry.name not in home_before and not entry.name.startswith(".")
    )
    print(f"İstenmeyen yan etki (ev dizininde yeni dosya): {unexpected or 'yok'}")
    if json_path:
        Path(json_path).write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description="OmniAgent hız + doğruluk benchmark'ı")
    parser.add_argument("--runs", type=int, required=True, help="Senaryo başına koşu sayısı")
    parser.add_argument("--concurrency", type=int, required=True, help="Aynı anda koşan görev sayısı")
    parser.add_argument("--backend", default=None, help="Başlangıç backend'i (varsayılan: config.DEFAULT_BACKEND)")
    parser.add_argument("--only", default=",".join(CORE_SCENARIOS), help="Virgülle ayrılmış senaryo adları")
    parser.add_argument("--json", default=None, help="Ham sonuçların yazılacağı JSON dosyası")
    arguments: argparse.Namespace = parser.parse_args()
    selected: List[str] = [name for name in arguments.only.split(",") if name]
    unknown: List[str] = [name for name in selected if name not in SCENARIO_NAMES]
    if unknown:
        parser.error(f"Bilinmeyen senaryo: {unknown}; geçerli: {', '.join(SCENARIO_NAMES)}")
    asyncio.run(main(arguments.runs, arguments.concurrency, arguments.backend, selected, arguments.json))
