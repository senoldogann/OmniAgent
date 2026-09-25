"""
Deneyim belleği: hatalardan kalıcı öğrenme.

Ders yalnız DOĞRULANMIŞ bir kurtarmadan çıkarılır: aynı araçta başarısız bir çağrıdan sonra
argümanı değiştirilmiş ve ilişkili (ortak belirteçli) bir çağrı başarılı olmuş ve görev de
başarıyla bitmiş olmalıdır. Ders görev başında modele VERİLMEZ; yalnız aynı araç aynı hata
imzasını benzer (aynı hatalı) argümanla yeniden ürettiğinde o araç sonucunun altına eklenir.
Önceki ölçümde her göreve eklenen "benzer görev rotaları" alakasız ipuçları ve başka görevlerin
yollarını taşıyıp hedef sapmasına yol açmıştı; hata anına koşullanan ders başarılı yolu
etkilemez. Hatırlatılan dersten sonra aynı aracın ilk çağrısı başarılıysa ders işe yaramış
sayılır; en az PRUNE_MIN_USES kez hatırlatılıp çoğunlukla işe yaramayan ders silinir.

Görev içi sayaç aynı hatanın tekrarını da yakalar: aynı imza ikinci kez oluşunca modele
aynı yaklaşımı tekrarlamaması, nedeni araştırması söylenir.
"""
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple, TypedDict

from omniagent.config import redact

MAX_LESSONS: int = 100
# Hata anahtarının ve gösterilen çağrıların üst sınırı (karakter)
ERROR_KEY_LIMIT: int = 200
CALL_DISPLAY_LIMIT: int = 280
MAX_TOKENS: int = 48
# Başarılı çağrı, başarısız çağrının belirteçlerinin en az bu oranını içermelidir: düzeltme
# "hatalı çağrı + değişiklik"tir; --help, ls gibi keşif adımları düzeltme sayılmaz.
FIX_COVERAGE: float = 0.6
# Araç başına düzeltme bekleyen en fazla başarısız çağrı
MAX_OPEN_FAILURES: int = 5
# Kayıtlı dersin yeni başarısız çağrıya uyması için ortak belirteç oranı
MATCH_SIMILARITY: float = 0.5
PRUNE_MIN_USES: int = 3
PRUNE_MIN_HELP_RATIO: float = 0.34
# Aynı hata bu görevde bu kadar kez oluşunca tekrar uyarısı eklenir
REPEAT_WARNING_THRESHOLD: int = 2

# Argümanı değişince sonucu anlamsızlaşan veya kurtarması argümanla olmayan araçlar ders üretmez:
# ekran koordinatları, izinler, kullanıcı cevabı ve hafıza kayıtları genellenebilir çözüm değildir.
EXCLUDED_TOOLS: frozenset[str] = frozenset({
    "take_screenshot", "cua_get_app", "cua_get_ax_state", "cua_click", "cua_click_point",
    "cua_type_text", "cua_press_key", "cua_submit_text", "smart_click", "run_action_sequence",
    "chrome_active_tab", "capture_photo", "ask_user", "user_memory", "process_list",
    "discover_capabilities", "schedule_task",
})
# Çağrı gösteriminde tek başına yeterli olan ana argüman alanları
_PRIMARY_ARGUMENTS: Tuple[str, ...] = ("command", "code", "url", "path", "query")

