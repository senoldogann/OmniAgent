"""Modelin görebileceği araç kontratları ve görev bazlı tool routing."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from omniagent.core.conversation import Exchange
from omniagent.integrations.capabilities import DISCOVERY_SCHEMA


# Ekran noktası tek [x, y] alanıdır: ayrı x/y tamsayı alanlarında qwen, yerel biçimi olan
# [x, y]'yi x alanına yazıyordu (ölçümde 10 çağrının 7'si bozuk; point ile 0/10).
POINT_SCHEMA: Dict[str, Any] = {
    "type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2,
    "description": "[x, y]: 1000×1000 ekran görüntüsündeki nokta.",
}


def screen_reading_schemas() -> List[Dict[str, Any]]:
    """
    Her GUI yolunda açık olan OCR tıklama, kaydırma ve baştan sona okuma şemaları. Modelde kaydırma
    aracı hiç yoktu: LinkedIn görevinde paneli aşağı ok tuşlarıyla birkaç satır oynatıp "tüm ilanları
    inceledim" dedi. Saf.
    """
    return [
        _function_schema(
            "cua_click_text",
            "Görünür metne (bağlantı, düğme, sekme, liste/ilan başlığı, menü öğesi, onay kutusu etiketi) "
            "OCR ile bulup tam ortasına tıklar; nokta tahmininden kesindir. Metin birden çok yerdeyse "
            "near ile hedefin yaklaşık noktasını ver.",
            {
                "text": {"type": "string", "description": "Ekranda görünen metin (tamamı veya ayırt edici parçası)."},
                "near": {
                    "type": ["array", "null"], "items": {"type": "integer"}, "minItems": 2, "maxItems": 2,
                    "description": "Hedefin yaklaşık [x, y] noktası; metin tekse null.",
                },
            },
        ),
        _function_schema(
            "cua_scroll",
            "point'in altındaki paneli/sayfayı kaydırır. amount görüntü yüksekliğinin binde biridir "
            "(500 = yarım görüntü). Sonuç 'KAYMADI' derse o yönde içerik bitmiştir.",
            {
                "point": POINT_SCHEMA,
                "direction": {"type": "string", "enum": ["down", "up", "right", "left"]},
                "amount": {"type": "integer", "description": "1-1000; panel yüksekliğinin ~%80'i."},
            },
        ),
        _function_schema(
            "cua_read_scrollable",
            "point'in altındaki paneli/sayfayı BAŞTAN SONA okur: başa döner, sonuna kadar kaydırır ve tüm "
            "metni OCR ile tek sonuçta döner (ilan açıklaması, makale, uzun liste).",
            {
                "point": POINT_SCHEMA,
                "max_pages": {"type": "integer", "description": "En çok kaydırma sayısı (1-15)."},
            },
        ),
    ]


def _function_schema(
    name: str, description: str, properties: Dict[str, Dict[str, Any]],
    required: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Function-calling şemasını kurar; belirtilmeyen zorunlu alanlar eskisi gibi tüm alanlardır."""
    return {"type": "function", "function": {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object", "properties": properties,
            "required": list(properties) if required is None else required,
        },
    }}


def camera_photo_goal(goal: str) -> bool:
    """Tek kare kamera çekimi hedeflerinde özel aracı açar; diğer görevleri sade tutar."""
    lowered: str = goal.casefold()
    return (
        any(term in lowered for term in ("fotoğraf", "fotograf", "photo", "selfie", "picture"))
        and any(term in lowered for term in ("çek", "take", "capture", "shoot"))
        and any(term in lowered for term in ("desktop", "masaüst", "masaust"))
        and not any(extension in lowered for extension in (".jpg", ".jpeg", ".png"))
        and not any(app in lowered for app in ("photo booth", "photobooth"))
    )


def skills_sh_goal(goal: Optional[str]) -> bool:
    """Kullanıcı skills.sh kaynağını açıkça istedi mi? Saf."""
    return bool(goal and re.search(r"\bskills\.sh\b", goal, re.IGNORECASE))


