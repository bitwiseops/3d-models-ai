"""Tracking aggregato del consumo Anthropic (Claude).

Persistito in `outputs/claude_usage.json` per resistere ai restart. Append-only
in memoria + write atomico a ogni `record()`. Thread-safe via RLock.

Schema v2: tracking per (section, model) — i prezzi cambiano per modello
(Opus 4.7 ≫ Sonnet 4.6 ≫ Haiku 4.5), quindi sommare token sezione-wide e
applicare un singolo prezzo darebbe stime sbagliate per SCAD (Sonnet).
"""
from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Literal

from . import config

Section = Literal["oggetti", "portraits"]

# Prezzi ufficiali Anthropic ($ per milione di token, input / output).
# Aggiornare quando cambiano: https://www.anthropic.com/pricing
PRICES_PER_M_USD: dict[str, tuple[float, float]] = {
    "claude-opus-4-7":     (15.0, 75.0),
    "claude-opus-4-6":     (15.0, 75.0),
    "claude-sonnet-4-6":   (3.0, 15.0),
    "claude-sonnet-4-5":   (3.0, 15.0),
    "claude-haiku-4-5":    (1.0, 5.0),
}
# Default per modelli sconosciuti / dati legacy: assume Opus (conservativo).
_DEFAULT_PRICE = (15.0, 75.0)

# Per dati legacy senza model info: euristica = il modello che la sezione usa
# attualmente (config.py). Sottostima leggermente se in passato si usava un
# modello diverso, ma è la migliore inferenza possibile retroattivamente.
# Sezioni storiche non più attive (es. "scad") cadono sul fallback Opus.
_LEGACY_MODEL_BY_SECTION: dict[str, str] = {
    "oggetti": config.ANTHROPIC_MODEL,
    "portraits": config.ANTHROPIC_MODEL,
}

# Meshy non pubblica un USD/credit ufficiale stabile; valore tipico PAYG.
# Override via env var per allinearsi al piano attivo.
MESHY_USD_PER_CREDIT = float(os.environ.get("MESHY_USD_PER_CREDIT", "0.05"))

_USAGE_FILE = config.OUTPUTS_ROOT / "claude_usage.json"
_lock = threading.RLock()


def _empty_section() -> dict:
    return {"by_model": {}}


def _empty() -> dict:
    return {
        "schema_version": 2,
        "by_section": {
            "oggetti": _empty_section(),
            "portraits": _empty_section(),
        },
    }


def _migrate_v1_to_v2(data: dict) -> dict:
    """Schema v1 aveva `by_section[sec] = {input, output, calls}` senza model.
    Sposta quei numeri sotto by_model[<modello attuale della sezione>]."""
    new = _empty()
    for sec, old in (data.get("by_section") or {}).items():
        if not isinstance(old, dict):
            continue
        # già v2?
        if "by_model" in old:
            new["by_section"][sec] = {"by_model": dict(old["by_model"])}
            continue
        model = _LEGACY_MODEL_BY_SECTION.get(sec, config.ANTHROPIC_MODEL)
        sec_node = new["by_section"].setdefault(sec, _empty_section())
        sec_node["by_model"][model] = {
            "input_tokens": int(old.get("input_tokens", 0)),
            "output_tokens": int(old.get("output_tokens", 0)),
            "calls": int(old.get("calls", 0)),
        }
    return new


def _read() -> dict:
    if not _USAGE_FILE.exists():
        return _empty()
    try:
        data = json.loads(_USAGE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _empty()
    if data.get("schema_version") != 2:
        data = _migrate_v1_to_v2(data)
    # Garantisce le sezioni standard (eventuali sezioni storiche come "scad"
    # restano leggibili senza essere ricreate).
    for sec in ("oggetti", "portraits"):
        data["by_section"].setdefault(sec, _empty_section())
    return data


def _write(data: dict) -> None:
    config.OUTPUTS_ROOT.mkdir(parents=True, exist_ok=True)
    tmp = _USAGE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, _USAGE_FILE)


_session_writer = threading.local()


@contextmanager
def session_scope(writer: Callable[[str, int, int], None]):
    """Cattura ogni `record()` chiamata in questo thread, oltre allo store
    globale. `writer(model, input_tokens, output_tokens)` viene invocato per
    persistere il delta nella session metadata. Thread-local; safe per più
    job paralleli del ThreadPoolExecutor."""
    prev = getattr(_session_writer, "fn", None)
    _session_writer.fn = writer
    try:
        yield
    finally:
        _session_writer.fn = prev


