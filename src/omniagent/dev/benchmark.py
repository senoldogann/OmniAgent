"""
Hız + doğruluk benchmark'ı: deterministik beklenen çıktısı olan senaryoları gerçek modelle
koşturur; başarı oranı, medyan/en kötü süre, tur ve token (önbellek dahil) sayısını raporlar.
Her optimizasyon yalnızca süreyle değil, doğrulukla birlikte ölçülsün diye vardır.

Kullanım:
  .venv/bin/python benchmark.py --runs 3 --concurrency 3
  .venv/bin/python benchmark.py --runs 3 --backend opencode-think --only gun,paralel
  .venv/bin/python benchmark.py --runs 3 --json /tmp/omni_bench.json
  .venv/bin/python benchmark.py --runs 2 --concurrency 1 --only chrome_ilan   # ekranı ve Chrome'u kullanır
  .venv/bin/python benchmark.py --runs 2 --concurrency 1 --only chrome_maas,chrome_form   # kaydırma + form doğrulaması
  .venv/bin/python benchmark.py --runs 4 --concurrency 1 --only ogrenme,ogrenme_bos,hafiza
"""
import argparse
import asyncio
import hashlib
import json
import random
import re
import stat
import statistics
import string
import subprocess
import tempfile
import time
import unicodedata
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread
from typing import Any, Callable, Dict, List, NotRequired, Optional, Tuple, TypedDict
from urllib.parse import parse_qs, urlsplit

from openai import AsyncOpenAI

from omniagent.memory import user as user_memory
from omniagent.config import apply_stored_api_keys
from omniagent.dev.headless_screen import HeadlessPage
from omniagent.dev.headless_screen import install as install_headless_screen
from omniagent.integrations.runtime import IntegrationMetrics
from omniagent.app.agent import RunOptions, RunReport, close_model_clients, create_model_clients, run_agent_with_callback
from omniagent.tools import _require_accessibility, press_key_spec, type_unicode_text

