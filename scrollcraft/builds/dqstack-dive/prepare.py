"""Prepare the landing page's images from the supplied photographs in source/.

    python scrollcraft/builds/dqstack-dive/prepare.py

Writes site/assets/img/dive/. The city photograph is split into planes: a clean sky
plate (the city painted out), and the city cut out with an alpha edge, once as shot
(lights on) and once with its lights taken down (lights off). The page lights the
city by revealing one over the other.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

HERE = Path(__file__).resolve().parent
SRC = HERE / "source"
OUT = HERE.parents[2] / "site" / "assets" / "img" / "dive"
OUT.mkdir(parents=True, exist_ok=True)


def save(im: Image.Image, name: str, q: int = 82, max_w: int | None = None) -> None:
    if max_w and im.width > max_w:
        im = im.resize((max_w, round(im.height * max_w / im.width)), Image.LANCZOS)
    im.save(OUT / name, "WEBP", quality=q, method=6)
    print(f"{name:22s} {im.size} {(OUT / name).stat().st_size // 1024} KB")


def city_planes() -> None:
    im = Image.open(SRC / "2.jpg").convert("RGB")
    a = np.asarray(im).astype(np.float32)
    h, w, _ = a.shape

    # Sky: grown from the top edge through small colour steps (a smooth gradient);
    # building edges and lit windows stop it.
    sky = np.zeros((h, w), bool)
    q = deque((0, x) for x in range(w))
    for _, x in list(q):
        sky[0, x] = True
    while q:
        y, x = q.popleft()
        for dy, dx in ((1, 0), (0, 1), (0, -1), (-1, 0)):
            ny, nx = y + dy, x + dx
            if 0 <= ny < h and 0 <= nx < w and not sky[ny, nx] and ny < 330:
                if np.abs(a[ny, nx] - a[y, x]).max() < 7:
                    sky[ny, nx] = True
                    q.append((ny, nx))

    # City: whatever is not sky and is connected to the bottom edge (clouds are islands).
    city = np.zeros((h, w), bool)
    q = deque((h - 1, x) for x in range(w) if not sky[h - 1, x])
    for y, x in list(q):
        city[y, x] = True
    while q:
        y, x = q.popleft()
        for dy, dx in ((1, 0), (0, 1), (0, -1), (-1, 0)):
            ny, nx = y + dy, x + dx
            if 0 <= ny < h and 0 <= nx < w and not city[ny, nx] and not sky[ny, nx]:
                city[ny, nx] = True
                q.append((ny, nx))

    alpha = Image.fromarray((city * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.8))

    # Clean sky plate: each column's sky colour carried down under the skyline, softened.
    # Clean sky plate: masked pixels filled along each row from the sky either side (the
    # sky is a horizontal gradient); rows that are all city copy the row above.
    plate = a.copy()
    grown = np.asarray(Image.fromarray((city * 255).astype(np.uint8))
                       .filter(ImageFilter.MaxFilter(11))) > 0
    xs = np.arange(w)
    for y in range(h):
        m = grown[y]
        if not m.any():
            continue
        if m.all():
            plate[y] = plate[y - 1]
            continue
        for c in range(3):
            plate[y, m, c] = np.interp(xs[m], xs[~m], plate[y, ~m, c])
    plate_im = Image.fromarray(plate.clip(0, 255).astype(np.uint8))
    blurred = plate_im.filter(ImageFilter.GaussianBlur(14))
    soft = Image.fromarray((city * 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(9))
    plate_im = Image.composite(blurred, plate_im, soft)
    # Below the horizon the plate is never seen except at the very edge; darken it.
    save(plate_im, "city-sky.webp", q=80)

    # Lights off: bright, warm pixels (windows, signs, street light) taken right down;
    # the night blue of the buildings kept.
    lum = a.mean(2)
    warm = np.clip((a[..., 0] - a[..., 2]) / 80 + 0.5, 0, 1)
    k = np.clip((lum - 45) / 110, 0, 1) ** 0.8
    fall = (1 - k * (0.88 + 0.1 * warm)) * 0.72
    off = a * fall[..., None]
    off = off * np.array([0.78, 0.86, 1.1]) + np.array([2, 4, 11])
    off_im = Image.fromarray(off.clip(0, 255).astype(np.uint8))

    lit = im.copy()
    lit.putalpha(alpha)
    off_im.putalpha(alpha)
    save(im, "city.webp", q=82)        # the whole photograph, for the still page
    save(lit, "city-lit.webp", q=84)
    save(off_im, "city-off.webp", q=84)


def upscale_clean(im: Image.Image, factor: int = 2) -> Image.Image:
    """A small photo the page enlarges a lot: take out the JPEG blocks first (gentle
    non-local means), then enlarge with Lanczos and sharpen lightly, so the browser is
    not stretching 8x8 blocks. The layout keeps the photo's original coordinates."""
    from skimage.restoration import denoise_nl_means, estimate_sigma
    a = np.asarray(im.convert("RGB")).astype("float32") / 255
    sigma = float(np.mean(estimate_sigma(a, channel_axis=-1)))
    d = denoise_nl_means(a, h=0.8 * sigma, sigma=sigma, patch_size=5, patch_distance=6,
                         fast_mode=True, channel_axis=-1)
    clean = Image.fromarray((np.clip(d, 0, 1) * 255 + 0.5).astype("uint8"))
    big = clean.resize((im.width * factor, im.height * factor), Image.LANCZOS)
    return big.filter(ImageFilter.UnsharpMask(radius=1.6, percent=70, threshold=2))


def plain() -> None:
    # The aisle (5.jpg) is only 735 px wide and is shown up to about 5x: see below.
    save(upscale_clean(Image.open(SRC / "5.jpg")), "aisle.webp", q=84)
    names = {"1.webp": "street.webp", "3.webp": "front.webp",
             "6.webp": "front-diwali.webp", "7.webp": "shelf-gaps.webp",
             "8.webp": "shelf-over.webp", "9.webp": "label.webp", "10.webp": "people.webp"}
    for src, name in names.items():
        save(Image.open(SRC / src).convert("RGB"), name, q=82)
    # Mobile copies of the two widest scenes are not needed: every scene is cropped
    # by cover around a centred anchor, and the files are already under 1700 px.
    shutter = Image.open(SRC / "4.jpg").convert("RGB").crop((146, 96, 597, 350))
    save(shutter, "shutter.webp", q=86)
    # The closing scene and the social card reuse the open shopfront.


if __name__ == "__main__":
    city_planes()
    plain()
