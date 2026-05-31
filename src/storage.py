import json
import os
import re
import shutil
import threading
import unicodedata
from datetime import datetime
from pathlib import Path

import requests

from . import config

# Per-session reentrant lock per serializzare read-modify-write su metadata.json.
# RLock (non asyncio.Lock) perché update_metadata è chiamato sia da CLI sync
# che da worker thread del JobManager (entrambi sync context).
_session_locks: dict[Path, threading.RLock] = {}
_locks_guard = threading.Lock()


def _get_lock(session: Path) -> threading.RLock:
    """Lazy creation di un lock per-sessione. Thread-safe self."""
    key = session.resolve()
    with _locks_guard:
        lock = _session_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _session_locks[key] = lock
        return lock


# File intermedi 3D cancellabili da `cleanup_session`: pesanti e non serviti
# dalla UI Objects (viewer = model.glb, download = model_printable.*). Sono
# invece usati dai re-run CLI (decimate/flatten) e dai download Portraits,
# quindi la cancellazione è on-demand — mai automatica nella pipeline.
INTERMEDIATE_3D_FILES = (
    "3d/model.stl",
    "3d/model_repaired.stl",
    "3d/model_repaired.3mf",
    "3d/model_lowpoly.stl",
    "3d/model_lowpoly.3mf",
    "3d/model_meshy_repaired.glb",
    "3d/model_meshy_repaired.stl",
    "3d/model_repaired.glb",  # legacy: output Meshy /print/repair pre-rename
)


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


def save_3d_from_url(session: Path, fmt: str, url: str, *, filename: str | None = None) -> Path:
    path = session / "3d" / (filename or f"model.{fmt}")
    r = requests.get(url, timeout=300)
    r.raise_for_status()
    path.write_bytes(r.content)
    return path


def update_metadata(session: Path, **patch) -> None:
    """Patch read-modify-write su metadata.json, atomico e thread-safe.

    Atomicità: scrive prima su `metadata.json.tmp` e poi `os.replace()` →
    su POSIX e Windows è una rename atomica, quindi il file non viene mai
    visto in stato parziale anche se il processo crasha mid-write.

    Thread-safety: tutto il read-modify-write sotto _get_lock(session).
    """
    path = session / "metadata.json"
    tmp = path.with_suffix(".json.tmp")
    with _get_lock(session):
        data = json.loads(path.read_text(encoding="utf-8"))
        for k, v in patch.items():
            if k == "append_iteration":
                data["iterations"].append(v)
            elif k == "add_credits":
                for bucket, amount in v.items():
                    data["credits"][bucket] = data["credits"].get(bucket, 0) + amount
            elif k == "add_claude_usage":
                cu = data.setdefault("claude_usage", {"by_model": {}})
                bm = cu.setdefault("by_model", {})
                m = bm.setdefault(v["model"], {"input_tokens": 0, "output_tokens": 0, "calls": 0})
                m["input_tokens"] += int(v.get("input_tokens", 0))
                m["output_tokens"] += int(v.get("output_tokens", 0))
                m["calls"] += 1
            else:
                data[k] = v
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)


def load_metadata(session: Path) -> dict:
    """Legge metadata.json sotto lock (consistent con writer concorrenti)."""
    with _get_lock(session):
        return json.loads((session / "metadata.json").read_text(encoding="utf-8"))


def cleanup_session(session: Path, *, apply: bool = False) -> dict:
    """Elenca (o cancella, se apply=True) gli intermedi 3D pesanti di una
    sessione, tenendo `model.glb` (preview viewer) e `model_printable.{stl,3mf}`
    (download finali) + images/references/prompts/metadata.

    Guard: agisce solo se la pipeline è completa (`3d/model_printable.stl`
    esiste), altrimenti ritorna `skipped=True` senza toccare nulla — protegge
    sessioni image-only o pipeline parziali.

    Ritorna `{"skipped": bool, "files": [(rel, bytes)...], "total_bytes": int}`.
    Con `apply=True` cancella i file e pota i path stale da metadata.json.
    """
    if not (session / "3d" / "model_printable.stl").exists():
        return {"skipped": True, "files": [], "total_bytes": 0}

    found: list[tuple[str, int]] = []
    for rel in INTERMEDIATE_3D_FILES:
        p = session / rel
        if p.exists():
            found.append((rel, p.stat().st_size))
    total = sum(sz for _, sz in found)

    if apply and found:
        for rel, _ in found:
            (session / rel).unlink()
        # Pota i riferimenti diventati stale (la UI Objects non li usa).
        meta = load_metadata(session)
        model_files = {
            k: v for k, v in (meta.get("model_files") or {}).items() if k != "stl"
        }
        update_metadata(
            session,
            model_files=model_files,
            repaired_files={},
            lowpoly_files={},
            meshy_repaired_files={},
        )

    return {"skipped": False, "files": found, "total_bytes": total}
