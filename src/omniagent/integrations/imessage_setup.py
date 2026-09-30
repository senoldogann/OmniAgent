"""iMessage kurulumu: karakter, model ölçümü, servis kurulumu ve servisin içinde eşleştirme.

TCC izinleri (Full Disk Access, Messages için Automation) sorumlu sürece verilir: Terminal'den çalışan komutta
Terminal'e, LaunchAgent altında python ikilisine (bkz. platform/macos/permissions.py). Bu yüzden eşleştirme kodunu
izlemek ve ilk mesajı göndermek terminalde değil servisin kendisinde yapılır; kurulum komutu servisin companion.db'ye
yazdığı durumu izler.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import subprocess
import sys
import time
from contextlib import aclosing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, TypedDict

from openai import APIError, AsyncOpenAI

from omniagent.app.agent import close_model_clients, create_model_clients
from omniagent.app.model_runtime import stream_completion
from omniagent.companion.chat import CHAT_TOOLS
from omniagent.companion.persona import write_persona_if_missing
from omniagent.config import API_KEY_VARIABLES, BACKENDS, apply_model_preferences, refresh_api_keys
from omniagent.core.events import AgentEvent
from omniagent.integrations.imessage_rules import message_text
from omniagent.integrations.imessage_settings import (
    DraftSettings, HeartbeatMinutes, ImessageConfigError, ImessageSettings, PairingRequest, QuietHours,
    load_pairing, load_settings, normalize_handle, pairing_expired, save_pairing, save_settings,
)
from omniagent.integrations.imsg import ImsgClient, ImsgError, ImsgUnavailable, IncomingMessage, imsg_command
from omniagent.memory.personal import PersonalStore, to_utc_iso, utc_iso
from omniagent.paths import companion_db_file, data_root, imessage_pairing_file, imessage_settings_file, persona_file
from omniagent.platform.macos import launch_agent

SERVICE_LABEL: str = "com.omniagent.imessage"
PAIRING_SECONDS: float = 180.0
# Tam Disk Erişimi'nin verilmesi dahil servisin hazır olmasını bekleme üst sınırı
SERVICE_READY_SECONDS: float = 900.0
SETUP_POLL_SECONDS: float = 1.0
PAIRING_STATUS_KEY: str = "pairing_status"
NO_PAIRING_MESSAGE: str = (
    "iMessage eşleşmesi ve bekleyen eşleştirme isteği yok: 'omniagent-imessage setup' çalıştırın."
)
FULL_DISK_SETTINGS_URL: str = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
BENCH_PROMPT: str = "selam, naber? tek kısa cümleyle cevap ver."
# Tasarım konuşmasında onaylanan değerler (spec: "Yapılandırma ve dosyalar")
APPROVED_QUIET_HOURS: QuietHours = {"start": "23:30", "end": "09:00"}
APPROVED_BURST_QUIET_SECONDS: float = 2.0
APPROVED_GUI_IDLE_SECONDS: int = 180
APPROVED_HEARTBEAT_MINUTES: HeartbeatMinutes = {"base": 30, "jitter": 10, "min": 20, "max": 240}


class BenchResult(TypedDict):
    backend: str
    seconds: Optional[float]
    error: str


def bridge_command() -> List[str]:
    """Köprüyü bu yorumlayıcıyla çalıştıran komut (launchd kaydı)."""
    return [sys.executable, "-m", "omniagent.integrations.imessage", "run"]


def build_launchd_record() -> Dict[str, object]:
    """iMessage köprüsünün sürekli çalışan LaunchAgent kaydı."""
    root: Path = data_root()
    return launch_agent.launchd_record(SERVICE_LABEL, bridge_command(), root / "imessage-stdout.log",
                                       root / "imessage-stderr.log")


def install_service() -> None:
    """
    LaunchAgent'i idempotent kurar/yeniler; eşleşme yoksa servis bekleyen isteği eşleştirme kipinde açılır. Eşleşme de
    istek de yoksa servis her açılışta hata verip KeepAlive ile sonsuz yeniden başlardı: baştan ImessageConfigError.
    """
    if not imessage_settings_file().exists() and load_pairing(imessage_pairing_file(), BACKENDS.keys()) is None:
        raise ImessageConfigError(NO_PAIRING_MESSAGE)
    data_root().mkdir(parents=True, exist_ok=True)
    path: Path = launch_agent.plist_path(SERVICE_LABEL)
    launch_agent.install(SERVICE_LABEL, path, build_launchd_record(), "iMessage")
    print(f"iMessage hizmeti kuruldu/güncellendi: {path}")


def new_pairing(draft: DraftSettings, code: str, expires_at: datetime) -> PairingRequest:
    """Eşleştirme isteği. Saf."""
    return {"code": code, "expires_at": utc_iso(expires_at), "draft": draft}


def pairing_created_at(request: PairingRequest) -> datetime:
    """Eşleştirme isteğinin geçerlilik penceresinin başlangıcı: kod gösterildiği an (son geçerlilik − PAIRING_SECONDS). Saf."""
    return datetime.fromisoformat(request["expires_at"]) - timedelta(seconds=PAIRING_SECONDS)


def pairing_handle(message: IncomingMessage, request: PairingRequest, now: datetime) -> Optional[str]:
    """
    Mesaj, süresi dolmamış kodu birebir sohbette taşıyorsa ve kod gösterildikten sonra yazılmışsa gönderenin normalize
    adresi; değilse None. İzleme tüm geçmişi yeniden oynattığı için kodla aynı metni taşıyan eski bir mesaj kimseyi
    eşleştiremez (spec: kodu 180 sn içinde gönderen). Saf.
    """
    if message["is_from_me"] or message["is_group"] or pairing_expired(request, now):
        return None
    if to_utc_iso(message["created_at"]) < utc_iso(pairing_created_at(request)):
        return None
    if message_text(message) != request["code"]:
        return None
    try:
        return normalize_handle(message["sender"])
    except ImessageConfigError:
        return None


def paired_settings(draft: DraftSettings, handle: str) -> ImessageSettings:
    """Taslak ayarlar + eşleşen adres. Saf."""
    return {
        "persona_name": draft["persona_name"], "chat_backend": draft["chat_backend"],
        "memory_backend": draft["memory_backend"], "quiet_hours": draft["quiet_hours"],
        "transcribe_backend": draft.get("transcribe_backend"), "transcribe_model": draft.get("transcribe_model"),
        "burst_quiet_seconds": draft["burst_quiet_seconds"], "gui_idle_seconds": draft["gui_idle_seconds"],
        "heartbeat_minutes": draft["heartbeat_minutes"], "handle": handle,
    }


def _report_failure(store: PersonalStore, error: Exception) -> None:
    """Servis tarafı hatayı kurulum komutunun okuyacağı duruma yazar."""
    store.set_state(PAIRING_STATUS_KEY, f"error: {error}")


async def pair_from_service(store: PersonalStore) -> ImessageSettings:
    """
    Servis tarafı eşleştirme: bekleyen istekteki kodu birebir sohbette gönderen adresi eşleştirir. Önce karşılama
    mesajı gönderilir (Messages için Automation izni bu gönderimde servis ikilisine sorulur); başarılıysa ayarlar
    yazılır, imleç kod satırına kurulur ve istek silinir. imsg bulunamaz, veritabanı okunamaz ya da gönderim
    reddedilirse durum kurulum komutuna bildirilir ve hata yükselir (launchd servisi yeniden başlatır).
    """
    request: Optional[PairingRequest] = load_pairing(imessage_pairing_file(), BACKENDS.keys())
    if request is None:
        raise ImessageConfigError(NO_PAIRING_MESSAGE)
    try:
        command: List[str] = imsg_command()
    except ImsgUnavailable as error:
        _report_failure(store, error)
        raise
    client = ImsgClient(command)
    try:
        try:
            await client.start()
        except ImsgUnavailable as error:
            _report_failure(store, error)
            raise
        store.set_state(PAIRING_STATUS_KEY, "waiting")
        async with aclosing(client.subscribe(0)) as stream:
            async for message in stream:
                current: Optional[PairingRequest] = load_pairing(imessage_pairing_file(), BACKENDS.keys())
                if current is None:
                    raise ImessageConfigError("Eşleştirme isteği kurulum sırasında silindi.")
                handle: Optional[str] = pairing_handle(message, current, datetime.now(timezone.utc))
                if handle is None:
                    continue
                settings: ImessageSettings = paired_settings(current["draft"], handle)
                try:
                    await client.send_text(
                        handle, f"eşleştik 👋 ben {settings['persona_name']}. beni rehbere kaydet, sonra yazışalım",
                    )
                except ImsgError as error:
                    _report_failure(store, ImessageConfigError(
                        f"Messages'a mesaj gönderilemedi ({error}). Sistem Ayarları > Gizlilik ve Güvenlik > "
                        "Otomasyon'da python için Messages iznini açıp kodu yeniden gönderin."
                    ))
                    raise
                save_settings(imessage_settings_file(), settings)
                store.advance_cursor(message["rowid"])
                imessage_pairing_file().unlink()
                store.set_state(PAIRING_STATUS_KEY, "paired")
                return settings
    finally:
        await client.close()
    raise ImessageConfigError("imsg izleme akışı eşleşmeden kapandı.")


async def measure_first_token(client: AsyncOpenAI, backend: str) -> float:
    """Sabit Türkçe istemle ilk metin parçasına kadar geçen süre (sn); metin gelmezse turun sonuna kadar."""
    first: List[float] = []
    started: float = time.monotonic()

    def emit(event: AgentEvent) -> None:
        if event["kind"] == "text_delta" and not first:
            first.append(time.monotonic())

    await stream_completion(client, BACKENDS[backend], [{"role": "user", "content": BENCH_PROMPT}], CHAT_TOOLS,
                            f"imessage-bench-{backend}", emit, lambda: False)
    return (first[0] if first else time.monotonic()) - started


async def bench_backends() -> List[BenchResult]:
    """Kullanılabilir her profilin ilk token süresi, hızlıdan yavaşa; hata veren profil hatasıyla sonda."""
    apply_model_preferences()
    clients: Dict[str, AsyncOpenAI] = await asyncio.to_thread(create_model_clients)
    results: List[BenchResult] = []
    try:
        for backend, client in sorted(clients.items()):
            try:
                results.append({"backend": backend, "seconds": await measure_first_token(client, backend), "error": ""})
            except (APIError, TimeoutError) as error:
                results.append({"backend": backend, "seconds": None, "error": f"{type(error).__name__}: {error}"})
    finally:
        await close_model_clients(clients)
    return sorted(results, key=lambda item: (item["seconds"] is None, item["seconds"] or 0.0))


def ask_text(prompt: str) -> str:
    """Boş olmayan cevap alana dek sorar."""
    while True:
        answer: str = input(prompt).strip()
        if answer:
            return answer


def ask_choice(prompt: str, allowed: Sequence[str]) -> str:
    """Listedeki bir cevap alana dek sorar."""
    while True:
        answer: str = input(f"{prompt} [{', '.join(allowed)}]: ").strip()
        if answer in allowed:
            return answer
        print(f"Geçerli seçenekler: {', '.join(allowed)}")


def ask_transcription() -> Tuple[Optional[str], Optional[str]]:
    """Ask for an explicitly keyed provider profile and compatible transcription model, or disabled."""
    keyed = [name for name in refresh_api_keys() if name in API_KEY_VARIABLES]
    if not keyed:
        print("Anahtarı olan profil yok; sesli mesaj dökümü kapalı.")
        return None, None
    print("Ses dökümü seçtiğin profilin audio/transcriptions uç noktasına gönderilir. "
          "Profilin sağlayıcısı ve seçtiğin model bu uç noktayı desteklemeli; sohbet modeli kullanılmaz.")
    backend = ask_choice("Sesli mesaj döküm profili", ["kapalı", *keyed])
    if backend == "kapalı":
        return None, None
    model = ask_text("Bu sağlayıcının desteklediği döküm modeli (tam model kimliği): ")
    return backend, model


def transcription() -> None:
    """Configure voice on an existing paired installation and restart its service without re-pairing."""
    settings = load_settings(imessage_settings_file(), BACKENDS.keys())
    backend, model = ask_transcription()
    settings["transcribe_backend"] = backend
    settings["transcribe_model"] = model
    save_settings(imessage_settings_file(), settings)
    install_service()
    state = f"{backend} / {model}" if backend is not None else "kapalı"
    print(f"Sesli mesaj dökümü: {state}. iMessage hizmeti yeniden başlatıldı.")


def open_full_disk_settings() -> None:
    """Tam Disk Erişimi ayar bölmesini açar; açılamazsa uyarır (kullanıcı elle açabilir)."""
    result = subprocess.run(["open", FULL_DISK_SETTINGS_URL], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        logging.warning("Tam Disk Erişimi ayarları açılamadı", extra={"stderr": result.stderr.strip()[:200]})


async def wait_for_service(store: PersonalStore) -> None:
    """Servis 'waiting' bildirene kadar bekler; her yeni hatayı bir kez gösterir (veritabanı hatasında ayarı açar)."""
    deadline: float = time.monotonic() + SERVICE_READY_SECONDS
    reported: Optional[str] = None
    while time.monotonic() < deadline:
        status: Optional[str] = store.get_state(PAIRING_STATUS_KEY)
        if status == "waiting":
            return
        if status is not None and status.startswith("error:") and status != reported:
            reported = status
            print(f"\nServis hazır değil: {status.removeprefix('error:').strip()}")
            if "Full Disk Access" in status:
                print("Sistem Ayarları > Gizlilik ve Güvenlik > Tam Disk Erişimi'nde yukarıdaki python ikilisini "
                      "ekleyip açın; servis kendiliğinden yeniden dener.")
                open_full_disk_settings()
        await asyncio.sleep(SETUP_POLL_SECONDS)
    raise ImessageConfigError(
        f"Servis {SERVICE_READY_SECONDS / 60:.0f} dk içinde hazır olmadı; günlük: {data_root() / 'imessage-stderr.log'}"
    )


async def wait_for_pairing(store: PersonalStore) -> None:
    """Servisin kodu görüp eşleşmeyi yazmasını bekler; servis hatası ya da süre dolması açık hatadır."""
    deadline: float = time.monotonic() + PAIRING_SECONDS
    while time.monotonic() < deadline:
        status: Optional[str] = store.get_state(PAIRING_STATUS_KEY)
        if status == "paired" and imessage_settings_file().exists():
            return
        if status is not None and status.startswith("error:"):
            raise ImessageConfigError(f"Eşleştirme başarısız: {status.removeprefix('error:').strip()}")
        await asyncio.sleep(SETUP_POLL_SECONDS)
    raise ImessageConfigError("Kod 3 dakika içinde gelmedi. 'omniagent-imessage setup' komutunu yeniden çalıştırın.")


async def setup() -> None:
    """Etkileşimli kurulum: karakter, model seçimi, servis kurulumu, servisin içinde eşleştirme."""
    if imessage_settings_file().exists():
        raise ImessageConfigError(
            f"Zaten eşleşmiş. Yeniden eşleştirmek için önce şu dosyayı silin: {imessage_settings_file()}"
        )
    print(f"imsg: {imsg_command()[0]}")
    name: str = ask_text("Ajanın adı (iMessage'da görünecek karakter): ")
    if write_persona_if_missing(persona_file(), name):
        print(f"Karakter dosyası yazıldı: {persona_file()} (istediğin gibi düzenleyebilirsin)")
    else:
        print(f"Mevcut karakter dosyası korunuyor: {persona_file()}")
    print("Model profilleri ölçülüyor (ilk token süresi)…")
    results: List[BenchResult] = await bench_backends()
    for result in results:
        shown: str = (f"{result['seconds']:.2f} sn" if result["seconds"] is not None
                      else f"hata: {result['error'][:120]}")
        print(f"  {result['backend']:<16} {shown}")
    usable: List[str] = [result["backend"] for result in results if result["seconds"] is not None]
    if not usable:
        raise ImessageConfigError("Hiçbir model profili yanıt vermedi; API anahtarlarını Ayarlar'dan kontrol edin.")
    chat_backend = ask_choice("Sohbet profili (en hızlısı tablonun en üstünde)", usable)
    memory_backend = ask_choice("Hafıza/doğrulama profili", usable)
    transcribe_backend, transcribe_model = ask_transcription()
    draft: DraftSettings = {
        "persona_name": name,
        "chat_backend": chat_backend,
        "memory_backend": memory_backend,
        "transcribe_backend": transcribe_backend,
        "transcribe_model": transcribe_model,
        "quiet_hours": APPROVED_QUIET_HOURS,
        "burst_quiet_seconds": APPROVED_BURST_QUIET_SECONDS,
        "gui_idle_seconds": APPROVED_GUI_IDLE_SECONDS,
        "heartbeat_minutes": APPROVED_HEARTBEAT_MINUTES,
    }
    code: str = f"{secrets.randbelow(10 ** 6):06d}"
    store = PersonalStore(companion_db_file())
    try:
        store.set_state(PAIRING_STATUS_KEY, "starting")
        save_pairing(imessage_pairing_file(),
                     new_pairing(draft, code, datetime.now(timezone.utc) + timedelta(seconds=SERVICE_READY_SECONDS)))
        install_service()
        await wait_for_service(store)
        save_pairing(imessage_pairing_file(),
                     new_pairing(draft, code, datetime.now(timezone.utc) + timedelta(seconds=PAIRING_SECONDS)))
        print(f"\niPhone'dan ajanın Apple ID adresine şu kodu gönder: {code}  (3 dakika)")
        await wait_for_pairing(store)
    finally:
        store.close()
    print("Eşleşme tamam: ajan sana 'eşleştik' yazdı. iPhone'da onu isim ve fotoğrafla rehbere kaydet.")