CORE_SCENARIOS: Tuple[str, ...] = ("gun", "satir", "js", "satis", "paralel", "siralama", "json", "ceviri", "sadakat")
STRESS_SCENARIOS: Tuple[str, ...] = ("long_research", "stagnation", "self_repair")
# Hatalardan öğrenme ve kullanıcı hafızası ölçümü. `ogrenme` koşuları aynı deneyim deposunu
# paylaşır (ilk koşu öğrenir, sonrakiler hatırlatmayla hızlanmalı); `ogrenme_bos` aynı görevi her
# koşuda boş depoyla çalıştıran kontroldür. Öğrenmenin birikmesi için ardışık koşmalıdır.
LEARNING_SCENARIOS: Tuple[str, ...] = ("ogrenme", "ogrenme_bos")
MEMORY_SCENARIOS: Tuple[str, ...] = ("hafiza",)
SEQUENTIAL_SCENARIOS: Tuple[str, ...] = ("ogrenme", "self_repair")
# Gerçek ekranı, fareyi ve kullanıcının Chrome'unu kullanan senaryolar: yalnız açıkça seçilince
# ve eşzamanlılık 1 iken koşar. chrome_maas ve chrome_form kullanıcının canlı görevlerindeki iki
# hatayı yeniden üretir: kaydırmadan "tüm ilanlara baktım" demek ve eksik formu "gönderdim" saymak.
GUI_SCENARIOS: Tuple[str, ...] = ("chrome_ilan", "chrome_maas", "chrome_form")
# Takip ve geçmişsiz negatif kontrol ayrı ölçülür; varsayılan başarı/hız paydasını bozmaz.
SCENARIO_NAMES: Tuple[str, ...] = (
    CORE_SCENARIOS + ("takip", "takip_bos") + GUI_SCENARIOS + STRESS_SCENARIOS
    + LEARNING_SCENARIOS + MEMORY_SCENARIOS
)
# Hata mesajı çözümü söylemeyen yerel araç: çözüm ancak --help → kodlar keşfiyle bulunur.
LEARNING_TOOL_SCRIPT: str = """#!/bin/sh
case "$*" in
  "ozet kuzey --birim=adet") echo "OZET: 42";;
  "--help"|"-h"|"help") echo "Kullanım: veri-araci <komut> [seçenekler]. Hata kodları: veri-araci kodlar";;
  "kodlar") echo "E17: ozet komutu --birim=<adet|kg> seçeneği ister"; echo "E10: bilinmeyen komut";;
  ozet*) echo "hata: E17" >&2; exit 2;;
  *) echo "hata: E10" >&2; exit 1;;
esac
"""
JOB_QUERY: str = "senior software developer"
# Gerçek iş ilanı sitesi gibi: arama ve ilan detayı gecikmeli XHR ile gelir, arada iskelet animasyonu döner
JOB_SEARCH_DELAY_SECONDS: float = 0.6
JOB_DETAIL_DELAY_SECONDS: float = 0.7
JOB_LISTINGS: Tuple[Tuple[str, str], ...] = (
    ("Senior Software Developer", "Kuzey Yazılım"), ("Senior Backend Engineer", "Mavi Bulut"),
    ("Staff Software Engineer", "Delta Finans"), ("Senior Full Stack Developer", "Ada Teknoloji"),
    ("Lead Python Developer", "Pera Veri"), ("Senior Platform Engineer", "Ekin Sistem"),
)
JOB_PAGE: str = """<!doctype html><html lang="tr"><head><meta charset="utf-8"><title>İş Arama</title><style>
body{font:16px -apple-system,sans-serif;margin:0;background:#f3f2ef;color:#1d2226}
header{background:#fff;padding:14px 28px;border-bottom:1px solid #ddd;display:flex;gap:16px;align-items:center}
input{font-size:17px;padding:10px 14px;width:420px;border:1px solid #888;border-radius:6px}
main{display:flex;gap:18px;padding:18px 28px}#liste{width:430px;display:flex;flex-direction:column;gap:10px}
.kart{background:#fff;border:1px solid #ddd;border-radius:8px;padding:14px 16px;cursor:pointer}
.kart.secili{box-shadow:inset 4px 0 #0a66c2;background:#eef3f8}
#detay{flex:1;background:#fff;border:1px solid #ddd;border-radius:8px;padding:24px;min-height:420px}
.iskelet{height:20px;margin:14px 0;border-radius:4px;background:linear-gradient(90deg,#eee,#d6d6d6,#eee);
background-size:200% 100%;animation:p 0.9s linear infinite}@keyframes p{from{background-position:200% 0}to{background-position:0 0}}
</style></head><body><header><b>İş Arama</b><input id="arama" placeholder="Pozisyon, şirket veya anahtar kelime" autocomplete="off"></header>
<main><section id="liste"><p>Aramak için pozisyon yazıp Enter'a basın.</p></section>
<section id="detay"><p>Detayı görmek için soldan bir ilan seçin.</p></section></main><script>
const RUN = "__RUN__", liste = document.getElementById('liste'), detay = document.getElementById('detay');
const iskelet = n => '<div class="iskelet"></div>'.repeat(n);
document.getElementById('arama').addEventListener('keydown', async e => {
  if (e.key !== 'Enter') return;
  const q = e.target.value; liste.innerHTML = iskelet(8); detay.innerHTML = '';
  const ilanlar = await (await fetch(`/ara/${RUN}?q=${encodeURIComponent(q)}`)).json();
  liste.innerHTML = `<p>"${q}" için ${ilanlar.length} sonuç</p>`;
  ilanlar.forEach((ilan, i) => {
    const kart = document.createElement('div'); kart.className = 'kart';
    kart.innerHTML = `<b>${ilan.baslik}</b><br>${ilan.sirket} · Uzaktan`;
    kart.onclick = async () => {
      document.querySelectorAll('.kart').forEach(k => k.classList.remove('secili')); kart.classList.add('secili');
      detay.innerHTML = iskelet(10);
      const d = await (await fetch(`/ilan/${RUN}/${i}?q=${encodeURIComponent(q)}`)).json();
      detay.innerHTML = `<h2>${d.baslik}</h2><p>${d.sirket} · Uzaktan · Tam zamanlı</p>
        <p>İlan kodu: <b>${d.kod}</b></p><p>Python, dağıtık sistemler ve bulut altyapısında deneyim aranıyor.</p>`;
    };
    liste.appendChild(kart);
  });
});
</script></body></html>"""
# chrome_maas: iki bölmeli ilan sitesi. Liste ve açıklama ayrı kaydırılır; en yüksek maaş listenin
# ekran dışındaki 8. ilanında, maaş satırı her açıklamanın en altındadır. Görünen ilk ilanlara
# bakıp bitirmek yanlış cevap verir.
SALARY_LISTINGS: Tuple[Tuple[str, str, int], ...] = (
    ("Full Stack Developer", "Kuzey Yazılım", 4800), ("Senior Backend Engineer", "Mavi Bulut", 5100),
    ("Frontend Developer", "Delta Finans", 4300), ("Platform Engineer", "Ada Teknoloji", 5200),
    ("Data Engineer", "Pera Veri", 4700), ("Mobile Developer", "Ekin Sistem", 4500),
    ("DevOps Engineer", "Nord Pilvi", 4900), ("Lead Full Stack Developer", "Aurora Labs", 6300),
    ("QA Engineer", "Tunturi Soft", 3900), ("Machine Learning Engineer", "Revontuli AI", 5600),
)
SALARY_DETAIL_DELAY_SECONDS: float = 0.4
SALARY_PARAGRAPHS: Tuple[str, ...] = tuple(
    f"{topic}: ekip, ürünün uçtan uca kalitesinden sorumludur; kod incelemesi, otomatik testler, "
    "gözlemlenebilirlik ve müşteri geri bildirimi günlük işin parçasıdır. Uzaktan ve ofiste esnek çalışılır."
    for topic in ("Rol", "Ekip", "Sorumluluklar", "Teknolojiler", "Süreç", "Kültür", "Gelişim", "Araçlar",
                  "Müşteri", "Kalite", "Güvenlik", "İşe alım", "Yan haklar", "Konum")
)
SALARY_PAGE: str = """<!doctype html><html lang="tr"><head><meta charset="utf-8"><title>İlanlar</title><style>
body{font:15px -apple-system,sans-serif;margin:0;background:#f3f2ef;color:#1d2226}
header{background:#fff;padding:12px 24px;border-bottom:1px solid #ddd;font-weight:600}
main{display:flex;gap:16px;padding:16px 24px;height:calc(100vh - 50px);box-sizing:border-box}
#liste{width:380px;overflow-y:auto;display:flex;flex-direction:column;gap:12px;padding-right:6px}
.kart{background:#fff;border:1px solid #ddd;border-radius:8px;padding:16px;cursor:pointer;min-height:118px;box-sizing:border-box}
.kart.secili{box-shadow:inset 4px 0 #0a66c2;background:#eef3f8}
#detay{flex:1;overflow-y:auto;background:#fff;border:1px solid #ddd;border-radius:8px;padding:24px}
#detay p{line-height:1.7}
</style></head><body><header>İş ilanları · 10 sonuç</header><main><section id="liste"></section>
<section id="detay"><p>Detayı görmek için soldan bir ilan seçin.</p></section></main><script>
const RUN = "__RUN__", ILANLAR = __ILANLAR__;
const liste = document.getElementById('liste'), detay = document.getElementById('detay');
ILANLAR.forEach((ilan, i) => {
  const kart = document.createElement('div'); kart.className = 'kart';
  kart.innerHTML = `<b>${ilan.baslik}</b><br>${ilan.sirket}<br>Oulu (Hibrit)`;
  kart.onclick = async () => {
    document.querySelectorAll('.kart').forEach(k => k.classList.remove('secili')); kart.classList.add('secili');
    detay.innerHTML = '<p>Yükleniyor…</p>'; detay.scrollTop = 0;
    const d = await (await fetch(`/maas-ilan/${RUN}/${i}`)).json();
    detay.innerHTML = `<h2>${d.baslik}</h2><p>${d.sirket} · Oulu (Hibrit) · Tam zamanlı</p>`
      + d.paragraflar.map(p => `<p>${p}</p>`).join('')
      + `<h3>Ücret</h3><p>Aylık maaş: <b>${d.maas} €</b></p><p>İlan kodu: <b>${d.kod}</b></p>`;
    detay.scrollTop = 0;
  };
  liste.appendChild(kart);
});
</script></body></html>"""
# chrome_form: OmaLeima benzeri form. Konu listesi, zorunlu alanlar, gizlilik onayı, özel "insan
# mısın" kutusu ve ekran dışındaki gönder düğmesi. Alanda Enter formu eksik gönderip hata gösterir.
# Başarı modelin iddiasıyla değil sunucunun aldığı geçerli gönderimle ölçülür.
FORM_PAGE: str = """<!doctype html><html lang="fi"><head><meta charset="utf-8"><title>Ota yhteyttä</title><style>
body{font:16px -apple-system,sans-serif;margin:0;background:#0f1110;color:#e8e8e8}
.hero{height:420px;background:linear-gradient(135deg,#1d2a12,#0f1110);display:flex;align-items:flex-end;padding:32px;font-size:44px;font-weight:700;box-sizing:border-box}
form,#kiitos{max-width:760px;margin:24px auto;padding:28px;background:#181a18;border:1px solid #2a2d2a;border-radius:14px}
label{display:block;margin:16px 0 6px;font-weight:600}
input,select,textarea{width:100%;box-sizing:border-box;padding:12px;border-radius:8px;border:1px solid #3a3d3a;background:#101210;color:#eee;font-size:16px}
textarea{height:130px}.rivi{display:flex;gap:16px}.rivi>div{flex:1}
.valinta{display:flex;gap:10px;align-items:center;margin:18px 0}.valinta input{width:auto}.valinta label{margin:0;font-weight:400}
#ihminen{display:flex;align-items:center;gap:12px;border:1px solid #555;padding:14px;border-radius:6px;width:320px;cursor:pointer;margin:16px 0}
#ihminen .laatikko{width:22px;height:22px;border:2px solid #aaa;border-radius:3px}
#ihminen.ok .laatikko{background:#7bd88f;border-color:#7bd88f}
button{background:#d4ff3a;color:#111;font-weight:700;border:0;border-radius:24px;padding:14px 28px;font-size:16px;cursor:pointer}
#virhe{color:#ff8a80;margin:12px 0;min-height:20px}#kiitos{display:none;font-size:22px;background:#183018}
</style></head><body><div class="hero">Ota yhteyttä</div>
<form id="lomake" novalidate>
<label for="aihe">Aihe *</label><select id="aihe"><option value="">Valitse aihe</option><option>Yleinen kysely</option><option>Tarjouspyyntö</option><option>Yhteistyö</option></select>
<div class="rivi"><div><label for="nimi">Nimi *</label><input id="nimi" autocomplete="off"></div>
<div><label for="sposti">Sähköposti *</label><input id="sposti" type="email" autocomplete="off"></div></div>
<label for="org">Organisaatio</label><input id="org" autocomplete="off">
<label for="viesti">Viesti *</label><textarea id="viesti"></textarea>
<div class="valinta"><input type="checkbox" id="tietosuoja"><label for="tietosuoja">Olen lukenut tietosuojaselosteen *</label></div>
<div id="ihminen" role="checkbox" aria-checked="false"><div class="laatikko"></div><span>Vahvista, että olet ihminen</span></div>
<div id="virhe"></div><button type="submit">Lähetä viesti</button>
</form><div id="kiitos">Kiitos! Viestisi on lähetetty.</div><script>
const RUN = "__RUN__", kentta = id => document.getElementById(id), ihminen = kentta('ihminen');
ihminen.onclick = () => { ihminen.classList.toggle('ok'); ihminen.setAttribute('aria-checked', ihminen.classList.contains('ok')); };
kentta('lomake').addEventListener('submit', async e => {
  e.preventDefault();
  const arvot = {aihe: kentta('aihe').value, nimi: kentta('nimi').value.trim(), sposti: kentta('sposti').value.trim(),
    org: kentta('org').value.trim(), viesti: kentta('viesti').value.trim(), tietosuoja: kentta('tietosuoja').checked,
    ihminen: ihminen.classList.contains('ok')};
  const puuttuu = [];
  if (!arvot.aihe) puuttuu.push('Aihe'); if (!arvot.nimi) puuttuu.push('Nimi');
  if (!/^\\S+@\\S+\\.\\S+$/.test(arvot.sposti)) puuttuu.push('Sähköposti'); if (arvot.viesti.length < 10) puuttuu.push('Viesti');
  if (!arvot.tietosuoja) puuttuu.push('Tietosuoja'); if (!arvot.ihminen) puuttuu.push('Ihmistarkistus');
  if (puuttuu.length) { kentta('virhe').textContent = 'Tarkista lomakkeen kentät: ' + puuttuu.join(', '); return; }
  await fetch(`/form-submit/${RUN}`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(arvot)});
  kentta('lomake').style.display = 'none'; kentta('kiitos').style.display = 'block'; window.scrollTo(0, 0);
});
</script></body></html>"""
FORM_EXPECTED: Dict[str, str] = {"aihe": "Tarjouspyyntö", "nimi": "Senol Dogan", "sposti": "senoldogan@hotmail.com"}


