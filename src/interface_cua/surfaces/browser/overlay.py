"""Screenshot post-processing: sensitive-region masking and set-of-marks overlays.

Drawn on the image, not injected into the DOM, so observation never mutates the target page.
The unmasked screenshot is never written to disk or sent anywhere.
"""

from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont

from interface_cua.domain.observations import Control, Rect

MARK_COLORS = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#008080", "#9a6324", "#800000"]


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in ("arialbd.ttf", "DejaVuSans-Bold.ttf", "Arial Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render(png: bytes, masks: list[tuple[Rect, str]], controls: list[Control] | None) -> bytes:
    img = Image.open(io.BytesIO(png)).convert("RGB")
    draw = ImageDraw.Draw(img)
    small = _font(10)
    for rect, _kind in masks:
        draw.rectangle((rect.x - 1, rect.y - 1, rect.x + rect.w + 1, rect.y + rect.h + 1), fill="#3a3a3a")
    labelled: list[Rect] = []
    for rect, kind in sorted(masks, key=lambda m: -(m[0].w * m[0].h)):
        cx, cy = rect.center
        if any(r.x <= cx <= r.x + r.w and r.y <= cy <= r.y + r.h for r in labelled):
            continue  # one label per masked region
        labelled.append(rect)
        if rect.w > 40 and rect.h >= 9:
            draw.text((rect.x + 3, rect.y + max(0, rect.h / 2 - 6)), f"masked:{kind}"[: int(rect.w / 6)], fill="#ffffff", font=small)
    if controls:
        label_font = _font(12)
        for c in controls:
            if c.bbox is None:
                continue
            color = MARK_COLORS[int(c.handle) % len(MARK_COLORS)] if c.handle.isdigit() else "#e6194b"
            b = c.bbox
            draw.rectangle((b.x, b.y, b.x + b.w, b.y + b.h), outline=color, width=2)
            tag = c.handle
            tw = 8 * len(tag) + 6
            lx, ly = max(0, b.x - 2), max(0, b.y - 15)
            draw.rectangle((lx, ly, lx + tw, ly + 14), fill=color)
            draw.text((lx + 3, ly), tag, fill="#ffffff", font=label_font)
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()
