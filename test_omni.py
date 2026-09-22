import asyncio

from main import run_agent


async def main() -> None:
    """Gerçek API üzerinden temel ajan akışını sınar."""
    print("🧪 OmniAgent temel akışı sınanıyor...")
    await run_agent("Çalışma dizinindeki test_omni.py dosyasının varlığını denetle.")
    print("\n✅ Ajan döngüsü tamamlandı.")

if __name__ == "__main__":
    asyncio.run(main())
