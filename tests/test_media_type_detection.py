"""Regression: il media type delle immagini passate ad Anthropic e Meshy
deve essere rilevato dai magic bytes, non dall'estensione. Errore reale
visto in produzione:

    Error code: 400 - The image was specified using the image/png media
    type, but the image appears to be a image/jpeg image
"""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from src.prompt_refiner import _detect_media_type, _image_block
from src.workflow import _to_data_uri_any


def test_detect_png() -> None:
    assert _detect_media_type(b"\x89PNG\r\n\x1a\n_rest") == "image/png"


def test_detect_jpeg() -> None:
    assert _detect_media_type(b"\xff\xd8\xff\xe0JFIF") == "image/jpeg"


def test_detect_webp() -> None:
    assert _detect_media_type(b"RIFF\x00\x00\x00\x00WEBP_") == "image/webp"


def test_detect_gif() -> None:
    assert _detect_media_type(b"GIF89a_") == "image/gif"
    assert _detect_media_type(b"GIF87a_") == "image/gif"


def test_detect_unknown_defaults_png() -> None:
    assert _detect_media_type(b"\x00\x00\x00") == "image/png"


def test_image_block_jpeg_with_png_extension(tmp_path: Path) -> None:
    """File con estensione .png ma contenuto JPEG → block deve dichiarare jpeg.
    Caso che ha fatto fallire Anthropic in produzione."""
    p = tmp_path / "photo.png"  # estensione bugiarda
    img = Image.new("RGB", (10, 10), (255, 0, 0))
    img.save(p, format="JPEG")  # ma contenuto JPEG
    block = _image_block(p)
    assert block["source"]["media_type"] == "image/jpeg"


def test_data_uri_any_jpeg(tmp_path: Path) -> None:
    p = tmp_path / "ref.png"  # estensione bugiarda
    Image.new("RGB", (8, 8), (0, 0, 255)).save(p, format="JPEG")
    uri = _to_data_uri_any(p)
    assert uri.startswith("data:image/jpeg;base64,")


def test_data_uri_any_png(tmp_path: Path) -> None:
    p = tmp_path / "ref.png"
    Image.new("RGB", (8, 8), (0, 255, 0)).save(p, format="PNG")
    uri = _to_data_uri_any(p)
    assert uri.startswith("data:image/png;base64,")
