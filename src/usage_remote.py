"""Stats live recuperate dai provider (per ora solo Meshy balance).

Tutte le funzioni sono best-effort: tornano `{"ok": True, ...}` se hanno dati,
`{"ok": False, "error": "..."}` altrimenti. Niente eccezioni propagate al caller.
Timeout corti perché il popup deve restare reattivo.
"""
from __future__ import annotations

import requests

from . import config

TIMEOUT = 8  # secondi


def fetch_meshy_balance(api_key: str | None = None) -> dict:
    """Balance live per UNA chiave Meshy (default: la principale)."""
    key = api_key or config.MESHY_API_KEY
    if not key:
        return {"ok": False, "error": "MESHY_API_KEY non configurata"}
    try:
        r = requests.get(
            f"{config.MESHY_BASE}/balance",
            headers={"Authorization": f"Bearer {key}"},
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


def fetch_meshy_balances() -> list[dict]:
    """Balance per OGNI chiave Meshy configurata (principale poi fallback). Ogni
    voce ha `label` ('principale'/'fallback'/'key #N') e `key_hint` (prefisso
    mascherato) per distinguerle nella UI. Best-effort come `fetch_meshy_balance`."""
    keys = config.meshy_api_keys()
    out: list[dict] = []
    for i, k in enumerate(keys):
        label = "principale" if i == 0 else ("fallback" if i == 1 else f"key #{i + 1}")
        entry = fetch_meshy_balance(k)
        entry["label"] = label
        entry["key_hint"] = (k[:7] + "…") if k else ""
        out.append(entry)
    if not out:
        out.append({"ok": False, "error": "nessuna chiave Meshy configurata",
                    "label": "principale", "key_hint": ""})
    return out
