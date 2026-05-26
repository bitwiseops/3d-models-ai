"""Copy IT condivisa per i template Jinja.

Iniettato come `C` nei globals dell'environment Jinja in `app.py`.
Centralizza etichette ricorrenti per evitare drift IT/EN tra le sezioni.
"""
from __future__ import annotations


class C:
    # Buttons primari
    BTN_GENERATE = "Genera"
    BTN_REGEN = "Rigenera"
    BTN_ACCEPT = "Conferma"
    BTN_DOWNLOAD = "Scarica"
    BTN_CANCEL = "Annulla"
    BTN_RETRY = "Riprova"
    BTN_CLOSE = "Chiudi"
    BTN_LOAD_MORE = "Carica altri"

    # Labels generiche
    LABEL_REFERENCE = "Reference"
    LABEL_REFERENCE_OPT = "Reference (opzionale)"
    LABEL_FEEDBACK = "Feedback"
    LABEL_IDEA = "Idea"
    LABEL_PROMPT = "Prompt"

    # Placeholder
    PLACEHOLDER_IDEA_OBJ = "es. supporto cuffie da scrivania, base ø80, altezza 200"
    PLACEHOLDER_IDEA_SCAD = "es. supporto cuffie ø80 h200, gancio inclinato 15°"
    PLACEHOLDER_PROMPT_PORTRAIT = (
        "es. wearing red coat, neutral expression "
        "— lascia vuoto per default (mezzo busto, braccia lungo i fianchi)"
    )
    PLACEHOLDER_FEEDBACK = "es. più alto, più sottile, smussa gli angoli"

    # Status
    STATUS_DRAFT = "Bozza"
    STATUS_RUNNING = "In elaborazione"
    STATUS_ACCEPTED = "Generazione 3D…"
    STATUS_DONE = "Pronto"
    STATUS_FAILED = "Errore"