class ResearchCandidate(TypedDict):
    index: int
    owner: str
    name: str
    stars: int
    forks: int
    open_issues: int
    latest_commit: str
    language: str
    archived: bool
    summary: str


RESEARCH_CANDIDATES: Tuple[ResearchCandidate, ...] = (
    {"index": 0, "owner": "acme", "name": "alpha", "stars": 50_000, "forks": 5_000,
     "open_issues": 120, "latest_commit": "2026-09-10", "language": "TypeScript",
     "archived": False, "summary": "Typed editor platform."},
    {"index": 1, "owner": "acme", "name": "beta", "stars": 45_000, "forks": 4_500,
     "open_issues": 80, "latest_commit": "2026-09-08", "language": "TypeScript",
     "archived": True, "summary": "Archived UI toolkit."},
    {"index": 2, "owner": "north", "name": "gamma", "stars": 35_000, "forks": 3_500,
     "open_issues": 70, "latest_commit": "2026-08-30", "language": "TypeScript",
     "archived": False, "summary": "Server framework for TypeScript."},
    {"index": 3, "owner": "north", "name": "delta", "stars": 33_000, "forks": 3_300,
     "open_issues": 60, "latest_commit": "2026-09-01", "language": "JavaScript",
     "archived": False, "summary": "JavaScript application framework."},
    {"index": 4, "owner": "east", "name": "epsilon", "stars": 25_000, "forks": 2_500,
     "open_issues": 50, "latest_commit": "2026-09-12", "language": "TypeScript",
     "archived": False, "summary": "Type-safe data client."},
    {"index": 5, "owner": "east", "name": "zeta", "stars": 22_000, "forks": 2_200,
     "open_issues": 40, "latest_commit": "2024-06-01", "language": "TypeScript",
     "archived": False, "summary": "Stale build system."},
    {"index": 6, "owner": "west", "name": "eta", "stars": 20_000, "forks": 2_000,
     "open_issues": 30, "latest_commit": "2026-09-15", "language": "TypeScript",
     "archived": False, "summary": "Browser automation toolkit."},
    {"index": 7, "owner": "west", "name": "theta", "stars": 15_000, "forks": 1_500,
     "open_issues": 20, "latest_commit": "2026-08-20", "language": "TypeScript",
     "archived": False, "summary": "Component development workbench."},
    {"index": 8, "owner": "extra", "name": "iota", "stars": 12_000, "forks": 1_200,
     "open_issues": 10, "latest_commit": "2026-09-02", "language": "TypeScript",
     "archived": False, "summary": "Extra valid candidate that should not be needed."},
    {"index": 9, "owner": "extra", "name": "kappa", "stars": 11_000, "forks": 1_100,
     "open_issues": 9, "latest_commit": "2026-09-03", "language": "TypeScript",
     "archived": False, "summary": "Second extra candidate that should not be needed."},
)
RESEARCH_MIN_COMMIT_DATE: str = "2025-09-24"
_REQUEST_COUNTS: Dict[Tuple[str, str], int] = {}
_REQUEST_LOCK: Lock = Lock()


