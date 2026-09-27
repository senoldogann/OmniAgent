# macOS uygulaması

`OmniAgent.app` masaüstü arayüzünü Python çalıştırma komutu olmadan Finder, Spotlight ve Dock'tan
açar. Telegram köprüsü ayrı arka plan hizmetidir; uygulama kurulumu onu kapatmaz veya güncellemez.

## Yerel kurulum

Apple Silicon Mac'te proje kökünden:

```sh
uv sync --python 3.11 --frozen
sh packaging/build_macos.sh
mkdir -p ~/Applications
ditto dist/OmniAgent.app ~/Applications/OmniAgent.app
open ~/Applications/OmniAgent.app
```

Derleme paketi `dist/OmniAgent.app` içinde üretir. Betik Info.plist, kod imzası ve Keychain'e
dokunmayan Tk açılış denetimini çalıştırır. `~/Applications/OmniAgent.app` Finder'daki Uygulamalar
klasöründen ve Spotlight'tan açılır; Dock'a sürüklenerek sabitlenebilir. `/Applications` hedefi için
aynı `ditto` komutunda hedef yolu değiştirin; bu yolun yazılabilir olması gerekir.

Uygulama ilk kez farklı bir imzayla açıldığında macOS mevcut Keychain anahtarlarına erişim isteyebilir.
İsteği kullanıcı değerlendirir; eski anahtarlar aktarılmasa bile ayarlardan yeniden kaydedilebilir.
Ekran Kaydı, Erişilebilirlik, Mikrofon, Konuşma Tanıma ve Chrome otomasyonu izinleri yeni uygulama
kimliği için ayrıca istenebilir. Bu izinler macOS Sistem Ayarları'ndan verilir; Telegram hizmetinin
Python işlemine verilmiş izinleri uygulamaya kendiliğinden taşınmaz.

## Güncelleme ve bakım

Bu paket kendi kodunu içerir. Kaynak depoda `git pull` veya Telegram'da `/update` komutu masaüstü
uygulamasını değiştirmez. Yeni sürüm için derleme ve `ditto` kopyalama adımlarını tekrar çalıştırın;
önce uygulamadan çıkın. Kullanıcı verileri `~/Library/Application Support/OmniAgent/` altında kalır.
Telegram hizmeti kaynak depodaki Python kurulumunu kullanmaya devam eder ve kendi `/update`
mekanizmasıyla güncellenir.

Bu kurulum aynı Mac'te yerel kullanım içindir. Başka Mac'lere dağıtım için Developer ID imzası ve
noter onayı ayrı bir yayın sürecidir; bu derleme o aşamayı tamamlamış gibi sunulmaz.
