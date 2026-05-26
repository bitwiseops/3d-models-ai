"""Crop di un'immagine 2x2 in 4 quadranti nominati (front / 3-4 / side / back).
Convenzione di layout condivisa tra portraits e oggetti, allineata ai system
prompt di prompt_refiner che chiedono esplicitamente quel layout."""
from __future__ import annotations

from pathlib import Path

from PIL import Image

# Ordine quadranti del grid 2x2: top-left=FRONT, top-right=3/4,
# bottom-left=SIDE, bottom-right=BACK. (col, row) in coordinate griglia.
GRID_QUADRANTS: list[tuple[str, int, int]] = [
    ("front", 0, 0),
    ("three_quarter", 1, 0),
    ("side", 0, 1),
    ("back", 1, 1),
]


def crop_grid_2x2(grid_path: Path, *, prefix: str = "grid") -> list[Path]:
    """Ritaglia un'immagine 2x2 in 4 file `<prefix>_<name>.png` accanto al grid."""
    out: list[Path] = []
    with Image.open(grid_path) as img:
        img = img.convert("RGB")
        w, h = img.size
        hw, hh = w // 2, h // 2
        for name, col, row in GRID_QUADRANTS:
            box = (col * hw, row * hh, (col + 1) * hw, (row + 1) * hh)
            crop = img.crop(box)
            dst = grid_path.parent / f"{prefix}_{name}.png"
            crop.save(dst, format="PNG")
            out.append(dst)
    return out
