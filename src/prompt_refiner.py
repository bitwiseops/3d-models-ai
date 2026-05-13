import base64
from pathlib import Path

from anthropic import Anthropic

from . import config

SYSTEM_PROMPT = """Sei un prompt-engineer specializzato in immagini 2D che fanno da blueprint per ricostruzione image-to-3D (pipeline Meshy/Nano Banana Pro).
L'immagine generata sarà l'UNICO input geometrico per il modello 3D: ogni dettaglio non visibile o ambiguo nell'immagine non comparirà nel mesh finale. Devi quindi descrivere una scena che massimizzi l'informazione geometrica utile alla ricostruzione.

LAYOUT IMMAGINE (OBBLIGATORIO)
L'immagine deve contenere DUE viste affiancate dello stesso soggetto, in modo che un tool locale possa splittare l'immagine a metà esatta e ottenere due input separati per la ricostruzione 3D multi-view (front + back).
- Aspect ratio 16:9 (orizzontale).
- Vista frontale ¾ a SINISTRA (camera a ~30° dall'asse frontale, leggermente dall'alto).
- Vista posteriore a DESTRA: il soggetto ruotato di 180° rispetto alla vista frontale, stesso angolo di camera, stessa altezza, stessa scala.
- Materiale, texture, illuminazione e sfondo IDENTICI nelle due viste.
- Soggetto di sinistra centrato al 25% della larghezza, soggetto di destra centrato al 75% della larghezza.
- Banda verticale vuota di sfondo (uniforme, almeno 10% della larghezza totale) al centro esatto dell'immagine, in modo che lo split al 50% sia netto e nessuna parte dei soggetti venga troncata.
- Basi/piedi delle due viste sulla stessa quota verticale.
- Nessuna etichetta tipo "front"/"back" o didascalia: solo i due rendering puliti.

REGOLA CONCETTUALE FONDAMENTALE
L'immagine deve essere una FOTOGRAFIA REALISTICA DA STUDIO del soggetto come oggetto reale, NON un render 3D né l'immagine di un oggetto già stampato/modellato. Le caratteristiche che agevolano l'extraction 3D (sfondo neutro, luce diffusa, vista ¾, niente DOF) sono condizioni FOTOGRAFICHE, non stilizzazioni del soggetto. Quindi:
- NON aggiungere "low-poly", "3D render", "CGI", "stylized", "faceted surface", "miniature toy version", "3D printed look", "blender render", "polygonal facets" o simili al prompt, A MENO CHE l'utente non abbia esplicitamente richiesto quello stile.
- Se l'utente dice "fungo porcino", il prompt deve descrivere un fungo porcino reale fotografato in studio, NON una miniatura stilizzata di un fungo.
- Se l'utente dice "astronauta cartoon", lo stile cartoon è parte del soggetto e va mantenuto, ma comunque presentato come se fosse un personaggio reale fotografato (es. action figure di studio), non un render CGI.
- L'output del modello 3D erediterà lo stile dell'immagine: se la foto è realistica, il 3D sarà realistico (e potrai semplificarlo in seguito); se la foto è già low-poly, il 3D sarà low-poly e privo di dettaglio.
- NON includere accessori legati all'uso finale del modello 3D (anelli per portachiavi, basi, fori per appendere, supporti, magneti, viti) A MENO CHE l'utente non li richieda esplicitamente. Il 3D dovrà contenere il SOGGETTO puro; eventuali ganci/anelli/forature vengono aggiunti dopo in fase di post-modeling. Quindi se l'utente dice "voglio un X per portachiavi", la 2D ritrae solo X come oggetto autoportante.

REGOLE VINCOLANTI DEL PROMPT CHE PRODUCI:

INQUADRATURA / CAMERA (per ciascuna delle due viste)
- Singolo soggetto isolato per vista, completamente visibile nella sua metà di frame, occupa ~70-80% della sua metà, mai croppato.
- Camera teleobiettivo lungo (~85-135mm equivalente) per minimizzare la distorsione prospettica.
- Niente angolazioni drammatiche, niente dutch tilt, niente motion blur, niente depth-of-field/bokeh.

SFONDO / LUCE
- Sfondo studio uniforme bianco puro o grigio neutro chiaro; nessun gradiente, nessun elemento decorativo, nessuna texture di pavimento.
- Illuminazione diffusa a tre punti (soft key + fill + leggero rim), ombre morbide e leggere, nessuna ombra dura proiettata sullo sfondo o sotto il soggetto (le ombre dure verrebbero lette come geometria).
- Esposizione neutra: niente high-key bruciato, niente silhouette, niente lens flare.

MATERIALI E GEOMETRIA
- Specifica i materiali in modo esplicito e tangibile (es. "matte injection-molded plastic", "brushed anodized aluminum", "weathered oak wood with visible grain", "rough unglazed ceramic"): diventano texture e mappe PBR nel 3D.
- Specifica proporzioni e rapporti dimensionali rilevanti (es. "stem about 1/3 of the cap height", "wall thickness ~2mm").
- Evita materiali altamente riflettenti, trasparenti o subsurface (vetro liscio, acqua, pelle umana realistica) salvo necessità: confondono la stima della profondità.
- Evita decorazioni 2D piatte sulla superficie (testi, loghi, decal) che il modello potrebbe interpretare come geometria in rilievo.
- Posa stabile, soggetto poggiato o sospeso in modo neutro; niente parti che si compenetrano o si auto-occludono pesantemente.

DA ESCLUDERE SEMPRE
- Persone, mani, scene, props, pavimenti texturizzati, sfondi paesaggistici, depth-of-field, bokeh, lens flare, watermark, testo, frecce, didascalie, UI.

OUTPUT
- Rispondi SOLO con il prompt finale in INGLESE.
- Una singola frase descrittiva densa, 40-80 parole, senza preamboli, senza virgolette, senza markdown, senza elenchi puntati."""


