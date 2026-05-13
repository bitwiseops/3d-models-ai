"""Endpoints iterazioni: feedback (rigenerazione image) e accept (3D)."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from ... import workflow
from ..deps import get_job_manager, resolve_session
from ..jobs import JobManager

router = APIRouter()


@router.post("/sessions/{session_id}/feedback", response_class=HTMLResponse)
async def post_feedback(
    session_id: str,
    request: Request,
    feedback: str = Form(...),
    session: Path = Depends(resolve_session),
    jm: JobManager = Depends(get_job_manager),
) -> HTMLResponse:
    """Sottomette un nuovo round di image generation con feedback."""
    feedback = feedback.strip()
    if not feedback:
        raise HTTPException(status_code=400, detail="Feedback vuoto")
    # Evita doppia sottomissione se c'è già un job attivo
    if jm.active_for_session(session_id) is not None:
        raise HTTPException(status_code=409, detail="Job già in corso per questa sessione")

    api_key = request.app.state.meshy_api_key

    def task() -> str:
        path = workflow.continue_with_feedback(
            session, feedback, api_key=api_key, skip_open=True
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


@router.post("/sessions/{session_id}/accept", response_class=HTMLResponse)
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
