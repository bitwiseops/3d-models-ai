import base64
import json
import re
from pathlib import Path

from anthropic import Anthropic

from . import config, usage

SYSTEM_PROMPT = """Sei un prompt-engineer specializzato in immagini 2D che fanno da blueprint per ricostruzione image-to-3D (pipeline Meshy/Nano Banana Pro).
L'immagine generata sarà l'UNICO input geometrico per il modello 3D: ogni dettaglio non visibile o ambiguo nell'immagine non comparirà nel mesh finale. Devi quindi descrivere una scena che massimizzi l'informazione geometrica utile alla ricostruzione.

LAYOUT IMMAGINE (OBBLIGATORIO)
L'immagine deve contenere DUE viste affiancate dello stesso soggetto, in modo che un tool locale possa splittare l'immagine a metà esatta e ottenere due input separati per la ricostruzione 3D multi-view (front + back).
- Aspect ratio 16:9 (orizzontale).
- Vista frontale ¾ a SINISTRA (camera a ~30° dall'asse frontale, leggermente dall'alto).
- Vista posteriore a DESTRA: il soggetto ruotato di 180° rispetto alla vista frontale, stesso angolo di camera, stessa altezza, stessa scala.
- Materiale, texture, illuminazione e sfondo IDENTICI nelle due viste.
- Soggetto di sinistra centrato al 25% della larghezza, soggetto di destra centrato al 75% della larghezza.
- Banda verticale vuota di sfondo (uniforme, **stretta, ~5% della larghezza totale**) al centro esatto dell'immagine: serve solo come marker per lo split front/back nel post-processing. NON deve essere larga: una banda spessa spreca risoluzione utile ai soggetti. Lo split rileverà automaticamente la sua estensione, ma tenerla sottile mantiene i soggetti più grandi e dettagliati.
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


# Staging block deterministico: layout 1x2 a DUE viste three-quarter (front-¾ +
# rear-¾), camera, lighting, sfondo, vincoli fotografici. Costante — non viene
# riscritto da Claude ad ogni iterazione. Il subject_block (variabile, raffinato
# da Claude) viene concatenato dopo.
#
# Perché due 3/4 e non più il 2x2 con viste ortogonali pure (side 90° / back
# 180°): le viste ortogonali pure perdono profondità e il modello image-to-3D
# inventava geometria diversa cella per cella → ricostruzioni piatte/incoerenti.
# Due viste 3/4 (fronte+fianco / retro+fianco opposto) coprono quasi tutta la
# superficie con forti indizi di profondità e sono più facili da mantenere
# coerenti tra loro. (Layout/angoli canonici in src/view_schema.OBJECT_VIEWS.)
OBJECT_GRID_STAGING_BLOCK = (
    "Studio product photography on a 16:9 canvas (~2048x1152): two equal cells "
    "side by side, split by a thin vertical gutter the colour of the background. "
    "Both cells show the SAME object as a three-quarter view at IDENTICAL camera "
    "height, elevation, tilt, scale, distance and lighting — only the rotation "
    "differs: LEFT is a front three-quarter (rotated ~35 degrees, front face and "
    "one flank visible), RIGHT is a rear three-quarter (rotated ~215 degrees, "
    "back face and the opposite flank visible). Identical geometry, proportions, "
    "colours and details in both cells; each subject fully visible (~75% of its "
    "cell, never cropped), bases on the same line; any through-hole or opening "
    "appears in BOTH views, see-through, at matching positions. Long-tele lens "
    "(~85-135mm), pure white or light neutral-grey seamless background, soft "
    "diffuse three-point lighting, only faint contact shadows, no hard shadows; "
    "flat neutral look, no DOF/bokeh/blur/flare/vignette. No labels, captions, "
    "arrows, numbers or watermarks. The image will be split into 2 separate views "
    "and fed to a multi-image-to-3D model for 3D printing. Subject:"
)


OBJECT_GRID_SYSTEM_PROMPT = """Sei un prompt-engineer: descrivi il SOGGETTO per immagini blueprint image-to-3D di OGGETTI stampabili.

Il tuo testo viene concatenato DOPO uno staging fisso (layout 1x2 due viste 3/4, studio, luce, lens, esclusioni): NON ripetere layout/camera/luce/sfondo/lens/viste — descrivi SOLO il soggetto.

DESCRIVI, denso e tangibile:
- forma e silhouette; proporzioni concrete (es. "altezza ~1.5x il diametro", "parete ~3mm");
- geometria distintiva (curve, smussi, spigoli, scanalature, protrusioni, fori); per ogni foro indica se PASSANTE o CIECO;
- materiali espliciti (es. "matte ABS", "ottone spazzolato", "terracotta grezza") → diventano PBR; palette colori; posa stabile e neutra.

