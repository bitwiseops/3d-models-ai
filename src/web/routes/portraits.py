"""Endpoints pagina Portraits: lista, creazione, dettaglio, file serving."""
from __future__ import annotations

import io
import tempfile
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse

from ... import portraits
from ..deps import get_job_manager
from ..jobs import JobManager
from ..list_utils import group_by_day, parse_pagination, slice_with_more

router = APIRouter(prefix="/portraits")

_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def _resolve_portrait(portrait_id: str) -> Path:
    try:
        return portraits.resolve_portrait(portrait_id)
    except (ValueError, FileNotFoundError):
        raise HTTPException(status_code=404, detail="portrait non trovato")


def _portraits_page_context(offset: int, limit: int) -> dict:
    offset, limit = parse_pagination(offset, limit)
    all_items = portraits.list_portraits()
    page, has_more, next_offset = slice_with_more(all_items, offset, limit)
    return {
        "portraits": page,
        "groups": group_by_day(page),
        "has_more": has_more,
        "next_offset": next_offset,
        "limit": limit,
    }


@router.get("", response_class=HTMLResponse)
async def portraits_index(
    request: Request, offset: int = 0, limit: int = 20,
) -> HTMLResponse:
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "portraits_index.html", _portraits_page_context(offset, limit),
    )


@router.get("/list", response_class=HTMLResponse)
async def portraits_list_fragment(
    request: Request, offset: int = 0, limit: int = 20,
) -> HTMLResponse:
    """Fragment lista per HTMX auto-refresh."""
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "_portraits_list.html", _portraits_page_context(offset, limit),
    )


@router.post("")
async def create_portrait(
    request: Request,
    prompt: str = Form(default=""),
    reference: UploadFile = File(...),
    jm: JobManager = Depends(get_job_manager),
) -> RedirectResponse:
    prompt = prompt.strip()
    if not reference.filename:
        raise HTTPException(status_code=400, detail="Reference mancante")

    # Salva l'upload in un tmp, poi copialo nella portrait dir.
    suffix = Path(reference.filename).suffix.lower() or ".png"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await reference.read())
        tmp_path = Path(tmp.name)

    portrait_dir = portraits.create_portrait_session(prompt)
    portraits.save_reference(portrait_dir, tmp_path)
    tmp_path.unlink(missing_ok=True)

    portrait_id = portrait_dir.name
    api_key = request.app.state.meshy_api_key

    def task() -> str:
        path = portraits.generate_portrait(portrait_dir, api_key=api_key)
        return str(path)

    jm.submit(portrait_id, "portrait", task)
    return RedirectResponse(url=f"{request.scope.get('root_path', '')}/portraits/{portrait_id}", status_code=303)


@router.post("/{portrait_id}/regen")
async def regen_portrait(
    portrait_id: str,
    request: Request,
    prompt: str = Form(default=""),
    jm: JobManager = Depends(get_job_manager),
) -> RedirectResponse:
    """Crea un nuovo portrait collegato (parent_id) usando la reference del
    sorgente. Il prompt può essere modificato; se vuoto eredita quello sorgente."""
    src_dir = _resolve_portrait(portrait_id)
    src_meta = portraits.load_metadata(src_dir)
    src_prompt = (src_meta.get("user_prompt") or "").strip()
    new_prompt = (prompt or "").strip() or src_prompt

    # Crea il nuovo portrait
    new_dir = portraits.create_portrait_session(new_prompt)
    # Copia la reference del sorgente (se presente)
    src_ref_rel = src_meta.get("reference")
    if src_ref_rel:
        src_ref = src_dir / src_ref_rel
        if src_ref.exists():
            portraits.save_reference(new_dir, src_ref)
    # Marca parent
    portraits.update_metadata(
        new_dir,
        parent_id=portrait_id,
        parent_prompt=src_prompt,
    )

    new_id = new_dir.name
    api_key = request.app.state.meshy_api_key

    def task() -> str:
        path = portraits.generate_portrait(new_dir, api_key=api_key)
        return str(path)

    jm.submit(new_id, "portrait", task)
    return RedirectResponse(
        url=f"{request.scope.get('root_path', '')}/portraits/{new_id}",
        status_code=303,
    )