_URL_PATTERN = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)
# Yol yalnız sözcük sınırında başlar ("and/or" gibi metinler yol sayılmaz)
_PATH_PATTERN = re.compile(r"(?<![\w.~])(?:~|\.{1,2})?/[^\s'\"<>:,;|&()]+")
_UUID_PATTERN = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE)
_HEX_PATTERN = re.compile(r"\b(?:0x)?[0-9a-f]{8,}\b", re.IGNORECASE)
_LONG_NUMBER_PATTERN = re.compile(r"\b\d{4,}\b")
_TOKEN_SPLIT = re.compile(r"[\s,;|&()\[\]{}\"'=<>]+")
_SECRET_PATTERNS: Tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\b(?:sk|pk|rk|ghp|gho|ghs|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{12,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{12,}=*"),
    re.compile(r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key|apikey|parola|sifre|şifre)\s*[=:]\s*\S+"),
)


class Lesson(TypedDict):
    """Doğrulanmış kurtarmadan çıkarılan ders: eşleşme anahtarları, gösterim ve etki sayaçları."""
    id: str
    tool: str
    error_key: str
    failed_tokens: List[str]
    failed_call: str
    fixed_call: str
    created_at: str
    updated_at: str
    uses: int
    helped: int


class ExperienceState(TypedDict):
    lessons: List[Lesson]


class LessonCandidate(TypedDict):
    """Görev içinde doğrulanan, görev başarıyla biterse kalıcılaşacak ders adayı."""
    tool: str
    error_key: str
    failed_tokens: List[str]
    failed_call: str
    fixed_call: str


class TaskTracker(TypedDict):
    """
    Görev içi izleme: hata imzası sayaçları, araç başına düzeltme bekleyen son başarısız
    çağrılar (argüman, hata), doğrulanmış ders adayları, cevabı beklenen hatırlatmalar (araç →
    ders kimliği ve gösterildiği tur), tamamlanan geri bildirimler ve gösterilmiş dersler.
    """
    failure_counts: Dict[str, int]
    open_failures: Dict[str, List[Tuple[str, str]]]
    candidates: Dict[str, LessonCandidate]
    pending: Dict[str, Tuple[str, int]]
    feedback: List[Tuple[str, bool]]
    shown: List[str]


class Observation(TypedDict):
    """Bir araç sonucunun gözlemi: model mesajına eklenecek notlar ve (varsa) gösterilen ders."""
    notes: List[str]
    lesson_id: Optional[str]


def empty_state() -> ExperienceState:
    return {"lessons": []}


def new_tracker() -> TaskTracker:
    return {"failure_counts": {}, "open_failures": {}, "candidates": {}, "pending": {},
            "feedback": [], "shown": []}


def scrub_secrets(text: str) -> str:
    """Bilinen sır değerlerini ve yaygın token/parola kalıplarını maskeler (ders dosyasına sır yazılmaz)."""
    cleaned: str = redact(text)
    for pattern in _SECRET_PATTERNS:
        cleaned = pattern.sub("[gizli]", cleaned)
    return cleaned


def _basename(raw: str) -> str:
    """Yol veya URL'nin son anlamlı parçası (koşuya özgü dizinler eşleşmeyi bozmasın). Saf."""
    trimmed: str = raw.rstrip("/")
    return trimmed.rsplit("/", 1)[-1] if "/" in trimmed else trimmed


def _url_skeleton(url: str) -> str:
    """URL'yi 'host son-yol-parçası' biçimine indirir; port, sorgu ve üst dizinler atılır. Saf."""
    without_scheme: str = url.split("://", 1)[-1]
    host: str = without_scheme.split("/", 1)[0].split(":", 1)[0].casefold()
    path: str = without_scheme.split("/", 1)[1] if "/" in without_scheme else ""
    last: str = _basename(path.split("?", 1)[0].split("#", 1)[0])
    return f"{host} {last}" if last else host


def normalize_text(text: str) -> str:
    """Hata metnindeki kararsız parçaları (URL, yol, uuid, uzun sayı, hex) sabitler. Saf."""
    normalized: str = _URL_PATTERN.sub(lambda match: _url_skeleton(match.group(0)), text)
    normalized = _PATH_PATTERN.sub(lambda match: _basename(match.group(0)), normalized)
    normalized = _UUID_PATTERN.sub("<id>", normalized)
    normalized = _HEX_PATTERN.sub("<hex>", normalized)
    normalized = _LONG_NUMBER_PATTERN.sub("<n>", normalized)
    return " ".join(normalized.casefold().split())


def error_key(detail: str) -> str:
    """Araç hata metninin (tip: mesaj) eşleşme anahtarı. Saf."""
    return normalize_text(detail)[:ERROR_KEY_LIMIT]


def _string_values(value: object) -> List[str]:
    """JSON değerindeki metin/sayı parçalarını anahtar sırasıyla toplar. Saf."""
    if isinstance(value, dict):
        return [part for key in sorted(value) for part in _string_values(value[key])]
    if isinstance(value, list):
        return [part for item in value for part in _string_values(item)]
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return [str(value)]
    return []


def _parse_arguments(arguments: str) -> object:
    """Araç argüman JSON'unu çözer; bozuk JSON ham metin olarak ele alınır. Saf."""
    try:
        return json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return arguments


def argument_tokens(arguments: str) -> List[str]:
    """
    Argüman JSON'unu karşılaştırılabilir belirteçlere çevirir: URL → host ve son parça, yol →
    dosya adı; saf sayı, uuid ve hex kimlikler atılır. Koşuya özgü dizinler farklı olsa da aynı
    komut aynı belirteçleri üretir. Saf.
    """
    tokens: List[str] = []
    for text in _string_values(_parse_arguments(arguments)):
        # Belirteçler ders dosyasına yazılır: sırlar belirteçlere ayrılmadan önce maskelenir.
        spaced: str = _URL_PATTERN.sub(lambda match: " " + _url_skeleton(match.group(0)) + " ", scrub_secrets(text))
        for raw in _TOKEN_SPLIT.split(spaced):
            token: str = _basename(raw) if "/" in raw and not raw.startswith("-") else raw
            token = token.casefold().strip(".:")
            if (not token or token.isdigit() or _UUID_PATTERN.fullmatch(token)
                    or _HEX_PATTERN.fullmatch(token) or token in tokens):
                continue
            tokens.append(token)
            if len(tokens) >= MAX_TOKENS:
                return tokens
    return tokens


def coverage(failed: List[str], fixed: List[str]) -> float:
    """Başarısız çağrı belirteçlerinin başarılı çağrıda bulunan oranı (0 ile 1 arası). Saf."""
    if not failed:
        return 0.0
    expected: frozenset[str] = frozenset(failed)
    return len(expected & frozenset(fixed)) / len(expected)


def similarity(first: List[str], second: List[str]) -> float:
    """Kapsama benzerliği: ortak belirteç / küçük kümenin boyu (0 ile 1 arası). Saf."""
    if not first or not second:
        return 0.0
    left: frozenset[str] = frozenset(first)
    right: frozenset[str] = frozenset(second)
    return len(left & right) / min(len(left), len(right))


def display_call(arguments: str) -> str:
    """
    Çağrının kısa, sırları maskelenmiş gösterimi: tek ana alan (komut, kod, URL, yol, sorgu)
    ve yanında yalnız bayrak/sayı değerleri varsa yalnız o alan, değilse tüm JSON gösterilir.
    """
    parsed: object = _parse_arguments(arguments)
    shown: str = str(parsed)
    if isinstance(parsed, dict):
        primary: List[str] = [str(parsed[key]) for key in _PRIMARY_ARGUMENTS if isinstance(parsed.get(key), str)]
        others: List[object] = [value for key, value in parsed.items() if key not in _PRIMARY_ARGUMENTS]
        simple_others: bool = all(value is None or isinstance(value, (bool, int, float)) for value in others)
        shown = primary[0] if len(primary) == 1 and simple_others else json.dumps(parsed, ensure_ascii=False)
    single_line: str = " ".join(shown.split())
    clipped: str = single_line if len(single_line) <= CALL_DISPLAY_LIMIT else single_line[:CALL_DISPLAY_LIMIT] + " …"
    return scrub_secrets(clipped)


def lesson_id(tool: str, key: str, tokens: List[str]) -> str:
    """Dersin kararlı kimliği (araç + hata anahtarı + başarısız argüman belirteçleri). Saf."""
    return hashlib.sha256(f"{tool}|{key}|{' '.join(tokens)}".encode("utf-8")).hexdigest()[:12]


def pair_candidate(tool: str, failed_arguments: str, failed_detail: str, fixed_arguments: str) -> Optional[LessonCandidate]:
    """
    Başarısız çağrıyı izleyen başarılı çağrı onun düzeltmesi mi? Argümanlar farklı olmalı ve
    başarılı çağrı başarısızın belirteçlerini FIX_COVERAGE oranında kapsamalıdır: aynı çağrının
    tekrarla başarılması geçici hatadır, keşif adımı (--help, ls) düzeltme değildir. Saf.
    """
    if failed_arguments == fixed_arguments:
        return None
    failed_tokens: List[str] = argument_tokens(failed_arguments)
    if coverage(failed_tokens, argument_tokens(fixed_arguments)) < FIX_COVERAGE:
        return None
    return {"tool": tool, "error_key": error_key(failed_detail), "failed_tokens": failed_tokens,
            "failed_call": display_call(failed_arguments), "fixed_call": display_call(fixed_arguments)}


def match_lesson(state: ExperienceState, tool: str, key: str, tokens: List[str]) -> Optional[Lesson]:
    """Aynı araç + aynı hata anahtarı + benzer (aynı hatalı) argümanlı en iyi dersi döner. Saf."""
    best: Optional[Tuple[float, float, str]] = None
    chosen: Optional[Lesson] = None
    for lesson in state["lessons"]:
        if lesson["tool"] != tool or lesson["error_key"] != key:
            continue
        score: float = similarity(lesson["failed_tokens"], tokens)
        if score < MATCH_SIMILARITY:
            continue
        rank: Tuple[float, float, str] = (
            score, lesson["helped"] / lesson["uses"] if lesson["uses"] else 0.0, lesson["updated_at"],
        )
        if best is None or rank > best:
            best, chosen = rank, lesson
    return chosen


def lesson_hint(lesson: Lesson) -> str:
    """Hata sonucunun altına eklenen ders metni. Saf."""
    track: str = (f" ({lesson['helped']}/{lesson['uses']} hatırlatmada işe yaradı)"
                  if lesson["uses"] else "")
    return (
        f"DENEYİM BELLEĞİ: Aynı hata daha önce doğrulanmış bir değişiklikle çözüldü{track}.\n"
        f"  Başarısız: {lesson['failed_call']}\n"
        f"  Çalışan:   {lesson['fixed_call']}\n"
        "Farkı bu göreve uyarla (yol, ad ve kimlik değerlerini bu görevin hedefinden al); "
        "durum farklıysa başka bir yol dene."
    )


def repeat_warning(count: int) -> str:
    """Aynı hata görevde tekrarlandığında eklenen uyarı. Saf."""
    return (
        f"TEKRARLANAN HATA: Aynı hata bu görevde {count}. kez oluştu. Aynı yaklaşımı küçük "
        "değişikliklerle yeniden deneme; önce nedenini araştır (--help, belge, gerçek dosya/uygulama "
        "yolu, izin) ya da farklı bir yol seç."
    )


def observe_result(
    state: ExperienceState, tracker: TaskTracker, tool: str, arguments: str, ok: bool, detail: str,
    turn: int,
) -> Tuple[TaskTracker, Observation]:
    """
    Bir araç sonucunu görev izleyicisine işler. Önceki turda ders gösterilmiş araç yeniden
    çağrıldıysa dersin işe yarayıp yaramadığı kaydedilir. Başarılı sonuç, aynı aracın bekleyen
    başarısızlıklarından en yenisini kapsıyorsa ders adayı olur; o hata ve aynı imzalı daha eski
    hatalar kapanır, keşif adımları bekleyenleri kapatmaz. Başarısız sonuçta tekrar uyarısı ve eşleşen
    ders notu üretilir; bir ders görev başına en çok bir kez gösterilir. Saf: yeni izleyici döner.
    """
    if tool in EXCLUDED_TOOLS:
        return tracker, {"notes": [], "lesson_id": None}
    pending: Dict[str, Tuple[str, int]] = dict(tracker["pending"])
    feedback: List[Tuple[str, bool]] = list(tracker["feedback"])
    waiting: Optional[Tuple[str, int]] = pending.get(tool)
    if waiting is not None and turn > waiting[1]:
        feedback.append((waiting[0], ok))
        del pending[tool]
    counts: Dict[str, int] = dict(tracker["failure_counts"])
    open_failures: Dict[str, List[Tuple[str, str]]] = {
        name: list(items) for name, items in tracker["open_failures"].items()
    }
    candidates: Dict[str, LessonCandidate] = dict(tracker["candidates"])
    shown: List[str] = list(tracker["shown"])
    notes: List[str] = []
    shown_id: Optional[str] = None
    if ok:
        waiting_failures: List[Tuple[str, str]] = open_failures.get(tool, [])
        for position in range(len(waiting_failures) - 1, -1, -1):
            failed_arguments, failed_detail = waiting_failures[position]
            candidate: Optional[LessonCandidate] = pair_candidate(tool, failed_arguments, failed_detail, arguments)
            if candidate is None:
                continue
            candidates[f"{tool}|{candidate['error_key']}"] = candidate
            open_failures[tool] = [
                item for index, item in enumerate(waiting_failures)
                if index > position or (index < position and error_key(item[1]) != candidate["error_key"])
            ]
            break
    else:
        open_failures[tool] = (open_failures.get(tool, []) + [(arguments, detail)])[-MAX_OPEN_FAILURES:]
        key: str = error_key(detail)
        signature: str = f"{tool}|{key}"
        counts[signature] = counts.get(signature, 0) + 1
        if counts[signature] >= REPEAT_WARNING_THRESHOLD:
            notes.append(repeat_warning(counts[signature]))
        lesson: Optional[Lesson] = match_lesson(state, tool, key, argument_tokens(arguments))
        if lesson is not None and lesson["id"] not in shown:
            notes.append(lesson_hint(lesson))
            shown.append(lesson["id"])
            pending[tool] = (lesson["id"], turn)
            shown_id = lesson["id"]
    updated: TaskTracker = {"failure_counts": counts, "open_failures": open_failures, "candidates": candidates,
                            "pending": pending, "feedback": feedback, "shown": shown}
    return updated, {"notes": notes, "lesson_id": shown_id}


def merge_candidates(state: ExperienceState, candidates: List[LessonCandidate], timestamp: str) -> ExperienceState:
    """
    Yeni dersleri ekler; aynı araç+hata anahtarı ve benzer başarısız argümanlı ders varsa en son
    doğrulanan çözümle günceller (sayaçlar korunur). En fazla MAX_LESSONS ders, en eskiler düşer. Saf.
    """
    lessons: List[Lesson] = [Lesson(**lesson) for lesson in state["lessons"]]
    for candidate in candidates:
        existing: Optional[int] = next(
            (index for index, lesson in enumerate(lessons)
             if lesson["tool"] == candidate["tool"] and lesson["error_key"] == candidate["error_key"]
             and similarity(lesson["failed_tokens"], candidate["failed_tokens"]) >= MATCH_SIMILARITY),
            None,
        )
        if existing is not None:
            lessons[existing] = {**lessons[existing], "failed_tokens": candidate["failed_tokens"],
                                 "failed_call": candidate["failed_call"], "fixed_call": candidate["fixed_call"],
                                 "updated_at": timestamp}
            continue
        lessons.append({
            "id": lesson_id(candidate["tool"], candidate["error_key"], candidate["failed_tokens"]),
            "tool": candidate["tool"], "error_key": candidate["error_key"],
            "failed_tokens": candidate["failed_tokens"], "failed_call": candidate["failed_call"],
            "fixed_call": candidate["fixed_call"], "created_at": timestamp, "updated_at": timestamp,
            "uses": 0, "helped": 0,
        })
    ordered: List[Lesson] = sorted(lessons, key=lambda lesson: lesson["updated_at"])
    return {"lessons": ordered[-MAX_LESSONS:]}


def apply_feedback(state: ExperienceState, tracker: TaskTracker, timestamp: str) -> ExperienceState:
    """
    Görevde gösterilen derslerin sonucunu sayaçlara işler. Görev biterken hâlâ yanıt beklenen
    (ders sonrası araç yeniden çağrılmamış) hatırlatma işe yaramamış sayılır. İşe yaramadığı
    kanıtlanan dersler budanır. Saf.
    """
    outcomes: List[Tuple[str, bool]] = list(tracker["feedback"]) + [
        (identifier, False) for identifier, _ in tracker["pending"].values()
    ]
    lessons: List[Lesson] = []
    for lesson in state["lessons"]:
        results: List[bool] = [helped for identifier, helped in outcomes if identifier == lesson["id"]]
        if not results:
            lessons.append(lesson)
            continue
        uses: int = lesson["uses"] + len(results)
        helped: int = lesson["helped"] + sum(results)
        if uses >= PRUNE_MIN_USES and helped / uses < PRUNE_MIN_HELP_RATIO:
            continue
        lessons.append({**lesson, "uses": uses, "helped": helped, "updated_at": timestamp})
    return {"lessons": lessons}


def finish_task(state: ExperienceState, tracker: TaskTracker, success: bool, timestamp: str) -> ExperienceState:
    """
    Görev sonu: gösterilen derslerin sonuçlarını işler; görev başarılıysa doğrulanmış adayları
    ders olarak ekler. Başarısız görevin "kurtarmaları" sonucu doğrulanmadığı için öğrenilmez. Saf.
    """
    with_feedback: ExperienceState = apply_feedback(state, tracker, timestamp)
    if not success:
        return with_feedback
    return merge_candidates(with_feedback, list(tracker["candidates"].values()), timestamp)


def tracker_changes_state(tracker: TaskTracker, success: bool) -> bool:
    """Görev sonunda deneyim dosyasının yeniden yazılması gerekiyor mu? Saf."""
    return bool(tracker["feedback"] or tracker["pending"] or (success and tracker["candidates"]))


def _lesson(value: object, index: int) -> Lesson:
    """JSON kaydını doğrular; bozuk ders sessizce atılmaz, hata yükselir."""
    if not isinstance(value, dict):
        raise ValueError(f"lessons[{index}] nesne olmalı")
    for field in ("id", "tool", "error_key", "failed_call", "fixed_call", "created_at", "updated_at"):
        if not isinstance(value.get(field), str):
            raise ValueError(f"lessons[{index}].{field} metin olmalı")
    tokens: object = value.get("failed_tokens")
    if not isinstance(tokens, list) or not all(isinstance(token, str) for token in tokens):
        raise ValueError(f"lessons[{index}].failed_tokens metin listesi olmalı")
    for field in ("uses", "helped"):
        count: object = value.get(field)
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError(f"lessons[{index}].{field} negatif olmayan tamsayı olmalı")
    return {
        "id": value["id"], "tool": value["tool"], "error_key": value["error_key"],
        "failed_tokens": list(tokens), "failed_call": value["failed_call"],
        "fixed_call": value["fixed_call"], "created_at": value["created_at"],
        "updated_at": value["updated_at"], "uses": value["uses"], "helped": value["helped"],
    }


def load_experience(path: str) -> ExperienceState:
    """Deneyim dosyasını yükler; yoksa boş durum. Bozuk dosya sessizce sıfırlanmaz."""
    source: Path = Path(path)
    if not source.exists():
        return empty_state()
    with source.open("r", encoding="utf-8") as handle:
        loaded: object = json.load(handle)
    if not isinstance(loaded, dict) or not isinstance(loaded.get("lessons"), list):
        raise ValueError(f"Deneyim dosyasında geçersiz yapı: {source} ('lessons' listesi bekleniyor)")
    return {"lessons": [_lesson(item, index) for index, item in enumerate(loaded["lessons"])]}


def save_experience(path: str, state: ExperienceState) -> None:
    """Deneyim durumunu atomik olarak (geçici dosya + fsync + os.replace) 0600 izinle yazar."""
    target_path: Path = Path(path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=target_path.parent, prefix=f".{target_path.name}.", delete=False,
        ) as target:
            temporary = Path(target.name)
            json.dump(state, target, ensure_ascii=False, separators=(",", ":"))
            target.flush()
            os.fsync(target.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target_path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
