"""Endpoints sessioni: lista, creazione, dettaglio."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from ... import storage, workflow
from ..deps import get_job_manager, get_outputs_root, resolve_session
from ..jobs import JobManager

router = APIRouter()


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
        if meta.get("model_task_id"):
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
            }
        )
    sessions.sort(key=lambda s: s["created_at"], reverse=True)
    return sessions


@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request, outputs_root: Path = Depends(get_outputs_root)
) -> HTMLResponse:
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "index.html", {"sessions": _list_sessions(outputs_root)}
    )


@router.get("/sessions", response_class=HTMLResponse)
async def session_list_fragment(
    request: Request, outputs_root: Path = Depends(get_outputs_root)
) -> HTMLResponse:
    """Solo il fragment lista — usato da HTMX per refresh leggero."""
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "_session_list.html", {"sessions": _list_sessions(outputs_root)}
    )


@router.post("/sessions")
async def create_session(
    request: Request,
    idea: str = Form(...),
    references: list[UploadFile] = File(default=[]),
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
    session = workflow.prepare_session(idea, ref_paths or None)
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
    return RedirectResponse(url=f"/sessions/{session_id}", status_code=303)


@router.get("/sessions/{session_id}", response_class=HTMLResponse)
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


@router.get("/sessions/{session_id}/ab", response_class=HTMLResponse)
async def session_ab_compare(
    session_id: str,
    request: Request,
    session: Path = Depends(resolve_session),
) -> HTMLResponse:
    """Pagina di confronto A/B (multi-view vs single-view) per una sessione
    che ha già il 3D normale + un output single-view in 3d_ab/single_view/."""
    import trimesh

    templates = request.app.state.templates
    meta = storage.load_metadata(session)
    ab_dir = session / "3d_ab" / "single_view"
    if not (ab_dir / "model.glb").exists():
        raise HTTPException(
            status_code=404,
            detail="A/B output non presente. Lancia tools/ab_single_view.py prima.",
        )

    def _stats(p: Path) -> dict:
        m = trimesh.load(str(p), force="mesh", process=False)
        bbox = (m.bounds[1] - m.bounds[0]).tolist()
        return {"bbox": [round(float(x), 1) for x in bbox]}

    a_stats = _stats(session / "3d" / "model.stl")
    b_stats = _stats(ab_dir / "model.stl")
    a_meta = meta.get("ab_single_view")  # potrebbe essere None
    y_diff_pct = round((a_stats["bbox"][1] - b_stats["bbox"][1]) / a_stats["bbox"][1] * 100, 1)

    return templates.TemplateResponse(
        request,
        "ab_compare.html",
        {
            "session_id": session_id,
            "meta": meta,
            "a": {"bbox": a_stats["bbox"], "credits": meta.get("credits", {}).get("image_to_3d")},
            "b": {"bbox": b_stats["bbox"], "credits": (a_meta or {}).get("credits")},
            "y_diff_pct": y_diff_pct,
        },
    )


@router.get("/sessions/{session_id}/fragment", response_class=HTMLResponse)
async def session_fragment(
    session_id: str,
    request: Request,
    session: Path = Depends(resolve_session),
    jm: JobManager = Depends(get_job_manager),
) -> HTMLResponse:
    """Fragment con solo la lista iterazioni + 3D viewer (post-SSE refresh)."""
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
