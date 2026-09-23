# Araç keşfi teslimi: aynı backend ile önce/sonra

Dokuz deterministik senaryo üçer kez, `opencode` backend'iyle çalıştırıldı. Ham sonuçlar [önce](before.json) ve [sonra](after.json) dosyalarındadır.

| Ölçü | Önce | Sonra |
| --- | ---: | ---: |
| Başarılı çalışma | 26/27 | 26/27 |
| Medyan süre | 4,56 sn | 4,63 sn |
| En uzun süre | 101,69 sn | 12,08 sn |
| Toplam model turu | 90 | 56 |
| Toplam araç çağrısı | 75 | 41 |

Medyan farkı +%1,5 ve başarı oranı aynı olduğu için varsayılan backend/özellik değişimi için belirtilen %10 hız eşiği aşılmadı. Her iki kümede birer `paralel` senaryo hatası var; tekil maksimum ve tur toplamları model/ağ değişkenliğine açıktır. Bu senaryolar yerel görevlerdir; Outlook ilk OAuth/kurulum süresi ve hazır bağlantı hızı ayrı canlı ölçülemedi.

Sahte Graph testi 100 eşleşen iletinin 5 batch isteğiyle taşındığını, eşleşmeyen iletinin değişmediğini ve ileti başına model turu oluşmadığını doğrular. Yerel katalog 1000 sorguda P95 <100 ms testinden geçer. Gerçek SDK ile yerel MCP stdio ve Streamable HTTP uçtan uca testleri çalışır.
