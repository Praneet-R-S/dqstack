"""Draw the social sharing image and the touch icon for the public site.

    python site/tools/make_og.py --regular GeistMono-Regular.ttf --bold GeistMono-Bold.ttf

Writes site/assets/og.png (1200 x 630) and site/assets/apple-touch-icon.png
(180 x 180). Colours are the purple system from ui/css/tokens.css; the type is
Geist Mono, one of the rulebook's permitted faces. Pass the paths of the two
Geist Mono font files. Run it again whenever the hero line changes.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ASSETS = Path(__file__).resolve().parents[1] / "assets"

BG = (13, 10, 22)            # plum-975
TEXT = (240, 236, 250)       # plum-50
TEXT_2 = (176, 166, 201)     # plum-300
TEXT_3 = (149, 139, 176)     # plum-600
LINE = (38, 32, 57)          # plum-850
ACTUAL = (213, 207, 230)     # plum-100
DATA = (143, 134, 255)       # violet-400
DATA_2 = (180, 174, 255)     # violet-300
RISK = (240, 98, 77)         # red-400
PURPLE_500 = (168, 85, 247)  # the favicon's hue

HERO = ["Order what will sell,", "from stock you can trust"]
CAPTION = "Stock and ordering for Indian retailers"

# The same illustrative series as the Today screen on the landing page.
HISTORY = [5, 4, 5, 6, 7, 8, 7, 5, 4, 5, 6, 6, 9, 8, 6, 5, 5, 7, 7, 0, 0, 5, 6, 6, 7, 8, 6, 5]
MEDIAN = [5.2, 5.6, 6.0, 6.4, 6.8, 6.0, 5.0]


def mark(draw: ImageDraw.ImageDraw, x: float, y: float, size: float,
         rule: tuple, wedge: tuple, rule_width: float) -> None:
    """The DQ-Stack mark: an origin rule and two nested wedges, on a 16 unit grid."""
    s = size / 16

    def p(px: float, py: float) -> tuple[float, float]:
        return (x + px * s, y + py * s)

    draw.polygon([p(5.25, 8), p(14.75, 2.75), p(14.75, 13.25)], fill=wedge + (128,))
    draw.polygon([p(5.25, 8), p(14.75, 5.4), p(14.75, 10.6)], fill=wedge + (255,))
    draw.line([p(5.25, 1.75), p(5.25, 14.25)], fill=rule + (255,), width=round(rule_width))
    r = rule_width / 2
    for cy in (1.75, 14.25):
        cx, cyy = p(5.25, cy)
        draw.ellipse([cx - r, cyy - r, cx + r, cyy + r], fill=rule + (255,))


def gradient_text(img: Image.Image, xy: tuple[int, int], text: str,
                  font: ImageFont.FreeTypeFont) -> None:
    """Left to right #FFFFFF to #9B9B9B on the text only, as the hero heading has it."""
    left, top, right, bottom = font.getbbox(text)
    w, h = right, bottom
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).text((0, 0), text, font=font, fill=255)
    ramp = Image.new("RGBA", (w, h))
    px = ramp.load()
    for i in range(w):
        t = i / max(1, w - 1)
        v = round(255 + (155 - 255) * t)
        for j in range(h):
            px[i, j] = (v, v, v, 255)
    img.paste(ramp, xy, mask)


