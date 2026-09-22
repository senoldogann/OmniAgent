import asyncio
from tools import Toolbox

async def main():
    print("--- OmniAgent Sistem Testi Başlıyor ---")
    toolbox = Toolbox()
    
    try:
        print("\n[1] Süreç Listesi Testi:")
        procs = toolbox.process_list()
        print(procs[:500] + "...") # İlk 500 karakteri yazdır
        
        print("\n[2] Fare Konumu Testi:")
        pos = toolbox.get_pointer_position()
        print(pos)
        
        print("\n[3] Yetki Durumu Testi:")
        auth = toolbox.session_authority_status()
        print(auth)
        
        print("\n[4] Sistem Prob Testi (Network):")
        net = toolbox.deep_system_probe("network")
        print(net[:500] + "...")
        
        print("\n✅ Tüm yeni araçlar başarıyla doğrulandı.")
        
    except Exception as e:
        print(f"❌ Test sırasında hata oluştu: {e}")
    finally:
        await toolbox.close_browser()

if __name__ == "__main__":
    asyncio.run(main())
