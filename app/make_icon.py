"""Vytvoří ikonu Aiko (app/assets/aiko.ico + aiko.png): zářivá koule v barvách aplikace."""
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parent / "assets"
S = 1024


def lerp(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(len(a)))


def orb() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    # jemná záře kolem koule
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((90, 90, S - 90, S - 90), fill=(109, 132, 255, 150))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(48)))
    # koule: radiální přechod modrá → fialová, světlo vlevo nahoře
    r0, cx, cy = 400, S / 2, S / 2
    ball = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    px = ball.load()
    hx, hy = cx - 150, cy - 170
    for y in range(int(cy - r0), int(cy + r0) + 1):
        for x in range(int(cx - r0), int(cx + r0) + 1):
            d = math.hypot(x - cx, y - cy)
            if d > r0 + 1:
                continue
            a = 255 if d <= r0 else int(255 * (r0 + 1 - d))
            t = min(1.0, math.hypot(x - hx, y - hy) / (r0 * 1.55))
            base = lerp((150, 172, 255), (79, 107, 237), min(1, t * 1.25)) if t < 0.8 else \
                lerp((79, 107, 237), (124, 77, 230), (t - 0.8) / 0.2)
            px[x, y] = base + (a,)
    img.alpha_composite(ball)
    # vnitřní bílé „oko“ (jako v aplikaci) s jemným stínem
    eye = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(eye).ellipse((cx - 150, cy - 150, cx + 150, cy + 150), fill=(30, 20, 90, 90))
    img.alpha_composite(eye.filter(ImageFilter.GaussianBlur(26)))
    ImageDraw.Draw(img).ellipse((cx - 140, cy - 140, cx + 140, cy + 140), fill=(255, 255, 255, 255))
    # odlesk
    hl = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(hl).ellipse((cx - 300, cy - 330, cx - 90, cy - 200), fill=(255, 255, 255, 110))
    img.alpha_composite(hl.filter(ImageFilter.GaussianBlur(22)))
    return img


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    big = orb()
    big.resize((512, 512), Image.LANCZOS).save(OUT / "aiko.png")
    big.resize((256, 256), Image.LANCZOS).save(OUT / "aiko.ico", sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
    print("ikona hotová:", OUT)
