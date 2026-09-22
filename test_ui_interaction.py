import asyncio
import pyautogui
import time
from tools import Toolbox

async def main():
    toolbox = Toolbox()
    
    # 1. Ekran görüntüsü alarak pencereyi doğrula
    toolbox.take_screenshot('ui_state_1.png')
    print("Sistem durumu kaydedildi: ui_state_1.png")

    # 2. Uygulama penceresine odaklan (OmniAgent v1.0)
    toolbox.cua_get_app("OmniAgent")
    time.sleep(1)

    # 3. Giriş alanına tıklama (Tahmini koordinatlar veya AX üzerinden)
    # UI'da giriş alanı genellikle alt kısımdadır. 
    # Ancak daha güvenli olması için pyautogui ile merkez civarına tıklayıp yazalım
    #’Hedefinizi girin...’ alanı 450x600'lük pencerenin altında.
    # Pencere merkezine yakın bir yere tıklayalım (örnek: 225, 530)
    pyautogui.click(225, 530)
    time.sleep(0.5)

    # 4. Yeni eklediğimiz araçları test eden bir hedef gir
    goal = "Sistemdeki aktif süreçleri listele ve fare konumunu raporla"
    pyautogui.write(goal)
    pyautogui.press('enter')
    print(f"Hedef girildi: {goal}")

    # 5. Ajanın çalışması için bekle ve sonucu izle
    time.sleep(10)
    toolbox.take_screenshot('ui_state_2.png')
    print("İşlem sonrası durum kaydedildi: ui_state_2.png")

    await toolbox.close_browser()

if __name__ == "__main__":
    asyncio.run(main())
