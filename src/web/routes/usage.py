"""Endpoint statistiche uso Claude (popup)."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ... import usage as usage_mod, usage_remote

router = APIRouter()


def _gather() -> dict:
    return {
        "local": usage_mod.read_stats(),
        "meshy_keys": usage_remote.fetch_meshy_balances(),
    }


@router.get("/usage", response_class=HTMLResponse)
async def usage_fragment(request: Request) -> HTMLResponse:
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "_usage_stats.html", _gather(),
    )


@router.get("/usage.json", response_class=JSONResponse)
async def usage_json() -> JSONResponse:
    return JSONResponse(_gather())


@router.get("/_pill", response_class=HTMLResponse)
async def pill_fragment(request: Request) -> HTMLResponse:
    """Fragment auto-refresh per la progress pill globale.
    Si rende solo se il middleware ha trovato un job attivo."""
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "_progress_pill.html", {})