def record(
    section: Section, model: str, input_tokens: int, output_tokens: int,
) -> None:
    """Aggiunge il consumo di una singola chiamata Claude per (sezione, modello).
    Se uno `session_scope` è attivo per questo thread, invoca anche il writer
    per accumulare lo stesso consumo nella session metadata."""
    if input_tokens <= 0 and output_tokens <= 0:
        return
    model = model or "unknown"
    with _lock:
        data = _read()
        sec_node = data["by_section"].setdefault(section, _empty_section())
        m = sec_node["by_model"].setdefault(
            model, {"input_tokens": 0, "output_tokens": 0, "calls": 0},
        )
        m["input_tokens"] += int(input_tokens)
        m["output_tokens"] += int(output_tokens)
        m["calls"] += 1
        _write(data)
    fn = getattr(_session_writer, "fn", None)
    if fn is not None:
        try:
            fn(model, int(input_tokens), int(output_tokens))
        except Exception:
            # Tracking per-sessione è best-effort: non rompere la pipeline.
            pass


def record_response(section: Section, resp) -> None:
    """Helper per estrarre `usage` + `model` da un AnthropicResponse."""
    u = getattr(resp, "usage", None)
    if u is None:
        return
    model = getattr(resp, "model", None) or "unknown"
    record(
        section,
        model,
        int(getattr(u, "input_tokens", 0) or 0),
        int(getattr(u, "output_tokens", 0) or 0),
    )


def _cost(model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = PRICES_PER_M_USD.get(model, _DEFAULT_PRICE)
    return round(
        input_tokens / 1_000_000 * pin + output_tokens / 1_000_000 * pout, 4,
    )


def read_stats() -> dict:
    """Snapshot per la UI. Calcola costi per (sezione, modello), aggrega per
    sezione, e fornisce totali globali. Schema:

    {
      "by_section": {
        "scad": {
          "calls": 59, "input_tokens": …, "output_tokens": …, "cost_usd": …,
          "by_model": [
            {"model": "claude-sonnet-4-6", "calls": …, "input_tokens": …,
             "output_tokens": …, "cost_usd": …},
            …
          ],
        },
        …
      },
      "totals": {"calls": …, "input_tokens": …, "output_tokens": …, "cost_usd": …},
    }
    """
    with _lock:
        data = _read()

    out_sections: dict[str, dict] = {}
    grand = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}

    for sec, node in data["by_section"].items():
        models_list: list[dict] = []
        sec_tot = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
        for model, vals in (node.get("by_model") or {}).items():
            cost = _cost(model, vals["input_tokens"], vals["output_tokens"])
            models_list.append({
                "model": model,
                "calls": vals["calls"],
                "input_tokens": vals["input_tokens"],
                "output_tokens": vals["output_tokens"],
                "cost_usd": cost,
            })
            sec_tot["calls"] += vals["calls"]
            sec_tot["input_tokens"] += vals["input_tokens"]
            sec_tot["output_tokens"] += vals["output_tokens"]
            sec_tot["cost_usd"] += cost
        # Costo arrotondato dopo somma per evitare double-rounding artifacts.
        sec_tot["cost_usd"] = round(sec_tot["cost_usd"], 4)
        models_list.sort(key=lambda d: d["cost_usd"], reverse=True)
        out_sections[sec] = {**sec_tot, "by_model": models_list}

        grand["calls"] += sec_tot["calls"]
        grand["input_tokens"] += sec_tot["input_tokens"]
        grand["output_tokens"] += sec_tot["output_tokens"]
        grand["cost_usd"] += sec_tot["cost_usd"]

    grand["cost_usd"] = round(grand["cost_usd"], 4)
    return {"by_section": out_sections, "totals": grand}


# --- Per-session cost helpers (per le card delle liste) ---


def meshy_cost(credits) -> float:
    """USD stimato per Meshy. `credits` può essere int o dict (es.
    `{text_to_image: N, image_to_3d: M}`)."""
    if not credits:
        return 0.0
    if isinstance(credits, dict):
        total = sum(int(v or 0) for v in credits.values())
    else:
        try:
            total = int(credits)
        except (TypeError, ValueError):
            return 0.0
    return round(total * MESHY_USD_PER_CREDIT, 4)


def claude_cost_from_tokens(model: str, usage_dict: dict | None) -> float:
    """USD da un dict `{input_tokens, output_tokens}` per un modello noto."""
    if not usage_dict:
        return 0.0
    return _cost(
        model,
        int(usage_dict.get("input_tokens", 0) or 0),
        int(usage_dict.get("output_tokens", 0) or 0),
    )


