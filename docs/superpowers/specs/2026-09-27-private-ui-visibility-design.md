# OmniAgent penceresini gizleme ve kendi yakalamalarından çıkarma

## Hedef

macOS masaüstü uygulaması `⌘X` ile sistem genelinde gizlenip aynı kısayolla geri
getirilecek. Gizleme, çalışan görevi veya Telegram köprüsünü durdurmayacak.
OmniAgent'ın kendi tam ekran görüntülerinde açık UI pencereleri yer almayacak.

## Platform sınırı ve ürün dili

`⌘X` sistem genelinde kaydedildiği için başka uygulamaların Kes kısayoluyla
çakışabilir; kullanıcı bu seçimi yaptı. Apple, `NSWindowSharingNone` değerinin
görünen bir pencereyi ekran yakalamalarından gizlemek için kullanılmamasını
belirtiyor. Bu yüzden üçüncü taraf ekran görüntüsü, QuickTime kaydı, görüntülü
görüşme, fiziksel kamera veya başka bir uygulamanın videosu için gizlilik
garantisi sunulmayacak. Böyle bir kayıtta UI görünmemesi gerekiyorsa kullanıcı
önce `⌘X` ile pencereyi gizlemeli.

## Tasarım

- Küçük macOS bağlayıcısı Carbon `RegisterEventHotKey` ile `⌘X` kaydeder.
  Başarısız kaydı arayüzde hata olarak gösterir; görünür pencereyi tek yönlü
  gizleyen yerel yedek kısayol açmaz. Bağlayıcı kapatılırken kaydı kaldırır.
- Hotkey geri çağrısı Tk'ye doğrudan dokunmaz; arayüzün kuyruğuna görünürlük
  isteği bırakır. Var olan `_tick` döngüsü bunu Tk ana thread'inde uygular.
  Görünürken `NSApplication.hide_`, gizliyken `unhide_` ve etkinleştirme çağrısı
  yapılır. Açık Ayarlar penceresi de uygulamayla birlikte gizlenir.
- Kullanıcı gizlediğinde `_sync_menu_status` açık gizli durumunu önce kontrol
  ederek geçici menü çubuğu durum öğesini kaldırır ve görev sürerken yeniden
  oluşturmaz. Görev ve geçmiş verileri bellekte kalır; açıldığında arayüz
  kaldığı yerden sürer.
- `tools/screen.py` tam ekran Quartz yakalamalarında on-screen pencere listesini
  alır. Adı `OmniAgent` olan veya `OmniAgent —` ile başlayan ana, Ayarlar ya da
  girdi pencerelerinin PID'lerini bulursa aynı süreçteki
  pencereleri listeden çıkarıp `CGWindowListCreateImageFromArray` ile görüntüyü
  oluşturur. Böylece kullanıcının gördüğü UI, yalnız OmniAgent'ın kendi
  ekran görüntüsü, OCR ve durulma gözlemlerine girmez. Uygulama penceresi
  kapsamlı Chrome yakalaması zaten yalnız Chrome penceresini seçer.
- UI süreci bulunmazsa mevcut hızlı Quartz yolu korunur. Filtreli yakalama
  başarısız olursa UI içerebilecek ham kare sessizce döndürülmez; açık hata
  verilir. Bu, gizlilik iddiasının yanlış olmasını önler.

## Doğrulama

- Saf seçim testleri yalnız ilgili PID pencerelerini çıkarır; ana pencere
  küçültülüp Ayarlar veya girdi penceresi açıkken de PID'yi bulur; UI yokken eski
  yakalama yolunun kullanıldığını, filtre başarısızsa hata verildiğini ölçer.
- Gerçek Tk testinde gizle/göster isteği görev ve transkripti korur; aktif
  görev sırasında menü çubuğu durum öğesi tekrar oluşmaz; hotkey
  kaydı/kaldırılması ayrı bir testte doğrulanır.
- Canlı macOS'ta kayıt durumu, UI görünürlüğü ve kendi yakalama filtresi
  kontrol edilir; üçüncü taraf video görünmezliği başarı ölçütü değildir.
- Tam test, `OMNI_UI_TEST=1`, `compileall`, `git diff --check`, CI ve birleşme
  sonrası yeniden başlatma yapılır.
