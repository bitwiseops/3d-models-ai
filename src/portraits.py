"""Pagina Portraits: per ogni portrait genera in parallelo DUE outputs
complementari della stessa persona, partendo da una reference image + prompt:

- **multi-view**: 3 viste separate via Meshy `generate_multi_view=True`
  (ordine deciso server-side).
- **grid 2x2**: 1 immagine quadrata con 4 celle (front / 3-4 / side / back),
  poi ritagliata in 4 file separati lato Python.

Totale fino a 7 immagini per portrait. Output isolato in `outputs/portraits/`.
One-shot: niente loop di feedback.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import requests

from . import config, grid_utils, meshy_client, prompt_refiner, storage

PORTRAIT_ROOT = config.OUTPUTS_ROOT / "portraits"

# Lock per-portrait analogo a storage._session_locks (in-process).
_portrait_locks: dict[Path, threading.RLock] = {}
_locks_guard = threading.Lock()


def _get_lock(portrait_dir: Path) -> threading.RLock:
    key = portrait_dir.resolve()
    with _locks_guard:
        lock = _portrait_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _portrait_locks[key] = lock
        return lock


def _data_uri(path: Path) -> str:
    raw = path.read_bytes()
    media = prompt_refiner._detect_media_type(raw)
    data = base64.b64encode(raw).decode("ascii")
    return f"data:{media};base64,{data}"


def create_portrait_session(user_prompt: str) -> Path:
    """Crea la portrait dir + metadata skeleton. Sincrona (~10ms).

    Genererà SEMPRE entrambe le modalità in parallelo (multi-view 3 + grid 2x2
    croppato in 4). Niente scelta upfront.
    """
    PORTRAIT_ROOT.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    pdir = PORTRAIT_ROOT / f"{ts}-{storage._slugify(user_prompt)}"
    (pdir / "prompts").mkdir(parents=True, exist_ok=True)
    (pdir / "images").mkdir(parents=True, exist_ok=True)
    (pdir / "prompts" / "00_user.txt").write_text(user_prompt, encoding="utf-8")
    meta = {
        "created_at": ts,
        "user_prompt": user_prompt,
        "refined_prompt_multi": None,
        "refined_prompt_grid": None,
        "reference": None,
        "image_paths_multi": [],
        "image_paths_grid": [],
        "image_task_ids": [],
        "credits": 0,
        "status": "draft",  # draft → running → done | failed
        "error": None,
        "error_multi": None,
        "error_grid": None,
    }
    _write_meta(pdir, meta)
    return pdir


def save_reference(portrait_dir: Path, src_path: Path) -> Path:
    """Copia la reference (file caricato dall'utente) in `reference<ext>`."""
    ext = src_path.suffix.lower() or ".png"
    dst = portrait_dir / f"reference{ext}"
    shutil.copy2(src_path, dst)
    update_metadata(portrait_dir, reference=dst.name)
    return dst


_crop_grid_2x2 = grid_utils.crop_grid_2x2


def _run_multi_view(
    portrait_dir: Path,
    user_prompt: str,
    reference: Path,
    ref_uri: str,
    *,
    on_progress,
    api_key: str | None,
) -> tuple[list[Path], str, int, str]:
    """Branch A: Meshy native multi-view → 3 immagini separate."""
    refined = prompt_refiner.refine_portrait(user_prompt, reference)
    (portrait_dir / "prompts" / "01_refined_multi.txt").write_text(refined, encoding="utf-8")
    update_metadata(portrait_dir, refined_prompt_multi=refined)
    print(f"[portraits] [multi] Meshy image-to-image (native multi-view)…")
    task_id = meshy_client.image_to_image(
        refined,
        reference_image_urls=[ref_uri],
        generate_multi_view=True,
        api_key=api_key,
    )
    task = meshy_client.poll(
        "/image-to-image",
        task_id,
        label="portrait-multi",
        max_wait=600,
        on_progress=on_progress,
        api_key=api_key,
    )
    image_urls = task.get("image_urls") or []
    if not image_urls:
        raise RuntimeError("Meshy (multi-view) non ha ritornato image_urls.")
    saved: list[Path] = []
    for i, url in enumerate(image_urls, start=1):
        img_path = portrait_dir / "images" / f"multi_view_{i}.png"
        r = requests.get(url, timeout=120)
        r.raise_for_status()
        img_path.write_bytes(r.content)
        saved.append(img_path)
    return saved, refined, task.get("consumed_credits", 0), task_id


def _run_grid(
    portrait_dir: Path,
    user_prompt: str,
    reference: Path,
    ref_uri: str,
    *,
    on_progress,
    api_key: str | None,
) -> tuple[list[Path], str, int, str]:
    """Branch B: prompt 2x2 grid → 1 immagine quadrata + 4 crop."""
    refined = prompt_refiner.refine_portrait_grid(user_prompt, reference)
    (portrait_dir / "prompts" / "01_refined_grid.txt").write_text(refined, encoding="utf-8")
    update_metadata(portrait_dir, refined_prompt_grid=refined)
    print(f"[portraits] [grid] Meshy image-to-image (2x2 grid prompt)…")
    task_id = meshy_client.image_to_image(
        refined,
        reference_image_urls=[ref_uri],
        generate_multi_view=False,
        api_key=api_key,
    )
    task = meshy_client.poll(
        "/image-to-image",
        task_id,
        label="portrait-grid",
        max_wait=600,
        on_progress=on_progress,
        api_key=api_key,
    )
    image_urls = task.get("image_urls") or []
    if not image_urls:
        raise RuntimeError("Meshy (grid) non ha ritornato image_urls.")
    grid_path = portrait_dir / "images" / "grid.png"
    r = requests.get(image_urls[0], timeout=120)
    r.raise_for_status()
    grid_path.write_bytes(r.content)
    crops = _crop_grid_2x2(grid_path)
    return crops, refined, task.get("consumed_credits", 0), task_id


def generate_portrait(
    portrait_dir: Path,
    *,
    on_progress=None,
    api_key: str | None = None,
) -> dict[str, list[Path]]:
    """Genera in PARALLELO multi-view (3) e grid 2x2 (4 crop) via Meshy
    `nano-banana-pro` image-to-image. Se un branch fallisce, l'altro completa
    comunque e lo status finale è `done` (con `error_<branch>` popolato);
    `failed` solo se entrambi falliscono.
    """
    meta = load_metadata(portrait_dir)
    if not meta.get("reference"):
        raise RuntimeError("Portrait senza reference: aggiungila prima.")
    reference = portrait_dir / meta["reference"]
    user_prompt = meta["user_prompt"]

    print(f"\n[portraits] raffinamento + generazione (multi + grid in parallelo): {portrait_dir.name}")
    update_metadata(portrait_dir, status="running")

    ref_uri = _data_uri(reference)

    with ThreadPoolExecutor(max_workers=2) as ex:
        fut_multi = ex.submit(
            _run_multi_view, portrait_dir, user_prompt, reference, ref_uri,
            on_progress=on_progress, api_key=api_key,
        )
        fut_grid = ex.submit(
            _run_grid, portrait_dir, user_prompt, reference, ref_uri,
            on_progress=on_progress, api_key=api_key,
        )

    saved: dict[str, list[Path]] = {"multi": [], "grid": []}
    task_ids: list[str] = []
    credits = 0
    err_multi: str | None = None
    err_grid: str | None = None

    try:
        multi_paths, _, c_multi, tid_multi = fut_multi.result()
        saved["multi"] = multi_paths
        task_ids.append(tid_multi)
        credits += c_multi
    except Exception as e:
        err_multi = f"{type(e).__name__}: {e}"
        print(f"[portraits] [multi] FAILED: {err_multi}")

    try:
        grid_paths, _, c_grid, tid_grid = fut_grid.result()
        saved["grid"] = grid_paths
        task_ids.append(tid_grid)
        credits += c_grid
    except Exception as e:
        err_grid = f"{type(e).__name__}: {e}"
        print(f"[portraits] [grid] FAILED: {err_grid}")

    any_ok = bool(saved["multi"] or saved["grid"])
    status = "done" if any_ok else "failed"
    error = None if any_ok else (err_multi or err_grid)

    update_metadata(
        portrait_dir,
        image_paths_multi=[str(p.relative_to(portrait_dir)) for p in saved["multi"]],
        image_paths_grid=[str(p.relative_to(portrait_dir)) for p in saved["grid"]],
        image_task_ids=task_ids,
        credits=credits,
        status=status,
        error=error,
        error_multi=err_multi,
        error_grid=err_grid,
    )
    print(
        f"[portraits] salvate multi={len(saved['multi'])} grid={len(saved['grid'])} "
        f"in {portrait_dir / 'images'} (credits: {credits}, status: {status})"
    )
    return saved


def list_portraits() -> list[dict]:
    """Lista portrait ordinata per timestamp desc, per la index page."""
    if not PORTRAIT_ROOT.exists():
        return []
    out: list[dict] = []
    for d in PORTRAIT_ROOT.iterdir():
        if not d.is_dir() or not (d / "metadata.json").exists():
            continue
        try:
            meta = load_metadata(d)
        except Exception:
            continue
        # Thumb: prima multi-view, poi prima grid-crop, poi legacy
        # `image_paths`, infine reference.
        thumb_candidates = (
            (meta.get("image_paths_multi") or [])
            + (meta.get("image_paths_grid") or [])
            + (meta.get("image_paths") or [])
        )
        thumb = thumb_candidates[0] if thumb_candidates else meta.get("reference")
        out.append(
            {
                "id": d.name,
                "user_prompt": meta.get("user_prompt", ""),
                "created_at": meta.get("created_at", ""),
                "status": meta.get("status", "draft"),
                "thumb": thumb,
            }
        )
    out.sort(key=lambda x: x["created_at"], reverse=True)
    return out


def resolve_portrait(portrait_id: str) -> Path:
    """Risolvi un portrait id in path, con check di traversal."""
    if "/" in portrait_id or ".." in portrait_id or portrait_id.startswith("."):
        raise ValueError("portrait_id non valido")
    p = (PORTRAIT_ROOT / portrait_id).resolve()
    p.relative_to(PORTRAIT_ROOT.resolve())  # raise se fuori
    if not p.exists() or not (p / "metadata.json").exists():
        raise FileNotFoundError(portrait_id)
    return p


# --- metadata I/O (atomico + thread-safe, stesso pattern di storage.update_metadata) ---


def _write_meta(portrait_dir: Path, data: dict) -> None:
    path = portrait_dir / "metadata.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def load_metadata(portrait_dir: Path) -> dict:
    with _get_lock(portrait_dir):
        return json.loads(
            (portrait_dir / "metadata.json").read_text(encoding="utf-8")
        )


def update_metadata(portrait_dir: Path, **patch) -> None:
    with _get_lock(portrait_dir):
        data = json.loads(
            (portrait_dir / "metadata.json").read_text(encoding="utf-8")
        )
        data.update(patch)
        _write_meta(portrait_dir, data)
