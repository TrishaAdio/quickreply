"""Cinematic ".graph" card (Pillow only).

Composition (2400x1350):
  * Deep black stage with volumetric light beams, bokeh, light leaks and a
    faint perspective floor.
  * Hero numbers in a wide display face (Unbounded Black) with chrome
    gradient fill and multi-pass bloom.
  * Backdrop-blurred glass panel for the peaks.
  * "Light pillar" visualiser: DMs per hour rise above a horizon strip,
    INR per hour hangs below it; heat-mapped colour, neon cores, bloom and
    anamorphic lens flares on both peaks.
  * Post: threshold bloom, chromatic aberration, vignette and film grain.

Fonts are bundled (SIL OFL, see ``qr/assets/fonts``) so the card and the
rupee sign look identical on Windows and Linux.
"""
from __future__ import annotations

import io
import math
import random
from datetime import datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from qr.config.settings import IST
from qr.services.dm_stats import WINDOW_HOURS, DayStats
from qr.services.payments import fmt_inr

FONT_DIR = Path(__file__).resolve().parents[1] / "assets" / "fonts"
FONT_FILES = {"display": "Unbounded[wght].ttf", "text": "SpaceGrotesk[wght].ttf"}
W, H = 2400, 1350
X0, X1 = 150, 2250

BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
INK = (232, 230, 255)
MUTED = (138, 130, 190)
CYAN = (40, 225, 255)
BLUE = (60, 100, 255)
MAGENTA = (255, 50, 190)
HOT = (255, 170, 230)
GOLD = (255, 196, 80)
AMBER = (255, 120, 40)
GREEN = (60, 255, 170)
VIOLET = (130, 80, 255)


# ---------------------------------------------------------------------------
# primitives
# ---------------------------------------------------------------------------
@lru_cache(maxsize=64)
def font(kind: str, weight: int, size: int) -> ImageFont.FreeTypeFont:
    try:
        fnt = ImageFont.truetype(str(FONT_DIR / FONT_FILES[kind]), size)
    except (OSError, KeyError):
        return ImageFont.load_default(size)
    try:
        fnt.set_variation_by_axes([weight])
    except Exception:  # noqa: BLE001 - FreeType without variation support
        pass
    return fnt


def mix(a, b, t: float):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def scale(color, k: float):
    return tuple(max(0, min(255, int(c * k))) for c in color[:3])