def active_chrome_session_goal(goal: Optional[str]) -> bool:
    """Hedefte kullanıcının mevcut Chrome oturumu açıkça istendi mi? Saf."""
    if not goal:
        return False
    lowered = goal.casefold()
    return "chrome" in lowered and any(
        term in lowered for term in (
            "açık", "acik", "oturum", "session", "existing", "already open",
            "sekme", "tab", "kullan", "use", "chrome'da", "chrome’da",
        )
    ) and not any(term in lowered for term in ("chrome kullanma", "do not use chrome"))


# Yalnız açık Chrome oturumu yolunda bulunan araçlar: geçmişte görülmeleri o yolun kullanıldığını kanıtlar
_CHROME_SESSION_TOOLS: frozenset[str] = frozenset({
    "chrome_active_tab", "cua_click_point", "cua_type_text", "cua_press_key", "cua_submit_text", "cua_fill_field",
})
# Takip mesajı bunlardan birini içeriyorsa kullanıcı Chrome'dan yerel dosya/kabuk işine geçmiştir
_LOCAL_TASK_TERMS: re.Pattern[str] = re.compile(
    r"(?:dosya|klasör|klasor|terminal|kabuk|komut|masaüst|masaust|finder|\bshell\b|\bfile|\bfolder|\bdesktop)",
    re.IGNORECASE,
)


def continues_chrome_session(goal: str, history: List[Exchange]) -> bool:
    """
    Önceki görev açık Chrome oturumunda yürüdüyse ve yeni hedef yerel dosya/kabuk işine
    geçmiyorsa ("devam et", "formda eksik alanlar var") aynı oturum yolu sürer. Canlı kayıtta
    'chrome' kelimesi geçmeyen takip mesajı genel araç setine düşüyordu; otomatik gözlem de
    kalkınca model tıklamalarının sonucunu görmeden "formu gönderdim" dedi. Saf.
    """
    if not history or _LOCAL_TASK_TERMS.search(goal):
        return False
    return any(
        entry.removeprefix("başarısız: ").split(" ", 1)[0] in _CHROME_SESSION_TOOLS
        for entry in history[-1]["tools"]
    )


def chrome_session_route(goal: str, history: List[Exchange]) -> bool:
    """Görev kullanıcının açık Chrome oturumu yolunda mı çalışmalı: açık istek ya da o yolun devamı. Saf."""
    return active_chrome_session_goal(goal) or continues_chrome_session(goal, history)


_MEMORY_MUTATION_PATTERNS: Tuple[re.Pattern[str], ...] = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"\bhatırla\b",
    r"\bunut\b",
    r"\bremember\b",
    r"\bforget\b",
    r"\b(?:hafızaya|hafizaya|belleğe|bellege)\s+(?:kaydet|ekle|yaz)\b",
    r"\bkalıcı\s+(?:hafızaya|hafizaya|belleğe|bellege)\s+(?:kaydet|ekle|yaz)\b",
))


def memory_mutation_requested(goal: str) -> bool:
    """Kalıcı kullanıcı hafızasını değiştirmek için açık kullanıcı niyeti var mı? Saf."""
    return any(pattern.search(goal) is not None for pattern in _MEMORY_MUTATION_PATTERNS)


def build_tool_schemas(goal: Optional[str] = None, allow_edit: bool = False) -> List[Dict[str, Any]]:
    """Yalnız hedef metnine bakan araç listesi; Chrome oturumu açıkça istendiyse o yolun araçları. Saf."""
    return route_tool_schemas(goal, allow_edit, active_chrome_session_goal(goal))