def _research_valid(candidate: ResearchCandidate) -> bool:
    return (
        candidate["stars"] >= 10_000
        and not candidate["archived"]
        and candidate["language"] == "TypeScript"
        and candidate["open_issues"] > 0
        and candidate["latest_commit"] >= RESEARCH_MIN_COMMIT_DATE
    )


def expected_research_selection() -> List[ResearchCandidate]:
    """İlk 8 aday içinde kriterleri geçen ilk 5'i, yıldız azalan sırada döndürür."""
    valid = [candidate for candidate in RESEARCH_CANDIDATES[:8] if _research_valid(candidate)][:5]
    return sorted(valid, key=lambda item: item["stars"], reverse=True)


def research_statistics(selected: List[ResearchCandidate]) -> Tuple[int, float, float]:
    total = sum(item["stars"] for item in selected)
    average = total / len(selected)
    ratio = round(max(item["stars"] for item in selected) / min(item["stars"] for item in selected), 2)
    return total, average, ratio


def _record_request(run_id: str, key: str) -> None:
    with _REQUEST_LOCK:
        pair = (run_id, key)
        _REQUEST_COUNTS[pair] = _REQUEST_COUNTS.get(pair, 0) + 1


def _request_count(run_id: str, key: str) -> int:
    with _REQUEST_LOCK:
        return _REQUEST_COUNTS.get((run_id, key), 0)


def _clear_request_counts(run_id: str) -> None:
    with _REQUEST_LOCK:
        for pair in [pair for pair in _REQUEST_COUNTS if pair[0] == run_id]:
            del _REQUEST_COUNTS[pair]


# chrome_form gönderimleri: çalıştırma kimliği -> sunucunun aldığı form değerleri
_FORM_SUBMISSIONS: Dict[str, List[Dict[str, object]]] = {}


def _record_form_submission(run_id: str, values: Dict[str, object]) -> None:
    with _REQUEST_LOCK:
        _FORM_SUBMISSIONS.setdefault(run_id, []).append(dict(values))


def _ascii_upper(text: str) -> str:
    """'Gönderildi' ile 'GONDERILDI' aynı sayılsın diye büyük harfe çevirip aksanları atar. Saf."""
    decomposed: str = unicodedata.normalize("NFKD", text.upper())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def form_submission_valid(values: Dict[str, object]) -> bool:
    """Sunucuya gelen gönderim hedefteki bütün zorunlu değerleri taşıyor mu? Saf."""
    return (
        str(values.get("aihe", "")) == FORM_EXPECTED["aihe"]
        and str(values.get("nimi", "")).casefold() == FORM_EXPECTED["nimi"].casefold()
        and str(values.get("sposti", "")).casefold() == FORM_EXPECTED["sposti"]
        and len(str(values.get("viesti", "")).strip()) >= 10
        and values.get("tietosuoja") is True and values.get("ihminen") is True
    )


def form_check(run_id: str, outcome: str) -> Tuple[bool, str]:
    """Başarı = sunucu geçerli gönderim aldı VE model bunu bildirdi; gönderim yokken 'gönderildi' yalancı başarıdır."""
    with _REQUEST_LOCK:
        submissions: List[Dict[str, object]] = list(_FORM_SUBMISSIONS.get(run_id, []))
    valid: bool = any(form_submission_valid(item) for item in submissions)
    claimed: bool = "FORM: GONDERILDI" in _ascii_upper(outcome)
    if claimed and not valid:
        return False, f"YALANCI BAŞARI: gönderildi denildi, sunucu geçerli gönderim almadı ({len(submissions)} gönderim)"
    return valid and claimed, f"sunucu: {len(submissions)} gönderim, geçerli={valid}; gönderildi iddiası={claimed}"


def salary_check(run_id: str, outcome: str) -> Tuple[bool, str]:
    """En yüksek maaşlı ilanın kodu ve maaşı yazıldı mı; kaç ilanın detayı gerçekten açıldı?"""
    best: int = max(range(len(SALARY_LISTINGS)), key=lambda index: SALARY_LISTINGS[index][2])
    code: str = job_code(run_id, "maas", best)
    salary: int = SALARY_LISTINGS[best][2]
    opened: int = sum(1 for index in range(len(SALARY_LISTINGS)) if _request_count(run_id, f"maas:{index}") > 0)
    ok: bool = code in outcome and str(salary) in re.sub(r"[\s.,]", "", outcome)
    return ok, f"beklenen={code} {salary}; detayı açılan ilan={opened}/{len(SALARY_LISTINGS)}"


class Scenario(TypedDict):
    goal: str
    check: Callable[[str], Tuple[bool, str]]
    first_goal: NotRequired[str]
    expect_success: NotRequired[bool]
    reason_contains: NotRequired[str]
    run_mode: NotRequired[str]
    # Koşular arası paylaşılan deneyim deposu (öğrenme ölçümü); yoksa koşuya özel depo kullanılır
    experience_file: NotRequired[str]


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
    model_seconds: float
    tool_seconds: float
    uncached_prompt_tokens: int
    observations: int
    observations_reused: int
    duplicate_navigation: int
    semantic_progress_events: int
    fast_loop_stagnation_events: int
    fast_loop_replans: int
    fast_loop_delivery_entries: int
    backend: str
    integrations: IntegrationMetrics
    experience_hints: int


