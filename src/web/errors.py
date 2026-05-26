"""Classificazione errori job → titolo + recovery hint actionable per UI."""
from __future__ import annotations

# kind dei CTA gestiti in _error_card.html
# "retry": auto-retry button
# "log": apri il <details> log
# "stats": apri il popup statistiche
# "reupload": invita a ricaricare il file
# "none": solo messaggio, niente CTA
ERROR_HINTS: dict[str, tuple[str, str, str]] = {
    "meshy_timeout": (
        "Meshy sta rallentando",
        "Il task ha superato il timeout. Riprova fra qualche secondo; se persiste, "
        "potrebbe esserci congestione lato Meshy.",
        "retry",
    ),
    "meshy_429": (
        "Quota Meshy esaurita",
        "Hai raggiunto il limite di credit Meshy. Controlla il balance nelle "
        "statistiche o aspetta il reset.",
        "stats",
    ),
    "meshy_auth": (
        "Chiave Meshy non valida",
        "Verifica che MESHY_API_KEY in .env sia corretta e ricarica la pagina.",
        "none",
    ),
    "claude_429": (
        "Limite Claude raggiunto",
        "Aspetta un minuto prima di riprovare. Considera di passare a un modello "
        "più piccolo per SCAD se serve volumi alti.",
        "retry",
    ),
    "claude_auth": (
        "Chiave Claude non valida",
        "Verifica ANTHROPIC_API_KEY in .env.",
        "none",
    ),
    "openscad_crash": (
        "Compilazione OpenSCAD fallita",
        "Il compiler ha rifiutato il codice. Apri il log per dettagli, oppure "
        "rigenera la parte con un feedback testuale.",
        "log",
    ),
    "upload_too_big": (
        "File troppo grande",
        "Scegli un'immagine sotto i 10 MB o ridimensionala prima del caricamento.",
        "reupload",
    ),
    "network": (
        "Errore di rete",
        "Connessione interrotta verso il provider. Riprova.",
        "retry",
    ),
    "generic": (
        "Qualcosa è andato storto",
        "Riprova; se l'errore persiste, controlla i log per dettagli tecnici.",
        "retry",
    ),
}


def classify(exc_text: str | None) -> tuple[str, str, str, str]:
    """Mappa il testo dell'errore (stack trace, stderr ecc.) a una tupla
    (key, title, message, cta_kind). `key` utile per stile/iconografia."""
    if not exc_text:
        k = "generic"
        return (k,) + ERROR_HINTS[k]
    txt = exc_text.lower()

    # Match per keyword in ordine di specificità.
    rules: list[tuple[str, tuple[str, ...]]] = [
        ("meshy_429", ("429", "rate limit", "quota")),
        ("meshy_auth", ("401", "unauthorized", "meshy")),
        ("meshy_timeout", ("meshy", "timeout")),
        ("claude_429", ("anthropic", "rate_limit", "429", "overloaded")),
        ("claude_auth", ("anthropic", "401")),
        ("openscad_crash", ("openscad", "cgal", "warning: object", "compilazione", "parser error")),
        ("upload_too_big", ("too large", "413", "413 request")),
        ("network", ("connection", "connectionerror", "dns", "refused", "unreachable")),
    ]
    for key, keywords in rules:
        if all(kw in txt for kw in keywords[:1]) and any(kw in txt for kw in keywords):
            return (key,) + ERROR_HINTS[key]
    return ("generic",) + ERROR_HINTS["generic"]
