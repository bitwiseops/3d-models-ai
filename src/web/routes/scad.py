"""Endpoint pagina SCAD: Claude + OpenSCAD locale."""
from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from ... import scad
from ..deps import get_job_manager
from ..jobs import JobManager
from ..list_utils import group_by_day, parse_pagination, slice_with_more

router = APIRouter(prefix="/scad")

_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".stl": "model/stl",
    ".glb": "model/gltf-binary",
    ".3mf": "model/3mf",
    ".scad": "text/plain",
    ".hdr": "image/vnd.radiance",
}


def _resolve_scad(scad_id: str) -> Path:
    try:
        return scad.resolve_scad(scad_id)
    except (ValueError, FileNotFoundError):
        raise HTTPException(status_code=404, detail="sessione SCAD non trovata")


def _scad_page_context(offset: int = 0, limit: int = 20) -> dict:
    offset, limit = parse_pagination(offset, limit)
    all_items = scad.list_scad_sessions()
    page, has_more, next_offset = slice_with_more(all_items, offset, limit)
    return {
        "scads": page,
        "groups": group_by_day(page),
        "has_more": has_more,
        "next_offset": next_offset,
        "limit": limit,
    }


@router.get("", response_class=HTMLResponse)
async def scad_index(
    request: Request, offset: int = 0, limit: int = 20,
) -> HTMLResponse:
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "scad_index.html", _scad_page_context(offset, limit),
    )


@router.get("/list", response_class=HTMLResponse)
async def scad_list_fragment(
    request: Request, offset: int = 0, limit: int = 20,
) -> HTMLResponse:
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "_scad_list.html", _scad_page_context(offset, limit),
    )


@router.post("")
async def create_scad(
    request: Request,
    idea: str = Form(...),
    reference: UploadFile | None = File(default=None),
    jm: JobManager = Depends(get_job_manager),
) -> RedirectResponse:
    idea = idea.strip()
    if not idea:
        raise HTTPException(status_code=400, detail="Idea vuota")

    scad_dir = scad.create_scad_session(idea)
    if reference and reference.filename:
        suffix = Path(reference.filename).suffix.lower() or ".png"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(await reference.read())
            tmp_path = Path(tmp.name)
        scad.save_reference(scad_dir, tmp_path)
        tmp_path.unlink(missing_ok=True)

    scad_id = scad_dir.name

    def task() -> str:
        path = scad.generate_initial(scad_dir)
        return str(path)

    jm.submit(scad_id, "scad", task)
    return RedirectResponse(
        url=f"{request.scope.get('root_path', '')}/scad/{scad_id}", status_code=303,
    )


def _load_inline_code(scad_dir: Path, meta: dict) -> dict[int, dict[str, str]]:
    """Per ogni iterazione, mappa part_name → testo .scad. Iterazioni legacy
    (con `scad_path` top-level) usano la chiave `__legacy__`."""
    out: dict[int, dict[str, str]] = {}
    for it in meta.get("iterations", []):
        ver = it["version"]
        out[ver] = {}
        if it.get("parts"):
            for p in it["parts"]:
                sp = p.get("scad_path")
                if not sp:
                    continue
                try:
                    out[ver][p["name"]] = (scad_dir / sp).read_text(encoding="utf-8")
                except OSError:
                    pass
            # Aggiunge shared.scad come voce speciale per leggibilità.
            if it.get("shared_scad_path"):
                try:
                    out[ver]["__shared__"] = (scad_dir / it["shared_scad_path"]).read_text(encoding="utf-8")
                except OSError:
                    pass
        elif it.get("scad_path"):
            try:
                out[ver]["__legacy__"] = (scad_dir / it["scad_path"]).read_text(encoding="utf-8")
            except OSError:
                pass
    return out


@router.get("/{scad_id}", response_class=HTMLResponse)
async def scad_detail(
    scad_id: str,
    request: Request,
    jm: JobManager = Depends(get_job_manager),
) -> HTMLResponse:
    scad_dir = _resolve_scad(scad_id)
    templates = request.app.state.templates
    meta = scad.load_metadata(scad_dir)
    active_job = jm.active_for_session(scad_id)
    return templates.TemplateResponse(
        request,
        "scad_detail.html",
        {
            "scad_id": scad_id,
            "meta": meta,
            "code_by_version": _load_inline_code(scad_dir, meta),
            "active_job": active_job.snapshot() if active_job else None,
        },
    )


@router.get("/{scad_id}/fragment", response_class=HTMLResponse)
async def scad_fragment(
    scad_id: str,
    request: Request,
    jm: JobManager = Depends(get_job_manager),
) -> HTMLResponse:
    scad_dir = _resolve_scad(scad_id)
    templates = request.app.state.templates
    meta = scad.load_metadata(scad_dir)
    active_job = jm.active_for_session(scad_id)
    return templates.TemplateResponse(
        request,
        "_scad_iterations.html",
        {
            "scad_id": scad_id,
            "meta": meta,
            "code_by_version": _load_inline_code(scad_dir, meta),
            "active_job": active_job.snapshot() if active_job else None,
        },
    )