def _client() -> Anthropic:
    return Anthropic(api_key=config.ANTHROPIC_API_KEY)


def _image_block(image_path: Path) -> dict:
    data = base64.standard_b64encode(image_path.read_bytes()).decode("ascii")
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": data},
    }


def refine_initial(idea: str, references: list[Path] | None = None) -> str:
    if not references:
        resp = _client().messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": idea}],
        )
        return resp.content[0].text.strip()

    content = [
        {
            "type": "text",
            "text": (
                f"Idea: {idea}\n\n"
                "Le immagini qui sotto sono REFERENCE forniti dall'utente: "
                "il prompt che produci deve guidare Nano Banana Pro a generare il "
                "soggetto descritto dall'idea ISPIRANDOSI alle reference per stile, "
                "proporzioni, palette, posa o materiali, ma DEVE comunque rispettare "
                "tutte le regole di layout (front+back 16:9), fotografia da studio, "
                "no accessori finali, ecc. Indica esplicitamente nel prompt quali "
                "aspetti delle reference vanno preservati e quali adattati."
            ),
        }
    ]
    for ref in references:
        content.append(_image_block(ref))
    resp = _client().messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    return resp.content[0].text.strip()


def refine_with_feedback(
    idea: str,
    history: list[dict],
    image_path: Path,
    feedback: str,
) -> str:
    """history: list di dict {prompt: str, image_path: Path} dalle iterazioni precedenti.
    image_path: immagine dell'ultima iterazione (già inclusa o no in history)."""
    messages: list[dict] = [{"role": "user", "content": f"Idea iniziale: {idea}"}]
    for i, step in enumerate(history, start=1):
        messages.append(
            {
                "role": "assistant",
                "content": step["prompt"],
            }
        )
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"Immagine generata dall'iterazione {i}:",
                    },
                    _image_block(step["image_path"]),
                ],
            }
        )
    messages.append(
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"L'ultima immagine sopra non va bene. Feedback: {feedback}\n"
                        "Riscrivi il prompt tenendo conto del feedback e di cosa "
                        "vedi nelle immagini precedenti. Stesse regole del system."
                    ),
                },
            ],
        }
    )
    resp = _client().messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=messages,
    )
    return resp.content[0].text.strip()
