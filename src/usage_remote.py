"""Stats live recuperate dai provider (per ora solo Meshy balance).

Tutte le funzioni sono best-effort: tornano `{"ok": True, ...}` se hanno dati,
`{"ok": False, "error": "..."}` altrimenti. Niente eccezioni propagate al caller.
Timeout corti perché il popup deve restare reattivo.
"""
from __future__ import annotations

import requests

from . import config

TIMEOUT = 8  # secondi


def fetch_meshy_balance() -> dict:
    if not config.MESHY_API_KEY:
        return {"ok": False, "error": "MESHY_API_KEY non configurata"}
    try:
        r = requests.get(
            f"{config.MESHY_BASE}/balance",
            headers={"Authorization": f"Bearer {config.MESHY_API_KEY}"},
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        return {"ok": False, "error": f"network: {e}"}
    if not r.ok:
        return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
    try:
        data = r.json()
    except ValueError:
        return {"ok": False, "error": "risposta non-JSON"}
    # Meshy ritorna tipicamente {"balance": <int>, "subscription": "..."} o
    # variante con `free_credits` / `paid_credits`. Normalizziamo difensivamente.
    return {"ok": True, "raw": data}