def route_tool_schemas(goal: Optional[str], allow_edit: bool, chrome_session: bool) -> List[Dict[str, Any]]:
    """
    Modelin gördüğü araçlar. Liste bilerek kısa tutulur: ölçümde 26 araçlı şemada model
    hedefteki tarihi 10 denemenin 5'inde yanlış kopyaladı, tek araçla 10/10 doğruydu.
    Fare/klavye adımları run_action_sequence, şablon tıklama smart_click içindedir.
    chrome_session: görev kullanıcının açık Chrome oturumunda yürüyor (bkz. chrome_session_route).
    """
    schemas: List[Dict[str, Any]] = [
        _function_schema("execute_shell", "Sistem kabuğunda (/bin/sh, macOS BSD araçları) komut çalıştırır.", {
            "command": {"type": "string", "description": "Çalıştırılacak kabuk komutu."},
            "use_sudo": {"type": "boolean", "description": "Komut sudo ile mi çalıştırılsın."},
            "timeout_seconds": {
                "type": ["integer", "null"],
                "description": "null: 60 sn. Uzun kurulum/derleme/indirme için en çok 900.",
            },
        }),
        _function_schema("process_list", "Süreç sayısını ve CPU'ya göre en ağır 15 süreci döner.", {}),
        _function_schema(
            "user_memory",
            "Kullanıcının kalıcı tercih, sık kullanılan yol veya kararını saklar, arar veya siler; "
            "history önceki görevlerin hedef ve sonuçlarında arar. Parola, token ve API anahtarı saklamaz.",
            {
                "action": {"type": "string", "enum": ["remember", "recall", "forget", "history"]},
                "key": {"type": ["string", "null"], "description": "Tercih/yol/karar anahtarı."},
                "value": {"type": ["string", "null"], "description": "remember işleminde saklanacak kısa değer."},
                "query": {"type": ["string", "null"], "description": "recall/history arama metni; boşsa en yeniler."},
                "category": {"type": ["string", "null"], "enum": ["preference", "path", "decision", None]},
            },
        ),
        _function_schema(
            "ask_user",
            "Görev sırasında kullanıcıya soru sorar ve yanıtı bekler. Yalnız para hareketi/geri "
            "alınamaz dış eylem onayı (confirm), yalnız kullanıcının bildiği bilgi veya pahalı bir "
            "belirsizlik (text) için kullan.",
            {
                "question": {"type": "string", "description": "Kısa, eksiksiz soru (tutar, alıcı, hesap dahil)."},
                "kind": {"type": "string", "enum": ["confirm", "text"]},
            },
        ),
        _function_schema("read_file", "Bir dosyanın içeriğini okur (uzun dosyalar kısaltılır).", {
            "path": {"type": "string", "description": "Okunacak dosyanın yolu."},
        }),
        _function_schema(
            "write_file",
            "Dosyaya tam içerik yazar: eksik üst dizinleri oluşturur, yazılanı doğrular, eski sürümü "
            "yedekler. Başarı mesajı kanıttır; geri okuma yapma.",
            {
                "path": {"type": "string", "description": "Yazılacak dosyanın yolu."},
                "content": {"type": "string", "description": "Dosyanın tam içeriği."},
            },
        ),
        _function_schema(
            "web_search",
            "DDGS metasearch ile web veya haber araması yapar. Güncel haber için category=news ve "
            "freshness_days kullan; auto modu sorgudan haber/freshness niyetini de algılar.",
            {
                "query": {"type": "string", "description": "Arama sorgusu."},
                "category": {
                    "type": "string", "enum": ["auto", "text", "news"],
                    "description": "auto: sorgudan seç; text: genel web; news: haber akışı.",
                },
                "freshness_days": {
                    "type": ["integer", "null"], "minimum": 1, "maximum": 30,
                    "description": "Son kaç güne odaklanılacağı; güncellik gerekmiyorsa null.",
                },
            },
            required=["query"],
        ),
        _function_schema(
            "fetch_raw",
            "curl ile hızlı HTTP çekimi: JSON olduğu gibi, HTML temiz metin olarak döner. JavaScript "
            "gerektirmeyen sayfalarda browse_url'den hızlıdır.",
            {"url": {"type": "string", "description": "Çekilecek URL."}},
        ),
        _function_schema(
            "browse_url",
            "Arka planda, kullanıcıya görünmeyen ayrı Chromium sekmesi; açık Google Chrome oturumunu "
            "kullanmaz. url verilirse gider (null: mevcut sayfa), actions'ı sırayla uygular; sonunda "
            "URL, başlık, sayfa metni ve seçicileri döner. Doldur → gönder → oku tek çağrıdır.",
            {
                "url": {"type": ["string", "null"], "description": "Gidilecek URL; mevcut sayfada kalmak için null."},
                "actions": {
                    "type": "array",
                    "description": "Sırayla uygulanacak eylemler; yalnızca okumak için boş liste.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string", "enum": ["click", "fill", "press", "wait_for"]},
                            "selector": {"type": "string", "description": "Öğe seçicisi (dönen ÖĞELER listesinden)."},
                            "value": {"type": ["string", "null"], "description": "fill: metin; press: tuş; wait_for: visible/hidden/attached/detached; click: null."},
                        },
                        "required": ["action", "selector", "value"],
                    },
                },
            },
        ),
        _function_schema(
            "execute_js",
            "Node.js ile JavaScript çalıştırır. Tekrarlı görevde ilk satır '// omni:save ad' "
            "ile başarılı kodu görev boyunca sakla; '// omni:run ad' ve isteğe bağlı ikinci "
            "satır JSON ile yeniden çalıştır (JS process.argv[2] okur).",
            {"code": {"type": "string", "description": "Kod veya görev içi yardımcı çağrısı."}},
        ),
        _function_schema(
            "take_screenshot",
            "Seçilen ekranın 1000×1000 görüntüsünü alır. display_index=2 ikinci monitörü seçer; "
            "sonraki görüntü ve tıklamalar aynı ekranda kalır. Noktalar tıklama araçlarıyla aynı uzaydadır. "
            "Son eylemden sonra ekranın durulmasını kendisi bekler.",
            {
                "filename": {"type": "string", "description": "Kaydedilecek .png veya .jpg dosya yolu."},
                "display_index": {
                    "type": ["integer", "null"],
                    "description": "1 ana ekran, 2 ikinci ekran; null/eksik son seçilen ekran (başlangıçta ana).",
                },
            },
            required=["filename"],
        ),
        _function_schema("cua_get_app", "Uygulamayı başlatır veya öne getirir.", {
            "app_name": {"type": "string", "description": "Uygulama adı (örn. Safari, Notes)."},
        }),
        _function_schema(
            "cua_get_ax_state",
            "Uygulamanın öndeki penceresindeki etkileşimli öğeleri (buton, alan, bağlantı, satır…) numara, "
            "tür, etiket ve merkez koordinatıyla listeler. Ekran görüntüsünden çok daha hızlıdır.",
            {"app_name": {"type": "string", "description": "Uygulama adı."}},
        ),
        _function_schema(
            "cua_click",
            "cua_get_ax_state listesindeki numaralı öğeye erişilebilirlik ile tıklar; metin alanlarını odaklar.",
            {
                "app_name": {"type": "string", "description": "Uygulama adı."},
                "element_id": {"type": "integer", "description": "Son listedeki öğe numarası."},
            },
        ),
        _function_schema(
            "smart_click",
            "Hibrit tıklama: önce AX öğe numarası, olmazsa görsel şablon (take_screenshot görüntüsünden "
            "kırpılmış PNG) dener.",
            {
                "app_name": {"type": "string", "description": "Uygulama adı."},
                "element_id": {"type": ["integer", "null"], "description": "AX öğe numarası veya null."},
                "template_path": {"type": ["string", "null"], "description": "Şablon görsel yolu veya null."},
                "confidence": {"type": "number", "description": "Şablon eşleşme eşiği (0-1, genelde 0.8)."},
            },
        ),
        _function_schema(
            "run_action_sequence",
            "Fare/klavye eylemlerini TEK çağrıda sırayla çalıştırır. click/move: point [x, y] (ekran "
            "görüntüsü/AX uzayı); type: text (her Unicode metin, Türkçe dahil); press: key ('enter', 'tab', "
            "'escape', 'cmd+c', 'cmd+shift+t'); wait: seconds (en çok 5).",
            {
                "steps": {
                    "type": "array",
                    "description": "Sırayla çalıştırılacak eylemler.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string", "enum": ["click", "move", "type", "press", "wait"]},
                            "point": POINT_SCHEMA,
                            "button": {"type": "string", "enum": ["left", "right", "middle"]},
                            "text": {"type": "string"},
                            "key": {"type": "string"},
                            "seconds": {"type": "number"},
                        },
                        "required": ["action"],
                    },
                },
            },
        ),
    ] + screen_reading_schemas() + [DISCOVERY_SCHEMA]
    if allow_edit:
        schemas.append(_function_schema(
            "edit_file",
            "Var olan büyük metin dosyasında benzersiz eski metni yenisiyle değiştirir. "
            "Tam dosya içeriği içeride write_file ile doğrulanarak yazılır. "
            "Eski metin tam ve tek eşleşmeli olmalı; ilgisiz içerik korunur.",
            {
                "path": {"type": "string", "description": "Düzenlenecek dosyanın yolu."},
                "old_text": {"type": "string", "description": "Birebir ve benzersiz eski metin."},
                "new_text": {"type": "string", "description": "Yerine geçecek tam metin."},
            },
        ))
    if chrome_session:
        # Kullanıcının açık oturumu istendiğinde gizli Playwright/API yolu ve CDP
        # araştırmasına yol açan kabuk/Node araçları bu görevden çıkarılır.
        # Chrome AX ağacı sayfa içeriğini değil yalnız tarayıcı çubuğunu gösterdiği için AX
        # araçları canlı ölçümde yalnız boşa tur harcattı; öne getirme chrome_active_tab'dadır.
        excluded = {
            "browse_url", "discover_capabilities", "fetch_raw", "web_search",
            "execute_shell", "execute_js", "process_list",
            "run_action_sequence", "smart_click", "cua_get_ax_state", "cua_click", "cua_get_app",
        }
        if skills_sh_goal(goal):
            # Açık Chrome eylemleri görünür kalır; açıkça istenen skill kaynağı okunabilir.
            excluded.difference_update({"discover_capabilities", "fetch_raw"})
        schemas = [entry for entry in schemas if entry["function"]["name"] not in excluded]
        schemas.append(_function_schema(
            "chrome_active_tab",
            "Kullanıcının açık Google Chrome penceresini kullanır. URL verilirse aynı sitedeki "
            "mevcut sekmeyi bulur ve görünür kılar; yoksa etkin sekmeye gider; sayfanın yüklenmesini bekler. "
            "Null ise etkin sekmeyi okur. Giriş yapılmış Chrome profilini korur, ayrı tarayıcı açmaz.",
            {"url": {"type": ["string", "null"], "description": "Gidilecek http(s) adresi; mevcut sekmeyi okumak için null."}},
        ))
        schemas.extend([
            _function_schema("cua_click_point", "Son ekran görüntüsündeki noktaya sol tıklar.", {
                "point": POINT_SCHEMA,
            }),
            _function_schema("cua_type_text", "Odaklı alana Unicode metin yazar.", {
                "text": {"type": "string"},
            }),
            _function_schema("cua_press_key", "Tuşa/kısayola basar: enter, tab, escape, cmd+a gibi.", {
                "key": {"type": "string"},
            }),
            _function_schema(
                "cua_submit_text",
                "point'teki alana tıklar, içeriğini text ile değiştirir ve Enter'a basar. Yalnız arama "
                "kutusu veya tek alanlı gönderim içindir; çok alanlı formda cua_fill_field kullan.",
                {"point": POINT_SCHEMA, "text": {"type": "string"}},
            ),
            _function_schema(
                "cua_fill_field",
                "Form alanına tıklar, içeriğini text ile değiştirir ve Enter'a BASMAZ. Formu yalnız tüm "
                "zorunlu alanlar dolunca gönder düğmesine tıklayarak gönder.",
                {"point": POINT_SCHEMA, "text": {"type": "string"}},
            ),
        ])
    if goal is not None and camera_photo_goal(goal):
        schemas.append(_function_schema(
            "capture_photo",
            "Varsayılan Mac kamerasından TEK fotoğrafı otomatik benzersiz adla ~/Desktop'a kaydeder; "
            "görüntüyü doğrular, mevcut dosyayı ezmez ve tam yolu sonuçta verir. "
            "Ayrı kamera/ffmpeg/Photo Booth yoklaması yapmadan doğrudan kullan. Başarısız olursa Photo Booth'a geç.",
            {},
        ))
    return schemas


