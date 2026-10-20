"""Neon "glow card" renderer for ``.graph`` (Pillow only, no network).

Look: dark cinematic gradient, blurred colour blooms, glass cards with glowing
edges, glowing title text, gradient bars with bloom for DMs per hour, a gold
glowing spline + area for income per hour, film grain and vignette.

The bundled Poppins font (SIL OFL, see ``qr/assets/fonts/OFL.txt``) is used so
the card looks the same on Windows and Linux and the rupee sign renders.
"""
from __future__ import annotations

import io
import math
from datetime import datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from qr.config.settings import IST
from qr.services.dm_stats import WINDOW_HOURS, DayStats
from qr.services.payments import fmt_inr

FONT_DIR = Path(__file__).resolve().parents[1] / "assets" / "fonts"
W, H = 1600, 1000
MARGIN = 60

BG_TOP = (5, 3, 18)
BG_BOTTOM = (18, 8, 42)
CYAN = (34, 230, 255)
MAGENTA = (255, 64, 200)
VIOLET = (140, 82, 255)
GOLD = (255, 200, 87)
GREEN = (64, 255, 170)
ORANGE = (255, 140, 60)
TEXT = (238, 236, 255)
MUTED = (150, 142, 205)


# ---------------------------------------------------------------------------
# primitives
# ---------------------------------------------------------------------------
@lru_cache(maxsize=64)
def font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    path = FONT_DIR / f"Poppins-{weight}.ttf"
    try:
        return ImageFont.truetype(str(path), size)
    except OSError:
        return ImageFont.load_default(size)


def rgba(color, alpha: int = 255):
    return (*color[:3], alpha)


def vertical_gradient(size, top, bottom) -> Image.Image:
    w, h = size
    column = Image.new("RGBA", (1, max(1, h)))
    for y in range(h):
        t = y / max(1, h - 1)
        column.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(len(top))))
    return column.resize((max(1, w), max(1, h)))


def bloom(canvas: Image.Image, layer: Image.Image, radius: float, strength: int = 1,
          offset=(0, 0)) -> None:
    """Composite a blurred copy of ``layer`` (the glow) and then ``layer``."""
    glow = layer.filter(ImageFilter.GaussianBlur(radius))
    for _ in range(strength):
        canvas.alpha_composite(glow, offset)
    canvas.alpha_composite(layer, offset)


def tint(color, amount: float = 0.55):
    """Mix ``color`` toward white (neon tube core)."""
    return tuple(int(c + (255 - c) * amount) for c in color[:3])


def glow_text(canvas, xy, text, fnt, color, *, radius=10, strength=2, anchor="la",
              core=None, wide: bool = False, stroke: int = 0) -> tuple[int, int, int, int]:
    """Neon text: coloured bloom + bright core. Returns the text bbox."""
    xy = (round(xy[0]), round(xy[1]))
    probe = ImageDraw.Draw(canvas)
    box = tuple(int(round(v)) for v in probe.textbbox(xy, text, font=fnt, anchor=anchor))
    pad = int(radius * 3)
    size = (box[2] - box[0] + pad * 2, box[3] - box[1] + pad * 2)
    origin = (box[0] - pad, box[1] - pad)
    local = (xy[0] - origin[0], xy[1] - origin[1])
    halo = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(halo).text(local, text, font=fnt, fill=rgba(color), anchor=anchor)
    if wide:  # soft outer haze before the tight bloom
        _composite_at(canvas, halo.filter(ImageFilter.GaussianBlur(radius * 2.6)), origin)
    glow = halo.filter(ImageFilter.GaussianBlur(radius))
    for _ in range(strength):
        _composite_at(canvas, glow, origin)
    sharp = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(sharp).text(local, text, font=fnt, fill=rgba(core or tint(color)), anchor=anchor,
                               stroke_width=stroke, stroke_fill=(10, 6, 28, 255))
    _composite_at(canvas, sharp, origin)
    return box


