"""Test che `_detect_center_band` e `_split_image` rimuovano correttamente
la banda monocolore tra le viste front/back generate da Meshy."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from src.workflow import _detect_center_band, _split_image


def _make_combined(w: int, h: int, band_w: int, bg=(255, 255, 255)) -> Image.Image:
    """Crea un'immagine sintetica 16:9 con due "soggetti" colorati e una banda
    bianca al centro. Soggetto sx = quadrato rosso, dx = quadrato blu."""
    img = Image.new("RGB", (w, h), bg)
    draw = ImageDraw.Draw(img)
    side = h // 2
    # Soggetto sx a 25% width
    cx_l = w // 4
    draw.rectangle([cx_l - side // 2, h // 4, cx_l + side // 2, h * 3 // 4], fill="red")
    # Soggetto dx a 75% width
    cx_r = w * 3 // 4
    draw.rectangle([cx_r - side // 2, h // 4, cx_r + side // 2, h * 3 // 4], fill="blue")
    # La banda centrale è già bianca (background), ma simulo che essa sia
    # esplicitamente la zona piatta (i quadrati lasciano spazio per band_w).
    return img


def test_detect_band_wide() -> None:
    """Banda di 20% larghezza deve essere rilevata."""
    img = _make_combined(1000, 500, band_w=200)
    band = _detect_center_band(img)
    assert band is not None
    L, R = band
    # La banda è simmetrica intorno al centro
    assert abs((L + R) // 2 - 500) < 10
    # Deve essere larga almeno qualche decina di pixel
    assert R - L >= 50


def test_detect_band_none_when_image_busy() -> None:
    """Immagine senza zone piatte centrali → nessuna banda rilevata."""
    # Riempo l'intera immagine con rumore visivo
    import numpy as np
    rng = np.random.default_rng(42)
    arr = rng.integers(0, 256, size=(500, 1000, 3), dtype=np.uint8)
    img = Image.fromarray(arr, "RGB")
    band = _detect_center_band(img)
    # Può essere None oppure una banda molto stretta
    if band is not None:
        L, R = band
        assert R - L < 30, f"banda inattesa larga: {R-L}px"


def test_split_with_band_produces_symmetric_halves(tmp_path: Path) -> None:
    """Lo split deve produrre front e back con stessa larghezza (simmetrici)."""
    img = _make_combined(1000, 500, band_w=200)
    src = tmp_path / "test.png"
    img.save(src)
    front_p, back_p, clean_p = _split_image(src)
    fw, _ = Image.open(front_p).size
    bw, _ = Image.open(back_p).size
    assert fw == bw, f"front e back devono essere simmetrici: {fw} vs {bw}"
    # Clean composite esiste e include un gutter
    assert clean_p is not None
    cw, _ = Image.open(clean_p).size
    assert cw == fw + 8 + bw  # gutter di 8px


def test_split_without_band_falls_back_to_midpoint(tmp_path: Path) -> None:
    """Se nessuna banda è rilevata, lo split usa la metà esatta e non crea
    il clean composite (clean_path è None)."""
    # Immagine totalmente rumorosa: nessuna banda centrale
    import numpy as np
    rng = np.random.default_rng(0)
    arr = rng.integers(0, 256, size=(500, 1000, 3), dtype=np.uint8)
    img = Image.fromarray(arr, "RGB")
    src = tmp_path / "test.png"
    img.save(src)
    front_p, back_p, clean_p = _split_image(src)
    fw, _ = Image.open(front_p).size
    bw, _ = Image.open(back_p).size
    # Metà esatta: 500 + 500 = 1000
    assert fw + bw == 1000
    # Nessuna banda rilevata → no clean composite
    assert clean_p is None