# Modelin çağırabileceği adlar: getattr ile Toolbox'ın özel yöntemlerine
# (_read_full, close_browser…) ulaşılmasın.
TOOL_NAMES: frozenset[str] = frozenset(
    schema["function"]["name"]
    for sample in ("fotoğraf çek masaüstüne", "açık Chrome oturumunu kullan")
    for schema in build_tool_schemas(sample, allow_edit=True)
)

# Salt okunur araçlar aynı (ad + argüman) için önbelleklenebilir. Canlı durum (AX listesi)
# önbelleklenmez; her başarılı yan etkili çağrıdan sonra önbellek tamamen temizlenir.
_CACHEABLE_TOOLS: frozenset[str] = frozenset({"process_list", "read_file", "web_search", "fetch_raw"})

# Yan etkili araçlar model sırasıyla SERİ çalışır (aynı anda iki tıklama/yazma çakışmasın,
# paralel bir okuma yazmadan önce bayat sonuç önbelleğe girmesin, eylem→gözlem sırası
# korunsun); aralarındaki bağımsız salt okunur bloklar gerçek paralellikle çalışır.
_SIDE_EFFECT_TOOLS: frozenset[str] = frozenset({
    "execute_shell", "write_file", "edit_file", "execute_js", "take_screenshot", "browse_url",
    "cua_get_app", "cua_click", "smart_click", "run_action_sequence", "capture_photo",
    "chrome_active_tab", "cua_click_point", "cua_type_text", "cua_press_key", "cua_submit_text",
    "cua_fill_field", "cua_click_text", "cua_scroll", "cua_read_scrollable",
    "user_memory", "ask_user",
})