def _composite_at(canvas: Image.Image, layer: Image.Image, origin) -> None:
    """alpha_composite ``layer`` at ``origin``, clipped to the canvas."""
    x, y = int(origin[0]), int(origin[1])
    left, top = max(0, -x), max(0, -y)
    right = min(layer.width, canvas.width - x)
    bottom = min(layer.height, canvas.height - y)
    if right <= left or bottom <= top:
        return
    canvas.alpha_composite(layer.crop((left, top, right, bottom)), (x + left, y + top))


def glass_card(canvas, box, accent, *, radius=28, glow=18, fill_alpha=22) -> None:
    x0, y0, x1, y1 = box
    edge = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(edge).rounded_rectangle(box, radius, outline=rgba(accent, 210), width=3)
    canvas.alpha_composite(edge.filter(ImageFilter.GaussianBlur(glow)))
    body = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(body)
    draw.rounded_rectangle(box, radius, fill=(255, 255, 255, fill_alpha))
    canvas.alpha_composite(body)
    # top sheen
    sheen = vertical_gradient((x1 - x0, (y1 - y0) // 2), (255, 255, 255, 26), (255, 255, 255, 0))
    mask = Image.new("L", (x1 - x0, (y1 - y0) // 2), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, x1 - x0 - 1, (y1 - y0)), radius, fill=255)
    canvas.paste(sheen, (x0, y0), ImageChops.multiply(mask, sheen.getchannel("A")))
    canvas.alpha_composite(edge)


def nice_ceiling(value: float, ticks: int = 4) -> tuple[float, float]:
    """Axis maximum and step (1/2/2.5/5 x 10^n)."""
    if value <= 0:
        return float(ticks), 1.0
    raw = value / ticks
    exponent = 10 ** math.floor(math.log10(raw))
    for factor in (1, 2, 2.5, 5, 10):
        step = factor * exponent
        if step * ticks >= value:
            return step * ticks, step
    return 10 * exponent * ticks, 10 * exponent


def compact_inr(value) -> str:
    """Indian compact money: 950, 1.2k, 45k, 1.5L, 2.1Cr."""
    value = float(value)
    for limit, suffix in ((1e7, "Cr"), (1e5, "L"), (1e3, "k")):
        if value >= limit:
            number = value / limit
            text = f"{number:.1f}".rstrip("0").rstrip(".") if number < 100 else f"{number:.0f}"
            return f"\u20b9{text}{suffix}"
    return f"\u20b9{value:.0f}"


def monotone_curve(points, samples: int = 14):
    """Smooth curve through ``points`` that never overshoots them
    (Fritsch-Carlson monotone cubic), so the line never shows money that did
    not arrive between two hours."""
    n = len(points)
    if n < 3:
        return list(points)
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    deltas = [(ys[i + 1] - ys[i]) / (xs[i + 1] - xs[i]) for i in range(n - 1)]
    tangents = [deltas[0]] + [
        0.0 if deltas[i - 1] * deltas[i] <= 0 else (deltas[i - 1] + deltas[i]) / 2
        for i in range(1, n - 1)] + [deltas[-1]]
    for i, delta in enumerate(deltas):
        if delta == 0:
            tangents[i] = tangents[i + 1] = 0.0
            continue
        a, b = tangents[i] / delta, tangents[i + 1] / delta
        s = a * a + b * b
        if s > 9:
            t = 3 / math.sqrt(s)
            tangents[i], tangents[i + 1] = t * a * delta, t * b * delta
    out = []
    for i in range(n - 1):
        h = xs[i + 1] - xs[i]
        for step in range(samples):
            t = step / samples
            h00, h10 = 2 * t ** 3 - 3 * t ** 2 + 1, t ** 3 - 2 * t ** 2 + t
            h01, h11 = -2 * t ** 3 + 3 * t ** 2, t ** 3 - t ** 2
            out.append((xs[i] + t * h, h00 * ys[i] + h10 * h * tangents[i]
                        + h01 * ys[i + 1] + h11 * h * tangents[i + 1]))
    out.append(points[-1])
    return out


# ---------------------------------------------------------------------------
# scene
# ---------------------------------------------------------------------------
def _background() -> Image.Image:
    canvas = vertical_gradient((W, H), rgba(BG_TOP), rgba(BG_BOTTOM))
    blooms = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(blooms)
    for (cx, cy, r, color, alpha) in ((180, 120, 380, CYAN, 70), (1450, 900, 460, MAGENTA, 70),
                                      (900, 520, 520, VIOLET, 45), (1500, 120, 260, GOLD, 30)):
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=rgba(color, alpha))
    canvas.alpha_composite(blooms.filter(ImageFilter.GaussianBlur(140)))
    grid = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(grid)
    for x in range(0, W, 40):
        gdraw.line((x, 0, x, H), fill=(255, 255, 255, 7))
    for y in range(0, H, 40):
        gdraw.line((0, y, W, y), fill=(255, 255, 255, 7))
    canvas.alpha_composite(grid)
    return canvas


def _finish(canvas: Image.Image) -> Image.Image:
    vignette = Image.new("L", (W, H), 0)
    ImageDraw.Draw(vignette).ellipse((-W * 0.15, -H * 0.25, W * 1.15, H * 1.25), fill=255)
    vignette = vignette.filter(ImageFilter.GaussianBlur(160))
    shade = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    shade.putalpha(ImageChops.invert(vignette).point(lambda v: int(v * 0.55)))
    canvas.alpha_composite(shade)
    rgb = canvas.convert("RGB")
    grain = Image.effect_noise((W, H), 28).convert("RGB")
    return Image.blend(rgb, grain, 0.03)


def _header(canvas, stats: DayStats, generated: datetime, account: str) -> None:
    day = datetime.fromisoformat(stats.date)
    x = MARGIN
    box = glow_text(canvas, (x, 44), "DM TRAFFIC", font("ExtraBold", 66), CYAN, radius=12,
                    strength=3, wide=True)
    glow_text(canvas, (box[2] + 22, 44), "& INCOME", font("ExtraBold", 66), MAGENTA, radius=12,
              strength=3, wide=True)
    subtitle = f"{day:%A, %d %B %Y}  \u00b7  Asia/Kolkata  \u00b7  per hour"
    if account:
        subtitle = f"{account}  \u00b7  " + subtitle
    ImageDraw.Draw(canvas).text((x + 4, 128), subtitle, font=font("Medium", 25), fill=rgba(MUTED))

    if stats.test:
        label, color = "TEST DATA", ORANGE
    elif stats.now_hour is not None:
        label, color = f"LIVE  \u2022  {generated.astimezone(IST):%H:%M} IST", GREEN
    else:
        label, color = "FULL DAY", VIOLET
    pill_font = font("Bold", 24)
    draw = ImageDraw.Draw(canvas)
    tw = draw.textlength(label, font=pill_font)
    x1 = W - MARGIN
    x0 = int(x1 - tw - 56)
    pill = (x0, 58, x1, 112)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(pill, 27, outline=rgba(color), width=3,
                                            fill=rgba(color, 40))
    bloom(canvas, layer, 12, 2)
    glow_text(canvas, ((x0 + x1) // 2, 85), label, pill_font, color, radius=6, strength=1, anchor="mm")


def _stat_cards(canvas, stats: DayStats) -> None:
    peak = stats.peak_dm_hour
    peak_inr = stats.peak_inr_hour
    window = stats.busiest_window()
    cards = [
        ("TOTAL DMS", f"{stats.total_dms:,}", f"from {stats.unique_senders:,} people", CYAN),
        ("PEAK HOUR", f"{peak:02d}:00" if peak is not None else "\u2014",
         (f"{stats.dms[peak]} DMs \u00b7 busiest {WINDOW_HOURS}h {window[0]:02d}\u2013{(window[0] + WINDOW_HOURS) % 24:02d}"
          if peak is not None else "no DMs yet"), MAGENTA),
        ("INCOME", fmt_inr(stats.total_inr) if stats.total_payments else "\u20b90",
         f"{stats.total_payments} payment(s)", GOLD),
        ("PEAK INCOME HOUR", f"{peak_inr:02d}:00" if peak_inr is not None else "\u2014",
         f"{fmt_inr(stats.inr[peak_inr])} in that hour" if peak_inr is not None else "no payments yet", GREEN),
    ]
    gap = 24
    width = (W - 2 * MARGIN - gap * 3) // 4
    top, bottom = 182, 330
    for index, (label, value, sub, accent) in enumerate(cards):
        x0 = MARGIN + index * (width + gap)
        box = (x0, top, x0 + width, bottom)
        glass_card(canvas, box, accent, radius=26, glow=16)
        draw = ImageDraw.Draw(canvas)
        draw.text((x0 + 28, top + 22), label, font=font("Bold", 19), fill=rgba(accent, 230))
        value_font = font("ExtraBold", 50)
        while draw.textlength(value, font=value_font) > width - 56 and value_font.size > 26:
            value_font = font("ExtraBold", value_font.size - 2)
        glow_text(canvas, (x0 + 28, top + 50), value, value_font, accent, radius=9, strength=2,
                  core=tint(accent, 0.7))
        sub_font = font("Medium", 19)
        while draw.textlength(sub, font=sub_font) > width - 56 and sub_font.size > 13:
            sub_font = font("Medium", sub_font.size - 1)
        draw.text((x0 + 28, top + 112), sub, font=sub_font, fill=rgba(MUTED))


def _chart(canvas, stats: DayStats) -> None:
    card = (MARGIN, 360, W - MARGIN, 930)
    glass_card(canvas, card, VIOLET, radius=30, glow=22, fill_alpha=16)
    draw = ImageDraw.Draw(canvas)
    draw.text((card[0] + 30, card[1] + 22), "DMS PER HOUR", font=font("Bold", 21), fill=rgba(TEXT))
    # legend
    legend = [("DMs", CYAN), ("Income", GOLD)] + ([("Now", GREEN)] if stats.now_hour is not None else [])
    lx = card[2] - 30
    for label, color in reversed(legend):
        width = draw.textlength(label, font=font("Medium", 19))
        lx -= width
        draw.text((lx, card[1] + 24), label, font=font("Medium", 19), fill=rgba(MUTED))
        lx -= 30
        dot = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(dot).ellipse((lx, card[1] + 31, lx + 16, card[1] + 47), fill=rgba(color))
        bloom(canvas, dot, 6, 2)
        lx -= 28

    left, right = card[0] + 92, card[2] - 104
    top, bottom = card[1] + 92, card[3] - 64
    plot_h = bottom - top
    slot = (right - left) / 24
    dm_max, dm_step = nice_ceiling(max(stats.dms) if any(stats.dms) else 4)
    inr_values = [float(v) for v in stats.inr]
    inr_max, inr_step = nice_ceiling(max(inr_values) if any(inr_values) else 1000)

    # grid + axes labels
    grid = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(grid)
    ticks = int(round(dm_max / dm_step))
    for i in range(ticks + 1):
        y = bottom - plot_h * i / ticks
        gdraw.line((left, y, right, y), fill=(255, 255, 255, 22 if i else 60), width=1 if i else 2)
        label = f"{dm_step * i:g}"
        gdraw.text((left - 18, y), label, font=font("Medium", 17), fill=rgba(CYAN, 190), anchor="rm")
        inr_label = compact_inr(inr_step * i) if i else "\u20b90"
        gdraw.text((right + 18, y), inr_label, font=font("Medium", 17), fill=rgba(GOLD, 190), anchor="lm")
    canvas.alpha_composite(grid)

    future_from = (stats.now_hour + 1) if stats.now_hour is not None else 24
    peak = stats.peak_dm_hour

    # bars ---------------------------------------------------------------
    bars = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gradient = vertical_gradient((1, plot_h), rgba(MAGENTA), rgba(CYAN))
    hot = vertical_gradient((1, plot_h), rgba((255, 236, 160)), rgba(ORANGE))
    ghosts = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gh = ImageDraw.Draw(ghosts)
    bar_w = slot * 0.6
    for hour in range(24):
        x0 = left + hour * slot + (slot - bar_w) / 2
        x1 = x0 + bar_w
        if hour >= future_from:
            gh.rounded_rectangle((x0, bottom - 18, x1, bottom), 6, outline=(255, 255, 255, 40), width=1)
            continue
        value = stats.dms[hour]
        if not value:
            continue
        height = max(6, plot_h * value / dm_max)
        y0 = bottom - height
        mask = Image.new("L", (W, H), 0)
        ImageDraw.Draw(mask).rounded_rectangle((x0, y0, x1, bottom), min(10, bar_w / 2), fill=255)
        column = (hot if hour == peak else gradient).resize((int(math.ceil(bar_w)) + 2, plot_h))
        tile = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        tile.paste(column, (int(x0) - 1, top))
        bars.paste(tile, (0, 0), mask)
    canvas.alpha_composite(ghosts)
    bloom(canvas, bars, 16, 2)

    # income spline + area -------------------------------------------------
    last_hour = min(23, future_from - 1)
    points = [(left + h * slot + slot / 2, bottom - plot_h * inr_values[h] / inr_max)
              for h in range(last_hour + 1)]
    if any(inr_values[: last_hour + 1]) and len(points) >= 2:
        curve = [(x, min(bottom, max(top, y))) for x, y in monotone_curve(points)]
        area_mask = Image.new("L", (W, H), 0)
        ImageDraw.Draw(area_mask).polygon(curve + [(curve[-1][0], bottom), (curve[0][0], bottom)], fill=255)
        fill = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        fill.paste(vertical_gradient((W, plot_h), rgba(GOLD, 95), rgba(GOLD, 0)), (0, top))
        area = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        area.paste(fill, (0, 0), area_mask)
        canvas.alpha_composite(area)
        line = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ldraw = ImageDraw.Draw(line)
        ldraw.line(curve, fill=rgba(GOLD), width=4, joint="curve")
        peak_inr = stats.peak_inr_hour
        for hour in range(last_hour + 1):
            if stats.payments[hour]:
                x, y = points[hour]
                r = 9 if hour == peak_inr else 6
                ldraw.ellipse((x - r, y - r, x + r, y + r), fill=rgba((255, 240, 200)), outline=rgba(GOLD), width=3)
        bloom(canvas, line, 10, 2)
        if peak_inr is not None and peak_inr <= last_hour:
            x, y = points[peak_inr]
            label = compact_inr(stats.inr[peak_inr])
            # beside the dot (right, or left near the edge) so it never sits on
            # the bar's own value / PEAK label
            if x + 120 < right:
                lx, anchor = x + 16, "lm"
            else:
                lx, anchor = x - 16, "rm"
            glow_text(canvas, (lx, min(bottom - 14, max(top + 12, y))), label, font("Bold", 19), GOLD,
                      radius=7, strength=2, anchor=anchor, core=(255, 245, 210), stroke=3)

    # value labels
    draw = ImageDraw.Draw(canvas)
    for hour in range(min(24, future_from)):
        value = stats.dms[hour]
        if not value:
            continue
        cx = left + hour * slot + slot / 2
        y = bottom - max(6, plot_h * value / dm_max) - 10
        if hour == peak:
            glow_text(canvas, (cx, y - 2), f"PEAK {value}", font("ExtraBold", 19), GOLD,
                      radius=7, strength=2, anchor="ms", core=(255, 245, 210), stroke=3)
        else:
            draw.text((cx, y), str(value), font=font("Medium", 15), fill=rgba(TEXT, 225), anchor="ms",
                      stroke_width=3, stroke_fill=(10, 6, 28, 255))

    # now marker -------------------------------------------------------------
    if stats.now_hour is not None:
        x = left + (stats.now_hour + (stats.now_minute or 0) / 60) * slot
        marker = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        mdraw = ImageDraw.Draw(marker)
        for y in range(int(top), int(bottom), 14):
            mdraw.line((x, y, x, min(bottom, y + 7)), fill=rgba(GREEN), width=2)
        bloom(canvas, marker, 6, 2)
        glow_text(canvas, (x, top - 12), "NOW", font("Bold", 16), GREEN, radius=5, strength=1, anchor="ms")

    # hour labels --------------------------------------------------------
    draw = ImageDraw.Draw(canvas)
    for hour in range(24):
        cx = left + hour * slot + slot / 2
        colour = rgba(GOLD) if hour == peak else rgba(MUTED, 120 if hour >= future_from else 220)
        draw.text((cx, bottom + 16), f"{hour:02d}", font=font("Bold" if hour == peak else "Medium", 17),
                  fill=colour, anchor="mt")

    if not stats.total_dms and not stats.total_payments:
        glow_text(canvas, ((left + right) // 2, (top + bottom) // 2 - 20), "NO DMS RECORDED YET",
                  font("ExtraBold", 42), VIOLET, radius=14, strength=2, anchor="mm")
        draw.text(((left + right) // 2, (top + bottom) // 2 + 30),
                  "DMs are counted live from now on  \u00b7  send .graph scan to backfill today",
                  font=font("Medium", 21), fill=rgba(MUTED), anchor="mm")


def _footer(canvas, stats: DayStats, generated: datetime) -> None:
    draw = ImageDraw.Draw(canvas)
    glow_text(canvas, (MARGIN, 958), "QUICKREPLY", font("ExtraBold", 20), CYAN, radius=6, strength=1)
    draw.text((MARGIN + 140, 959), "ANALYTICS", font=font("Medium", 20), fill=rgba(MUTED))
    parts = []
    conversion = stats.conversion()
    if conversion is not None:
        parts.append(f"{conversion * 100:.1f}% of DM senders paid")
    if stats.total_dms:
        busy = max(stats.senders)
        parts.append(f"max {busy} people in one hour")
    if stats.tracking_since and stats.tracking_since.astimezone(IST).date().isoformat() == stats.date:
        parts.append(f"tracking since {stats.tracking_since.astimezone(IST):%H:%M}")
    parts.append(f"generated {generated.astimezone(IST):%d %b %H:%M} IST")
    draw.text((W - MARGIN, 960), "  \u00b7  ".join(parts), font=font("Medium", 19),
              fill=rgba(MUTED), anchor="ra")


def render(stats: DayStats, *, generated: datetime | None = None, account: str = "",
           fmt: str = "JPEG") -> bytes:
    """Render the card. Returns JPEG (default) or PNG bytes."""
    from qr.services.clock import utcnow

    generated = generated or utcnow()
    canvas = _background()
    _header(canvas, stats, generated, account)
    _stat_cards(canvas, stats)
    _chart(canvas, stats)
    _footer(canvas, stats, generated)
    image = _finish(canvas)
    out = io.BytesIO()
    if fmt.upper() == "PNG":
        image.save(out, "PNG", optimize=True)
    else:
        image.save(out, "JPEG", quality=94, subsampling=0, optimize=True)
    return out.getvalue()


__all__ = ["render", "compact_inr", "nice_ceiling", "Decimal"]
