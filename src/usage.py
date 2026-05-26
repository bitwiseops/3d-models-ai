"""Tracking aggregato del consumo Anthropic (Claude).

Persistito in `outputs/claude_usage.json` per resistere ai restart. Append-only
in memoria + write atomico a ogni `record()`. Thread-safe via RLock.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Literal

from . import config

Section = Literal["oggetti", "portraits", "scad"]

# Prezzi indicativi Claude Opus 4.7 ($ per milione di token).
PRICE_PER_M_INPUT_USD = 15.0
PRICE_PER_M_OUTPUT_USD = 75.0

_USAGE_FILE = config.OUTPUTS_ROOT / "claude_usage.json"
_lock = threading.RLock()


def _empty() -> dict:
    return {
        "by_section": {
            "oggetti": {"input_tokens": 0, "output_tokens": 0, "calls": 0},
            "portraits": {"input_tokens": 0, "output_tokens": 0, "calls": 0},
            "scad": {"input_tokens": 0, "output_tokens": 0, "calls": 0},
        },
        "totals": {"input_tokens": 0, "output_tokens": 0, "calls": 0},
    }


def _read() -> dict:
    if not _USAGE_FILE.exists():
        return _empty()
    try:
        data = json.loads(_USAGE_FILE.read_text(encoding="utf-8"))
        # Migrazione difensiva: garantisce le chiavi base.
        skeleton = _empty()
        for k, v in skeleton.items():
            data.setdefault(k, v)
        for sec, defaults in skeleton["by_section"].items():
            data["by_section"].setdefault(sec, defaults)
        return data
    except (json.JSONDecodeError, OSError):
        return _empty()


def _write(data: dict) -> None:
    config.OUTPUTS_ROOT.mkdir(parents=True, exist_ok=True)
    tmp = _USAGE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, _USAGE_FILE)


def record(section: Section, input_tokens: int, output_tokens: int) -> None:
    """Aggiunge il consumo di una singola chiamata Claude."""
    if input_tokens <= 0 and output_tokens <= 0:
        return
    with _lock:
        data = _read()
        sec = data["by_section"].setdefault(
            section, {"input_tokens": 0, "output_tokens": 0, "calls": 0}
        )
        sec["input_tokens"] += int(input_tokens)
        sec["output_tokens"] += int(output_tokens)
        sec["calls"] += 1
        tot = data["totals"]
        tot["input_tokens"] += int(input_tokens)
        tot["output_tokens"] += int(output_tokens)
        tot["calls"] += 1
        _write(data)


def record_response(section: Section, resp) -> None:
    """Helper per estrarre `usage` da un AnthropicResponse e registrarla."""
    u = getattr(resp, "usage", None)
    if u is None:
        return
    record(
        section,
        int(getattr(u, "input_tokens", 0) or 0),
        int(getattr(u, "output_tokens", 0) or 0),
    )


def read_stats() -> dict:
    """Snapshot per la UI. Aggiunge costi USD stimati."""
    with _lock:
        data = _read()
    out = {"by_section": {}, "totals": {}}
    for sec, vals in data["by_section"].items():
        out["by_section"][sec] = {
            **vals,
            "cost_usd": _cost(vals["input_tokens"], vals["output_tokens"]),
        }
    out["totals"] = {
        **data["totals"],
        "cost_usd": _cost(
            data["totals"]["input_tokens"], data["totals"]["output_tokens"]
        ),
    }
    return out


def _cost(input_tokens: int, output_tokens: int) -> float:
    return round(
        input_tokens / 1_000_000 * PRICE_PER_M_INPUT_USD
        + output_tokens / 1_000_000 * PRICE_PER_M_OUTPUT_USD,
        4,
    )
