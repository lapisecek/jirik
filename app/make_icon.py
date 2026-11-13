"""Ikona Jiříka: z app/assets/jirik-1024.png (vykreslí ji app/make_icon.js) udělá jirik.png a jirik.ico."""
from pathlib import Path

from PIL import Image

OUT = Path(__file__).resolve().parent / "assets"

if __name__ == "__main__":
    src = Image.open(OUT / "jirik-1024.png").convert("RGBA")
    box = src.getbbox()                                   # ořez na postavičku + malý okraj (ikona ať je co největší)
    if box:
        side = max(box[2] - box[0], box[3] - box[1])
        pad = round(side * 0.04)
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        half = side / 2 + pad
        src = src.crop((round(cx - half), round(cy - half), round(cx + half), round(cy + half)))
    src.resize((512, 512), Image.LANCZOS).save(OUT / "jirik.png")
    src.resize((256, 256), Image.LANCZOS).save(OUT / "jirik.ico",
                                                sizes=[(s, s) for s in (16, 20, 24, 32, 40, 48, 64, 128, 256)])
    (OUT / "jirik-1024.png").unlink()
    print("ikona hotová:", OUT)
