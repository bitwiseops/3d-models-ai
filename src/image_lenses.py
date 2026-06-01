"""Filtri "lente" sulle immagini di riferimento per il parsing percettivo.

Le pipeline `fusion-build` / `blender-build` partono da 4 viste ortogonali e ne
estraggono un PART-MANIFEST (cosa deve esistere). La foto grezza, da sola, rende
poco leggibili contorni, separazione tra parti e proporzioni. Questo modulo
genera in modo DETERMINISTICO alcune varianti filtrate ("lenti") di ogni vista,
che il subagent percettivo legge ACCANTO all'originale per descrivere l'oggetto
con più accuratezza:

- ``edges``      → contorno esterno + linee di feature interne (scanalature,
                   giunzioni, spigoli, fori): aiuta silhouette/dettagli/proporzioni.
- ``silhouette`` → maschera binaria oggetto/sfondo: proporzioni e simmetria pulite.
- ``color``      → quantizzazione a poche tinte piatte: separa parti DISTINTE e
                   raggruppamenti per materiale.

È una pura utility immagine (come `src/grid_utils.py`), CONDIVISA tra le due
pipeline: i filtri sono CAD-agnostici. Usa solo Pillow + NumPy + SciPy (niente
OpenCV: non è una dipendenza del progetto).

Il subagent ha solo Read/Write: NON può eseguire codice. Per questo le lenti
vanno generate nel main loop (o all'`init`) e passate come file già pronti.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter, ImageOps

# Lenti di default per il parser percettivo (§1). Il gate (§5) ne usa un
# sottoinsieme (edges + silhouette): il giudice IGNORA i colori per principio.
DEFAULT_LENSES: tuple[str, ...] = ("edges", "silhouette", "color")
GATE_LENSES: tuple[str, ...] = ("edges", "silhouette")


def _edges(img: Image.Image) -> Image.Image:
    """Mappa degli spigoli: denoise leggero → FIND_EDGES → autocontrast.

    Lo sfocato lieve toglie il rumore di compressione (JPEG/PNG) così l'edge
    detection evidenzia i contorni reali (silhouette + linee di feature interne)
    invece del grano. Ritorna RGB (spigoli chiari su fondo scuro).
    """
    gray = img.convert("L").filter(ImageFilter.GaussianBlur(radius=1.0))
    edges = gray.filter(ImageFilter.FIND_EDGES)
    edges = ImageOps.autocontrast(edges)
    return edges.convert("RGB")


def _background_mask(arr: np.ndarray) -> np.ndarray | None:
    """Stima la maschera dell'OGGETTO (True = soggetto) dal colore di sfondo.

    Lo sfondo si stima dalla cornice di bordo dell'immagine (mediana dei pixel
    sui 4 lati): le viste Oggetti hanno tipicamente un fondo uniforme. Un pixel è
    "oggetto" se dista dal colore di sfondo oltre una soglia relativa. Ritorna
    None se il risultato è degenere (quasi tutto o quasi niente oggetto), così il
    chiamante può ripiegare su una soglia di luminanza.
    """
    h, w = arr.shape[:2]
    b = max(2, min(h, w) // 50)  # spessore cornice di campionamento
    border = np.concatenate([
        arr[:b, :, :].reshape(-1, 3),
        arr[-b:, :, :].reshape(-1, 3),
        arr[:, :b, :].reshape(-1, 3),
        arr[:, -b:, :].reshape(-1, 3),
    ])
    bg = np.median(border, axis=0)
    dist = np.linalg.norm(arr.astype(np.float32) - bg, axis=2)
    # soglia relativa alla dispersione dei colori dell'immagine
    thr = max(30.0, float(dist.mean()))
    mask = dist > thr
    frac = float(mask.mean())
    if frac < 0.02 or frac > 0.98:
        return None
    return mask


def _otsu_mask(gray: np.ndarray) -> np.ndarray:
    """Soglia di Otsu (fallback): separa oggetto/sfondo massimizzando la varianza
    inter-classe sull'istogramma di luminanza. Sceglie come "oggetto" il lato meno
    esteso (i soggetti riempiono di norma meno della metà dell'inquadratura)."""
    hist, _ = np.histogram(gray, bins=256, range=(0, 256))
    total = gray.size
    sum_all = float(np.dot(np.arange(256), hist))
    w_bg = 0.0
    sum_bg = 0.0
    best_t, best_var = 0, -1.0
    for t in range(256):
        w_bg += hist[t]
        if w_bg == 0:
            continue
        w_fg = total - w_bg
        if w_fg == 0:
            break
        sum_bg += t * hist[t]
        m_bg = sum_bg / w_bg
        m_fg = (sum_all - sum_bg) / w_fg
        var = w_bg * w_fg * (m_bg - m_fg) ** 2
        if var > best_var:
            best_var, best_t = var, t
    dark = gray <= best_t
    # "oggetto" = la classe meno numerosa (soggetto centrale vs fondo ampio)
    return dark if dark.mean() <= 0.5 else ~dark


def _silhouette(img: Image.Image) -> Image.Image:
    """Silhouette binaria: oggetto bianco su fondo nero.

    Prova prima la stima per colore di sfondo (robusta su fondo uniforme), poi
    ripiega su Otsu sulla luminanza. Best-effort: serve a leggere proporzioni e
    simmetria, non è una segmentazione esatta.
    """
    rgb = np.array(img.convert("RGB"))
    mask = _background_mask(rgb)
    if mask is None:
        mask = _otsu_mask(np.array(img.convert("L")))
    out = np.where(mask, 255, 0).astype(np.uint8)
    return Image.fromarray(out, mode="L").convert("RGB")


def _color_segment(img: Image.Image, colors: int = 8) -> Image.Image:
    """Quantizzazione a poche tinte piatte: riduce a `colors` colori così le
    regioni omogenee (parti/materiali distinti) emergono come campi piatti."""
    q = img.convert("RGB").quantize(colors=colors, method=Image.Quantize.MEDIANCUT)
    return q.convert("RGB")


_FILTERS = {
    "edges": _edges,
    "silhouette": _silhouette,
    "color": _color_segment,
}


def generate_lenses(
    view_paths: dict[str, Path],
    out_dir: Path,
    *,
    lenses: tuple[str, ...] = DEFAULT_LENSES,
) -> dict[str, dict[str, str]]:
    """Genera le lenti per ogni vista in `out_dir/<view>__<lens>.png`.

    `view_paths` mappa nome-vista → path PNG. Idempotente (sovrascrive) e
    best-effort per-lente: se una lente fallisce su una vista, la salta e
    prosegue (un filtro rotto non deve bloccare il parsing). Ritorna la mappa
    {view: {lens: path_assoluto}} delle lenti effettivamente prodotte.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, dict[str, str]] = {}
    for view, src in view_paths.items():
        src = Path(src)
        if not src.exists():
            continue
        produced: dict[str, str] = {}
        with Image.open(src) as im:
            im.load()
            for lens in lenses:
                fn = _FILTERS.get(lens)
                if fn is None:
                    continue
                try:
                    dst = out_dir / f"{view}__{lens}.png"
                    fn(im).save(dst, format="PNG")
                    produced[lens] = str(dst.resolve())
                except Exception as exc:  # best-effort: una lente rotta non blocca
                    print(f"[image_lenses] lente '{lens}' fallita su {view}: {exc}")
        if produced:
            result[view] = produced
    return result
