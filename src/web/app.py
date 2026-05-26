"""FastAPI app entrypoint per la web UI Models3D-AI.

Lifecycle:
- All'avvio: crea un ThreadPoolExecutor condiviso e un JobManager.
- A shutdown: ferma l'executor (cancel futures in attesa, lascia girare quelli avviati).

Per avviare: `uv run models3d-web` oppure `uv run uvicorn src.web.app:app`.
"""
from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

import jinja2
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import config
from .jobs import JobManager
from .routes import events, files, iterations, portraits, scad, sessions, usage

TEMPLATES_DIR = Path(__file__).parent / "templates"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Setup/teardown delle risorse globali."""
    max_workers = int(os.environ.get("MODELS3D_MAX_WORKERS", "4"))
    executor = ThreadPoolExecutor(
        max_workers=max_workers, thread_name_prefix="models3d-job"
    )
    loop = asyncio.get_running_loop()
    app.state.executor = executor
    app.state.job_manager = JobManager(executor, loop)
    app.state.meshy_api_key = config.MESHY_API_KEY  # snapshot at boot, immutable

    # Assicura che OUTPUTS_ROOT esista
    config.OUTPUTS_ROOT.mkdir(parents=True, exist_ok=True)

    print(f"[web] startup: {max_workers} worker thread, outputs in {config.OUTPUTS_ROOT}")
    if not app.state.meshy_api_key:
        print("[web] ATTENZIONE: MESHY_API_KEY non configurata — le chiamate falliranno.")

    try:
        yield
    finally:
        print("[web] shutdown: stop executor (no wait, cancel pending)")
        executor.shutdown(wait=False, cancel_futures=True)


def create_app() -> FastAPI:
    app = FastAPI(
        title="Models3D-AI Web UI",
        description="UI mobile-first per pipeline Meshy + Claude",
        lifespan=lifespan,
        root_path=os.environ.get("ROOT_PATH", ""),
    )

    @app.middleware("http")
    async def attach_active_job(request, call_next):
        """Inietta `request.state.active_job_global` per il progress pill
        in base.html. Best-effort: se job_manager non è ancora attached
        (avvio), salta silenziosamente."""
        try:
            jm = request.app.state.job_manager
            active = jm.active_global()
            request.state.active_job_global = active.snapshot() if active else None
        except AttributeError:
            request.state.active_job_global = None
        return await call_next(request)

    # Environment Jinja2 esplicito con auto_reload=True (ricontrolla mtime
    # ad ogni render) e cache_size=0 (no cache compilata): cambiando un
    # template, anche un partial incluso, basta refreshare la pagina senza
    # riavviare uvicorn. Trascurabile per single-user.
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
        auto_reload=True,
        cache_size=0,
        autoescape=jinja2.select_autoescape(["html", "xml"]),
    )
    env.globals["root_path"] = os.environ.get("ROOT_PATH", "")
    # Copy IT condivisa + classificatore errori → disponibili in tutti i template.
    from .copy import C
    from .errors import classify as classify_error
    env.globals["C"] = C
    env.globals["classify_error"] = classify_error
    templates = Jinja2Templates(env=env)
    app.state.templates = templates

    # Routes
    app.include_router(sessions.router)
    app.include_router(iterations.router)
    app.include_router(events.router)
    app.include_router(files.router)
    app.include_router(portraits.router)
    app.include_router(scad.router)
    app.include_router(usage.router)

    # Static assets (HDR per model-viewer environment, ecc.)
    assets_dir = config.PROJECT_ROOT / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    return app


app = create_app()


def run() -> None:
    """Entrypoint per `uv run models3d-web` (definito in pyproject.toml scripts)."""
    import uvicorn

    host = os.environ.get("MODELS3D_HOST", "0.0.0.0")
    port = int(os.environ.get("MODELS3D_PORT", "8000"))
    reload = os.environ.get("MODELS3D_RELOAD", "0") == "1"
    uvicorn.run(
        "src.web.app:app",
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )
