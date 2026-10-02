# Sürekli mod ve sohbet tipografisi — doğrulama

## Teslim

Kaynak commit: `2e6dd2fac625cdad51358bb6c54a0dc3458e0cfe`. Kurulu uygulama: `/Applications/OmniAgent.app`.
İkili SHA-256: `041f504c65c129794fab9a9bd35e2796574a7f465166eed742228fb5d1f61085`.
Developer ID takım kimliği `79DZ4AA4DW`; mevcut kalıcı imza kimliği korundu.
`codesign --verify --deep --strict` ve kurulu paketin `--bundle-check` kontrolü geçti.
Önceki paket: `/Users/dogan/Library/Application Support/OmniAgent/app-backups/OmniAgent-20261002-112439.app`.

## Davranış

- Sürekli modda sıradan sohbet ve salt okunur araştırma ortak yönlendirmeyi kullanır;
  yalnız mod seçimi bu istekleri ağır görev döngüsüne zorlamaz.
- Görev döngüsünün `report_goal_met` sonucu tamamlanma adayıdır. Kullanıcıdan
  tamamlanma onayı istemeden ortak kanıt denetimine gider. Bilinmeyen kanıt id'leri,
  bekleyen yayınlama işlemi ve kanıtsız sonuçlar başarıya çevrilmez.
- Sürekli modda `ask` arayüzü açıp kullanıcı beklemez; InteractionRequired üretir.
  Bağımsız işler devam edebilir; kurtarma sınırı dolduğunda açık eksik sonuçla kapanır.
  Sessizlik ödeme/yayınlama izni sayılmaz. macOS izin ve Keychain pencereleri bu
  uygulama kuralının kapsamı dışındadır.
- Doğrulama bütçesi tükenen sürekli görev adayı başarılı sayılmaz.

## Tipografi

Normal gövde 16; h1/h2/h3 başlıklar 25/21/18 punto ve kalın. Üçüncü seviye
başlıklar sıcak vurgu renginde. Liste numaraları ayrı kalın ve vurgu renginde;
listelerde satır/girinti boşlukları arttı. Kod blokları Menlo kullanır.
Başlık etiketleri satır içi kalın/italik etiketlerinden yüksek önceliklidir.
Düz kanıt çıktısındaki Kaynak başlıkları da üçüncü seviye başlık olarak ayrılır.

Mevcut kullanıcı görünümünde Menlo açıkça seçiliydi. Kullanıcının tipografi isteğiyle
Helvetica Neue'ye geçirildi; önceki tercih dosyası şu konumda yedeklendi:
`/Users/dogan/Library/Application Support/OmniAgent/appearance-before-typography-20261002-111354.json`.

## Testler ve sınırlar

- Tam macOS/Tk koşusu: 2850 geçti, 26 atlandı; dört test eski sürekli-mod/onay
  sözleşmesini bekliyordu. Bu beklentiler yeni sözleşmeye göre güncellendi.
- Son kapsam koşusu: 310 geçti; tek 100 ms iptal testi 116 ms sürdü ve başarısızdı.
  Aynı test tek başına tekrar çalıştırıldığında geçti. Böylece son kapsamın 311
  testi doğrulandı; zamanlama değişkenliği ayrıca kaydedildi.
- Gerçek Tk arayüz testleri: 79 geçti; başlık önceliği, boyut, ağırlık ve liste
  rengi ayrıca sınandı. Ortak kanıt ve hassas GUI işlem denetimleri korunuyor.
- Gerçek seçili ollama-cloud modelle izole, kamuya açık test dosyası okundu:
  4 model turu, 1 araç çağrısı, 0 kullanıcı sorusu,
  başarı `True`. Yanıt: “Proje adı Atlas, sayı ise 7'dir.”
- İlk canlı testin “Verification code” satırı mevcut gizli bilgi filtresince
  maskelendi; o deneme başarılı sayılmadı. Kamuya açık Atlas/sayı testi bunun
  ardından ayrı yapıldı; gizli bilgi filtresi gevşetilmedi.
- Telegram ve iMessage launchd servisleri iş yokken yeni kaynakla yeniden
  başlatıldı. PID'ler 39823/39825; iMessage köprü durumu `connected`.
  Bu gözlemler alıcı teslimi veya Apple hesabı giriş doğrulaması değildir.
- Bilgisayar kullanım aracı iki denemede `Sky Computer Use native pipe startup
  failed` verdi. Yeni kurulu paketin ekrandaki son görünümü ve native sohbeti bu
  araçla doğrulanamadı. Paket açılışı, Tk bileşenleri ve gerçek model çekirdeği
  ayrı doğrulandı; ekran üzerinden yapılmış kontrol iddiası yok.

Kalıcı iş devamı tasarımı bu düzeltmenin parçası olarak uygulanmadı.