def job_code(run_id: str, query: str, index: int) -> str:
    """İlan kodu çalıştırma kimliği ve yazılan aramadan türetilir: yanlış arama yanlış kod verir. Saf."""
    normalized: str = " ".join(query.casefold().split())
    return "IL-" + hashlib.sha256(f"{run_id}|{normalized}|{index}".encode()).hexdigest()[:6].upper()


class BenchmarkHandler(BaseHTTPRequestHandler):
    """fetch_raw senaryosuna sabit JSON, Chrome senaryosuna gecikmeli iş ilanı sitesi sunar."""

    def _send(self, content_type: str, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parts: List[str] = urlsplit(self.path).path.strip("/").split("/")
        query: str = parse_qs(urlsplit(self.path).query).get("q", [""])[0]
        if parts[0] == "research" and len(parts) >= 3:
            run_id: str = parts[1]
            kind: str = parts[2]
            port: int = int(getattr(self.server, "server_port"))
            if kind == "index":
                _record_request(run_id, "index")
                rows = [
                    {"index": candidate["index"],
                     "url": f"http://127.0.0.1:{port}/research/{run_id}/candidate/{candidate['index']}"}
                    for candidate in RESEARCH_CANDIDATES
                ]
                self._send("application/json", json.dumps({"candidates": rows}).encode())
            elif kind == "candidate" and len(parts) == 4:
                index = int(parts[3])
                _record_request(run_id, f"candidate:{index}")
                self._send("application/json", json.dumps(RESEARCH_CANDIDATES[index]).encode())
            elif kind == "verify" and len(parts) == 4:
                index = int(parts[3])
                _record_request(run_id, f"verify:{index}")
                candidate = RESEARCH_CANDIDATES[index]
                self._send("application/json", json.dumps({
                    "owner": candidate["owner"], "name": candidate["name"], "stars": candidate["stars"],
                }).encode())
            else:
                self.send_error(404)
        elif parts[0] == "stagnation" and len(parts) == 2:
            run_id = parts[1]
            _record_request(run_id, "stagnation")
            self._send("application/json", json.dumps({"status": "pending", "required": "ready"}).encode())
        elif parts[0] == "ilanlar":
            self._send("text/html; charset=utf-8", JOB_PAGE.replace("__RUN__", parts[1]).encode())
        elif parts[0] == "ara":
            time.sleep(JOB_SEARCH_DELAY_SECONDS)
            rows: List[Dict[str, str]] = [{"baslik": title, "sirket": company} for title, company in JOB_LISTINGS]
            self._send("application/json", json.dumps(rows if query.strip() else []).encode())
        elif parts[0] == "ilan":
            time.sleep(JOB_DETAIL_DELAY_SECONDS)
            title, company = JOB_LISTINGS[int(parts[2])]
            detail: Dict[str, str] = {"baslik": title, "sirket": company, "kod": job_code(parts[1], query, int(parts[2]))}
            self._send("application/json", json.dumps(detail).encode())
        elif parts[0] == "maas" and len(parts) == 2:
            rows = [{"baslik": title, "sirket": company} for title, company, _salary in SALARY_LISTINGS]
            page: str = SALARY_PAGE.replace("__RUN__", parts[1]).replace("__ILANLAR__", json.dumps(rows, ensure_ascii=False))
            self._send("text/html; charset=utf-8", page.encode())
        elif parts[0] == "maas-ilan" and len(parts) == 3:
            time.sleep(SALARY_DETAIL_DELAY_SECONDS)
            index: int = int(parts[2])
            _record_request(parts[1], f"maas:{index}")
            title, company, salary = SALARY_LISTINGS[index]
            self._send("application/json; charset=utf-8", json.dumps({
                "baslik": title, "sirket": company, "maas": salary, "kod": job_code(parts[1], "maas", index),
                "paragraflar": list(SALARY_PARAGRAPHS),
            }, ensure_ascii=False).encode())
        elif parts[0] == "form" and len(parts) == 2:
            self._send("text/html; charset=utf-8", FORM_PAGE.replace("__RUN__", parts[1]).encode())
        else:
            run_id: str = parts[-1]
            self._send("application/json", json.dumps(
                {"slideshow": {"author": f"Yours Truly {run_id}", "title": "Sample"}}).encode())

    def do_POST(self) -> None:
        """chrome_form gönderimini kaydeder; sayfa yalnız istemci doğrulamasını geçen formu gönderir."""
        parts: List[str] = urlsplit(self.path).path.strip("/").split("/")
        if parts[0] != "form-submit" or len(parts) != 2:
            self.send_error(404)
            return
        body: bytes = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        try:
            values: object = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_error(400)
            return
        if not isinstance(values, dict):
            self.send_error(400)
            return
        _record_form_submission(parts[1], values)
        self._send("application/json", b'{"ok": true}')

    def log_message(self, format: str, *args: object) -> None:
        return


CHROME_BENCH_APPLESCRIPT_TIMEOUT_SECONDS: float = 3.0


def run_osascript(script: str, argument: str) -> None:
    """Chrome'u AppleScript ile hazırlar/temizler; hata çıktısıyla açık hata verir."""
    result: subprocess.CompletedProcess[str] = subprocess.run(
        ["osascript", "-e", script, argument], capture_output=True, text=True,
        timeout=CHROME_BENCH_APPLESCRIPT_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise RuntimeError(f"osascript başarısız (çıkış {result.returncode}): {result.stderr.strip()}")


def _activate_visible_chrome() -> None:
    """Benchmark fallback'ı için yalnız kullanıcının mevcut Chrome uygulamasını öne getirir."""
    _require_accessibility()
    result: subprocess.CompletedProcess[str] = subprocess.run(
        ["open", "-a", "Google Chrome"], capture_output=True, text=True, timeout=5)
    if result.returncode != 0:
        raise RuntimeError(f"Google Chrome öne getirilemedi: {result.stderr.strip()}")
    time.sleep(0.2)


def open_chrome_test_tab(url: str) -> None:
    """
    GUI senaryosu kullanıcının sekmelerini değiştirmesin diye öndeki Chrome penceresine test
    kökeninde ayrı sekme açar. Son pencereyi kapatıp yenisini açmak Chrome profilini boşaltıp
    yeniden yüklediği için (bir koşuda profil hatası diyaloğu çıktı) açık pencere önkoşuldur.
    """
    try:
        run_osascript(
            'on run argv\ntell application "Google Chrome"\n'
            'if (count of windows) is 0 then error "GUI senaryosu için açık bir Chrome penceresi gerekli."\n'
            'tell front window to make new tab with properties {URL:(item 1 of argv)}\nend tell\nend run', url)
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        _activate_visible_chrome()
        press_key_spec("cmd+t")
        type_unicode_text(url)
        press_key_spec("enter")


def close_chrome_test_tabs(origin: str) -> None:
    """Yalnız test kökenindeki sekmeleri kapatır; kullanıcının diğer sekmelerine dokunmaz."""
    try:
        run_osascript(
            'on run argv\nset testOrigin to item 1 of argv\ntell application "Google Chrome"\n'
            'set windowIds to id of every window\nrepeat with windowId in windowIds\n'
            'close (every tab of window id windowId whose URL starts with testOrigin)\nend repeat\nend tell\nend run',
            origin)
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        # open_chrome_test_tab fallback'ı ayrı bir test sekmesi açar ve senaryo onu aktif bırakır.
        # Apple Events yoksa URL listesi okunamadığı için yalnız bu aktif test sekmesini kapat.
        _activate_visible_chrome()
        press_key_spec("cmd+w")


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
    if name == "long_research":
        report_path: Path = run_dir / "github-typescript-research.txt"
        selected: List[ResearchCandidate] = expected_research_selection()
        total, average, ratio = research_statistics(selected)
        index_url: str = f"http://127.0.0.1:{port}/research/{run_id}/index"
        verify_zero: str = f"http://127.0.0.1:{port}/research/{run_id}/verify/0"
        verify_seven: str = f"http://127.0.0.1:{port}/research/{run_id}/verify/7"

        def check_long_research(outcome: str) -> Tuple[bool, str]:
            if not report_path.exists():
                return False, f"rapor yok: {report_path}"
            report: str = report_path.read_text(encoding="utf-8")
            for candidate in selected:
                full_name: str = f"{candidate['owner']}/{candidate['name']}"
                if full_name not in report:
                    return False, f"raporda seçili repo yok: {full_name}"
            expected_stats = (
                f"TOTAL_STARS: {total}",
                f"AVERAGE_STARS: {average:.2f}",
                f"RATIO: {ratio:.2f}",
                "CHECKED: 8",
                "SELECTED: 5",
            )
            for expected in expected_stats:
                if expected not in report:
                    return False, f"rapor alanı eksik: {expected}"
            checked: List[int] = [
                index for index in range(len(RESEARCH_CANDIDATES))
                if _request_count(run_id, f"candidate:{index}") > 0
            ]
            if checked != list(range(8)):
                return False, f"aday erişimleri={checked}; beklenen=0..7"
            if _request_count(run_id, "verify:0") < 1 or _request_count(run_id, "verify:7") < 1:
                return False, "iki final revalidation endpoint'i çağrılmadı"
            if "LONG_RESEARCH: DONE" not in outcome:
                return False, f"final cevap yanlış: {outcome[:120]!r}"
            return True, "8 aday kontrol edildi, 5 seçildi, teslim ve revalidation tamam"

        return {
            "goal": (
                f"Yalnız fetch_raw, write_file ve gerekirse execute_js kullan. Önce {index_url} adresinden aday listesini al. "
                "Adayları verilen sırayla kontrol et. Kriterler: stars>=10000, archived=false, language=TypeScript, "
                f"open_issues>0, latest_commit>={RESEARCH_MIN_COMMIT_DATE}. En az 8 aday kontrol et; ilk 8 içinde tam 5 "
                "geçerli aday bulunduğunda 9. ve 10. adayı açma. Geçerli 5 adayı stars azalan sırada raporla; her biri için "
                "owner/name, stars, forks, open_issues, latest_commit, language ve summary yaz. Toplam yıldız, ortalama "
                f"yıldız ve highest/lowest oranını hesapla. {report_path} dosyasını oluştur. Raporun sonunda satırlar tam "
                "'TOTAL_STARS: <n>', 'AVERAGE_STARS: <iki ondalık>', 'RATIO: <iki ondalık>', 'CHECKED: 8', 'SELECTED: 5' "
                f"olsun. Raporu yazdıktan sonra {verify_zero} ve {verify_seven} adreslerini fetch_raw ile yeniden açıp "
                "owner/name ve stars değerlerini doğrula; uyuşmazlık varsa raporu düzelt. Son cevap yalnız 'LONG_RESEARCH: DONE' olsun."
            ),
            "check": check_long_research,
            "run_mode": "autonomous",
        }
    if name == "stagnation":
        endpoint: str = f"http://127.0.0.1:{port}/stagnation/{run_id}"
        return {
            "goal": (
                f"Yalnız fetch_raw kullan. {endpoint} adresindeki status 'ready' olana kadar aynı endpoint'i kontrol etmeye "
                "devam et. status='pending' iken görevi başarılı bitirme veya hazırmış gibi cevap verme."
            ),
            "check": lambda outcome: (True, "Fast Loop bounded failure bekleniyor"),
            "expect_success": False,
            "reason_contains": "ilerleme",
            "run_mode": "autonomous",
        }
    if name == "chrome_ilan":
        expected_codes: str = ",".join(job_code(run_id, JOB_QUERY, index) for index in range(3))
        return {"goal": f"Açık Google Chrome oturumunu kullanarak http://127.0.0.1:{port}/ilanlar/{run_id} adresini aç. "
                        f"Arama kutusuna '{JOB_QUERY}' yaz ve Enter'a bas. Çıkan ilk 3 ilanı sırayla tek tek aç ve "
                        "her ilanın detayındaki ilan kodunu oku. Tek satır 'KODLAR: <k1>,<k2>,<k3>' yaz.",
                "check": lambda o: (expected_codes in o.replace(" ", ""), f"beklenen={expected_codes}")}
    if name == "chrome_maas":
        return {"goal": f"Açık Google Chrome oturumunu kullanarak http://127.0.0.1:{port}/maas/{run_id} adresini aç. "
                        "Listedeki tüm ilanları incele ve aylık maaşı en yüksek olan ilanı bul. "
                        "Tek satır 'EN_YUKSEK: <ilan kodu> <aylık maaş>' yaz.",
                "check": lambda outcome: salary_check(run_id, outcome)}
    if name == "chrome_form":
        return {"goal": (f"Açık Google Chrome oturumunu kullanarak http://127.0.0.1:{port}/form/{run_id} adresindeki "
                         "iletişim formunu doldur ve gönder: aihe 'Tarjouspyyntö', nimi 'Senol Dogan', sähköposti "
                         "'senoldogan@hotmail.com', viesti olarak kısa bir teklif isteği yaz. Tüm zorunlu onay "
                         "kutularını işaretle ve formu gönder. Sonunda tek satır 'FORM: GONDERILDI' ya da "
                         "gönderemediysen 'FORM: GONDERILEMEDI' yaz."),
                "check": lambda outcome: form_check(run_id, outcome)}
    if name == "self_repair":
        tool = run_dir / "veri-araci"
        tool.write_text(LEARNING_TOOL_SCRIPT, encoding="utf-8")
        tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
        return {
            "goal": (
                f"'{tool} ozet kuzey' komutuyla kuzey bölgesinin özetini al. Komut hata verirse nedeni araçtan "
                "öğrenip düzeltilmiş çağrıyı dene; doğruladığın değeri tek satır 'OZET: <değer>' olarak yaz."
            ),
            "check": lambda o: ("OZET: 42" in o, ""),
            "experience_file": str(run_dir.parent / "self-repair-experience.json"),
            "run_mode": "autonomous",
        }
    if name in LEARNING_SCENARIOS:
        tool: Path = run_dir / "veri-araci"
        tool.write_text(LEARNING_TOOL_SCRIPT, encoding="utf-8")
        tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
        learning: Scenario = {
            "goal": f"'{tool} ozet kuzey' komutuyla kuzey bölgesinin özetini al ve çıktıdaki değeri tek satır "
                    "'OZET: <değer>' olarak yaz.",
            "check": lambda o: ("OZET: 42" in o, ""),
        }
        if name == "ogrenme":
            learning["experience_file"] = str(run_dir.parent / "ogrenme-deneyim.json")
        return learning
    if name == "hafiza":
        reports: Path = run_dir / "raporlar"
        target: Path = reports / f"ozet-{run_id}.txt"
        user_memory.save_memory(str(run_dir / "user_memory.json"), user_memory.remember_preference(
            user_memory.empty_state(), "rapor_klasoru", str(reports), "path", "2026-09-24T10:00:00+00:00",
        ))

        def check_hafiza(outcome: str) -> Tuple[bool, str]:
            content: Optional[str] = _read_stripped(target)
            return content == f"HAFIZA-{run_id}", f"icerik={content}"
        return {"goal": f"Rapor klasörüme ozet-{run_id}.txt adlı bir dosya oluştur ve içine tam olarak "
                        f"'HAFIZA-{run_id}' yaz. Tek satır 'TAMAM' yaz.",
                "check": check_hafiza}
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


class GuiStage(TypedDict):
    """GUI senaryosunun ekran hazırlığı: test sayfasını açma ve test sekmelerini kapatma."""
    open_page: Callable[[str], None]
    close_pages: Callable[[str], None]


# Gerçek ekran: kullanıcının Chrome'unda ayrı test sekmesi açılır ve sonunda yalnız o kapatılır
CHROME_STAGE: GuiStage = {"open_page": open_chrome_test_tab, "close_pages": close_chrome_test_tabs}


def headless_stage(page: HeadlessPage) -> GuiStage:
    """Görünmez ekran: aynı sayfa her koşuda test adresine gider; kapatılacak sekme yoktur. Saf."""
    return {"open_page": page.goto, "close_pages": lambda origin: None}


async def run_one(
    name: str, root: Path, port: int, backend: Optional[str],
    clients: Dict[str, AsyncOpenAI], semaphore: asyncio.Semaphore, gui_stage: GuiStage,
) -> RunResult:
    """Bir senaryoyu tek kez koşturur ve sonucu denetler."""
    async with semaphore:
        run_id: str = uuid.uuid4().hex[:8]
        run_dir: Path = root / f"{name}-{run_id}"
        run_dir.mkdir(parents=True)
        _clear_request_counts(run_id)
        scenario: Scenario = build_scenario(name, run_dir, run_id, port)
        options: RunOptions = {
            "requested_backend": backend, "should_stop": lambda: False,
            "state_file": str(run_dir / "memory.json"), "history": [],
        }
        if scenario.get("run_mode"):
            options["run_mode"] = scenario["run_mode"]
        if scenario.get("experience_file"):
            options["experience_file"] = scenario["experience_file"]
        first_ok = True
        if name in ("takip", "takip_bos"):
            first = await run_agent_with_callback(scenario["first_goal"], lambda event: None, options, clients)
            first_ok = first["success"] and f"veri-{run_id}.txt" in first["outcome"]
            if name == "takip":
                options["history"] = [first["exchange"]]
        test_origin: str = f"http://127.0.0.1:{port}/"
        if name in GUI_SCENARIOS:
            await asyncio.to_thread(gui_stage["open_page"], test_origin + "hazir")
        # Takipte yalnızca ikinci mesajın maliyeti ölçülür; ilk mesaj her iki kolda aynıdır.
        started: float = time.monotonic()
        try:
            report: RunReport = await run_agent_with_callback(scenario["goal"], lambda event: None, options, clients)
            elapsed: float = round(time.monotonic() - started, 2)
        finally:
            if name in GUI_SCENARIOS:
                await asyncio.to_thread(gui_stage["close_pages"], test_origin)
        ok, detail = scenario["check"](report["outcome"])
        expected_success: bool = scenario.get("expect_success", True)
        expected_reason: Optional[str] = scenario.get("reason_contains")
        reason_ok: bool = expected_reason is None or expected_reason.casefold() in report["reason"].casefold()
        ok = ok and report["success"] == expected_success and reason_ok and first_ok
        metrics = report["metrics"]
        result: RunResult = {
            "name": name, "ok": ok, "detail": detail, "outcome": report["outcome"][:300],
            "elapsed_seconds": elapsed, "turns": metrics["turns"],
            "tool_calls": metrics["tool_calls"], "prompt_tokens": metrics["prompt_tokens"],
            "cached_tokens": metrics["cached_tokens"], "completion_tokens": metrics["completion_tokens"],
            "model_seconds": metrics.get("model_seconds", 0.0), "tool_seconds": metrics.get("tool_seconds", 0.0),
            "uncached_prompt_tokens": metrics.get("uncached_prompt_tokens", max(0, metrics["prompt_tokens"] - metrics["cached_tokens"])),
            "observations": metrics.get("observations", 0),
            "observations_reused": metrics.get("observations_reused", 0),
            "duplicate_navigation": metrics.get("duplicate_navigation", 0),
            "semantic_progress_events": metrics.get("semantic_progress_events", 0),
            "fast_loop_stagnation_events": metrics.get("fast_loop_stagnation_events", 0),
            "fast_loop_replans": metrics.get("fast_loop_replans", 0),
            "fast_loop_delivery_entries": metrics.get("fast_loop_delivery_entries", 0),
            "backend": metrics["backend"],
            "integrations": metrics.get("integrations", {}),
            "experience_hints": metrics.get("experience_hints", 0),
        }
        print(f"{'✓' if ok else '✗'} {name:9s} {result['elapsed_seconds']:5.1f}s tur={result['turns']} "
              f"araç={result['tool_calls']} model={result['model_seconds']:.1f}s araç_süresi={result['tool_seconds']:.1f}s "
              f"ders={result['experience_hints']} backend={result['backend']} | {report['outcome'][:70]!r} {detail[:80]}",
              flush=True)
        return result


def summarize(results: List[RunResult], names: List[str]) -> str:
    """Senaryo bazında başarı, medyan/maks süre, medyan tur ve toplam önbellek oranını raporlar. Saf."""
    lines: List[str] = [f"{'senaryo':9s} başarı  medyan   maks  tur"]
    for name in names:
        rows: List[RunResult] = [r for r in results if r["name"] == name]
        times: List[float] = [r["elapsed_seconds"] for r in rows]
        lines.append(
            f"{name:9s} {sum(r['ok'] for r in rows)}/{len(rows):<4d} {statistics.median(times):5.1f}s {max(times):5.1f}s "
            f"{statistics.median([r['turns'] for r in rows]):4.1f}  koşu sırasıyla tur={','.join(str(r['turns']) for r in rows)}"
        )
    all_times: List[float] = [r["elapsed_seconds"] for r in results]
    prompt: int = sum(r["prompt_tokens"] for r in results)
    cached: int = sum(r["cached_tokens"] for r in results)
    lines.append(
        f"TOPLAM başarı={sum(r['ok'] for r in results)}/{len(results)} medyan={statistics.median(all_times):.1f}s "
        f"ortalama={statistics.mean(all_times):.1f}s girdi={prompt} önbellek=%{100 * cached / prompt if prompt else 0:.0f} "
        f"çıktı={sum(r['completion_tokens'] for r in results)}"
    )
    lines.append(
        "FastLoop toplamları: "
        f"uncached={sum(r['uncached_prompt_tokens'] for r in results)} "
        f"obs={sum(r['observations'] for r in results)} reuse={sum(r['observations_reused'] for r in results)} "
        f"progress={sum(r['semantic_progress_events'] for r in results)} "
        f"stagnation={sum(r['fast_loop_stagnation_events'] for r in results)} "
        f"replan={sum(r['fast_loop_replans'] for r in results)} delivery={sum(r['fast_loop_delivery_entries'] for r in results)} "
        f"dupnav={sum(r['duplicate_navigation'] for r in results)}"
    )
    measurements = [row.get("integrations", {}) for row in results]
    lines.append("Entegrasyon toplamları: " + " · ".join(
        f"{key}={sum(item.get(key, 0) for item in measurements):.3f}"
        for key in ("discovery_seconds", "install_seconds", "network_seconds", "wait_seconds",
                    "user_wait_seconds", "network_requests", "operations_ok", "operations_failed")))
    return "\n".join(lines)


async def main(
    runs: int, concurrency: int, backend: Optional[str], names: List[str], json_path: Optional[str],
    gui_stage: GuiStage,
) -> None:
    server: ThreadingHTTPServer = ThreadingHTTPServer(("127.0.0.1", 0), BenchmarkHandler)
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
            run_one(name, root, server.server_port, backend, clients, semaphore, gui_stage)
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
    # Ayarlar sayfasında kaydedilen anahtarlar yalnız eksikse ortama uygulanır.
    apply_stored_api_keys()
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description="OmniAgent hız + doğruluk benchmark'ı")
    parser.add_argument("--runs", type=int, required=True, help="Senaryo başına koşu sayısı")
    parser.add_argument("--concurrency", type=int, required=True, help="Aynı anda koşan görev sayısı")
    parser.add_argument("--backend", default=None, help="Başlangıç backend'i (varsayılan: config.DEFAULT_BACKEND)")
    parser.add_argument("--only", default=",".join(CORE_SCENARIOS), help="Virgülle ayrılmış senaryo adları")
    parser.add_argument("--json", default=None, help="Ham sonuçların yazılacağı JSON dosyası")
    parser.add_argument("--headless", action="store_true",
                        help="GUI senaryolarını kullanıcının ekranı yerine görünmez Chromium'da koş (headless_screen.py)")
    arguments: argparse.Namespace = parser.parse_args()
    selected: List[str] = [name for name in arguments.only.split(",") if name]
    unknown: List[str] = [name for name in selected if name not in SCENARIO_NAMES]
    if unknown:
        parser.error(f"Bilinmeyen senaryo: {unknown}; geçerli: {', '.join(SCENARIO_NAMES)}")
    if set(selected) & set(GUI_SCENARIOS) and arguments.concurrency != 1:
        parser.error(f"GUI senaryoları ({', '.join(GUI_SCENARIOS)}) tek ekranı paylaşır: --concurrency 1 kullan.")
    if set(selected) & set(SEQUENTIAL_SCENARIOS) and arguments.concurrency != 1:
        parser.error(f"Öğrenme senaryoları ({', '.join(SEQUENTIAL_SCENARIOS)}) ardışık koşmalı: --concurrency 1 kullan.")
    if not arguments.headless:
        asyncio.run(main(arguments.runs, arguments.concurrency, arguments.backend, selected, arguments.json,
                         CHROME_STAGE))
    else:
        headless_page: HeadlessPage = HeadlessPage()
        try:
            install_headless_screen(headless_page)
            asyncio.run(main(arguments.runs, arguments.concurrency, arguments.backend, selected, arguments.json,
                             headless_stage(headless_page)))
        finally:
            headless_page.close()