def session_cost_meshy(meta: dict) -> float:
    """Costo di una sessione Meshy (oggetti): credits Meshy. La quota Claude
    del prompt-refiner non è tracciata per-sessione, quindi è esclusa qui."""
    return meshy_cost(meta.get("credits"))


def session_cost_portrait(meta: dict) -> float:
    """Costo di una sessione Portrait: credits Meshy (Claude refiner non
    tracciato per-sessione, escluso)."""
    return meshy_cost(meta.get("credits"))


# --- Breakdown dettagliato per il mini-report nella scheda detail ---


def _claude_lines_from_meta(meta: dict) -> tuple[list[dict], float]:
    """Estrae le righe Claude (per modello) e il loro costo totale USD da
    `meta['claude_usage']`. Ritorna ([], 0.0) se assente."""
    cu = (meta.get("claude_usage") or {}).get("by_model") or {}
    lines: list[dict] = []
    total = 0.0
    for model, vals in cu.items():
        inp = int(vals.get("input_tokens", 0) or 0)
        out = int(vals.get("output_tokens", 0) or 0)
        c = _cost(model, inp, out)
        lines.append({
            "label": f"Claude · {model}",
            "model": model,
            "input_tokens": inp,
            "output_tokens": out,
            "cost_usd": c,
        })
        total += c
    return lines, round(total, 4)


def breakdown_meshy(meta: dict) -> dict:
    """Breakdown costo di una sessione Meshy: credits per categoria + costo
    Claude prompt-refiner (se tracciato per-sessione)."""
    credits = meta.get("credits") or {}
    meshy_lines: list[dict] = []
    if isinstance(credits, dict):
        for k, v in credits.items():
            iv = int(v or 0)
            if iv:
                meshy_lines.append({"label": f"Meshy · {k}", "credits": iv, "cost_usd": meshy_cost(iv)})
        total_credits = sum(int(v or 0) for v in credits.values())
    else:
        total_credits = int(credits or 0)
        if total_credits:
            meshy_lines.append({"label": "Meshy", "credits": total_credits, "cost_usd": meshy_cost(total_credits)})
    meshy_total = round(total_credits * MESHY_USD_PER_CREDIT, 4)
    claude_lines, claude_total = _claude_lines_from_meta(meta)
    return {
        "kind": "mixed",
        "meshy_lines": meshy_lines,
        "meshy_total_credits": total_credits,
        "meshy_total_usd": meshy_total,
        "claude_lines": claude_lines,
        "claude_total_usd": claude_total,
        "total_usd": round(meshy_total + claude_total, 4),
        "rate_note": (
            f"Meshy ~${MESHY_USD_PER_CREDIT:.2f}/credit · "
            f"Claude tariffato per modello (Opus 4.7 $15/$75, Sonnet 4.6 $3/$15 per M)."
        ),
    }


def breakdown_portrait(meta: dict) -> dict:
    """Breakdown costo Portrait: credits Meshy (generazione 2D + eventuali
    run 3D busto) + costo Claude prompt-refiner."""
    credits_2d = meta.get("credits") or 0
    try:
        credits_2d = int(credits_2d)
    except (TypeError, ValueError):
        credits_2d = 0

    meshy_lines: list[dict] = []
    if credits_2d:
        meshy_lines.append({"label": "Meshy · 2D", "credits": credits_2d, "cost_usd": meshy_cost(credits_2d)})

    bust_credits = 0
    for r in meta.get("bust_3d_runs") or []:
        c = int(r.get("credits", 0) or 0)
        if c:
            meshy_lines.append({
                "label": f"Meshy · 3D run #{r.get('run_id')}",
                "credits": c, "cost_usd": meshy_cost(c),
            })
            bust_credits += c

    total_credits = credits_2d + bust_credits
    meshy_total = meshy_cost(total_credits)
    claude_lines, claude_total = _claude_lines_from_meta(meta)
    return {
        "kind": "mixed",
        "meshy_lines": meshy_lines,
        "meshy_total_credits": total_credits,
        "meshy_total_usd": meshy_total,
        "claude_lines": claude_lines,
        "claude_total_usd": claude_total,
        "total_usd": round(meshy_total + claude_total, 4),
        "rate_note": (
            f"Meshy ~${MESHY_USD_PER_CREDIT:.2f}/credit · "
            f"Claude tariffato per modello (Opus 4.7 $15/$75 per M)."
        ),
    }
