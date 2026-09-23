# Sohbet bağlamı ve Markdown — uygulama/ölçüm kaydı

Tarih: 23 Eylül 2026. Backend: `opencode`. Her senaryo üç kez, eşzamanlılık üç ile çalıştırıldı.

## Uygulama

- `markdown_render.py`: saf blok/satır içi çözümleme; başlık, vurgu, bağlantı, liste, alıntı, çizgi, kod ve mono tablo hizalama. Kod parçaları vurgudan önce korunur. Alt çizgi italik sayılmaz, kapanmamış kod çiti metin sonuna kadar sürer.
- `ui.py`: metin akarken daktilo gösterimi sürer; akış kuyruğu boşalınca asistan bölgesi biçimlendirilir. Yeniden deneme ham metni de siler.
- `conversation.py`: son 8 alışveriş, cevap başına en fazla 1200 karakter, son 5 farklı araç özeti (her biri en fazla 240 karakter). Dosya içerikleri araç özetlerine eklenmez. Kullanıcı mesajları korunur; dolayısıyla toplam bağlam için sabit bir token üst sınırı yoktur.
- `main.py`: geçmiş, değişmeyen sistem mesajının arkasında kullanıcı/asistan mesajları olarak taşınır. Başarısız ve durdurulmuş görevler de bir exchange üretir; kısmi başarısız yanıtlarda neden ayrıca etiketlenir.
- `ui.py`: başlıkta bağlam sayısı; Temizle/⌘K ile oturum bağlamı sıfırlama. Tamamlanma olayı Tk kuyruğundan işlenmeden yeni görev/temizleme yapılması engellenir. Başlangıç hataları da arayüzde sohbet kaydı olarak saklanır.
- CLI boş geçmişle çalışır. Sohbet geçmişi yalnızca açık UI oturumunda tutulur; kalıcı epizot belleğinden modele aktarım yapılmaz.
- Benchmark görevleri ayrı bellek dosyaları kullanır; paralel koşular aynı dosyayı ezmez.
- Planda isteğe bağlı olarak anılan sistem mesajına çalışma dizini ekleme değişikliği uygulanmadı.

## Regresyon: özgün 9 senaryo × 3 koşu

| Ölçüt | Önce | Sonra |
| --- | ---: | ---: |
| Başarı | 25/27 | 27/27 |
| Medyan süre | 4,71 sn | 4,78 sn |
| En uzun süre | 23,72 sn | 9,96 sn |
| Medyan model turu | 2 | 2 |
| Girdi token toplamı | 183795 | 172265 |
| Önbellek isabeti | %67,97 | %68,95 |

Önceki sürümde bir satış dosyasına fazladan sonuç satırı yazıldı ve bir paralel okuma yanıtında KOD- önekleri atlandı. Sonraki koşuda tüm doğrulamalar geçti. Medyan farkı +0,07 sn. Canlı model/ağ değişkenliği nedeniyle bu küçük örneklem tek başına genel hız veya doğruluk artışı kanıtı değildir. Boş geçmişli isteklerde sistem mesajı ve mesaj sırası korunur.

## Takip: iki mesajlı senaryo

İlk mesaj rastgele adlı bir dizindeki en büyük dosyanın tam yolunu buldurur. İkinci mesaj yalnızca “Onun satır sayısını söyle” der. Doğru cevap 137 satırdır. `takip` ilk raporun exchange'ini taşır; `takip_bos` aynı iki aşamayı geçmiş olmadan yürütür.

Aşağıdaki süre/token/tur değerleri yalnızca ikinci mesaja aittir. İlk mesajın doğru dosyayı bulması da başarı koşuludur.

| Ölçüt | Bağlamlı | Bağlamsız |
| --- | ---: | ---: |
| Başarı | 3/3 | 0/3 |
| Medyan süre | 6,71 sn | 57,34 sn |
| En uzun süre | 6,76 sn | 79,20 sn |
| Medyan model turu | 3 | 25 |
| Girdi token toplamı | 24880 | 273806 |

Bağlamlı koşular 2–3 turda sonuçlandı; planın 1–2 tur beklentisi tüm koşularda sağlanmadı. Bağlamsız bir koşu yanlış dosyanın 646 satırını bildirdi; iki koşu 25 tur sınırına ulaştı. Tüm ölçümlerde ev dizininde beklenmeyen yeni görünür dosya oluşmadı.

## Doğrulama

`OMNI_UI_TEST=1 .venv/bin/python -m pytest -q`: **27 geçti, 1 atlandı**. Atlanan mevcut bağlantı testi `OMNI_LIVE_API_TEST=1` gerektirir; canlı model erişimi ayrıca benchmark ile kullanıldı.

Saf dönüşüm testleri: komut/alt çizgi sadakati, kod içinde yıldızlar, iç içe kalın/kod işaretleri, tablo hizalama, kaçırılmış borular, kapanmamış çit, boş geçmiş, sıra/sınır/girdi değişmezliği ve araç özeti.

Ajan entegrasyon testleri: geçmiş mesajlarının sistem mesajından sonra gelmesi; başarılı, hata, durdurulmuş ve tur sınırına ulaşmış raporların exchange üretmesi.

Gerçek Tk bileşenlerinde otomatik duman testi: tablo/kod/başlık etiketleri, 300 satırlık cevap, daktilo bitişi, tekrarlı bitişin metni bozmaması, stream_reset sonrası eski metnin silinmesi, bitmiş Future'ın kuyruğa teslimi ve temizleme sonrası sıfır bağlam. Görsel elle inceleme yerine gerçek widget içeriği ve etiketleri doğrulandı.

## Tekrar çalıştırma

```sh
.venv/bin/python benchmark.py --runs 3 --concurrency 3 --only gun,satir,js,satis,paralel,siralama,json,ceviri,sadakat --json /tmp/omni-regression.json
.venv/bin/python benchmark.py --runs 3 --concurrency 3 --only takip,takip_bos --json /tmp/omni-followup.json
OMNI_UI_TEST=1 .venv/bin/python -m pytest -q
```

Ham ölçümler: [before.json](before.json), [after.json](after.json), [followup.json](followup.json).
