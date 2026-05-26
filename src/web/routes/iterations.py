"""Endpoints iterazioni: feedback (rigenerazione image), accept (3D), to_scad."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ... import scad, storage, workflow
from ..deps import get_job_manager, resolve_session
from ..jobs import JobManager

router = APIRouter(prefix="/sessions/{session_id}")


@router.post("/feedback", response_class=HTMLResponse)
async def post_feedback(
    session_id: str,
    request: Request,
    feedback: str = Form(...),
    use_previous_image: str | None = Form(default=None),
    session: Path = Depends(resolve_session),
    jm: JobManager = Depends(get_job_manager),
) -> HTMLResponse:
    """Sottomette un nuovo round di image generation con feedback.

    `use_previous_image`: checkbox HTML — presente=on (sempre la stringa "1"),
    assente=None. Mappiamo a bool.
    """
    feedback = feedback.strip()
    if not feedback:
        raise HTTPException(status_code=400, detail="Feedback vuoto")
    # Evita doppia sottomissione se c'è già un job attivo
    if jm.active_for_session(session_id) is not None:
        raise HTTPException(status_code=409, detail="Job già in corso per questa sessione")

    api_key = request.app.state.meshy_api_key
    use_prev = use_previous_image is not None

    def task() -> str:
        path = workflow.continue_with_feedback(
            session, feedback, api_key=api_key, skip_open=True,
            use_previous_image=use_prev,
        )
        return str(path)

    job_id = jm.submit(session_id, "feedback", task)

    templates = request.app.state.templates
    rec = jm.get(job_id)
    return templates.TemplateResponse(
        request,
        "_job_progress.html",
        {"session_id": session_id, "job": rec.snapshot()},
    )


@router.post("/accept", response_class=HTMLResponse)
async def post_accept(
    session_id: str,
    request: Request,
    session: Path = Depends(resolve_session),
    jm: JobManager = Depends(get_job_manager),
) -> HTMLResponse:
    """Accetta l'ultima iterazione e lancia la pipeline 3D completa."""
    if jm.active_for_session(session_id) is not None:
        raise HTTPException(status_code=409, detail="Job già in corso per questa sessione")

    api_key = request.app.state.meshy_api_key

    def task() -> dict:
        return workflow.accept_and_generate_3d(session, api_key=api_key)

    job_id = jm.submit(session_id, "accept", task)

    templates = request.app.state.templates
    rec = jm.get(job_id)
    return templates.TemplateResponse(
        request,
        "_job_progress.html",
        {"session_id": session_id, "job": rec.snapshot()},
    )


def _pick_reference_path(session_dir: Path, it: dict) -> Path | None:
    """Sceglie una vista singola dell'iterazione come reference per SCAD.

    Preferenza: quadrante three_quarter > front > altro quadrante > clean > image.
    Ritorna path assoluto o None se nessuna immagine disponibile.
    """
    grid = it.get("image_paths_grid") or []
    by_view: dict[str, str] = {}
    for rel in grid:
        # path es. "images/01_grid_three_quarter.png" → view='three_quarter'
        stem = Path(rel).stem
        view = stem.rsplit("_", 1)[-1] if "_" in stem else stem
        by_view[view] = rel

    for preferred in ("three_quarter", "front", "side", "back"):
        if preferred in by_view:
            return session_dir / by_view[preferred]

    if grid:
        return session_dir / grid[0]

    fallback = it.get("clean_path") or it.get("image_path")
    if fallback:
        return session_dir / fallback
    return None


@router.post("/iterations/{version}/to_scad")
async def iteration_to_scad(
    session_id: str,
    version: int,
    request: Request,
    session: Path = Depends(resolve_session),
    jm: JobManager = Depends(get_job_manager),
) -> RedirectResponse:
    """Crea una nuova sessione SCAD a partire da una iterazione Meshy.

    Riusa una vista singola del grid come reference; l'idea è suffissata
    "Decomposizione SCAD: <idea originale>". Lancia il job e redirige al
    detail SCAD.
    """
    meta = storage.load_metadata(session)
    iterations = meta.get("iterations") or []
    if version < 1 or version > len(iterations):
        raise HTTPException(status_code=404, detail="iterazione non trovata")
    it = iterations[version - 1]

    ref_abs = _pick_reference_path(session, it)
    if ref_abs is None or not ref_abs.exists():
        raise HTTPException(status_code=400, detail="nessuna immagine disponibile per questa iterazione")

    src_idea = (meta.get("idea") or "").strip() or "oggetto"
    scad_idea = f"Decomposizione SCAD: {src_idea}"

    scad_dir = scad.create_scad_session(scad_idea)
    scad.save_reference(scad_dir, ref_abs)
    scad_id = scad_dir.name

    def task() -> str:
        path = scad.generate_initial(scad_dir)
        return str(path)

    jm.submit(scad_id, "scad", task)
    return RedirectResponse(
        url=f"{request.scope.get('root_path', '')}/scad/{scad_id}", status_code=303,
    )