@router.post("/{scad_id}/feedback", response_class=HTMLResponse)
async def post_scad_feedback(
    scad_id: str,
    request: Request,
    feedback: str = Form(...),
    use_previous_render: str | None = Form(default=None),
    jm: JobManager = Depends(get_job_manager),
) -> HTMLResponse:
    scad_dir = _resolve_scad(scad_id)
    feedback = feedback.strip()
    if not feedback:
        raise HTTPException(status_code=400, detail="Feedback vuoto")
    if jm.active_for_session(scad_id) is not None:
        raise HTTPException(status_code=409, detail="Job già in corso")

    use_prev = use_previous_render is not None

    def task() -> str:
        path = scad.continue_with_feedback(
            scad_dir, feedback, use_previous_render=use_prev,
        )
        return str(path)

    job_id = jm.submit(scad_id, "scad", task)
    templates = request.app.state.templates
    rec = jm.get(job_id)
    return templates.TemplateResponse(
        request,
        "_scad_progress.html",
        {"scad_id": scad_id, "job": rec.snapshot()},
    )


@router.post("/{scad_id}/feedback_part", response_class=HTMLResponse)
async def post_scad_feedback_part(
    scad_id: str,
    request: Request,
    part_name: str = Form(...),
    feedback: str = Form(...),
    use_previous_render: str | None = Form(default=None),
    jm: JobManager = Depends(get_job_manager),
) -> HTMLResponse:
    scad_dir = _resolve_scad(scad_id)
    feedback = feedback.strip()
    part_name = part_name.strip()
    if not feedback or not part_name:
        raise HTTPException(status_code=400, detail="Feedback o parte mancanti")
    if jm.active_for_session(scad_id) is not None:
        raise HTTPException(status_code=409, detail="Job già in corso")

    use_prev = use_previous_render is not None

    def task() -> str:
        path = scad.continue_with_part_feedback(
            scad_dir, part_name, feedback, use_previous_render=use_prev,
        )
        return str(path)

    job_id = jm.submit(scad_id, "scad", task)
    templates = request.app.state.templates
    rec = jm.get(job_id)
    return templates.TemplateResponse(
        request,
        "_scad_progress.html",
        {"scad_id": scad_id, "job": rec.snapshot()},
    )


def _synthesize_warning_feedback(validation: dict) -> str:
    """Trasforma i warning della validazione in un feedback testuale per Claude."""
    bits = ["La validazione mesh segnala questi problemi sulla parte attuale:"]
    if not validation.get("is_manifold"):
        bits.append("- mesh non manifold (pymeshfix non è riuscito a ripararlo)")
    for w in validation.get("warnings") or []:
        bits.append(f"- {w}")
    bits.append("")
    bits.append(
        "Correggi il codice della parte mantenendo gli INCASTRI con le altre "
        "parti del montaggio (parametri condivisi in shared.scad: dimensioni di "
        "interfaccia, perni, fori, tolleranze). In particolare:"
    )
    bits.append("- NON cambiare i parametri usati per fit/clearance/peg/hole/socket.")
    bits.append("- NON ridurre le dimensioni esterne dichiarate in shared.scad.")
    bits.append(
        "- Aumenta SOLO gli spessori interni/decorativi (wall, rib, base_thickness) "
        "fino a portare il wall minimo ≥ 0.8 mm."
    )
    bits.append("- Se serve, allarga lievemente l'oggetto verso l'esterno, mai verso superfici di accoppiamento.")
    return "\n".join(bits)


@router.post("/{scad_id}/fix_part", response_class=HTMLResponse)
async def post_scad_fix_part(
    scad_id: str,
    request: Request,
    part_name: str = Form(...),
    jm: JobManager = Depends(get_job_manager),
) -> HTMLResponse:
    """Auto-fix dei warning di validazione per una parte: sintetizza un
    feedback Claude-friendly e lo passa al refine per-parte."""
    scad_dir = _resolve_scad(scad_id)
    if jm.active_for_session(scad_id) is not None:
        raise HTTPException(status_code=409, detail="Job già in corso")

    meta = scad.load_metadata(scad_dir)
    if not meta.get("iterations"):
        raise HTTPException(status_code=400, detail="Nessuna iterazione")
    prev = meta["iterations"][-1]
    part = next((p for p in prev.get("parts") or [] if p["name"] == part_name), None)
    if part is None:
        raise HTTPException(status_code=404, detail="Parte non trovata")
    validation = part.get("validation") or {}
    if not (validation.get("warnings") or not validation.get("is_manifold")):
        raise HTTPException(status_code=400, detail="Nessun avviso da correggere")

    feedback = _synthesize_warning_feedback(validation)

    def task() -> str:
        path = scad.continue_with_part_feedback(
            scad_dir, part_name, feedback, use_previous_render=True,
        )
        return str(path)

    job_id = jm.submit(scad_id, "scad", task)
    templates = request.app.state.templates
    rec = jm.get(job_id)
    return templates.TemplateResponse(
        request,
        "_scad_progress.html",
        {"scad_id": scad_id, "job": rec.snapshot()},
    )


@router.post("/{scad_id}/accept", response_class=RedirectResponse)
async def post_scad_accept(
    scad_id: str,
    request: Request,
) -> RedirectResponse:
    scad_dir = _resolve_scad(scad_id)
    scad.accept(scad_dir)
    return RedirectResponse(
        url=f"{request.scope.get('root_path', '')}/scad/{scad_id}", status_code=303,
    )


@router.get("/{scad_id}/files/{path:path}")
async def serve_scad_file(scad_id: str, path: str) -> FileResponse:
    scad_dir = _resolve_scad(scad_id)
    if path.startswith("/") or ".." in Path(path).parts:
        raise HTTPException(status_code=400, detail="path non valido")
    full = (scad_dir / path).resolve()
    try:
        full.relative_to(scad_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="path fuori dalla sessione")
    if not full.exists() or not full.is_file():
        raise HTTPException(status_code=404, detail="file non trovato")
    return FileResponse(str(full), media_type=_MEDIA_TYPES.get(full.suffix.lower()))
