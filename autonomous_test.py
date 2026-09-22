import asyncio
import pyautogui
import time
from tools import Toolbox

async def main():
    print("--- Tam Otonom Hibrit Vizyon Testi Başlıyor ---")
    toolbox = Toolbox()
    
    try:
        # 1. Pencere Sınırlarını Dinamik Olarak Belirle
        # UI'mız python3 süreci altında çalışıyor. 
        # get_window_bounds içinde 'python' içeren ilk süreci arıyoruz.
        print("\n[1] Pencere konumları belirleniyor...")
        bounds = toolbox.get_window_bounds("python") 
        x, y, w, h = bounds
        print(f"Pencere bulundu: x={x}, y={y}, w={w}, h={h}")

        # 2. Relatif Koordinat Hesaplama
        # ui.py layout: 
        # Entry alanı alt kısımda, yüksekliği 45px, genişliği neredeyse tam ekran.
        # Padding: padx=20, pady=(0, 20)
        # Giriş kutusunun merkezi yaklaşık olarak:
        # X = x + (w / 2)
        # Y = y + h - 50 (yaklaşık)
        input_x = x + (w // 2)
        input_y = y + h - 60 
        
        print(f"Giriş alanı hedefi hesaplandı: ({input_x}, {input_y})")

        # 3. Etkileşim
        print("\n[2] Etkileşim başlatılıyor...")
        pyautogui.click(input_x, input_y)
        time.sleep(0.5)
        
        goal = "Sistem süreçlerini listele ve ekran görüntüsü al"
        pyautogui.write(goal)
        pyautogui.press('enter')
        print(f"Hedef girildi: {goal}")

        # 4. Gözlem ve Doğrulama
        print("\n[3] Sonuçlar bekleniyor (15sn)...")
        time.sleep(15)
        
        result_file = "autonomous_result.png"
        toolbox.take_screenshot(result_file)
        print(f"Doğrulama ekran görüntüsü alındı: {result_file}")
        
        print("\n✅ Test başarıyla tamamlandı. Koordinatlar dinamik olarak hesaplandı ve hedef girildi.")

    except Exception as e:
        print(f"❌ Test sırasında hata oluştu: {e}")
    finally:
        await toolbox.close_browser()

if __name__ == "__main__":
    asyncio.run(main())
