# Entegrasyonlar

OmniAgent harici hizmet görevinde `discover_capabilities` ile önce yerel kataloğu sorgular. Outlook/Hotmail için yerleşik Microsoft Graph adaptörü hazırdır. Yerel dosya görevlerinde keşif yapılmaz. Yeni bir hizmette registry ve resmî kaynak taraması toplam sekiz saniyeyle sınırlıdır; bulunan kayıtlar incelenmeden güvenilir ya da çalıştırılabilir sayılmaz.

## Outlook kurulumu

1. [Microsoft Entra uygulama kayıtları](https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade) içinde **New registration** açın; **Supported account types** olarak **Personal Microsoft accounts only** seçin. Bu adaptör `consumers` otoritesini kullanır; iş/okul hesabıyla giriş yapmaz.
2. **Authentication → Add a platform → Mobile and desktop applications** altında sistem tarayıcısı yönlendirmesi `http://localhost` değerini seçin. **Advanced settings → Allow public client flows** seçeneğini **Yes** yapıp kaydedin. [Microsoft masaüstü uygulaması kurulumu](https://learn.microsoft.com/en-us/entra/identity-platform/scenario-desktop-app-configuration)
3. **API permissions → Add a permission → Microsoft Graph → Delegated permissions** altında `Mail.ReadWrite` ekleyin. **Application permissions** seçmeyin; kişisel Microsoft hesabı bu delegated izne giriş sırasında onay verebilir. [Microsoft Graph izinleri](https://learn.microsoft.com/en-us/graph/permissions-reference#mailreadwrite)
4. **Overview → Application (client) ID** değerini ilk Outlook görevinde açılan OmniAgent formuna girin. Alternatif olarak `OMNI_OUTLOOK_CLIENT_ID` ortam değişkenini ayarlayın.
5. Microsoft giriş ekranında kişisel Outlook/Hotmail hesabınızı seçip `Mail.ReadWrite` onayını verin. İstemci sırrı ve posta gönderme izni kullanılmaz.

MSAL token önbelleği macOS Keychain'de, temizlik kuralı ve işlem günlüğü `~/Library/Application Support/OmniAgent/mail/<hesap-kimliği-özeti>/` altında saklanır. Hesap kimliği dosya adında düz metin olarak tutulmaz. Kurallar hesap başına bir kez sorulur; `outlook_clean(reset_rule=true)` ile yeniden tanımlanabilir. Varsayılan işlem Çöp Kutusu'na taşımadır. `outlook_restore(operation_id=...)` günlükteki taşınan iletileri kaynak klasörlerine geri taşır.

İlk canlı denemede yalnız önceden seçilmiş deneme iletileri için kural tanımlayın, sonucu kontrol edip işlem kimliğiyle geri yükleyin. Bu projede uygulama kimliği/hesap bağlantısı bulunmadığından canlı posta kutusunda taşıma yapılmadı; Graph akışı sahte sağlayıcı testleriyle doğrulandı.

## MCP ve skill kataloğu

Kalıcı katalog `~/Library/Application Support/OmniAgent/catalog.json` dosyasıdır. Güvenilir MCP kaydında kaynak, sabit sürüm, taşıma ve desteklenen işlemler açıkça tanımlanır. `operation_tools` eşlemesi, bir işlem etiketi için modele açılabilecek uzak araç adlarını sınırlar. `readonly_tools` listesinde olmayan uzak çağrılar yan etkili kabul edilip sırayla yürütülür. Önceden incelenmemiş registry sonuçları otomatik kurulmaz.

Kalıcı skill'ler `~/.agents/skills/<ad>/SKILL.md` veya `~/.codex/skills/<ad>/SKILL.md` altında bulunur. Kataloğa eklenen MCP paketleri `~/Library/Application Support/OmniAgent/packages/<paket-özeti>/` içinde ayrı ortamda kurulur; kayıtları `catalog.json` içinde kalır. Uzak HTTP MCP için paket indirilmez, katalog kaydı sonraki sohbette yeniden bağlanmak için yeterlidir. Masaüstü composer'ında `/tools` veya `/yetenekler` kayıtlı entegrasyonları ve skill sayısını, `/skills` skill adlarını model ya da ağ çağrısı olmadan gösterir. Belirli bir yeteneği kullanmak için hedefe adını yazın: `Context7 kullanarak React 19 API belgesine bak` gibi. Composer'da `@` özel seçim sözdizimi değildir. Yeni, incelenmemiş skill/MCP/plugin, yalnız keşif sonucunda görünür; kalıcı kurulum ayrı doğrulama gerektirir.

Sabit sürümlü Python paketleri ana ajan ortamından ayrı sanal ortama; Node paketleri ayrı dizine kurulur. Kurulum görev başına bir kez denenir ve 60 saniyeyle sınırlıdır. Hazır stdio/Streamable HTTP MCP bağlantıları uygulama oturumunda yeniden kullanılır. Güvenilir skill metni yalnız görevle ilgili yardımcı bilgi olarak modele verilir; sistem istemini veya hesap yetkisini değiştirmez. Skill tek başına seçilmiş çalıştırılabilir bağlantı sayılmaz; çevrimiçi keşif istenmişse API/MCP araması sürer. Kullanıcı özellikle skills.sh isterse `discover_capabilities(query="skills.sh:outlook", operations=["mail"], allow_online=true)` ile oradaki adaylar aranır; sonuç kaynağı incelenir, otomatik güvenilir/kurulu sayılmaz. skills.sh araması CLI’nin kullandığı deneysel uç noktaya dayanır; erişilemezse mevcut Graph veya tarayıcı yolu korunur. Kurulu yerel skill'ler `~/.agents/skills/*/SKILL.md` ve `~/.codex/skills/*/SKILL.md` yollarından katalog açılırken ve yeni eşleşme gerektiğinde dizinlenir. Özel dizinler `OMNI_SKILLS_DIRS` ile, macOS'ta iki noktayla ayrılarak verilebilir. `discover_capabilities` aracına `query="catalog"`, `operations=[]`, `allow_online=false` gönderildiğinde mevcut entegrasyon ve skill adları ağ isteği yapılmadan listelenir. Skill içeriği yalnız seçildiğinde okunur; 64 KB üzerindeki veya izin verilen dizinin dışına çözülen bağlantılar atlanır.

### Context7

Context7, kurulum gerektirmeyen güvenilir uzak MCP kaydıdır. `discover_capabilities(query="context7", operations=["docs"], allow_online=false)` çağrısı `resolve-library-id` ve `query-docs` araçlarını göreve açar; bağlantı oturumda yeniden kullanılır. Resmî `https://mcp.context7.com/mcp` uç noktası anahtarsız temel kullanım sunar, ancak hız sınırları uygulanır. Context7'nin doğrudan REST API'si ise API anahtarı ister; OmniAgent varsayılan olarak anahtarsız MCP yolunu kullanır. Sürüm veya API davranışı belirsiz olduğunda kullanılır; sırlar ve özel kod sorguya eklenmez. [Context7 resmî MCP kılavuzu](https://github.com/upstash/context7/tree/master/packages/mcp) · [REST API kılavuzu](https://context7.com/docs/api-guide)

## Composer modları ve sesli giriş

Composer'da üç açık görev profili bulunur:

- **Normal**: 25 model turu / 10 dakika (varsayılan).
- **Uzun**: 50 model turu / 20 dakika.
- **Otonom**: 100 model turu / 45 dakika.

Otonom mod yalnızca bütçeyi genişletir; dosya, shell ve GUI güvenlik raylarını kapatmaz.
Dört ardışık tamamen başarısız araç turunda ajan ilerleme yok sayarak durur.

Mikrofon düğmesi macOS'un Speech framework'ünü ve `AVAudioEngine` canlı buffer
akışını kullanır. Gizlilik için ağ fallback'i yoktur: seçili dilde `supportsOnDeviceRecognition`
desteklenmiyorsa sesli giriş açık hatayla devre dışı kalır ve Speech request'i
`requiresOnDeviceRecognition=true` ile çalışır. İzin API'lerine girmeden önce
`NSMicrophoneUsageDescription` ve `NSSpeechRecognitionUsageDescription` runtime'da doğrulanır;
eksik metadata crash riski yerine kullanıcıya hata olarak gösterilir. Kayıt 55 saniyeyle sınırlıdır.
İlk kullanımda mikrofon ve konuşma tanıma izinleri istenir. Konuşma sürerken partial sonuçlar composer'a yazılır, düğmeye tekrar basılınca
buffer akışı durur ve final sonuç taslağa eklenir; otomatik gönderilmez. Geçici ses dosyası
oluşturulmaz. Mikrofon düğmesi ve transcript kopyalama düğmesi SVG ikon kullanır.
Paketlenmiş veya Python.app tabanlı runtime'ın Info.plist dosyasında bu iki privacy anahtarı bulunmalıdır.

## Kalıcı kullanıcı hafızası

`user_memory` aracı, kullanıcı açıkça istediği kısa tercih, sık kullanılan yol veya kalıcı kararı
`user_memory.json` dosyasına atomik olarak yazar. Görevler arasında hatırlanabilir; ancak kayıtlar
otomatik olarak model istemine enjekte edilmez, yalnızca model gerektiğinde arar. `remember` ve
`forget` işlemleri yalnız mevcut kullanıcı hedefi açıkça kalıcı hafıza değişikliği istediğinde
runtime tarafından açılır; prompt talimatı tek güvenlik sınırı değildir. Parola/token/API anahtarı
tespiti best-effort bir denylist/desen filtresidir ve evrensel secret scanner garantisi vermez.
`recall` hafızayı değiştirmediği için mutation capability olmadan kullanılabilir.

Test: `OMNI_UI_TEST=1 .venv/bin/python -m pytest -q`. Hız karşılaştırması için `benchmark.py --runs 3 --json ...` kullanılır.
