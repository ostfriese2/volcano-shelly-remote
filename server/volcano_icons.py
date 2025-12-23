#!/usr/bin/env python3
"""
volcano_icons.py
- Generates glossy "ball" PNG icons for values 0..200 and caches them on disk.
- Intended for use as --icon argument to notify-send (or other notification systems).

Requires: Pillow (pip install pillow)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import math
import os
from typing import Tuple

from PIL import Image, ImageDraw, ImageFilter


RGB = Tuple[int, int, int]


@dataclass(frozen=True)
class ColorAnchor:
    value: int
    rgb: RGB


# Anchors you requested:
ANCHORS = [
    ColorAnchor(0,   (0x21, 0x96, 0xF3)),  # blue-ish baseline (tune if you want)
    ColorAnchor(180, (0xFF, 0xCC, 0x32)),  # yellow at 180
    ColorAnchor(200, (0xF4, 0x43, 0x36)),  # red at 200
]


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _lerp_rgb(c1: RGB, c2: RGB, t: float) -> RGB:
    return (
        int(_lerp(c1[0], c2[0], t)),
        int(_lerp(c1[1], c2[1], t)),
        int(_lerp(c1[2], c2[2], t)),
    )


def value_to_rgb(value: float) -> RGB:
    """
    Piecewise-linear interpolation between anchors.
    Ensures:
      value=180 -> #FFCC32
      value=200 -> #F44336
    """
    v = _clamp(float(value), 0.0, 200.0)

    # Find segment
    a0 = ANCHORS[0]
    for a1 in ANCHORS[1:]:
        if v <= a1.value:
            t = (v - a0.value) / float(a1.value - a0.value) if a1.value != a0.value else 0.0
            return _lerp_rgb(a0.rgb, a1.rgb, _clamp(t, 0.0, 1.0))
        a0 = a1
    return ANCHORS[-1].rgb


def _radial_gradient(size: int, inner: Tuple[int, int, int, int], outer: Tuple[int, int, int, int], focus=(0.35, 0.30)) -> Image.Image:
    """
    Creates an RGBA radial gradient image.
    focus: relative center of gradient (0..1, 0..1) where highlight is strongest.
    """
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    cx = focus[0] * size
    cy = focus[1] * size
    max_r = math.hypot(max(cx, size - cx), max(cy, size - cy))

    px = img.load()
    for y in range(size):
        for x in range(size):
            r = math.hypot(x - cx, y - cy) / max_r
            r = _clamp(r, 0.0, 1.0)
            # smoothstep for nicer falloff
            t = r * r * (3 - 2 * r)
            px[x, y] = (
                int(_lerp(inner[0], outer[0], t)),
                int(_lerp(inner[1], outer[1], t)),
                int(_lerp(inner[2], outer[2], t)),
                int(_lerp(inner[3], outer[3], t)),
            )
    return img


def make_glossy_ball_icon(value: float, size: int = 64, pad: int = 3) -> Image.Image:
    """
    Returns a glossy circular icon (RGBA).
    Gloss effect is achieved via:
      - subtle drop shadow
      - inner rim shading
      - specular highlight (radial gradient overlay)
    """
    rgb = value_to_rgb(value)

    # Base canvas
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    # Drop shadow (very subtle)
    shadow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.ellipse((pad + 2, pad + 3, size - pad + 2, size - pad + 3), fill=(0, 0, 0, 70))
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=3))
    img.alpha_composite(shadow)

    # Ball base
    ball = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(ball)
    d.ellipse((pad, pad, size - pad, size - pad), fill=(*rgb, 255))

    # Inner rim shading (gives depth, "button" look)
    rim = _radial_gradient(
        size,
        inner=(255, 255, 255, 10),
        outer=(0, 0, 0, 90),
        focus=(0.50, 0.55),
    )
    # Mask rim to ball shape only
    mask = Image.new("L", (size, size), 0)
    md = ImageDraw.Draw(mask)
    md.ellipse((pad, pad, size - pad, size - pad), fill=255)
    rim.putalpha(Image.composite(rim.split()[-1], Image.new("L", (size, size), 0), mask))
    ball = Image.alpha_composite(ball, Image.composite(rim, Image.new("RGBA", (size, size), (0,0,0,0)), mask))

    # Specular highlight (gloss)
    highlight = _radial_gradient(
        size,
        inner=(255, 255, 255, 140),
        outer=(255, 255, 255, 0),
        focus=(0.30, 0.25),
    )
    # Restrict highlight to upper portion for a "button" sheen
    clip = Image.new("L", (size, size), 0)
    cd = ImageDraw.Draw(clip)
    cd.ellipse((pad + 6, pad + 4, size - pad - 6, size // 2 + 6), fill=255)
    highlight.putalpha(Image.composite(highlight.split()[-1], Image.new("L", (size, size), 0), clip))
    ball = Image.alpha_composite(ball, Image.composite(highlight, Image.new("RGBA", (size, size), (0,0,0,0)), mask))

    # Subtle border (optional; very light)
    d = ImageDraw.Draw(ball)
    d.ellipse((pad, pad, size - pad, size - pad), outline=(0, 0, 0, 60), width=1)

    img.alpha_composite(ball)
    return img


def default_cache_dir(app_name: str = "volcano", size: int = 64) -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / app_name / f"icons_{size}"


def ensure_icon_cache(cache_dir: Path | None = None, size: int = 64) -> Path:
    cache_dir = cache_dir or default_cache_dir(size=size)
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def icon_path_for_value(value: int, cache_dir: Path, size: int = 64) -> Path:
    v = int(_clamp(value, 0, 200))
    return cache_dir / f"ball_{v:03d}.png"


def build_cache_0_200(cache_dir: Path | None = None, size: int = 64, overwrite: bool = False) -> Path:
    cache_dir = ensure_icon_cache(cache_dir=cache_dir, size=size)
    for v in range(0, 201):
        p = icon_path_for_value(v, cache_dir, size=size)
        if p.exists() and not overwrite:
            continue
        img = make_glossy_ball_icon(v, size=size)
        img.save(p, "PNG")
    return cache_dir


def get_cached_icon(value: int, cache_dir: Path | None = None, size: int = 64) -> str:
    """
    Returns a filesystem path to a cached icon for `value` (0..200).
    Generates the full cache on first use if needed.
    """
    cache_dir = ensure_icon_cache(cache_dir=cache_dir, size=size)
    p = icon_path_for_value(value, cache_dir, size=size)

    # If not present, lazily build only this one (fast path)
    if not p.exists():
        img = make_glossy_ball_icon(value, size=size)
        img.save(p, "PNG")
    return str(p)


if __name__ == "__main__":
    # One-shot cache builder:
    d = build_cache_0_200(size=64, overwrite=False)
    print(f"Built/verified cache in: {d}")
