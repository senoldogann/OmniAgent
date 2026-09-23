# Ray ve kaynak sağlamlığı çalışması

Aynı `opencode` backend'iyle dokuz temel senaryo üçer kez çalıştırıldı. İlk küme değişikliklerden önceki 11 senaryolu koşudan yalnız temel dokuz senaryoyu içerir. Son iki küme, `benchmark.py --runs 3 --concurrency 3` varsayılanıyla çalıştırıldı. Ham sonuçlar [önce](before.json), [sonra A](after-a.json), [sonra B](after-b.json) dosyalarındadır.

| Ölçü | Önce | Sonra A | Sonra B |
| --- | ---: | ---: | ---: |
| Başarı | 25/27 | 26/27 | 26/27 |
| Medyan süre | 4,84 sn | 4,17 sn | 4,39 sn |
| En uzun süre | 13,14 sn | 9,39 sn | 7,42 sn |
| Model turu toplamı | 58 | 54 | 54 |
| Araç çağrısı toplamı | 39 | 39 | 40 |

Medyan süre iki son koşuda sırasıyla yaklaşık %13,8 ve %9,3 daha düşük. Farkların bir bölümü model/ağ değişkenliğinden gelebilir; başarı düşüşü gözlenmedi. Ev dizininde yeni istenmeyen dosya bulunmadı.

Yerel ölçümde korunan yol sorgusu önbelleklemeden önce yaklaşık 0,213 ms, sonra 0,062 ms sürdü (1000 sorgu ortalaması). `seq 1 2000` çıktısı 6 canlı UI olayına paketlendi ve yerel süreç yaklaşık 12 ms'de tamamlandı. Çıktı tavanları stdout için 64 KiB, stderr için 16 KiB; kırpılma hem araç sonucunda hem canlı olayda bildirilir.

`takip_bos` geçmişi bilerek verilmeyen negatif kontroldür. Önceki varsayılan 11 senaryolu koşuda üç kez başarısız olup uzun model döngüleri ürettiği için varsayılan dokuz temel senaryodan çıkarıldı; `--only takip,takip_bos` ile ayrı ölçülebilir. Bu kontrol, yeni rayların başarı ölçüsüne katılmamalıdır.

Uygulama doğrulaması: `OMNI_UI_TEST=1 .venv/bin/python -m pytest tests/ -q`, `uvx ruff check --select E4,E7,E9,F,B .`, `uv sync --locked`, gerçek Tk/AppKit başlık çubuğu kontrolü. Canlı Outlook hesabı ve OAuth uygulama kaydı bu ortamda olmadığı için posta kutusunda canlı işlem doğrulanmadı.

Tasarım kaynağı: Python'un [`shlex` belgeleri](https://docs.python.org/3/library/shlex.html) kabuk ayrıştırmasının tamamını sağlamadığını açıkça belirtir; komut rayları bu nedenle en iyi çaba korumasıdır. [`subprocess` belgelerindeki](https://docs.python.org/3/library/subprocess.html) PIPE tıkanması uyarısı, çıktı limiti dolsa da boruları boşaltma kararını destekler. Dosya kaydında `flush` ardından `fsync` kullanımı [`os.fsync` belgelerine](https://docs.python.org/3/library/os.html#os.fsync) uygundur.
