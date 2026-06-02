"""Endpoints sessioni: lista, creazione, dettaglio."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from ... import storage, usage as usage_mod, workflow
from ..deps import get_job_manager, get_outputs_root, resolve_session
from ..jobs import JobManager
from ..list_utils import group_by_day, parse_pagination, slice_with_more

router = APIRouter(prefix="/sessions")


def _list_sessions(outputs_root: Path) -> list[dict]:
    """Scansiona outputs/ e ritorna lista sessioni ordinata per timestamp desc."""
    if not outputs_root.exists():
        return []
    sessions = []
    for d in outputs_root.iterdir():
        if not d.is_dir() or not (d / "metadata.json").exists():
            continue
        try:
            meta = storage.load_metadata(d)
        except Exception:
            continue
        last_thumb = None
        if meta.get("iterations"):
            last_iter = meta["iterations"][-1]
            last_thumb = last_iter.get("clean_path") or last_iter.get("image_path")
        status = "draft"
        # `model_task_id` è il marcatore legacy (vecchie sessioni con 3D Meshy);
        # `breakdown_accepted` è il nuovo stato finale ("cerchio chiuso").
        if meta.get("breakdown_accepted") or meta.get("model_task_id"):
            status = "completed"
        elif meta.get("accepted_iteration") is not None:
            status = "accepted"
        sessions.append(
            {
                "id": d.name,
                "idea": meta.get("idea", ""),
                "created_at": meta.get("created_at", ""),
                "iterations": len(meta.get("iterations", [])),
                "status": status,
                "thumb": last_thumb,
                "cost_usd": usage_mod.session_cost_meshy(meta),
            }
        )
    sessions.sort(key=lambda s: s["created_at"], reverse=True)
    return sessions


def sessions_page_context(outputs_root: Path, offset: int, limit: int) -> dict:
    offset, limit = parse_pagination(offset, limit)
    all_items = _list_sessions(outputs_root)
    page, has_more, next_offset = slice_with_more(all_items, offset, limit)
    return {
        "sessions": page,
        "groups": group_by_day(page),
        "has_more": has_more,
        "next_offset": next_offset,
        "limit": limit,
    }


@router.get("", response_class=HTMLResponse)
async def session_list_fragment(
    request: Request,
    offset: int = 0,
    limit: int = 20,
    outputs_root: Path = Depends(get_outputs_root),
) -> HTMLResponse:
    """Solo il fragment lista — usato da HTMX per refresh leggero."""
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "_session_list.html", sessions_page_context(outputs_root, offset, limit),
    )


@router.post("")
async def create_session(
    request: Request,
    idea: str = Form(...),
    references: list[UploadFile] = File(default=[]),
    clean_references: bool = Form(False),
    jm: JobManager = Depends(get_job_manager),
) -> RedirectResponse:
    """Crea una nuova sessione (sincrona: dir + references) e lancia in
    background la generazione image v1. Redirect immediato a /sessions/{id}.
    """
    idea = idea.strip()
    if not idea:
        raise HTTPException(status_code=400, detail="Idea vuota")

    # Salva references in tmpdir, copiate nella session dir da prepare_session.
    ref_paths: list[Path] = []
    tmp_root: Path | None = None
    if references:
        tmp_root = Path(tempfile.mkdtemp(prefix="models3d-refs-"))
        for i, up in enumerate(references):
            if not up.filename:
                continue
            ext = Path(up.filename).suffix.lower() or ".png"
            dst = tmp_root / f"ref_{i:02d}{ext}"
            dst.write_bytes(await up.read())
            ref_paths.append(dst)

    # Sincrono: crea la session dir (~10ms). Da qui in poi abbiamo session_id.
    session = workflow.prepare_session(
        idea, ref_paths or None, clean_references=clean_references
    )
    if tmp_root is not None:
        shutil.rmtree(tmp_root, ignore_errors=True)

    api_key = request.app.state.meshy_api_key
    session_id = session.name

    def task() -> str:
        path = workflow.generate_first_iteration(
            session, idea, api_key=api_key, skip_open=True
        )
        return str(path)

    jm.submit(session_id, "image", task)
    return RedirectResponse(url=f"{request.scope.get('root_path', '')}/sessions/{session_id}", status_code=303)


@router.get("/{session_id}", response_class=HTMLResponse)
async def session_detail(
    session_id: str,
    request: Request,
    session: Path = Depends(resolve_session),
    jm: JobManager = Depends(get_job_manager),
) -> HTMLResponse:
    templates = request.app.state.templates
    meta = storage.load_metadata(session)
    active_job = jm.active_for_session(session_id)
    return templates.TemplateResponse(
        request,
        "session_detail.html",
        {
            "session_id": session_id,
            "meta": meta,
            "active_job": active_job.snapshot() if active_job else None,
        },
    )


@router.get("/{session_id}/fragment", response_class=HTMLResponse)
async def session_fragment(
    session_id: str,
    request: Request,
    session: Path = Depends(resolve_session),
    jm: JobManager = Depends(get_job_manager),
) -> HTMLResponse:
    """Fragment con la lista iterazioni + scomposizione (post-SSE refresh)."""
    templates = request.app.state.templates
    meta = storage.load_metadata(session)
    active_job = jm.active_for_session(session_id)
    return templates.TemplateResponse(
        request,
        "_iteration_list.html",
        {
            "session_id": session_id,
            "meta": meta,
            "active_job": active_job.snapshot() if active_job else None,
        },
    )
