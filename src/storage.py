import json
import re
import shutil
import unicodedata
from datetime import datetime
from pathlib import Path

import requests

from . import config


def _slugify(text: str, max_len: int = 40) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:max_len] or "session"


def create_session(idea: str) -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    session = config.OUTPUTS_ROOT / f"{ts}-{_slugify(idea)}"
    (session / "prompts").mkdir(parents=True, exist_ok=True)
    (session / "images").mkdir(parents=True, exist_ok=True)
    (session / "3d").mkdir(parents=True, exist_ok=True)
    (session / "prompts" / "00_idea.txt").write_text(idea, encoding="utf-8")
    metadata = {
        "created_at": ts,
        "idea": idea,
        "iterations": [],
        "accepted_iteration": None,
        "image_task_id": None,
        "model_task_id": None,
        "credits": {"text_to_image": 0, "image_to_3d": 0},
    }
    (session / "metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return session


def save_prompt(session: Path, version: int, text: str) -> Path:
    path = session / "prompts" / f"{version:02d}_refined.txt"
    path.write_text(text, encoding="utf-8")
    return path


def save_image_from_url(session: Path, version: int, url: str) -> Path:
    path = session / "images" / f"{version:02d}.png"
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    path.write_bytes(r.content)
    return path


def save_references(session: Path, paths: list[Path]) -> list[Path]:
    """Copia i file di reference nella session dir e ritorna i nuovi path."""
    if not paths:
        return []
    dst_dir = session / "references"
    dst_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for i, src in enumerate(paths, start=1):
        if not src.exists():
            raise FileNotFoundError(f"Reference non trovata: {src}")
        ext = src.suffix.lower() or ".png"
        dst = dst_dir / f"ref_{i:02d}{ext}"
        shutil.copy2(src, dst)
        saved.append(dst)
    return saved


def save_3d_from_url(session: Path, fmt: str, url: str) -> Path:
    path = session / "3d" / f"model.{fmt}"
    r = requests.get(url, timeout=300)
    r.raise_for_status()
    path.write_bytes(r.content)
    return path


def update_metadata(session: Path, **patch) -> None:
    path = session / "metadata.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for k, v in patch.items():
        if k == "append_iteration":
            data["iterations"].append(v)
        elif k == "add_credits":
            for bucket, amount in v.items():
                data["credits"][bucket] = data["credits"].get(bucket, 0) + amount
        else:
            data[k] = v
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