def soft_blur(img: Image.Image, radius: float) -> Image.Image:
    """Large Gaussian blur done at reduced resolution (fast, smooth bloom)."""
    if radius < 6:
        return img.filter(ImageFilter.GaussianBlur(radius))
    factor = 4 if radius >= 24 else 2
    small = img.resize((max(1, img.width // factor), max(1, img.height // factor)), Image.BILINEAR)
    return small.filter(ImageFilter.GaussianBlur(radius / factor)).resize(img.size, Image.BILINEAR)


def dim(layer: Image.Image, strength: float) -> Image.Image:
    return layer if strength == 1 else layer.point(lambda v: int(min(255, v * strength)))


def screen_at(img: Image.Image, layer: Image.Image, xy=(0, 0), strength: float = 1.0) -> None:
    """Additive-style (screen) blend of an RGB light layer, clipped to ``img``."""
    x, y = int(xy[0]), int(xy[1])
    left, top = max(0, -x), max(0, -y)
    right, bottom = min(layer.width, img.width - x), min(layer.height, img.height - y)
    if right <= left or bottom <= top:
        return
    piece = dim(layer.crop((left, top, right, bottom)), strength)
    box = (x + left, y + top, x + right, y + bottom)
    img.paste(ImageChops.screen(img.crop(box), piece), box[:2])


def vgradient(size, stops) -> Image.Image:
    """Vertical gradient through ``[(t, color), ...]`` stops."""
    w, h = max(1, int(size[0])), max(1, int(size[1]))
    column = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / max(1, h - 1)
        for (t0, c0), (t1, c1) in zip(stops, stops[1:], strict=False):
            if t0 <= t <= t1:
                column.putpixel((0, y), mix(c0, c1, (t - t0) / max(1e-6, t1 - t0)))
                break
    return column.resize((w, h))


def hgradient(size, stops) -> Image.Image:
    return vgradient((size[1], size[0]), stops).transpose(Image.ROTATE_90).transpose(Image.FLIP_TOP_BOTTOM)


def text_mask(text: str, fnt, tracking: float = 0) -> Image.Image:
    """Tight (cropped) L mask of ``text`` with optional letter spacing."""
    ascent, descent = fnt.getmetrics()
    if tracking:
        width = sum(fnt.getlength(ch) for ch in text) + tracking * max(0, len(text) - 1)
    else:
        width = fnt.getlength(text)
    mask = Image.new("L", (int(width) + 20, ascent + descent + 20), 0)
    draw = ImageDraw.Draw(mask)
    if tracking:
        x = 10.0
        for ch in text:
            draw.text((x, 10), ch, font=fnt, fill=255)
            x += fnt.getlength(ch) + tracking
    else:
        draw.text((10, 10), text, font=fnt, fill=255)
    box = mask.getbbox()
    return mask.crop(box) if box else Image.new("L", (1, 1), 0)


def place(mask: Image.Image, xy, anchor: str = "lt") -> tuple[int, int]:
    x, y = xy
    if anchor[0] == "m":
        x -= mask.width / 2
    elif anchor[0] == "r":
        x -= mask.width
    if anchor[1] == "m":
        y -= mask.height / 2
    elif anchor[1] == "b":
        y -= mask.height
    return int(x), int(y)


def neon_text(img, xy, text, fnt, *, fill, glow, tracking=0, anchor="lt", radii=((10, 0.9), (34, 0.55)),
              core_glow=True, outline=0) -> tuple[int, int, int, int]:
    """Text with gradient/solid fill and layered bloom. Returns its box."""
    mask = text_mask(text, fnt, tracking)
    x, y = place(mask, xy, anchor)
    if outline:
        img.paste((6, 4, 16), (x, y), mask.filter(ImageFilter.MaxFilter(outline * 2 + 1)))
    if glow is not None and radii:
        pad = int(max(r for r, _ in radii) * 3)
        light = Image.new("RGB", (mask.width + pad * 2, mask.height + pad * 2), BLACK)
        light.paste(glow, (pad, pad), mask)
        for radius, strength in radii:
            screen_at(img, soft_blur(light, radius), (x - pad, y - pad), strength)
    if isinstance(fill, Image.Image):
        img.paste(fill.resize(mask.size), (x, y), mask)
    else:
        img.paste(fill, (x, y), mask)
    if core_glow and glow is not None:
        sheen = Image.new("RGB", mask.size, BLACK)
        sheen.paste(mix(glow, WHITE, 0.5), (0, 0), mask)
        screen_at(img, soft_blur(sheen, 2), (x, y), 0.35)
    return x, y, x + mask.width, y + mask.height


def label(img, xy, text, size=22, color=MUTED, tracking=6, weight=500, anchor="lt", outline=0):
    mask = text_mask(text.upper(), font("text", weight, size), tracking)
    x, y = place(mask, xy, anchor)
    if outline:
        halo = mask.filter(ImageFilter.MaxFilter(outline * 2 + 1))
        img.paste((6, 4, 16), (x, y), halo)
    img.paste(color, (x, y), mask)
    return x, y, x + mask.width, y + mask.height


def glass(img: Image.Image, box, radius=34, tint=(150, 140, 255), border=0.55) -> None:
    """Frosted glass: blurred backdrop, light tint, specular top edge."""
    x0, y0, x1, y1 = (int(v) for v in box)
    w, h = x1 - x0, y1 - y0
    region = soft_blur(img.crop((x0, y0, x1, y1)), 28)
    region = ImageEnhance.Brightness(region).enhance(1.35)
    region = Image.blend(region, Image.new("RGB", (w, h), mix(tint, BLACK, 0.55)), 0.18)
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius, fill=255)
    img.paste(region, (x0, y0), mask)
    edge = Image.new("L", (w, h), 0)
    ImageDraw.Draw(edge).rounded_rectangle((0, 0, w - 1, h - 1), radius, outline=255, width=2)
    fade = vgradient((w, h), [(0, (255, 255, 255)), (0.5, (70, 70, 70)), (1, (120, 120, 120))]).convert("L")
    edge = ImageChops.multiply(edge, fade).point(lambda v: int(v * border))
    img.paste(WHITE, (x0, y0), edge)


def flare(img, center, color, *, length=900, intensity=1.0) -> None:
    """Anamorphic lens flare: horizontal streak + star + hot core."""
    cx, cy = center
    half_w, half_h = int(length * 0.6), 220
    layer = Image.new("RGB", (half_w * 2, half_h * 2), BLACK)
    draw = ImageDraw.Draw(layer)
    c = (half_w, half_h)
    draw.ellipse((c[0] - length / 2, c[1] - 3, c[0] + length / 2, c[1] + 3), fill=mix(color, WHITE, 0.4))
    draw.ellipse((c[0] - length / 3, c[1] - 1, c[0] + length / 3, c[1] + 1), fill=WHITE)
    draw.line((c[0], c[1] - 110, c[0], c[1] + 110), fill=scale(color, 0.8), width=2)
    for angle in (35, -35):
        dx, dy = math.cos(math.radians(angle)) * 70, math.sin(math.radians(angle)) * 70
        draw.line((c[0] - dx, c[1] - dy, c[0] + dx, c[1] + dy), fill=scale(color, 0.5), width=2)
    draw.ellipse((c[0] - 16, c[1] - 16, c[0] + 16, c[1] + 16), fill=WHITE)
    tight = soft_blur(layer, 3)
    halo = Image.new("RGB", layer.size, BLACK)
    ImageDraw.Draw(halo).ellipse((c[0] - 70, c[1] - 70, c[0] + 70, c[1] + 70), fill=color)
    combined = ImageChops.screen(tight, dim(soft_blur(layer, 16), 1.4))
    combined = ImageChops.screen(combined, dim(soft_blur(halo, 40), 0.9))
    screen_at(img, combined, (cx - half_w, cy - half_h), intensity)


def compact_inr(value) -> str:
    """Indian compact money: 950, 1.2k, 45k, 1.5L, 2.1Cr."""
    value = float(value)
    for limit, suffix in ((1e7, "Cr"), (1e5, "L"), (1e3, "k")):
        if value >= limit:
            number = value / limit
            text = f"{number:.1f}".rstrip("0").rstrip(".") if number < 100 else f"{number:.0f}"
            return f"\u20b9{text}{suffix}"
    return f"\u20b9{value:.0f}"


def h12(hour: int, suffix: bool = True) -> str:
    hour %= 24
    text = str(hour % 12 or 12)
    return f"{text} {'AM' if hour < 12 else 'PM'}" if suffix else text


def range12(start: int, end: int) -> str:
    same = (start % 24 < 12) == (end % 24 < 12) and end % 24 != 0
    return f"{h12(start, suffix=not same)}\u2013{h12(end)}"


def heat(t: float):
    """Low -> high intensity palette: deep blue, cyan, magenta, hot pink."""
    stops = [(0.0, BLUE), (0.35, CYAN), (0.75, MAGENTA), (1.0, HOT)]
    for (t0, c0), (t1, c1) in zip(stops, stops[1:], strict=False):
        if t <= t1:
            return mix(c0, c1, (t - t0) / (t1 - t0))
    return HOT


def gold_heat(t: float):
    return mix(AMBER, (255, 225, 140), t)


# ---------------------------------------------------------------------------
# scene
# ---------------------------------------------------------------------------
def _stage(seed: int) -> Image.Image:
    rng = random.Random(seed)
    img = vgradient((W, H), [(0, (4, 3, 12)), (0.55, (9, 5, 22)), (1, (6, 3, 16))])
    beams = Image.new("RGB", (W, H), BLACK)
    draw = ImageDraw.Draw(beams)
    for top_x, spread, color in ((380, 520, CYAN), (1250, 640, VIOLET), (2100, 560, MAGENTA)):
        draw.polygon([(top_x - 40, -50), (top_x + 40, -50), (top_x + spread, H), (top_x - spread, H)],
                     fill=scale(color, 0.55))
    screen_at(img, soft_blur(beams, 120), strength=0.2)
    leaks = Image.new("RGB", (W, H), BLACK)
    draw = ImageDraw.Draw(leaks)
    for cx, cy, r, color in ((2350, -40, 520, AMBER), (-60, 1380, 620, MAGENTA), (1200, 960, 900, VIOLET)):
        draw.ellipse((cx - r, cy - r * 0.6, cx + r, cy + r * 0.6), fill=color)
    screen_at(img, soft_blur(leaks, 200), strength=0.14)
    bokeh = Image.new("RGB", (W, H), BLACK)
    draw = ImageDraw.Draw(bokeh)
    placed = 0
    while placed < 26:
        r = rng.choice((3, 4, 6, 8, 12, 18, 26))
        x, y = rng.uniform(0, W), rng.uniform(0, H)
        # keep bokeh in the margins / gaps, never behind numbers or labels
        if 60 < y < 520 and X0 - 40 < x < X1 + 40 or 560 < y < 1230 and X0 - 40 < x < X1 + 40:
            continue
        color = scale(rng.choice((CYAN, MAGENTA, VIOLET, GOLD, BLUE)), rng.uniform(0.2, 0.5))
        draw.ellipse((x - r, y - r, x + r, y + r), fill=color)
        placed += 1
    screen_at(img, soft_blur(bokeh, 2.5), strength=0.7)
    return img


def _floor(img, horizon: int) -> None:
    grid = Image.new("RGB", (W, H - horizon), BLACK)
    draw = ImageDraw.Draw(grid)
    vx = W / 2
    for i in range(-22, 23):
        draw.line((vx, 0, vx + i * 260, H - horizon), fill=(90, 70, 190), width=1)
    y, step = 8.0, 10.0
    while y < H - horizon:
        draw.line((0, y, W, y), fill=(90, 70, 190), width=1)
        y += step
        step *= 1.32
    fade = vgradient(grid.size, [(0, (0, 0, 0)), (0.25, (255, 255, 255)), (1, (90, 90, 90))]).convert("L")
    grid = Image.composite(grid, Image.new("RGB", grid.size, BLACK), fade)
    screen_at(img, grid, (0, horizon), 0.28)
    glow = Image.new("RGB", (W, 400), BLACK)
    ImageDraw.Draw(glow).ellipse((X0 - 200, 160, X1 + 200, 240), fill=(120, 70, 255))
    screen_at(img, soft_blur(glow, 60), (0, horizon - 200), 0.55)


def _header(img, stats: DayStats, generated: datetime) -> None:
    label(img, (X0, 92), "QuickReply  \u2014  DM intelligence", 24, scale(CYAN, 0.9), tracking=9, weight=600)
    day = datetime.fromisoformat(stats.date)
    neon_text(img, (X0, 134), f"{day:%A}  \u00b7  {day:%d %B %Y}".upper(), font("display", 500, 34),
              fill=INK, glow=VIOLET, tracking=4, radii=((14, 0.5),), core_glow=False)
    if stats.test:
        text, color = "TEST DATA", AMBER
    elif stats.now_hour is not None:
        text, color = f"LIVE   {generated.astimezone(IST):%H:%M} IST", GREEN
    else:
        text, color = "FULL DAY", VIOLET
    mask = text_mask(text, font("text", 700, 24), 5)
    x1 = X1
    x0 = x1 - mask.width - 104
    glass(img, (x0, 96, x1, 158), radius=31, tint=color, border=0.7)
    dot = Image.new("RGB", (120, 120), BLACK)
    ImageDraw.Draw(dot).ellipse((50, 50, 70, 70), fill=color)
    screen_at(img, soft_blur(dot, 10), (x0 - 18, 67), 1.4)
    screen_at(img, dot, (x0 - 18, 67))
    img.paste(INK, (x0 + 66, 127 - mask.height // 2), mask)


def _hero(img, stats: DayStats) -> None:
    dms = f"{stats.total_dms:,}"
    chrome_cyan = vgradient((10, 200), [(0, WHITE), (0.45, (170, 245, 255)), (1, (40, 140, 255))])
    box = neon_text(img, (X0 - 6, 222), dms, font("display", 900, 196), fill=chrome_cyan, glow=CYAN,
                    radii=((14, 0.85), (46, 0.6), (120, 0.35)))
    digit_bottom = box[1] + text_mask("8", font("display", 900, 196)).height
    tag = label(img, (X0, 452), "DMs today", 24, INK, tracking=7, weight=600)
    label(img, (tag[2] + 26, 452), f"{stats.unique_senders:,} people", 24, MUTED, tracking=5)

    divider_x = max(box[2] + 70, 640)
    line = Image.new("RGB", (40, 300), BLACK)
    line.paste(vgradient((2, 300), [(0, BLACK), (0.5, (190, 150, 255)), (1, BLACK)]), (19, 0))
    screen_at(img, soft_blur(line, 8), (divider_x - 20, 200), 1.0)
    screen_at(img, line, (divider_x - 20, 200))

    money = fmt_inr(stats.total_inr) if stats.total_payments else "\u20b90"
    gold_chrome = vgradient((10, 200), [(0, (255, 250, 230)), (0.45, (255, 214, 120)), (1, (255, 120, 40))])
    left = divider_x + 70
    size = 150
    while size > 80 and font("display", 900, size).getlength(money) > 1560 - left:
        size -= 6
    money_font = font("display", 900, size)
    top = digit_bottom - text_mask("8", money_font).height     # same baseline as the DM count
    neon_text(img, (left, top), money, money_font, fill=gold_chrome, glow=AMBER,
              radii=((12, 0.8), (40, 0.55), (110, 0.3)))
    tag = label(img, (left + 4, 452), "Income", 24, INK, tracking=7, weight=600)
    label(img, (tag[2] + 26, 452), f"{stats.total_payments} payment{'s' if stats.total_payments != 1 else ''}",
          24, MUTED, tracking=5)


def _peaks_panel(img, stats: DayStats) -> None:
    box = (1620, 206, X1, 500)
    glass(img, box, radius=36)
    peak, peak_inr, window = stats.peak_dm_hour, stats.peak_inr_hour, stats.busiest_window()
    rows = [
        ("Peak DM hour", h12(peak) if peak is not None else "",
         f"{stats.dms[peak]} DMs" if peak is not None else "no DMs yet", MAGENTA),
        ("Peak income hour", h12(peak_inr) if peak_inr is not None else "",
         compact_inr(stats.inr[peak_inr]) if peak_inr is not None else "no payments", GOLD),
        (f"Busiest {WINDOW_HOURS} hours",
         range12(window[0], window[0] + WINDOW_HOURS) if window else "",
         f"{window[1]} DMs" if window else "no DMs yet", CYAN),
    ]
    for index, (title, value, detail, color) in enumerate(rows):
        y = box[1] + 34 + index * 88
        label(img, (box[0] + 40, y + 4), title, 19, MUTED, tracking=5)
        if value:
            neon_text(img, (box[0] + 40, y + 30), value, font("display", 800, 38), fill=mix(color, WHITE, 0.35),
                      glow=color, radii=((10, 0.7), (26, 0.4)))
        label(img, (box[2] - 40, y + 38), detail, 24, mix(color, WHITE, 0.25), tracking=3, weight=600,
              anchor="rt")
        if index < len(rows) - 1:
            sep = hgradient((box[2] - box[0] - 80, 1), [(0, BLACK), (0.5, (110, 100, 170)), (1, BLACK)])
            screen_at(img, sep, (box[0] + 40, y + 82))


def _visualiser(img, stats: DayStats) -> None:
    horizon = 975
    dm_base, inr_base = horizon - 26, horizon + 26
    dm_room, inr_room = 300, 150
    slot = (X1 - X0) / 24
    future_from = (stats.now_hour + 1) if stats.now_hour is not None else 24
    peak, peak_inr = stats.peak_dm_hour, stats.peak_inr_hour
    dm_max = max(stats.dms) or 1
    inr_values = [float(v) for v in stats.inr]
    inr_max = max(inr_values) or 1.0

    _floor(img, horizon)
    tri = ImageDraw.Draw(img)
    tri.polygon([(X0, 594), (X0 + 9, 578), (X0 + 18, 594)], fill=CYAN)
    label(img, (X0 + 34, 576), "DMs per hour", 21, scale(CYAN, 0.95), tracking=6, weight=600)
    tri.polygon([(X0, 1200), (X0 + 9, 1216), (X0 + 18, 1200)], fill=GOLD)
    label(img, (X0 + 34, 1199), "Income per hour", 21, scale(GOLD, 0.95), tracking=6, weight=600)

    glow_layer = Image.new("RGB", (W, H), BLACK)
    gdraw = ImageDraw.Draw(glow_layer)
    core_layer = Image.new("RGB", (W, H), BLACK)
    cdraw = ImageDraw.Draw(core_layer)
    ghost = Image.new("L", (W, H), 0)
    ghdraw = ImageDraw.Draw(ghost)
    bodies = []
    value_labels = []

    for hour in range(24):
        cx = X0 + slot * hour + slot / 2
        if hour >= future_from:
            ghdraw.rounded_rectangle((cx - 9, dm_base - 16, cx + 9, dm_base), 9, outline=70, width=2)
            continue
        value = stats.dms[hour]
        if value:
            t = value / dm_max
            color = mix(heat(t), WHITE, 0.25) if hour == peak else heat(t)
            height = max(18, dm_room * t)
            width = 34 if hour == peak else 28
            rect = (cx - width / 2, dm_base - height, cx + width / 2, dm_base)
            gdraw.rounded_rectangle(rect, width / 2, fill=color)
            cdraw.rounded_rectangle((cx - 4, rect[1] + 6, cx + 4, dm_base - 4), 4, fill=mix(color, WHITE, 0.75))
            bodies.append((rect, [(0, mix(color, WHITE, 0.55)), (0.35, color), (1, scale(color, 0.55))]))
            value_labels.append((cx, rect[1] - 18, str(value), hour == peak, color))
        else:
            gdraw.ellipse((cx - 4, dm_base - 8, cx + 4, dm_base), fill=(70, 70, 130))
        amount = inr_values[hour]
        if amount:
            t = amount / inr_max
            color = gold_heat(t)
            height = max(14, inr_room * t)
            width = 26 if hour == peak_inr else 20
            rect = (cx - width / 2, inr_base, cx + width / 2, inr_base + height)
            gdraw.rounded_rectangle(rect, width / 2, fill=color)
            cdraw.rounded_rectangle((cx - 3, inr_base + 4, cx + 3, rect[3] - 5), 3, fill=mix(color, WHITE, 0.7))
            bodies.append((rect, [(0, mix(color, WHITE, 0.5)), (0.5, color), (1, scale(color, 0.35))]))

    # horizon strip
    strip = hgradient((X1 - X0, 3), [(0, BLACK), (0.15, CYAN), (0.5, MAGENTA), (0.85, GOLD), (1, BLACK)])
    for y in (dm_base + 6, inr_base - 8):
        screen_at(img, soft_blur(strip.resize((X1 - X0, 2)), 1), (X0, y), 0.45)

    img.paste((44, 40, 90), (0, 0), ghost)
    has_data = bool(stats.total_dms or stats.total_payments)
    if stats.now_hour is not None and has_data:   # drawn first so pillars and labels sit on top
        x = X0 + slot * (stats.now_hour + stats.now_minute / 60)
        marker = Image.new("RGB", (40, 520), BLACK)
        mdraw = ImageDraw.Draw(marker)
        for y in range(0, 520, 16):
            mdraw.line((20, y, 20, y + 8), fill=GREEN, width=2)
        screen_at(img, soft_blur(marker, 6), (x - 20, 660), 0.9)
        screen_at(img, marker, (x - 20, 660), 0.55)
    screen_at(img, soft_blur(glow_layer, 70), strength=0.55)
    screen_at(img, soft_blur(glow_layer, 22), strength=0.95)
    for rect, stops in bodies:
        x0, y0, x1, y1 = (int(round(v)) for v in rect)
        w, h = max(2, x1 - x0), max(2, y1 - y0)
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), w // 2, fill=255)
        img.paste(vgradient((w, h), stops), (x0, y0), mask)
    screen_at(img, soft_blur(core_layer, 3), strength=1.0)
    screen_at(img, core_layer, strength=0.8)

    # hour timeline in the horizon strip
    for hour in range(24):
        cx = X0 + slot * hour + slot / 2
        if hour == stats.now_hour:
            pill = Image.new("RGB", (160, 90), BLACK)
            ImageDraw.Draw(pill).rounded_rectangle((40, 30, 120, 60), 15, outline=GREEN, width=2)
            screen_at(img, soft_blur(pill, 7), (cx - 80, horizon - 45), 1.1)
            screen_at(img, pill, (cx - 80, horizon - 45), 0.9)
            img.paste((8, 22, 18), (int(cx - 38), horizon - 13), Image.new("L", (76, 26), 200))
            label(img, (cx, horizon), "NOW", 17, mix(GREEN, WHITE, 0.4), tracking=3, weight=700, anchor="mm")
            continue
        if hour == peak:
            color, weight = mix(MAGENTA, WHITE, 0.45), 700
        elif hour == peak_inr:
            color, weight = mix(GOLD, WHITE, 0.3), 700
        elif hour >= future_from:
            color, weight = (70, 66, 110), 500
        else:
            color, weight = (150, 144, 200), 500
        label(img, (cx, horizon), f"{hour:02d}", 19, color, tracking=2, weight=weight, anchor="mm")

    # peaks: flares + callouts
    if peak is not None and peak < future_from:
        cx = X0 + slot * peak + slot / 2
        top = dm_base - max(18, dm_room * stats.dms[peak] / dm_max)
        flare(img, (cx, top), mix(CYAN, MAGENTA, 0.35), length=640, intensity=0.85)
        text = f"PEAK  {h12(peak)}  \u00b7  {stats.dms[peak]} DMS"
        mask = text_mask(text, font("text", 700, 26), 4)
        lx = min(max(cx, X0 + mask.width / 2), X1 - mask.width / 2)
        neon_text(img, (lx, top - 34), text, font("text", 700, 26), fill=WHITE, glow=MAGENTA, tracking=4,
                  anchor="mb", radii=((10, 0.9), (28, 0.5)), outline=3)
    if peak_inr is not None and peak_inr < future_from:
        cx = X0 + slot * peak_inr + slot / 2
        bottom = inr_base + max(14, inr_room * inr_values[peak_inr] / inr_max)
        flare(img, (cx, bottom), GOLD, length=420, intensity=0.7)
        text = f"{fmt_inr(stats.inr[peak_inr])}  \u00b7  {h12(peak_inr)}"
        mask = text_mask(text, font("text", 700, 24), 3)
        lx = min(max(cx, X0 + mask.width / 2), X1 - mask.width / 2)
        neon_text(img, (lx, bottom + 30), text, font("text", 700, 24), fill=(255, 244, 214), glow=AMBER,
                  tracking=3, anchor="mt", radii=((9, 0.9), (24, 0.5)), outline=3)

    # value labels
    for cx, y, text, is_peak, color in value_labels:
        if not is_peak:
            label(img, (cx, y), text, 19, mix(color, WHITE, 0.65), tracking=1, weight=600, anchor="mb", outline=2)
    for hour in range(min(24, future_from)):
        amount = inr_values[hour]
        if amount and hour != peak_inr:
            cx = X0 + slot * hour + slot / 2
            bottom = inr_base + max(14, inr_room * amount / inr_max)
            label(img, (cx, bottom + 10), compact_inr(amount), 18, mix(GOLD, WHITE, 0.25), tracking=0,
                  weight=600, anchor="mt", outline=2)

    if not stats.total_dms and not stats.total_payments:
        neon_text(img, (W / 2, 800), "WAITING FOR DMS", font("display", 800, 64), fill=WHITE, glow=VIOLET,
                  tracking=6, anchor="mm", radii=((16, 0.9), (50, 0.6)))
        label(img, (W / 2, 870), "Counting live from now  \u00b7  .graph scan backfills today", 24, MUTED,
              tracking=4, anchor="mm")


def _footer(img, stats: DayStats, generated: datetime, account: str) -> None:
    y = 1272
    parts = []
    if account:
        parts.append(account)
    conversion = stats.conversion()
    if conversion is not None:
        parts.append(f"{conversion * 100:.1f}% of senders paid")
    if stats.total_dms:
        parts.append(f"max {max(stats.senders)} people / hour")
    if stats.tracking_since and stats.tracking_since.astimezone(IST).date().isoformat() == stats.date:
        parts.append(f"tracking since {stats.tracking_since.astimezone(IST):%H:%M}")
    parts.append(f"{generated.astimezone(IST):%d %b %H:%M} IST")
    label(img, (X0, y), "   \u00b7   ".join(parts), 21, (170, 164, 215), tracking=3)
    neon_text(img, (X1, y - 4), "QUICKREPLY", font("display", 800, 24), fill=INK, glow=CYAN, tracking=6,
              anchor="rt", radii=((8, 0.8), (22, 0.4)), core_glow=False)


def _post(img: Image.Image, seed: int) -> Image.Image:
    bright = img.point(lambda v: 0 if v < 190 else int((v - 190) * 3.9))
    img = ImageChops.screen(img, dim(soft_blur(bright, 26), 0.5))
    img = ImageChops.screen(img, dim(soft_blur(bright, 90), 0.22))
    # contrast: crush the purple haze back toward black
    img = img.point(lambda v: int(255 * ((v / 255) ** 1.12)))
    red, green, blue = img.split()
    fringe = Image.merge("RGB", (ImageChops.offset(red, 2, 0), green, ImageChops.offset(blue, -2, 0)))
    centre = Image.new("L", (W, H), 0)
    ImageDraw.Draw(centre).ellipse((-W * 0.1, -H * 0.12, W * 1.1, H * 1.12), fill=255)
    img = Image.composite(img, fringe, soft_blur(centre, 160))
    vignette = Image.new("L", (W, H), 0)
    ImageDraw.Draw(vignette).ellipse((-W * 0.12, -H * 0.2, W * 1.12, H * 1.2), fill=255)
    vignette = soft_blur(vignette, 220).point(lambda v: int(90 + v * 165 / 255))
    img = ImageChops.multiply(img, Image.merge("RGB", (vignette, vignette, vignette)))
    random.seed(seed)
    grain = Image.effect_noise((W, H), 6).convert("RGB")
    return ImageChops.add(img, grain, 1.0, -128)


def render(stats: DayStats, *, generated: datetime | None = None, account: str = "",
           fmt: str = "JPEG") -> bytes:
    """Render the card; returns JPEG (default) or PNG bytes."""
    from qr.services.clock import utcnow

    generated = generated or utcnow()
    seed = int(stats.date.replace("-", ""))
    img = _stage(seed)
    _header(img, stats, generated)
    _hero(img, stats)
    _peaks_panel(img, stats)
    _visualiser(img, stats)
    _footer(img, stats, generated, account)
    img = _post(img, seed)
    out = io.BytesIO()
    if fmt.upper() == "PNG":
        img.save(out, "PNG", optimize=True)
    else:
        img.save(out, "JPEG", quality=93, subsampling=0, optimize=True)
    return out.getvalue()


__all__ = ["render", "compact_inr", "font", "h12", "range12", "W", "H", "Decimal"]