# Ekranı değiştiren araçlar. Bunlardan sonra görüntü alınmadıysa tur sonunda ekran
# kendiliğinden gözlenir: canlı kayıtta her tıklama ayrı bir "ekran görüntüsü al" turu ve 2 sn
# sabit bekleme gerektiriyordu (22 tur, 100 sn). Gözlem eskiden yalnız açık Chrome yolundaydı;
# genel yoldaki takip görevinde model eylem dizisinin sonucunu hiç görmeden "gönderdim" dedi.
_SCREEN_ACTION_TOOLS: frozenset[str] = frozenset({
    "chrome_active_tab", "cua_click_point", "cua_type_text", "cua_press_key", "cua_submit_text",
    "cua_fill_field", "cua_click_text", "cua_scroll", "cua_read_scrollable",
    "cua_click", "smart_click", "run_action_sequence",
})
# Bitişte doğrulama isteyen gerçek ekran eylemleri: yalnız sayfaya gitmek (chrome_active_tab) sayılmaz
_GUI_VERIFICATION_TOOLS: frozenset[str] = _SCREEN_ACTION_TOOLS - frozenset({"chrome_active_tab"})
_ACTION_RECEIPT_TOOLS: frozenset[str] = (_SCREEN_ACTION_TOOLS - frozenset({"cua_read_scrollable"})) | frozenset({"take_screenshot"})
_DETERMINISTIC_PROGRESS_TOOLS: frozenset[str] = frozenset({
    "write_file", "edit_file", "execute_js", "capture_photo", "user_memory",
})
_READ_PROGRESS_TOOLS: frozenset[str] = frozenset({
    "web_search", "fetch_raw", "read_file", "browse_url", "cua_read_scrollable",
})
AUTO_OBSERVATION_PREVIEW: str = "otomatik gözlem"
VERIFICATION_OBSERVATION_PREVIEW: str = "bitiş doğrulaması"
