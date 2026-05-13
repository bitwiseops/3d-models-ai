"""Dependency-injection helpers per FastAPI."""
from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException, Request

from .. import config
from .jobs import JobManager


def get_job_manager(request: Request) -> JobManager:
    """Ritorna il JobManager singletton attached all'app state."""
    jm: JobManager = request.app.state.job_manager
    return jm


def get_outputs_root() -> Path:
    """Ritorna il root delle sessioni (config.OUTPUTS_ROOT, mockable nei test)."""
    return config.OUTPUTS_ROOT


def resolve_session(session_id: str) -> Path:
    """Risolve un session_id (nome della directory in outputs/) a Path.
    404 se non esiste. Protegge da path traversal (no `/`, no `..`)."""
    if "/" in session_id or ".." in session_id:
        raise HTTPException(status_code=400, detail="session_id non valido")
    path = get_outputs_root() / session_id
    if not path.exists() or not path.is_dir():
        raise HTTPException(status_code=404, detail="sessione non trovata")
    return path
