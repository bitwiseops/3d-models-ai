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

from . import config, grid_utils, mesh_repair, meshy_client, prompt_refiner, storage, usage as usage_mod

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
    def _on_claude(model: str, inp: int, out: int) -> None:
        update_metadata(
            portrait_dir,
            add_claude_usage={"model": model, "input_tokens": inp, "output_tokens": out},
        )
    with usage_mod.session_scope(_on_claude):
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
    def _on_claude(model: str, inp: int, out: int) -> None:
        update_metadata(
            portrait_dir,
            add_claude_usage={"model": model, "input_tokens": inp, "output_tokens": out},
        )
    with usage_mod.session_scope(_on_claude):
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
                "cost_usd": usage_mod.session_cost_portrait(meta),
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
        cu_patch = patch.pop("add_claude_usage", None)
        if cu_patch is not None:
            cu = data.setdefault("claude_usage", {"by_model": {}})
            bm = cu.setdefault("by_model", {})
            m = bm.setdefault(cu_patch["model"], {"input_tokens": 0, "output_tokens": 0, "calls": 0})
            m["input_tokens"] += int(cu_patch.get("input_tokens", 0))
            m["output_tokens"] += int(cu_patch.get("output_tokens", 0))
            m["calls"] += 1
        bust_append = patch.pop("append_bust_run", None)
        if bust_append is not None:
            data.setdefault("bust_3d_runs", []).append(bust_append)
        bust_patch = patch.pop("patch_bust_run", None)
        if bust_patch is not None:
            run_id = bust_patch.pop("run_id")
            for r in data.get("bust_3d_runs", []):
                if r.get("run_id") == run_id:
                    r.update(bust_patch)
                    break
        data.update(patch)
        _write_meta(portrait_dir, data)


def next_bust_run_id(portrait_dir: Path) -> int:
    """Calcola il prossimo run_id (max esistente + 1, partendo da 1)."""
    with _get_lock(portrait_dir):
        data = json.loads(
            (portrait_dir / "metadata.json").read_text(encoding="utf-8")
        )
    runs = data.get("bust_3d_runs") or []
    return max((int(r.get("run_id", 0)) for r in runs), default=0) + 1


_BUST_TARGET_FORMATS = ("glb", "stl")


def generate_bust_3d(
    portrait_dir: Path,
    run_id: int,
    selected_rel_paths: list[str],
    *,
    enable_pbr: bool = False,
    target_polycount: int = 300_000,
    symmetry_mode: str | None = "auto",  # "off"|"auto"|"on"
    crop_top_pct: float | None = 0.55,    # None = niente crop
    on_progress=None,
    on_step=None,
    api_key: str | None = None,
) -> dict:
    """Genera un busto 3D a partire dalle immagini selezionate via Meshy
    multi-image-to-3d. Output salvato in `portrait_dir/3d/run_NN/`.

    Opzioni:
    - `enable_pbr=False` (default): geometria pulita, no texture. Per stampa.
    - `symmetry_mode="auto"` (default): Meshy decide; "on" forza simmetria
      verticale (utile per visi); "off" disabilita.
    - `crop_top_pct=0.55` (default): tiene il 55% superiore della bbox z →
      mezzo busto. None per non croppare.
    """
    if not 1 <= len(selected_rel_paths) <= 4:
        raise ValueError("selezione fuori range (1-4 immagini)")

    run_dir = portrait_dir / "3d" / f"run_{run_id:02d}"
    run_dir.mkdir(parents=True, exist_ok=True)

    abs_paths = []
    for rel in selected_rel_paths:
        p = (portrait_dir / rel).resolve()
        p.relative_to(portrait_dir.resolve())
        if not p.exists():
            raise FileNotFoundError(f"immagine selezionata mancante: {rel}")
        abs_paths.append(p)
    image_urls = [_data_uri(p) for p in abs_paths]

    update_metadata(
        portrait_dir,
        patch_bust_run={
            "run_id": run_id, "status": "running",
            "options": {
                "enable_pbr": enable_pbr,
                "target_polycount": target_polycount,
                "symmetry_mode": symmetry_mode,
                "crop_top_pct": crop_top_pct,
            },
        },
    )

    if on_step: on_step(
        f"Meshy multi-image-to-3d ({len(image_urls)} viste, "
        f"PBR={'on' if enable_pbr else 'off'}, poly={target_polycount//1000}k, "
        f"sym={symmetry_mode})…"
    )
    task_id = meshy_client.multi_image_to_3d(
        image_urls=image_urls,
        target_formats=_BUST_TARGET_FORMATS,
        enable_pbr=enable_pbr,
        target_polycount=target_polycount,
        symmetry_mode=symmetry_mode,
        api_key=api_key,
    )
    update_metadata(portrait_dir, patch_bust_run={"run_id": run_id, "task_id": task_id})

    task = meshy_client.poll(
        "/multi-image-to-3d",
        task_id,
        label=f"bust-3d-run-{run_id}",
        max_wait=1800,
        on_progress=on_progress,
        api_key=api_key,
    )

    model_urls = task.get("model_urls", {})
    saved: dict[str, str] = {}
    for fmt in _BUST_TARGET_FORMATS:
        url = model_urls.get(fmt)
        if not url:
            continue
        out_path = run_dir / f"model.{fmt}"
        import requests as _req
        r = _req.get(url, timeout=300)
        r.raise_for_status()
        out_path.write_bytes(r.content)
        saved[fmt] = str(out_path.relative_to(portrait_dir))

    repaired_rel: dict[str, str] = {}
    lowpoly_rel: dict[str, str] = {}
    bust_rel: dict[str, str] = {}
    src_stl = saved.get("stl") or saved.get("glb")
    if src_stl:
        try:
            if on_step: on_step("repair watertight…")
            repaired = mesh_repair.repair(
                portrait_dir / src_stl, run_dir, on_step=on_step,
            )
            repaired_rel = {k: str(v.relative_to(portrait_dir)) for k, v in repaired.items()}
            if "stl" in repaired:
                if on_step: on_step("decimazione lowpoly…")
                lowpoly = mesh_repair.decimate(
                    repaired["stl"], run_dir,
                    target_faces=mesh_repair.DEFAULT_TARGET_FACES,
                    on_step=on_step,
                )
                lowpoly_rel = {k: str(v.relative_to(portrait_dir)) for k, v in lowpoly.items()}

                if crop_top_pct is not None:
                    try:
                        if on_step: on_step(f"crop bust (top {int(crop_top_pct*100)}%)…")
                        bust = mesh_repair.crop_bust(
                            lowpoly["stl"], run_dir,
                            keep_top_pct=crop_top_pct, on_step=on_step,
                        )
                        bust_rel = {k: str(v.relative_to(portrait_dir)) for k, v in bust.items()}
                    except Exception as e:
                        print(f"[portraits] crop_bust fallito: {e}")
        except Exception as e:
            print(f"[portraits] bust run #{run_id} repair/decimate fallito: {e}")

    update_data = {
        "status": "done",
        "model_files": saved,
        "repaired_files": repaired_rel,
        "lowpoly_files": lowpoly_rel,
        "bust_files": bust_rel,
        "credits": int(task.get("consumed_credits", 0) or 0),
    }
    update_metadata(
        portrait_dir,
        patch_bust_run={"run_id": run_id, **update_data},
    )
    return update_data