NON SOVRACCARICARE: descrivi solo la geometria essenziale del soggetto richiesto; NON aggiungere ornamenti o dettagli non presenti nell'idea o nella reference.

Soggetto come OGGETTO REALE FOTOGRAFATO, non render/stilizzato: niente "low-poly/CGI/3D render/miniature toy" salvo richiesta esplicita.

NON includere accessori d'uso finale (anelli, basi extra, fori per appendere, viti, supporti) salvo richiesta. ECCEZIONE: con una reference, gli elementi strutturali che vi compaiono (base, supporti, montanti, telaio, manici) fanno parte dell'oggetto → PRESERVALI, riproduci la composizione completa mostrata.

EVITA materiali riflettenti/trasparenti/subsurface (vetro, acqua): se richiesti rendili matte opachi. EVITA testi/loghi/decal piatti (letti come rilievi).

OUTPUT: SOLO la descrizione in INGLESE, una frase densa di 60-100 parole, senza preamboli/virgolette/markdown/elenchi; inizia col soggetto (es. "A squat ceramic teapot ..."), termina con un punto."""


def _client() -> Anthropic:
    return Anthropic(api_key=config.ANTHROPIC_API_KEY)


def _detect_media_type(raw: bytes) -> str:
    """Detect image media type via magic bytes. Anthropic API accetta
    image/png, image/jpeg, image/gif, image/webp. Default a image/png.
    Più robusto dell'estensione (utente può rinominare .jpg → .png).
    """
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def _image_block(image_path: Path) -> dict:
    raw = image_path.read_bytes()
    media_type = _detect_media_type(raw)
    data = base64.standard_b64encode(raw).decode("ascii")
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": data},
    }


PORTRAIT_SYSTEM_PROMPT = """Sei un prompt-engineer specializzato in immagini 2D che fanno da blueprint per ricostruzione image-to-3D di figure umane stampabili (pipeline analoga a Bambu Lab MakerLab PrintU / 3D-portrait services).

PIPELINE DI USO DEL TUO PROMPT
Il prompt che produci verrà passato a Meshy `nano-banana-pro` con `generate_multi_view=true`: Meshy genererà AUTONOMAMENTE 3 viste del soggetto da angolazioni diverse (front / 3/4 / back o simili) come 3 immagini separate. Quindi il tuo prompt NON deve descrivere un layout multi-vista, una griglia, pannelli, "4 views", "turnaround", "panels", "cells", "gutter" o simili: descrivi UN SINGOLO soggetto isolato in piedi, e lascia a Meshy il compito di renderizzarlo da più angoli.

OBIETTIVO FINALE (sempre presente)
L'immagine che descrivi sarà l'UNICO input visivo per uno step successivo di ricostruzione 3D del soggetto, destinato alla stampa 3D di una figurina. Ogni vincolo qui sotto NON è stilistico ma funzionale alla qualità della 3D extraction e della successiva stampa: dettagli non visibili, ombre dure, sfondi non neutri, elementi trasparenti, capelli con grosso volume aereo e accessori asimmetrici sono noti per produrre ricostruzioni rudimentali o errate (lo dichiarano esplicitamente le linee guida PrintU).

