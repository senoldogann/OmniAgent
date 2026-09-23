# Entegrasyonlar

OmniAgent harici hizmet görevinde `discover_capabilities` ile önce yerel kataloğu sorgular. Outlook/Hotmail için yerleşik Microsoft Graph adaptörü hazırdır. Yerel dosya görevlerinde keşif yapılmaz. Yeni bir hizmette registry ve resmî kaynak taraması toplam sekiz saniyeyle sınırlıdır; bulunan kayıtlar incelenmeden güvenilir ya da çalıştırılabilir sayılmaz.

## Outlook kurulumu

1. [Microsoft Entra uygulama kayıtları](https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade) içinde kişisel Microsoft hesaplarını destekleyen bir public client uygulaması kaydedin. Platform olarak **Mobile and desktop applications**, yönlendirme URI'sı olarak `http://localhost` kullanın.
2. Uygulamanın **Application (client) ID** değerini ilk Outlook görevinde açılan OmniAgent formuna girin. Alternatif olarak `OMNI_OUTLOOK_CLIENT_ID` ortam değişkenini ayarlayın.
3. Microsoft giriş ekranında kişisel hesabınızı seçip **delegated Mail.ReadWrite** iznini verin. İstemci sırrı ve posta gönderme izni kullanılmaz.

MSAL token önbelleği macOS Keychain'de, temizlik kuralı ve işlem günlüğü `~/Library/Application Support/OmniAgent/mail/<hesap-kimliği-özeti>/` altında saklanır. Hesap kimliği dosya adında düz metin olarak tutulmaz. Kurallar hesap başına bir kez sorulur; `outlook_clean(reset_rule=true)` ile yeniden tanımlanabilir. Varsayılan işlem Çöp Kutusu'na taşımadır. `outlook_restore(operation_id=...)` günlükteki taşınan iletileri kaynak klasörlerine geri taşır.

İlk canlı denemede yalnız önceden seçilmiş deneme iletileri için kural tanımlayın, sonucu kontrol edip işlem kimliğiyle geri yükleyin. Bu projede uygulama kimliği/hesap bağlantısı bulunmadığından canlı posta kutusunda taşıma yapılmadı; Graph akışı sahte sağlayıcı testleriyle doğrulandı.

## MCP ve skill kataloğu

Kalıcı katalog `~/Library/Application Support/OmniAgent/catalog.json` dosyasıdır. Güvenilir MCP kaydında kaynak, sabit sürüm, taşıma ve desteklenen işlemler açıkça tanımlanır. `operation_tools` eşlemesi, bir işlem etiketi için modele açılabilecek uzak araç adlarını sınırlar. `readonly_tools` listesinde olmayan uzak çağrılar yan etkili kabul edilip sırayla yürütülür. Önceden incelenmemiş registry sonuçları otomatik kurulmaz.

Sabit sürümlü Python paketleri ana ajan ortamından ayrı sanal ortama; Node paketleri ayrı dizine kurulur. Kurulum görev başına bir kez denenir ve 60 saniyeyle sınırlıdır. Hazır stdio/Streamable HTTP MCP bağlantıları uygulama oturumunda yeniden kullanılır. Güvenilir skill metni yalnız görevle ilgili yardımcı bilgi olarak modele verilir; sistem istemini veya hesap yetkisini değiştirmez.

Test: `OMNI_UI_TEST=1 .venv/bin/python -m pytest -q`. Hız karşılaştırması için `benchmark.py --runs 3 --json ...` kullanılır.
