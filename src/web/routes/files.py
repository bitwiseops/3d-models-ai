"""Serve i file generati nelle session dir (immagini, mesh, prompt)."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from ..deps import resolve_session

router = APIRouter(prefix="/sessions/{session_id}/files")

# mimetypes di stdlib non conosce le estensioni 3D: serve mapping esplicito.
# Senza questi <model-viewer> riceve text/plain e silenziosamente non carica.
_MEDIA_TYPES = {
    ".glb": "model/gltf-binary",
    ".gltf": "model/gltf+json",
    ".stl": "model/stl",
    ".3mf": "model/3mf",
    ".obj": "model/obj",
}


@router.get("/{path:path}")
async def serve_file(session_id: str, path: str, session: Path = Depends(resolve_session)) -> FileResponse:
    """Serve un file all'interno della session dir, con check di path traversal."""
    # Disallow absolute paths e parent traversal
    if path.startswith("/") or ".." in Path(path).parts:
        raise HTTPException(status_code=400, detail="path non valido")
    full = (session / path).resolve()
    # Verifica che il path risolto sia ancora dentro la session dir
    try:
        full.relative_to(session.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="path fuori dalla session")
    if not full.exists() or not full.is_file():
        raise HTTPException(status_code=404, detail="file non trovato")
    media_type = _MEDIA_TYPES.get(full.suffix.lower())
    return FileResponse(str(full), media_type=media_type)