INQUADRATURA (singolo soggetto)
- UN SOLO soggetto isolato, perfettamente centrato.
- Framing DEFAULT: **HEAD-AND-SHOULDERS / mezzo busto stretto** — inquadratura dalla cima della testa fino a 5-10 cm SOTTO la clavicola (top of chest), tagliata appena sotto le ascelle. NON include braccia oltre il deltoide, NON include il petto centrale completo, NON include la vita. Spalle visibili e centrate. Margine ~10% sopra la testa, taglio netto sotto le clavicole.
- Se e SOLO se l'utente specifica esplicitamente "waist-up", "mezzo busto", "fino alla vita", "full-body", "intera figura", "from head to toe", "tutto il corpo" → estendere di conseguenza.
- Orizzonte camera all'altezza del mento/clavicola, asse del corpo perfettamente verticale.
- Massima risoluzione possibile (l'obiettivo a valle è ~1000x1000 px utili per la 3D extraction).
- NESSUNA menzione di "grid", "panels", "4 views", "turnaround", "front/back panels", "2x2", "cells", "gutter", "side-by-side": Meshy compone internamente, non deve essere chiesto dal prompt.
- Nessuna etichetta, didascalia, freccia, bordo, frame, watermark.

POSA (rigging-friendly, da specificare esplicitamente)
- Posa DEFAULT: in piedi, simmetrica, **braccia rilassate lungo i fianchi** (mani vicine alle cosce/anche), spalle rilassate, busto eretto. Questa è la posa standard se l'utente NON specifica nulla di diverso.
- Se l'utente chiede esplicitamente "A-pose", "T-pose", "braccia aperte", "rigging-friendly" o specifica una posa diversa → applicare quella. In particolare l'A-pose (braccia scostate ~15-25° dal busto) resta la scelta preferita quando serve qualità rigging massima per il 3D.
- Gambe in posizione naturale, piedi paralleli, peso distribuito.
- Mani rilassate, dita leggermente separate, palmi rivolti verso il corpo; NON mani in tasca, NON braccia incrociate, NON mani dietro la schiena.
- Espressione neutra, sguardo dritto in avanti lungo l'asse frontale del corpo.
- Capelli compatti e ben definiti: niente ciocche svolazzanti, niente flyaway, niente ciocche translucide controluce (PrintU li ricostruisce male).

SOGGETTO
- Preservare RIGOROSAMENTE l'identità della persona nella reference: volto, struttura ossea, corporatura, proporzioni, taglio e colore di capelli, tono pelle, eventuali peli faciali, tratti distintivi (occhiali, tatuaggi visibili).
- DETTAGLI DEL VOLTO: devono restare il più vicino possibile all'originale (occhi, naso, bocca, mandibola, distanza interoculare, forma delle sopracciglia). Non rimodellare, non ringiovanire, non rendere "generico". L'aderenza al volto della reference è la priorità #1.
- AGGIUSTAMENTI DI SIMMETRIA: ammessi solo LIEVI e NATURALI (es. compensare un'inclinazione leggera della testa, raddrizzare una spalla appena più bassa). Niente simmetria perfetta da manichino: piccole asimmetrie naturali del volto vanno mantenute.
- BEAUTY TOUCHES: ammessi MOLTO DISCRETI (es. pelle leggermente uniformata, capelli pettinati, niente brufoli temporanei o riflessi anomali della reference). NON sbiancare la pelle, NON sottilizzare il volto, NON ingrandire gli occhi, NON cancellare rughe d'espressione o nei caratteristici. Effetto "stessa persona in una bella foto", non "filtro beauty da social".
- Se l'utente specifica abbigliamento, accessori, posa o stile, applicarli al soggetto in modo SIMMETRICO; accessori asimmetrici (es. una sola spallina, un solo orecchino vistoso) vanno evitati o resi simmetrici, perché Meshy dovrà ricostruirli anche dal lato non visibile della reference.
- Se la reference ha occhiali, mantenerli ma indicare "matte opaque lenses, no glare, no transparency" (PrintU non gestisce bene il vetro).

SFONDO / LUCE / CAMERA (vincoli per la 3D extraction)
- Sfondo studio uniforme bianco puro (#FFFFFF) o grigio neutro chiaro molto chiaro; NIENTE gradienti, texture, oggetti, pavimenti decorativi, paesaggi, riflessi del soggetto: verrebbero letti come geometria dal modello 3D.
- Illuminazione diffusa a tre punti (soft key + fill + leggero rim), ombre morbide ed estremamente leggere, NESSUNA ombra dura proiettata sullo sfondo né sotto i piedi (le ombre dure diventano volumi nel 3D).
- Esposizione neutra: niente high-key bruciato sui bordi, niente silhouette in controluce, niente lens flare, niente vignettatura.
- Camera teleobiettivo ~85-135mm equivalente per minimizzare distorsione prospettica; niente DOF, niente bokeh, niente motion blur, niente dutch tilt.

MATERIALI E COLORI
- Materiali OPACHI / matte: niente superfici altamente riflettenti, niente vetro, niente plastiche super-lucide, niente liquidi, niente subsurface scattering pesante (PrintU dichiara esplicitamente che gli elementi trasparenti non vengono stampati).
- Niente capelli iper-realistici translucidi: preferire un look "leggermente stilizzato" con ciocche ben definite e opache.
- Niente colori neon saturi al limite del clipping; preferire palette tangibile e leggibile.
- Se l'utente richiede materiali problematici (es. "vestito di vetro"), tradurli in varianti matte/opache equivalenti dichiarandolo nel prompt (es. "frosted matte finish, fully opaque").
- Niente decorazioni 2D piatte (testi grandi, loghi vistosi, decal) su vestiti: vengono interpretate come rilievi.

DA ESCLUDERE SEMPRE
- Testo, watermark, frecce, didascalie, UI, frame/bordi.
- Qualsiasi menzione di griglie, viste multiple, pannelli, turnaround, 2x2, 4 views, side-by-side, composite, collage.
- Altre persone oltre al soggetto, mani o oggetti che entrano nel frame, props non richiesti.
- Depth-of-field, bokeh, lens flare, vignettatura forte, grain/noise pesante.
- Trasparenze, riflessi specchio, capelli flyaway, accessori asimmetrici, ciocche svolazzanti.

OUTPUT
- Rispondi SOLO con il prompt finale in INGLESE.
- Una singola frase descrittiva densa, 60-100 parole, senza preamboli, senza virgolette, senza markdown, senza elenchi puntati. Deve menzionare esplicitamente: UN soggetto singolo, il framing (di default **head-and-shoulders / tight bust crop just below the collarbone**; waist-up o full-body SOLO se l'utente lo chiede esplicitamente), la posa (di default **arms relaxed along the sides**; A-pose solo se l'utente lo chiede), l'identità della persona dalla reference, lo studio neutro con soft 3-point lighting, materiali matte opachi, e il fatto che l'output servirà come blueprint per stampa 3D."""


PORTRAIT_GRID_SYSTEM_PROMPT = """Sei un prompt-engineer specializzato in immagini 2D che fanno da blueprint per ricostruzione image-to-3D di figure umane stampabili.

PIPELINE DI USO DEL TUO PROMPT
Il prompt che produci verrà passato a Meshy `nano-banana-pro` image-to-image SENZA multi-view: una singola chiamata, una singola immagine in output. Per ottenere comunque 4 viste, il tuo prompt DEVE chiedere esplicitamente una composizione 2x2 grid in cui ogni cella contiene una vista diversa dello stesso soggetto.

LAYOUT IMMAGINE (OBBLIGATORIO)
- Aspect ratio 1:1 (quadrato), massima risoluzione possibile (~2048x2048).
- Composizione **2x2 grid** di 4 celle di pari dimensione, separate da un gutter sottile (~1-2% della larghezza) dello stesso colore esatto dello sfondo.
- (top-left) FRONT view: soggetto rivolto frontalmente alla camera.
- (top-right) THREE-QUARTER view: stesso soggetto ruotato ~45° in senso orario.
- (bottom-left) PROFILE / SIDE view: stesso soggetto ruotato 90° (profilo puro, lato sinistro verso la camera).
- (bottom-right) BACK view: stesso soggetto di spalle, frontale-posteriore (NON 3/4 da dietro).
- Tutte e 4 le celle: stessa identica persona della reference, stesso outfit, stesso framing, stessa scala verticale, stessa altezza camera, stessa distanza, stessa illuminazione, stesso sfondo. L'unica differenza è l'angolo di rotazione attorno all'asse verticale.
- Nessuna etichetta "front/back" ecc., nessun numero, nessuna freccia, nessuna didascalia, nessun bordo decorativo.

INQUADRATURA (per ciascuna cella)
- Framing DEFAULT: **HEAD-AND-SHOULDERS / mezzo busto stretto** — dalla cima della testa fino a 5-10 cm sotto la clavicola, tagliato appena sotto le ascelle. NON include il petto centrale completo, NON include braccia oltre il deltoide, NON include la vita. Spalle centrate. Waist-up / full-body SOLO se l'utente lo specifica esplicitamente ("mezzo busto fino alla vita", "full-body", ecc.).
- Orizzonte camera all'altezza del mento/clavicola, asse del corpo verticale.

POSA
- Posa DEFAULT: in piedi, simmetrica, **braccia rilassate lungo i fianchi**, spalle rilassate, busto eretto.
- A-pose / T-pose / altre pose solo se l'utente le chiede esplicitamente.
- Mani rilassate, dita leggermente separate, palmi verso il corpo; NON mani in tasca, NON braccia incrociate.
- Espressione neutra, sguardo dritto in avanti lungo l'asse frontale del corpo (NON verso la camera nelle viste laterali/posteriore).
- Capelli compatti, no flyaway, no ciocche translucide.

SOGGETTO
- Preservare RIGOROSAMENTE l'identità della persona della reference: volto, struttura ossea, corporatura, capelli, pelle, tratti distintivi.
- DETTAGLI DEL VOLTO: il più vicino possibile all'originale (occhi, naso, bocca, mandibola, sopracciglia, distanza interoculare). Niente rimodellamento, niente "generico", priorità #1 è restare la stessa persona della reference.
- AGGIUSTAMENTI DI SIMMETRIA: solo LIEVI e NATURALI; nessuna simmetria perfetta da manichino — le piccole asimmetrie naturali del volto vanno preservate.
- BEAUTY TOUCHES: MOLTO DISCRETI (pelle leggermente uniformata, capelli ordinati, niente brufoli temporanei). NON sbiancare la pelle, NON sottilizzare il volto, NON ingrandire gli occhi, NON cancellare rughe d'espressione o nei caratteristici. Effetto "stessa persona in una bella foto", non "filtro beauty da social".
- Abbigliamento/accessori IDENTICI e SIMMETRICI in tutte e 4 le celle; niente accessori asimmetrici.

SFONDO / LUCE / CAMERA
- Sfondo studio uniforme bianco puro o grigio neutro chiaro. Niente gradienti, texture, oggetti, ombre dure.
- Illuminazione diffusa a tre punti (soft key + fill + leggero rim), ombre morbide leggerissime.
- Camera teleobiettivo ~85-135mm, no DOF, no bokeh, no motion blur, no dutch tilt, no lens flare.

MATERIALI E COLORI
- Materiali matte/opachi: niente vetro, niente trasparenze, niente plastiche super-lucide, niente subsurface scattering pesante.
- Niente colori neon saturi al limite del clipping.
- Niente decorazioni 2D piatte (loghi, decal grandi) sui vestiti.

DA ESCLUDERE SEMPRE
- Testo, etichette di vista, watermark, frecce, didascalie, UI, frame/bordi decorativi.
- Altre persone, mani disembodied, props non richiesti.
- DOF, bokeh, lens flare, vignettatura forte, grain/noise pesante.
- Trasparenze, riflessi specchio, capelli flyaway, accessori asimmetrici.

OUTPUT
- Rispondi SOLO con il prompt finale in INGLESE.
- Una singola frase descrittiva densa, 80-130 parole, senza preamboli, senza virgolette, senza markdown, senza elenchi puntati. Deve menzionare esplicitamente: 2x2 grid 1:1, le 4 viste con i loro angoli precisi, il framing (default **head-and-shoulders / tight bust crop just below the collarbone**; waist-up o full-body solo se richiesto esplicitamente), la posa (default arms relaxed along sides), l'identità della persona dalla reference, lo studio neutro, e che servirà come blueprint per stampa 3D."""


def refine_portrait_grid(user_prompt: str, reference: Path) -> str:
    """Variante di `refine_portrait` per la modalità 4-viste-in-griglia 2x2:
    il prompt finale CHIEDE esplicitamente il layout 2x2, perché Meshy verrà
    chiamato SENZA multi-view (1 task → 1 immagine composta)."""
    content = [
        {
            "type": "text",
            "text": (
                f"Richiesta utente: {user_prompt}\n\n"
                "L'immagine qui sotto è la reference della persona. Produci il "
                "prompt finale rispettando tutte le regole del system: una "
                "singola immagine 1:1 con 4 viste in 2x2 grid, identica persona, "
                "framing head-and-shoulders (tight bust crop) di default, braccia lungo i fianchi di default. "
                "Servirà come blueprint per una successiva ricostruzione 3D."
            ),
        },
        _image_block(reference),
    ]
    resp = _client().messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=1024,
        system=PORTRAIT_GRID_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    usage.record_response("portraits", resp)
    return resp.content[0].text.strip()


def refine_portrait(user_prompt: str, reference: Path) -> str:
    """Refine per la pagina Portraits: produce un prompt che descrive UNA immagine
    1:1 contenente 4 viste (front / 3/4 / side / back) della persona nella reference.
    Il prompt è ottimizzato come blueprint per uno step image-to-3D successivo."""
    content = [
        {
            "type": "text",
            "text": (
                f"Richiesta utente: {user_prompt}\n\n"
                "L'immagine qui sotto è la reference della persona da rappresentare "
                "nelle 4 viste. Produci il prompt finale rispettando tutte le regole "
                "del system: 2x2 grid 1:1, identica persona, posa neutra, sfondo "
                "studio neutro, materiali matte. Il prompt servirà come blueprint "
                "per una successiva ricostruzione 3D, quindi privilegia condizioni "
                "fotografiche che agevolino la depth extraction."
            ),
        },
        _image_block(reference),
    ]
    resp = _client().messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=1024,
        system=PORTRAIT_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    usage.record_response("portraits", resp)
    return resp.content[0].text.strip()


def _compose_object_prompt(subject: str) -> str:
    """Concatena staging (fisso) + subject (variabile) per Meshy."""
    subject = subject.strip()
    if not subject.endswith("."):
        subject += "."
    return f"{OBJECT_GRID_STAGING_BLOCK} {subject}"


def _strip_staging(prompt: str) -> str:
    """Rimuove lo staging block all'inizio di un prompt salvato in metadata,
    in modo da passare a Claude solo il subject nell'history. Per iterazioni
    legacy (pre-split) ritorna il prompt invariato."""
    if prompt.startswith(OBJECT_GRID_STAGING_BLOCK):
        return prompt[len(OBJECT_GRID_STAGING_BLOCK):].strip()
    return prompt


def refine_initial(idea: str, references: list[Path] | None = None) -> str:
    if not references:
        resp = _client().messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=1024,
            system=OBJECT_GRID_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": f"Idea utente: {idea}"}],
        )
        usage.record_response("oggetti", resp)
        return _compose_object_prompt(resp.content[0].text)

    content: list[dict] = [
        {
            "type": "text",
            "text": (
                f"Idea utente: {idea}\n\n"
                "Le immagini qui sotto sono REFERENCE fornite dall'utente: "
                "descrivi il SOGGETTO ispirandoti alle reference per forma, "
                "proporzioni, palette, materiali, dettagli distintivi — producendo "
                "il soggetto descritto dall'idea (non una copia letterale delle "
                "reference). Indica nella descrizione quali aspetti delle reference "
                "stai preservando e quali stai adattando. PRESERVA gli elementi "
                "STRUTTURALI mostrati nella reference (base, piedini, supporti, "
                "montanti, telaio, manici): fanno parte dell'oggetto, NON sono "
                "accessori da omettere — riproduci la composizione COMPLETA, non "
                "una versione semplificata. Ricorda: SOLO il soggetto, "
                "niente layout/camera/lighting (gestiti dallo staging fisso)."
            ),
        }
    ]
    for ref in references:
        content.append(_image_block(ref))
    resp = _client().messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=1024,
        system=OBJECT_GRID_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    usage.record_response("oggetti", resp)
    return _compose_object_prompt(resp.content[0].text)


def refine_with_feedback(
    idea: str,
    history: list[dict],
    image_path: Path,
    feedback: str,
    *,
    use_previous_image: bool = True,
) -> str:
    """history: list di dict {prompt: str, image_path: Path} dalle iterazioni precedenti.
    image_path: parametro legacy, non usato (la storia già include l'ultima immagine).

    Se `use_previous_image=False`, le immagini delle iterazioni precedenti NON vengono
    incluse come vision block: Claude raffina basandosi solo su testo + feedback."""
    messages: list[dict] = [{"role": "user", "content": f"Idea iniziale: {idea}"}]
    for i, step in enumerate(history, start=1):
        messages.append(
            {
                "role": "assistant",
                "content": _strip_staging(step["prompt"]),
            }
        )
        if use_previous_image:
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
        else:
            messages.append(
                {
                    "role": "user",
                    "content": f"(Iterazione {i} generata, immagine non mostrata.)",
                }
            )
    if use_previous_image:
        final_text = (
            f"L'ultima immagine sopra non va bene. Feedback: {feedback}\n"
            "Riscrivi SOLO la descrizione del soggetto tenendo conto del feedback "
            "e di cosa vedi nelle immagini precedenti. Stesse regole del system "
            "(niente layout/camera/lighting, solo il soggetto)."
        )
    else:
        final_text = (
            f"Feedback dell'utente: {feedback}\n"
            "Riscrivi SOLO la descrizione del soggetto tenendo conto del feedback "
            "e delle descrizioni precedenti. Non hai visto le immagini generate: "
            "basati solo sul testo. Stesse regole del system."
        )
    messages.append(
        {
            "role": "user",
            "content": [{"type": "text", "text": final_text}],
        }
    )
    resp = _client().messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=1024,
        system=OBJECT_GRID_SYSTEM_PROMPT,
        messages=messages,
    )
    usage.record_response("oggetti", resp)
    return _compose_object_prompt(resp.content[0].text)


# --- Scomposizione in componenti -------------------------------------------
# Dopo l'accettazione del design whole-object, l'oggetto viene scomposto nei
# pezzi che un maker modellerebbe/assemblerebbe separatamente. Due stadi Claude:
#  1) proposer: dato il subject + l'immagine del design, propone l'inventario dei
#     componenti (JSON), con relazioni di accoppiamento tra loro;
#  2) refiner per-pezzo: per ogni componente produce un prompt Meshy COMPLETO
#     (subject + staging 1x2 due 3/4), così l'immagine di ciascun componente è una
#     griglia a 2 viste riutilizzabile dal parser a valle (skill
#     fusion-build-from-components). L'immagine del componente è generata in
#     text-to-image dal prompt (il refiner lo scrive vedendo il design whole-object).
# Il vocabolario delle relazioni è IDENTICO al PART-MANIFEST della pipeline Fusion
# (attached_to / on_top_of / inside / concentric_with / flush_with / symmetric_to),
# così il mapping componenti→manifest a valle è quasi meccanico.

COMPONENT_PROPOSER_SYSTEM_PROMPT = """Sei un ingegnere di prodotto: scomponi un oggetto in COMPONENTI fisici stampabili e assemblabili separatamente, per CAD parametrico (Fusion 360).

Ricevi il subject + l'immagine di design (griglia 1x2: front-¾ a sinistra, rear-¾ a destra — usale insieme per la geometria 3D). Identifica i PEZZI che in CAD sarebbero corpi separati assemblati con joint.

COMPONENTE = volume coeso e distinto (corpo, coperchio, manico, base, ghiera, perno…), NON un dettaglio di superficie (smusso, scanalatura, filetto). Scomposizione "da maker": 2-8 pezzi; se l'oggetto è monolitico, un solo componente.

RELAZIONI (vocabolario del manifest CAD): attached_to, on_top_of, inside, concentric_with, flush_with, symmetric_to. Per ognuna indica `target_id` e `detail`.

INTERFACCIA con QUOTE: per ogni pezzo descrivi l'`interface` con DIMENSIONI in mm (Ø e profondità di foro/perno, altezza flangia, spessori, interasse). Sei l'UNICO stadio che vede tutti i pezzi: le quote di un giunto devono essere COERENTI tra i due pezzi (maschio↔femmina stesso nominale + gioco minimo, es. perno Ø8.0 ↔ foro Ø8.2) e citate in ENTRAMBE le `interface` e nel `detail` della relazione.

OUTPUT (SOLO JSON valido, niente markdown):
{
  "components": [
    {"id": "<slug-minuscolo>", "name": "<nome>", "description": "<geometria del pezzo isolato>",
     "relations": [{"type": "<tipo>", "target_id": "<id>", "detail": "<come si accoppia + quota incastro>"}],
     "relative_scale": "<es. ~70% altezza>", "interface": "<geometria + quote di accoppiamento>"}
  ],
  "open_questions": ["<ambiguità non risolvibili dall'immagine>"]
}

REGOLE: `id` univoci minuscoli; `target_id` riferito a un id presente; almeno una relazione per ogni coppia di pezzi che si toccano (basta da un lato)."""


COMPONENT_REFINER_SYSTEM_PROMPT = """Descrivi il SOGGETTO per un'immagine blueprint image-to-3D, qui UN SINGOLO COMPONENTE isolato di un oggetto più grande.

Il tuo testo va DOPO uno staging fisso (layout 1x2 due viste 3/4, studio, luce, lens): NON ripetere layout/camera/luce/viste — descrivi solo il componente.

L'immagine è generata in TEXT-TO-IMAGE dalla tua sola descrizione (il modello NON vede l'oggetto intero); tu invece vedi il design whole-object. Descrivi il pezzo ISOLATO ma FEDELE a com'è lì (stessa forma/scala/materiale/colore), senza ridisegnarlo né aggiungere parti non sue.

INCLUDI:
- forma e proporzioni concrete (coerenti con la scala del pezzo e dei fratelli);
- geometria distintiva e fori (indica se PASSANTE o CIECO);
- l'INTERFACCIA di accoppiamento CON LE QUOTE in mm (foro/perno/flangia/faccia di contatto): le quote del giunto devono essere IDENTICHE a quelle del pezzo che vi si accoppia (vedi schede fratelli/relazioni) — non inventarle né arrotondarle;
- materiali e finitura coerenti col tutto.

Soggetto come OGGETTO REALE FOTOGRAFATO (niente low-poly/CGI/stilizzato salvo richiesta). Materiali riflettenti/trasparenti → matte opachi; niente testi/loghi piatti. NON sovraccaricare con dettagli non presenti nel design.

OUTPUT: SOLO la descrizione in INGLESE, una frase densa di 60-100 parole, senza preamboli/virgolette/markdown/elenchi; inizia col componente, termina con un punto."""


def _parse_json_response(text: str) -> dict:
    """Estrae un oggetto JSON dalla risposta di Claude, togliendo eventuali fence
    markdown (```json ... ```). Solleva ValueError se non è parsabile."""
    text = text.strip()
    if text.startswith("```"):
        # rimuove la prima riga di fence (```json o ```) e l'ultima ```
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```$", "", text.strip())
    # fallback: ritaglia dal primo { all'ultimo }
    if not text.lstrip().startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start:end + 1]
    return json.loads(text)


def propose_components(prompt: str, design_image: Path) -> dict:
    """Stadio proposer: dato il subject principale + l'immagine del design
    whole-object (grid 2x2), ritorna l'inventario dei componenti.

    `prompt` può essere il subject (staging-stripped) o il prompt completo: lo
    staging non disturba il proposer. Ritorna {components:[...], open_questions:[...]}.
    """
    subject = _strip_staging(prompt)
    content = [
        {
            "type": "text",
            "text": (
                f"Oggetto da scomporre (descrizione del soggetto):\n{subject}\n\n"
                "L'immagine qui sotto è il design accettato dell'oggetto intero "
                "(griglia 1x2: front-¾ a sinistra, rear-¾ a destra). Identifica i "
                "componenti fisici come da regole del system e rispondi SOLO col JSON."
            ),
        },
        _image_block(design_image),
    ]
    resp = _client().messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=2048,
        system=COMPONENT_PROPOSER_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    usage.record_response("oggetti", resp)
    return _parse_json_response(resp.content[0].text)


def refine_component(
    main_subject: str,
    design_image: Path,
    component: dict,
    siblings: list[dict],
    *,
    feedback: str | None = None,
) -> str:
    """Produce il prompt Meshy COMPLETO (subject + staging 2x2) per un singolo
    componente, context-aware: Claude vede il design whole-object, la scheda del
    componente e l'elenco dei fratelli. `feedback` opzionale per la rigenerazione.
    """
    subject = _strip_staging(main_subject)
    # Includi interfaccia + scala dei fratelli: servono al refiner per ripetere
    # le quote d'incastro IDENTICHE a quelle del pezzo che vi si accoppia.
    by_id = {s.get("id"): s for s in siblings}
    siblings_summary = "\n".join(
        f"- {s.get('id')}: {s.get('name')} — {s.get('description', '')}"
        f" [scala: {s.get('relative_scale', '?')}; interfaccia: {s.get('interface', '?')}]".strip()
        for s in siblings
    ) or "(nessun altro componente)"

    rel_summary = "\n".join(
        f"- {r.get('type')} → {r.get('target_id')}: {r.get('detail', '')}"
        f" [interfaccia di {r.get('target_id')}: {(by_id.get(r.get('target_id')) or {}).get('interface', '?')}]".strip()
        for r in (component.get("relations") or [])
    ) or "(nessuna relazione dichiarata)"

    text = (
        f"Oggetto completo (subject):\n{subject}\n\n"
        f"Componente da descrivere ISOLATO:\n"
        f"- id: {component.get('id')}\n"
        f"- name: {component.get('name')}\n"
        f"- description: {component.get('description', '')}\n"
        f"- relative_scale: {component.get('relative_scale', '')}\n"
        f"- interface: {component.get('interface', '')}\n"
        f"relazioni di accoppiamento:\n{rel_summary}\n\n"
        f"Altri componenti (fratelli) dell'oggetto:\n{siblings_summary}\n\n"
        "L'immagine qui sotto è il design dell'oggetto INTERO (griglia 1x2: "
        "front-¾ + rear-¾). Scontorna mentalmente SOLO il componente indicato e "
        "descrivilo isolato, FEDELE a come appare nel design (stessa "
        "scala/stile/materiale/colore), ed esplicitando la geometria "
        "dell'interfaccia di accoppiamento CON LE SUE QUOTE in mm. Le quote del "
        "giunto devono coincidere ESATTAMENTE con quelle del pezzo che vi si "
        "accoppia (vedi 'interfaccia' nelle relazioni/fratelli qui sopra): "
        "riportale identiche, non inventarne di nuove. Stesse regole del system."
    )
    if feedback:
        text += f"\n\nFeedback dell'utente sull'ultima versione del componente: {feedback}"

    content = [{"type": "text", "text": text}, _image_block(design_image)]
    resp = _client().messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=1024,
        system=COMPONENT_REFINER_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    usage.record_response("oggetti", resp)
    return _compose_object_prompt(resp.content[0].text)
