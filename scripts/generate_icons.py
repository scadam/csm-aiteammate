"""
Generate the Teams app icons for **CSM Autopilot** (L2Q-style brand treatment).

Brand palette (matches the control-plane dashboards):
  Navy #0A2540, Teal #00A3A1, Teal-bright #00C2C0, teal accent bar.

Outputs (Microsoft Teams app icon spec):
  - color.png   192x192  full-colour app tile
  - outline.png  32x32    transparent, single-colour (white) silhouette

Run: python -m scripts.generate_icons  [output_dir]
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# ── brand colours (L2Q look) ──────────────────────────
NAVY = (0x0A, 0x25, 0x40, 255)
TEAL = (0x00, 0xA3, 0xA1, 255)
TEAL_BRIGHT = (0x00, 0xC2, 0xC0, 255)
GREEN = (0x05, 0x96, 0x69, 255)
AMBER = (0xD9, 0x77, 0x06, 255)
WHITE = (0xFF, 0xFF, 0xFF, 255)

SS = 4  # supersample factor for crisp anti-aliased edges


def _font(size: int) -> ImageFont.FreeTypeFont:
    """Load Segoe UI Bold (primary typeface), with sensible fallbacks."""
    for name in ("segoeuib.ttf", "seguisb.ttf", "arialbd.ttf", "arial.ttf"):
        path = Path("C:/Windows/Fonts") / name
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def _lerp(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3)) + (255,)


def _accent_bar(draw: ImageDraw.ImageDraw, w: int, y0: int, h: int) -> None:
    """Teal accent bar across the top (L2Q signature)."""
    for x in range(w):
        t = x / max(1, w - 1)
        col = _lerp(TEAL, TEAL_BRIGHT, t)
        draw.line([(x, y0), (x, y0 + h)], fill=col)


def _orbit(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int, ring_w: int) -> None:
    """An 'autopilot orbit': a tilted teal ring + a node, suggesting an autonomous agent."""
    # Tilted orbit ring (ellipse outline).
    draw.ellipse(
        [cx - r, cy - int(r * 0.62), cx + r, cy + int(r * 0.62)],
        outline=TEAL_BRIGHT, width=ring_w,
    )
    # Inner core dot (the agent).
    core = int(r * 0.30)
    draw.ellipse([cx - core, cy - core, cx + core, cy + core], fill=TEAL)
    draw.ellipse(
        [cx - core + ring_w, cy - core + ring_w, cx + core - ring_w, cy + core - ring_w],
        fill=WHITE,
    )
    # Orbiting node on the ring (upper-right).
    nx, ny = cx + int(r * 0.72), cy - int(r * 0.30)
    node = int(r * 0.20)
    draw.ellipse([nx - node, ny - node, nx + node, ny + node], fill=WHITE)
    draw.ellipse(
        [nx - node + 2 * SS, ny - node + 2 * SS, nx + node - 2 * SS, ny + node - 2 * SS],
        fill=AMBER,
    )


def _wordmark(img: Image.Image, text: str, cy: int, size: int, tracking: int) -> None:
    """Draw a letter-spaced white wordmark centred horizontally at vertical centre cy."""
    draw = ImageDraw.Draw(img)
    font = _font(size)
    widths = [draw.textbbox((0, 0), ch, font=font)[2] for ch in text]
    total = sum(widths) + tracking * (len(text) - 1)
    x = (img.width - total) // 2
    asc, desc = font.getmetrics()
    y = cy - (asc + desc) // 2
    for ch, w in zip(text, widths):
        draw.text((x, y), ch, font=font, fill=WHITE)
        x += w + tracking


def make_color(path: Path) -> None:
    w = 192 * SS
    img = Image.new("RGBA", (w, w), NAVY)
    draw = ImageDraw.Draw(img)
    # Top teal accent bar.
    _accent_bar(draw, w, 0, 14 * SS)
    # Autopilot orbit motif (upper half).
    _orbit(draw, cx=w // 2, cy=int(w * 0.42), r=46 * SS, ring_w=7 * SS)
    # Brand wordmark (lower third).
    _wordmark(img, "CSM", cy=int(w * 0.78), size=52 * SS, tracking=8 * SS)
    img.resize((192, 192), Image.LANCZOS).save(path)
    print(f"wrote {path}")


def make_outline(path: Path) -> None:
    w = 32 * SS
    img = Image.new("RGBA", (w, w), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    cx = cy = w // 2
    r = 13 * SS
    ring_w = 3 * SS
    # White orbit ring + node on transparent (Teams rail icon).
    draw.ellipse([cx - r, cy - int(r * 0.62), cx + r, cy + int(r * 0.62)],
                 outline=WHITE, width=ring_w)
    core = int(r * 0.34)
    draw.ellipse([cx - core, cy - core, cx + core, cy + core], fill=WHITE)
    nx, ny = cx + int(r * 0.72), cy - int(r * 0.32)
    node = int(r * 0.26)
    draw.ellipse([nx - node, ny - node, nx + node, ny + node], fill=WHITE)
    img.resize((32, 32), Image.LANCZOS).save(path)
    print(f"wrote {path}")


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("appPackage")
    out.mkdir(parents=True, exist_ok=True)
    make_color(out / "color.png")
    make_outline(out / "outline.png")


if __name__ == "__main__":
    main()
