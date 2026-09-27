"""OmniAgent macOS simgesini kaynak koddan tekrar üretir."""

from pathlib import Path
import sys

from PIL import Image, ImageDraw


def main() -> None:
    """1024 piksellik simgeyi ICNS biçiminde oluşturur."""
    if len(sys.argv) != 2:
        raise SystemExit("Kullanım: create_icon.py HEDEF.icns")
    target = Path(sys.argv[1])
    target.parent.mkdir(parents=True, exist_ok=True)
    size = 1024
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((48, 48, 976, 976), radius=214, fill="#1C1C1A")
    draw.rounded_rectangle((83, 83, 941, 941), radius=185, outline="#34332F", width=9)
    draw.ellipse((240, 240, 784, 784), outline="#D97757", width=104)
    draw.ellipse((439, 439, 585, 585), fill="#F6C4AE")
    image.save(target, format="ICNS")


if __name__ == "__main__":
    main()
