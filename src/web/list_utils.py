"""Helper condivisi per le liste paginate (Oggetti, Portraits, SCAD).

Pattern: route prendono `?offset=N&limit=20`, ritornano un sottoinsieme
ordinato + `has_more` per il template. `relative_day` raggruppa per data
relativa (Oggi / Ieri / Questa settimana / mese).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable

DEFAULT_LIMIT = 20


def parse_pagination(offset: int = 0, limit: int = DEFAULT_LIMIT) -> tuple[int, int]:
    offset = max(0, int(offset or 0))
    limit = max(1, min(100, int(limit or DEFAULT_LIMIT)))
    return offset, limit


def slice_with_more(items: list[Any], offset: int, limit: int) -> tuple[list[Any], bool, int]:
    """Slice paginata. Ritorna (items_slice, has_more, next_offset)."""
    end = offset + limit
    page = items[offset:end]
    return page, end < len(items), end


def _parse_ts(s: str) -> datetime | None:
    """Formato ts della codebase: 'YYYYMMDD-HHMMSS' (vedi storage._slugify)."""
    if not s or len(s) < 8:
        return None
    try:
        return datetime.strptime(s[:15], "%Y%m%d-%H%M%S")
    except ValueError:
        try:
            return datetime.strptime(s[:8], "%Y%m%d")
        except ValueError:
            return None


def relative_day(ts: str, *, now: datetime | None = None) -> str:
    """'Oggi' / 'Ieri' / 'Questa settimana' / 'MMMM YYYY' / 'Sconosciuto'."""
    now = now or datetime.now()
    dt = _parse_ts(ts)
    if dt is None:
        return "Sconosciuto"
    delta = (now.date() - dt.date()).days
    if delta == 0:
        return "Oggi"
    if delta == 1:
        return "Ieri"
    if delta < 7 and dt.isocalendar()[1] == now.isocalendar()[1]:
        return "Questa settimana"
    if dt.year == now.year:
        return dt.strftime("%B")  # nome mese — Python format
    return dt.strftime("%B %Y")


def group_by_day(items: Iterable[dict], *, ts_key: str = "created_at") -> list[tuple[str, list[dict]]]:
    """Raggruppa per `relative_day` mantenendo l'ordine di input."""
    groups: list[tuple[str, list[dict]]] = []
    last_label: str | None = None
    for it in items:
        label = relative_day(it.get(ts_key) or "")
        if label != last_label:
            groups.append((label, []))
            last_label = label
        groups[-1][1].append(it)
    return groups
