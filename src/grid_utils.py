"""Crop di un'immagine composita nei suoi quadranti nominati, secondo uno
`ViewSchema` (vedi src/view_schema.py). Convenzione di layout condivisa tra
portraits (2x2) e oggetti (1x2 due 3/4), allineata ai system prompt di
prompt_refiner che chiedono esplicitamente quel layout."""
from __future__ import annotations

from pathlib import Path

from PIL import Image

from . import view_schema
from .view_schema import ViewSchema


def crop_grid(grid_path: Path, schema: ViewSchema, *, prefix: str = "grid") -> list[Path]:
    """Ritaglia un'immagine composta nelle celle definite da `schema`.

    Divide il canvas in `schema.cols` x `schema.rows` celle uguali e ritaglia
    ogni `ViewSpec` alla sua posizione (col, row), salvando `<prefix>_<name>.png`
    accanto al grid. Ritorna i path nell'ordine dello schema.
    """
    out: list[Path] = []
    with Image.open(grid_path) as img:
        img = img.convert("RGB")
        w, h = img.size
        cw, ch = w // schema.cols, h // schema.rows
        for v in schema.views:
            box = (v.col * cw, v.row * ch, (v.col + 1) * cw, (v.row + 1) * ch)
            crop = img.crop(box)
            dst = grid_path.parent / f"{prefix}_{v.name}.png"
            crop.save(dst, format="PNG")
            out.append(dst)
    return out


def crop_grid_2x2(grid_path: Path, *, prefix: str = "grid") -> list[Path]:
    """Wrapper retro-compatibile per il layout 2x2 a 4 viste (Portraits)."""
    return crop_grid(grid_path, view_schema.PORTRAIT_VIEWS, prefix=prefix)
