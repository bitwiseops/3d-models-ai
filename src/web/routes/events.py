"""Server-Sent Events: stream di update HTML pre-renderizzati per HTMX `sse-swap`.

Ogni evento SSE è un fragment HTML (`_job_progress_inner.html`) che HTMX swappa
direttamente nel `#job-progress-card`. Quando il job termina (`done`/`failed`),
il fragment finale contiene un `hx-trigger="load"` che innesca un refresh
dell'intera lista iterazioni della sessione.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from ..deps import get_job_manager
from ..jobs import JobManager

router = APIRouter(prefix="/events")


def _render_job_fragment(request: Request, session_id: str, snapshot: dict) -> str:
    """Renderizza _job_progress_inner.html come stringa HTML.

    `snapshot` ha già la forma `job.snapshot()` (job_id, kind, status, progress,
    progress_status, log_tail, error).
    """
    templates = request.app.state.templates
    kind = snapshot.get("kind")
    if kind == "portrait":
        tpl_name = "_portrait_progress_inner.html"
    elif kind == "scad":
        tpl_name = "_scad_progress_inner.html"
    else:
        tpl_name = "_job_progress_inner.html"
    return templates.get_template(tpl_name).render(
        request=request, job=snapshot, session_id=session_id
    )


async def _job_event_stream(request: Request, jm: JobManager, job_id: str):
    rec = jm.get(job_id)
    if rec is None:
        yield {"event": "error", "data": "job_not_found"}
        return
    session_id = rec.session_id
    async for snapshot in jm.subscribe(job_id):
        if await request.is_disconnected():
            break
        html = _render_job_fragment(request, session_id, snapshot)
        # HTMX legge data multi-riga concatenando con \n. La extension sse-swap
        # piazza il content come innerHTML del target. Niente template engine
        # client side.
        yield {"event": "message", "data": html}


@router.get("/{job_id}")
async def job_events(
    job_id: str,
    request: Request,
    jm: JobManager = Depends(get_job_manager),
) -> EventSourceResponse:
    """Stream SSE per un job. Se il job non esiste (es. server restartato
    e JobManager azzerato), emette un singolo HTML fragment che innesca un
    reload della pagina, così la stale SSE smette di ritentare."""
    if jm.get(job_id) is None:
        async def stale():
            # Fragment che usa hx-trigger="load" per ricaricare l'intera pagina.
            # Sostituisce il #job-progress-card che ospitava la stale SSE,
            # rimuovendo l'attributo hx-ext="sse" → connessione chiusa.
            html = (
                '<div hx-trigger="load" '
                'hx-on::load="window.location.reload()" '
                'class="bg-stone-100 border border-stone-300 rounded-xl p-4 text-sm text-stone-600">'
                'Sessione obsoleta dopo restart server. Ricarico…</div>'
            )
            yield {"event": "message", "data": html}

        return EventSourceResponse(stale())
    return EventSourceResponse(_job_event_stream(request, jm, job_id))


@router.get("/sessions/{session_id}")
async def session_events(
    session_id: str,
    request: Request,
    jm: JobManager = Depends(get_job_manager),
) -> EventSourceResponse:
    """SSE livello sessione: segue il job attivo (se esiste).
    Emette eventi "iteration_added" / "3d_ready" alla fine, leggibili da HTMX
    via `hx-trigger="sse:iteration_added"` per refreshare la lista iterazioni.
    """
    async def stream():
        active = jm.active_for_session(session_id)
        if active is None:
            # Nessun job attivo: heartbeat singolo per chiudere il SSE.
            yield {"event": "idle", "data": ""}
            return
        async for snapshot in jm.subscribe(active.job_id):
            if await request.is_disconnected():
                break
            status = snapshot.get("status")
            if status == "done":
                kind = snapshot.get("kind", "image")
                if kind == "accept":
                    event_type = "3d_ready"
                elif kind == "portrait":
                    event_type = "portrait_ready"
                else:
                    event_type = "iteration_added"
                yield {"event": event_type, "data": ""}
                return
            elif status == "failed":
                yield {"event": "error", "data": snapshot.get("error", "")}
                return
            else:
                # Progress: nessun evento semantico, solo heartbeat
                yield {"event": "progress", "data": str(snapshot.get("progress", 0))}

    return EventSourceResponse(stream())
