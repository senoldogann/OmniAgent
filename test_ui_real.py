import asyncio
import pyautogui
import time
from tools import Toolbox

async def main():
    toolbox = Toolbox()
    
    # 1. Uygulama penceresine odaklan
    try:
        toolbox.cua_get_app("OmniAgent v1.0")
        print("Uygulamaya başarıyla odaklanıldı.")
    except Exception as e:
        print(f"Uygulamaya odaklanılamadı: {e}")
        # Eğer odaklanılamazsa, pencere ismini arayalım veya koordinatla deneyelim
        return

    time.sleep(1)

    # 2. Giriş alanına tıkla
    # UI boyutları 450x600. Giriş alanı en altta.
    # Koordinatlar ekran çözünürlüğüne göre değişir, ancak uygulama 'topmost'
    # olduğu için pencere konumunu bulmak daha iyi olur.
    # Şimdilik ekranın orta-alt kısmına tıklayalım.
    pyautogui.click(x=500, y=800) # Varsayımsal konum
    time.sleep(0.5)

    # 3. Hedef gir
    goal = "Süreçleri listele ve fare konumunu söyle"
    pyautogui.write(goal)
    pyautogui.press('enter')
    print(f"Hedef gönderildi: {goal}")

    # 4. İzle
    time.sleep(15)
    toolbox.take_screenshot('result.png')
    print("Sonuç ekran görüntüsü alındı: result.png")

    await toolbox.close_browser()

if __name__ == "__main__":
    asyncio.run(main())
