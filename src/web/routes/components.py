"""Endpoint scomposizione in componenti: breakdown post-accept, regen e approve.

Stadio post-accettazione della pipeline Oggetti: l'oggetto viene scomposto in
componenti (vedi workflow.breakdown_into_components), ognuno con un prompt
context-aware e una grid 2x2. L'output è il bundle components/components.json
letto poi dalla skill PC `fusion-build-from-components`.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from ... import storage, workflow
from ..deps import get_job_manager, resolve_session
from ..jobs import JobManager

router = APIRouter(prefix="/sessions/{session_id}")


def _load_components(session: Path) -> dict | None:
    try:
        return storage.load_components_manifest(session)
    except FileNotFoundError:
        return None


@router.post("/breakdown", response_class=HTMLResponse)
async def post_breakdown(
    session_id: str,
    request: Request,
    force: str | None = Form(default=None),
    session: Path = Depends(resolve_session),
    jm: JobManager = Depends(get_job_manager),
) -> HTMLResponse:
    """Avvia la scomposizione in componenti dell'oggetto accettato."""
    meta = storage.load_metadata(session)
    if meta.get("accepted_iteration") is None:
        raise HTTPException(status_code=400, detail="Sessione non ancora accettata")
    if jm.active_for_session(session_id) is not None:
        raise HTTPException(status_code=409, detail="Job già in corso per questa sessione")

    api_key = request.app.state.meshy_api_key
    do_force = force is not None

    def task() -> str:
        manifest = workflow.breakdown_into_components(
            session, api_key=api_key, skip_open=True, force=do_force
        )
        return f"{len(manifest.get('components') or [])} componenti"

    job_id = jm.submit(session_id, "breakdown", task)
    templates = request.app.state.templates
    rec = jm.get(job_id)
    return templates.TemplateResponse(
        request,
        "_job_progress.html",
        {"session_id": session_id, "job": rec.snapshot()},
    )


@router.get("/components", response_class=HTMLResponse)
async def get_components(
    session_id: str,
    request: Request,
    session: Path = Depends(resolve_session),
    jm: JobManager = Depends(get_job_manager),
) -> HTMLResponse:
    """Fragment con la griglia delle card dei componenti."""
    templates = request.app.state.templates
    manifest = _load_components(session)
    active_job = jm.active_for_session(session_id)
    meta = storage.load_metadata(session)
    return templates.TemplateResponse(
        request,
        "_components_cards.html",
        {
            "session_id": session_id,
            "manifest": manifest,
            "breakdown_accepted": meta.get("breakdown_accepted", False),
            "active_job": active_job.snapshot() if active_job else None,
        },
    )


@router.post("/breakdown/accept", response_class=HTMLResponse)
async def post_breakdown_accept(
    session_id: str,
    request: Request,
    session: Path = Depends(resolve_session),
    jm: JobManager = Depends(get_job_manager),
) -> HTMLResponse:
    """Accetta la decomposizione nel suo insieme: chiude il cerchio (stato
    finale della sessione). Ri-renderizza la griglia con i controlli per-parte
    disabilitati e il badge finale."""
    if jm.active_for_session(session_id) is not None:
        raise HTTPException(status_code=409, detail="Job già in corso per questa sessione")
    try:
        workflow.accept_breakdown(session)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "_components_cards.html",
        {
            "session_id": session_id,
            "manifest": _load_components(session),
            "breakdown_accepted": True,
            "active_job": None,
        },
    )


@router.post("/components/{component_id}/regen", response_class=HTMLResponse)
async def post_component_regen(
    session_id: str,
    component_id: str,
    request: Request,
    feedback: str = Form(...),
    session: Path = Depends(resolve_session),
    jm: JobManager = Depends(get_job_manager),
) -> HTMLResponse:
    """Rigenera un singolo componente con feedback testuale."""
    feedback = feedback.strip()
    if not feedback:
        raise HTTPException(status_code=400, detail="Feedback vuoto")
    if jm.active_for_session(session_id) is not None:
        raise HTTPException(status_code=409, detail="Job già in corso per questa sessione")

    api_key = request.app.state.meshy_api_key

    def task() -> str:
        path = workflow.regenerate_component(
            session, component_id, feedback, api_key=api_key, skip_open=True
        )
        return str(path)

    job_id = jm.submit(session_id, "component_regen", task)
    templates = request.app.state.templates
    rec = jm.get(job_id)
    return templates.TemplateResponse(
        request,
        "_job_progress.html",
        {"session_id": session_id, "job": rec.snapshot()},
    )


@router.post("/components/{component_id}/approve", response_class=HTMLResponse)
async def post_component_approve(
    session_id: str,
    component_id: str,
    request: Request,
    session: Path = Depends(resolve_session),
) -> HTMLResponse:
    """Marca un componente come approvato e ri-renderizza la sua card."""
    try:
        comp = workflow.approve_component(session, component_id)
    except RuntimeError as e:
        raise HTTPException(status_code=404, detail=str(e))
    templates = request.app.state.templates
    meta = storage.load_metadata(session)
    return templates.TemplateResponse(
        request,
        "_component_card.html",
        {
            "session_id": session_id,
            "comp": comp,
            "breakdown_accepted": meta.get("breakdown_accepted", False),
        },
    )
