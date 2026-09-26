# Kanıtsız Final İddiasını Kullanıcıya Taşımama

**Tarih:** 2026-09-27  
**Kapsam:** Var olan host kanıt kapılarının reddettiği eylem ve durum iddialarının son yanıtta görünmesini engellemek.

## Sorun

`run_agent_with_callback`, kaynak değişikliği veya eylem için başarılı araç kanıtı bulunmadığında `success=False` döndürüyor. Buna karşın `outcome` modelin “yaptım” metni olarak kalıyor; Telegram kısa görünüm bunu `⚠️` işaretiyle yine de son mesaj olarak gösteriyor. `status='ready'` bekleme koşulu karşılanmadığında da aynı sızıntı mümkün. Yanlış araç çağrısı metni de gerçek çağrı yapılmadığı hâlde sonuçta kalabiliyor.

## Tasarım

- Mevcut kanıt kapıları, izinler ve model istemi değişmez. Yeni model çağrısı yalnız mevcut tek kurtarma turunun gerektiği yerde olur.
- Host'un eylem, kaynak yazımı veya bekleme durumu kapısına girebilen görevlerde modelin `text_delta` ve `reasoning_delta` parçaları kullanıcı yüzeylerine aktarılmaz. Araç çağrılı turda model anlatımı gösterilmez, araç olayları akmaya devam eder. Araçsız finalde kabul edilen tamamlanmış model metni veya host'un güvenli `Doğrulanmadı: ...` karşılığı `text_delta` olarak **`model_finished` olayından önce** yayınlanır. Böylece UI, CLI ve Telegram'ın kısa/ayrıntılı akışında ret edilen bir “yaptım” iddiası hiç görünmez. Sağlayıcı yeniden denemesi `stream_reset` üretirse önceki deneme metni de yayınlanmaz.
- Kaynak yazımı, eylem kanıtı veya beklenen durum yoksa final metni host tarafından kısa bir `Doğrulanmadı: ...` sonucuna çevrilir; neden ve `success=False` birlikte korunur.
- Model gerçek `tool_calls` yerine çağrıyı düz metin olarak yazarsa korunan görevlerde son başarısızlık dalı da aynı doğrulanamama sonucunu döndürür. Bilgi sorularının canlı metin akışı mevcut şekilde sürer.
- `run_finished` ve kalıcı görev geçmişi yalnız kabul edilen veya açıkça doğrulanamamış sonucu taşır. Başarısız korunan görevde modelin doğrulanmamış `STATE` bloğu geçmişe yazılmaz; varsa yalnız gerçek araç çıktılarından çıkarılmış host gerçekleri kalır. Modelin kendi açık başarısızlık bildirimi host tarafından reddedilmediyse `text_delta` ile yayımlanır ve korunur. Kurtarma turunda model metni atılır, fakat `model_finished` istatistiği yine yayınlanır.
- Bu değişiklik yalnız host'un zaten kanıtsız bulduğu durumlarda çalışır. Araç başarısının hedefteki her alt maddeyi kanıtladığına dair yeni, geniş bir iddia üretmez.

## Kabul ölçütleri

- Başarısız dosya silme, kaynak yazma ve bekleme koşulu senaryolarında `outcome` kanıtsız “tamamlandı” metnini içermez.
- Reddedilen finalin `text_delta` parçaları hiçbir yüzeye iletilmez; başarılı eylem finali ve bilgi yanıtı görünür. Telegram ayrıntılı akışı da eski yanlış metni içermez.
- Tam test paketi, `compileall`, `git diff --check` temizdir.
- Temel canlı ölçüm en az 27/27 ve medyan süre önceki 3,5 sn düzeyindedir; otonom `self_repair` ve `stagnation` beklenen sonuçları korur.