def og(regular: str, bold: str) -> None:
    k = 2                                  # draw at twice the size, then reduce
    W, H = 1200 * k, 630 * k
    img = Image.new("RGBA", (W, H), BG + (255,))
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)

    x0 = 80 * k
    mark(d, x0 - 6 * k, 66 * k, 44 * k, TEXT, DATA, 3.5 * k)
    d.text((x0 + 46 * k, 70 * k), "DQ-Stack", font=ImageFont.truetype(bold, 34 * k), fill=TEXT + (255,))

    img.alpha_composite(layer)
    hero_font = ImageFont.truetype(regular, 58 * k)
    gradient_text(img, (x0, 168 * k), HERO[0], hero_font)
    gradient_text(img, (x0, 240 * k), HERO[1], hero_font)

    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.text((x0, 334 * k), CAPTION, font=ImageFont.truetype(regular, 26 * k), fill=TEXT_2 + (255,))

    # ---- the forecast fan along the bottom: history, an out of stock gap,
    # the origin rule, and the range opening into the next seven days.
    cx0, cx1 = x0, 1120 * k
    cy_base, cy_top = 572 * k, 420 * k
    n = len(HISTORY) + len(MEDIAN)
    step = (cx1 - cx0) / (n - 1)
    umax = 13.0

    def X(i: float) -> float:
        return cx0 + i * step

    def Y(u: float) -> float:
        return cy_base - (u / umax) * (cy_base - cy_top)

    for u in (0, 5, 10):
        d.line([(cx0, Y(u)), (cx1, Y(u))], fill=LINE + (255,), width=k)

    # out of stock days 19 and 20: a hatched column
    hx0, hx1 = X(19) - step / 2, X(20) + step / 2
    hatch = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    hd = ImageDraw.Draw(hatch)
    hd.rectangle([hx0, cy_top, hx1, cy_base], fill=RISK + (24,))
    for off in range(-200 * k, 200 * k, 7 * k):
        hd.line([(hx0 + off, cy_base), (hx0 + off + (cy_base - cy_top), cy_top)], fill=RISK + (150,), width=2 * k)
    clip = Image.new("L", (W, H), 0)
    ImageDraw.Draw(clip).rectangle([hx0, cy_top, hx1, cy_base], fill=255)
    layer.paste(hatch, (0, 0), clip)

    last = len(HISTORY) - 1
    ox, oy = X(last), Y(HISTORY[last])

    def band(lo: float, hi: float, glo: float, ghi: float) -> list[tuple[float, float]]:
        upper = [(X(last + 1 + h), Y(m + hi + ghi * h)) for h, m in enumerate(MEDIAN)]
        lower = [(X(last + 1 + h), Y(max(0.2, m - lo - glo * h))) for h, m in enumerate(MEDIAN)]
        return [(ox, oy)] + upper + lower[::-1]

    fan = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    fd = ImageDraw.Draw(fan)
    fd.polygon(band(1.7, 2.1, 0.22, 0.38), fill=DATA + (60,))
    fan2 = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(fan2).polygon(band(0.8, 0.9, 0.10, 0.16), fill=DATA + (95,))
    layer.alpha_composite(fan)
    layer.alpha_composite(fan2)

    for yy in range(int(cy_top - 10 * k), int(cy_base), 8 * k):     # dashed origin rule
        d.line([(ox, yy), (ox, yy + 4 * k)], fill=TEXT_3 + (255,), width=k)
    pts = [(X(i), Y(u)) for i, u in enumerate(HISTORY)]
    d.line(pts, fill=ACTUAL + (255,), width=3 * k, joint="curve")
    med = [(ox, oy)] + [(X(last + 1 + h), Y(m)) for h, m in enumerate(MEDIAN)]
    d.line(med, fill=DATA_2 + (255,), width=3 * k, joint="curve")
    r = 6 * k
    d.ellipse([ox - r, oy - r, ox + r, oy + r], fill=ACTUAL + (255,), outline=BG + (255,), width=2 * k)

    img.alpha_composite(layer)
    img = img.convert("RGB").resize((1200, 630), Image.LANCZOS)
    img.save(ASSETS / "og.png", optimize=True)


def touch_icon() -> None:
    k = 4
    S = 180 * k
    img = Image.new("RGBA", (S, S), BG + (255,))
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    # The mark spans x 5.25 to 14.75 on its 16 unit grid, so it is shifted left
    # to sit optically centred.
    mark(ImageDraw.Draw(layer), 16 * k, 30 * k, 120 * k, PURPLE_500, PURPLE_500, 11 * k)
    img.alpha_composite(layer)
    img.convert("RGB").resize((180, 180), Image.LANCZOS).save(ASSETS / "apple-touch-icon.png", optimize=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--regular", required=True, help="path to GeistMono-Regular.ttf")
    ap.add_argument("--bold", required=True, help="path to GeistMono-Bold.ttf")
    args = ap.parse_args()
    og(args.regular, args.bold)
    touch_icon()
    print("wrote", ASSETS / "og.png", "and", ASSETS / "apple-touch-icon.png")