@router.post("/{portrait_id}/generate_bust_3d")
async def generate_portrait_bust(
    portrait_id: str,
    request: Request,
    selected_images: list[str] = Form(default=[]),
    enable_pbr: str | None = Form(default=None),         # checkbox: "1" se on
    target_polycount: int = Form(default=300_000),
    symmetry_mode: str = Form(default="auto"),           # "off"|"auto"|"on"
    crop_top_pct: int = Form(default=55),                # 0 = no crop, sennò % (1-100)
    jm: JobManager = Depends(get_job_manager),
) -> RedirectResponse:
    """Lancia generazione 3D del busto a partire da N (1-4) immagini
    selezionate. Ogni run salvata in `3d/run_NN/`."""
    portrait_dir = _resolve_portrait(portrait_id)
    cleaned: list[str] = []
    for rel in selected_images:
        if not rel or ".." in rel or rel.startswith("/"):
            raise HTTPException(status_code=400, detail=f"path non valido: {rel}")
        if not rel.startswith("images/"):
            raise HTTPException(status_code=400, detail=f"path fuori da images/: {rel}")
        full = (portrait_dir / rel).resolve()
        try:
            full.relative_to(portrait_dir.resolve())
        except ValueError:
            raise HTTPException(status_code=400, detail=f"path fuori dalla sessione: {rel}")
        if not full.exists():
            raise HTTPException(status_code=400, detail=f"immagine inesistente: {rel}")
        cleaned.append(rel)
    if not 1 <= len(cleaned) <= 4:
        raise HTTPException(status_code=400, detail="seleziona da 1 a 4 immagini")
    if jm.active_for_session(portrait_id) is not None:
        raise HTTPException(status_code=409, detail="Job già in corso per questa sessione")

    if target_polycount not in (50_000, 100_000, 300_000):
        raise HTTPException(status_code=400, detail="polycount non supportato")
    if symmetry_mode not in ("off", "auto", "on"):
        raise HTTPException(status_code=400, detail="symmetry_mode non valido")
    if not 0 <= crop_top_pct <= 100:
        raise HTTPException(status_code=400, detail="crop_top_pct deve essere 0-100")

    pbr_on = enable_pbr is not None
    crop_pct = None if crop_top_pct == 0 else crop_top_pct / 100.0

    from datetime import datetime
    run_id = portraits.next_bust_run_id(portrait_dir)
    portraits.update_metadata(
        portrait_dir,
        append_bust_run={
            "run_id": run_id,
            "status": "queued",
            "selected_images": cleaned,
            "created_at": datetime.now().strftime("%Y%m%d-%H%M%S"),
            "task_id": None,
            "credits": 0,
            "model_files": {},
            "repaired_files": {},
            "lowpoly_files": {},
            "bust_files": {},
            "options": {
                "enable_pbr": pbr_on,
                "target_polycount": target_polycount,
                "symmetry_mode": symmetry_mode,
                "crop_top_pct": crop_pct,
            },
            "error": None,
        },
    )

    api_key = request.app.state.meshy_api_key

    def task() -> dict:
        try:
            return portraits.generate_bust_3d(
                portrait_dir, run_id, cleaned,
                enable_pbr=pbr_on,
                target_polycount=target_polycount,
                symmetry_mode=symmetry_mode,
                crop_top_pct=crop_pct,
                api_key=api_key,
            )
        except Exception as e:
            portraits.update_metadata(
                portrait_dir,
                patch_bust_run={"run_id": run_id, "status": "failed", "error": str(e)},
            )
            raise

    jm.submit(portrait_id, "portrait_bust_3d", task)
    return RedirectResponse(
        url=f"{request.scope.get('root_path', '')}/portraits/{portrait_id}",
        status_code=303,
    )


@router.get("/{portrait_id}", response_class=HTMLResponse)
async def portrait_detail(
    portrait_id: str,
    request: Request,
    jm: JobManager = Depends(get_job_manager),
) -> HTMLResponse:
    portrait_dir = _resolve_portrait(portrait_id)
    templates = request.app.state.templates
    meta = portraits.load_metadata(portrait_dir)
    active_job = jm.active_for_session(portrait_id)
    return templates.TemplateResponse(
        request,
        "portrait_detail.html",
        {
            "portrait_id": portrait_id,
            "meta": meta,
            "active_job": active_job.snapshot() if active_job else None,
        },
    )


@router.get("/{portrait_id}/fragment", response_class=HTMLResponse)
async def portrait_fragment(
    portrait_id: str,
    request: Request,
    jm: JobManager = Depends(get_job_manager),
) -> HTMLResponse:
    """Fragment con result + reference, ricaricato a fine job via SSE."""
    portrait_dir = _resolve_portrait(portrait_id)
    templates = request.app.state.templates
    meta = portraits.load_metadata(portrait_dir)
    active_job = jm.active_for_session(portrait_id)
    return templates.TemplateResponse(
        request,
        "_portrait_full.html",
        {
            "portrait_id": portrait_id,
            "meta": meta,
            "active_job": active_job.snapshot() if active_job else None,
        },
    )


@router.get("/{portrait_id}/download.zip")
async def download_portrait_zip(portrait_id: str) -> StreamingResponse:
    """Zip in memoria con tutte le viste generate (multi + grid + reference)."""
    pdir = _resolve_portrait(portrait_id)
    meta = portraits.load_metadata(pdir)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for key in ("image_paths_multi", "image_paths_grid", "image_paths"):
            for rel in meta.get(key) or []:
                p = pdir / rel
                if p.exists():
                    zf.write(p, arcname=p.name)
        # Reference originale
        if meta.get("reference"):
            ref = pdir / meta["reference"]
            if ref.exists():
                zf.write(ref, arcname=f"reference{ref.suffix}")
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{portrait_id}.zip"',
        },
    )


@router.get("/{portrait_id}/files/{path:path}")
async def serve_portrait_file(portrait_id: str, path: str) -> FileResponse:
    portrait_dir = _resolve_portrait(portrait_id)
    if path.startswith("/") or ".." in Path(path).parts:
        raise HTTPException(status_code=400, detail="path non valido")
    full = (portrait_dir / path).resolve()
    try:
        full.relative_to(portrait_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="path fuori dal portrait")
    if not full.exists() or not full.is_file():
        raise HTTPException(status_code=404, detail="file non trovato")
    return FileResponse(str(full), media_type=_MEDIA_TYPES.get(full.suffix.lower()))
