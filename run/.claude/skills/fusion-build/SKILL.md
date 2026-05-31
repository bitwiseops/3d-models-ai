---
name: fusion-build
description: Costruisce in modo INCREMENTALE un modello 3D PARAMETRICO in Fusion 360 (live, via MCP) a partire dalla descrizione + 4 viste di una sessione Oggetti esistente. Copia-e-adatta della pipeline blender-build con backend CAD = Fusion: stesso flusso (parsing → manifest → piano → esecuzione → gate), ma la geometria è B-rep ESATTA per costruzione (sketch → extrude/revolve, NON mesh) e il posizionamento tra le parti si realizza con JOINTS/vincoli (coincident, concentric, flush), NON con trasformazioni assolute. Parte in AUTO MODE all'invocazione. Prima un parser Opus (subagent, sola percezione, niente MCP) estrae un PART-MANIFEST — l'inventario di COSA deve esistere (parti, primitive, relazioni); è la single source of truth. Poi un planner Opus (thinking medio) crea il piano focalizzato su GEOMETRIA e SIMMETRIA, ed executor Sonnet in sequenza applicano ogni step via API Fusion (`adsk.*`), si auto-verificano con render della viewport (saveAsImageFile) ripetendo fino a 3 volte, e salvano uno snapshot .f3d per modifica. Termina con un GATE DI CONVERGENZA che confronta input↔output e raccomanda in modo deterministico stop o re-iterazione. USARE quando l'utente chiede di "costruire/modellare in Fusion (360)" un oggetto da una sessione esistente. PRECONDIZIONE: Fusion 360 aperto su macOS/Windows, add-in MCP abilitato e server raggiungibile (vedi SETUP_MACOS.md).
---

# fusion-build — costruzione 3D parametrica incrementale live in Fusion 360

Pipeline a **più stadi** orchestrata da Claude Code (questo agente) sul tool di esecuzione
codice del server MCP Fusion: **parsing → piano → esecuzione → chiusura → gate**. Input: una
sessione Oggetti esistente (`outputs/<ts-slug>/`) con prompt raffinato + i 4 crop del grid 2x2
(front / three_quarter / side / back). Output: modello Fusion **parametrico** (B-rep) costruito
incrementalmente, con storico `.f3d` per ogni modifica.

> **Copia-e-adatta di `blender-build`.** Il flusso e la logica CAD-agnostica (parsing →
> PART-MANIFEST SSOT → piano → executor in sequenza → gate deterministico) sono gli stessi:
> cambia **solo** il layer di costruzione geometrica, che qui sfrutta il paradigma
> **parametrico / B-rep** di Fusion invece delle mesh+modifier di Blender. `blender-build`
> resta indipendente e invariato.

Helper deterministico: `src/fusion_build.py` (CLI `uv run python -m src.fusion_build ...`).
Niente IO a mano: usa sempre la CLI per init/manifest/plan/paths/record/status.

Argomento della skill: il path della sessione Oggetti sorgente (es.
`outputs/20260529-...-teapot`). Se manca, chiedi all'utente quale sessione usare.

> **Tool MCP di esecuzione.** In questa skill `mcp__fusion__execute_fusion_code` indica il tool
> del server Fusion che esegue **codice Python `adsk.*` arbitrario** sulla **coda main-thread**
> dell'add-in. Il render di verifica si ottiene DENTRO lo script con
> `app.activeViewport.saveAsImageFile(<png>, w, h)` — non serve un tool screenshot separato.
> Il nome esatto del tool dipende dal server scelto: **confermalo sul Mac e, se diverso,
> sostituisci QUESTO unico riferimento** (vedi `SETUP_MACOS.md`). Tutto il resto della skill non
> dipende dalla superficie di tool del server.

## Auto mode (default all'invocazione)

Quando la skill viene invocata, esegui l'**intera pipeline in autonomia** —
preflight → parsing/manifest → piano → tutti gli step in sequenza → chiusura → **gate di
convergenza** — **senza fermarti a chiedere conferma** tra una fase e l'altra né tra uno step
e il successivo. Non chiedere "vuoi che proceda?": procedi. Al gate (§5) la pipeline **si ferma
sempre** col verdetto e la raccomandazione: NON riavvia da sola; la re-iterazione (§6) parte
solo se l'utente la chiede.

Restano gli unici punti in cui ti fermi (sicurezza / perdita dati):
- **Preflight fallito** (Fusion non connesso) → fermati e istruisci l'utente (vedi §0.1).
- **Documento con lavoro esistente dell'utente** da cancellare → chiedi conferma (perdita dati).
  Documento vuoto/nuovo → procedi senza chiedere (vedi §0.2).
- **Path sessione sorgente mancante** → chiedi quale sessione usare.

## Vincoli HARD di Fusion (valgono per OGNI chiamata MCP — non violarli)

- **API non thread-safe** → ogni operazione gira sulla **coda main-thread** dell'add-in (è il
  server a gestirla). Non aggirarla.
- **Una sola chiamata alla volta, attesa.** Gli executor sono **rigorosamente in sequenza**
  (mai paralleli): una sola scena/documento condiviso mutabile. **Niente operazioni
  sovrapposte**: il batching di operazioni concorrenti fa crashare l'add-in.
- **Budget ~30s per chiamata.** Tieni ogni script entro il timeout: se uno step rischia di
  superarlo (timeline pesante, molte feature), **spezzalo in script più piccoli** e fai
  snapshot tra l'uno e l'altro. Evita loop enormi in un singolo script.
- **Unità interne = centimetri.** L'API Fusion lavora in **cm**: 1 mm = 0.1 cm. Scrivi le quote
  in cm, tonde e proporzionali. Il gate converte la bbox cm→mm (×10) prima di confrontarla con
  `target_mm`.
- **Niente "undo" programmatico fragile.** Per annullare NON usare comandi di undo: **riapri
  l'ultimo `.f3d` buono**, oppure riporta il marker della timeline indietro e cancella le
  feature dopo di esso (vedi §3 e `run/fusion_build_lessons.md`).

## 0. Preflight (OBBLIGATORIO — la pipeline gira SOLO con Fusion connesso)

1. Chiama `mcp__fusion__execute_fusion_code` con uno script minimo che legge lo stato (es.
   `app = adsk.core.Application.get(); design = app.activeProduct; return design.rootComponent.name`).
   - Se fallisce / va in timeout ⇒ Fusion non è connesso o l'add-in non è in esecuzione.
     **Fermati** e di' all'utente: apri Fusion 360, abilita ed esegui l'add-in del server MCP
     (Utilities → ADD-INS → Scripts and Add-Ins → Run), verifica che il server sia raggiungibile,
     riavvia Claude da `run/`. Vedi **`SETUP_MACOS.md`**. Non creare nessuna build session finché
     il preflight non passa.
2. Se il documento attivo è **vuoto/nuovo** (nessun body, nessuna feature in timeline), in auto
   mode procedi senza chiedere. Se contiene già **lavoro dell'utente** (body/feature/component),
   chiedi conferma prima di lavorarci sopra o di crearne uno nuovo (perdita dati). In dubbio,
   crea un **nuovo documento** (`app.documents.add(...)`) e lavora lì.

## 1. Parsing percettivo — PART-MANIFEST (parser = Opus subagent, sola percezione)

> Stadio **CAD-agnostico**: identico nel comportamento a blender-build.

Prima di pianificare, estrai un **inventario strutturato di COSA deve esistere**. Il manifest è
la **single source of truth**: planner ed executor consumano QUESTO, non ri-ragionano dalle
immagini. Separare *percezione* (cosa c'è) da *costruzione* (come farlo) evita che feature
viste in una sola vista vengano "dimenticate" durante la pianificazione.

1. Bootstrap della build session:
   ```
   uv run python -m src.fusion_build init <source_dir>
   ```
   → ritorna `{"build_dir": "outputs/fusion/<ts-slug>"}`. Copia già le 4 viste in
   `refs/`, scrive `prompts/00_source.txt` e `metadata.json` (**status=parsing**).
2. Lancia **un** subagent **parser** con il tool **Agent**:
   - `subagent_type: general-purpose`, `model: opus` — **sola percezione**: gli servono solo
     `Read` (le 4 viste) e `Write` (il manifest). **NIENTE MCP / niente Fusion** in questo stadio.
   - prompt = **template parser** sotto, con la descrizione (`prompts/00_source.txt`) e i path
     delle 4 viste `refs/<view>.png`.
3. Salva il manifest prodotto dal parser (porta lo stato `parsing → planning`):
   ```
   uv run python -m src.fusion_build save-manifest <build_dir> <manifest.json>
   ```
   → ritorna `{"manifest_path", "part_ids", "open_questions"}`. **Logga le `open_questions`**
   (ambiguità che il piano dovrà risolvere).

Schema `manifest.json` (parametri **relativi/proporzionali**, non quote assolute: le quote sono
compito del planner/gate). `primitive_type` resta un nome di primitiva **neutro** (lo stesso
vocabolario di blender-build): l'executor lo mappa al modo di costruzione Fusion più adatto
(cilindro/cono/sfera/toro → **revolve** di un profilo; cubo/plane → **extrude** di uno sketch):
```json
{
  "parts": [
    {
      "id": "body",
      "primitive_type": "cube|cylinder|sphere|cone|torus|plane|custom",
      "parameters": {"shape": "tronco-conico", "proportions": "altezza ~1.5x larghezza"},
      "relations": [{"type": "attached_to|on_top_of|inside|concentric_with|flush_with|symmetric_to", "target_id": "base", "detail": "centrato"}],
      "details": ["scanalatura orizzontale a metà altezza", "raccordo 2 mm sul bordo superiore"],
      "source_views": ["front", "side"],
      "confidence": "high|medium|low"
    }
  ],
  "open_questions": ["la maniglia è una o due? la vista back non è conclusiva"]
}
```

### Template prompt per il parser (Opus, sola percezione)

> Sei un analista percettivo. NON modelli nulla: il tuo unico compito è produrre l'**inventario
> strutturato** delle parti di un oggetto a partire dalla sua descrizione e da 4 viste ortogonali
> (front / three_quarter / side / back). Niente CAD, niente codice: solo osservazione.
>
> DESCRIZIONE: {prompts/00_source.txt}
> VISTE (aprile tutte con Read): {refs/front.png, refs/three_quarter.png, refs/side.png, refs/back.png}
>
> Procedura:
> 1. **Enumera le parti per ciascuna vista** separatamente (cosa vedi in front, poi in side, ecc.).
> 2. **Riconcilia tra le viste**: decidi quali osservazioni sono la STESSA parte vista da angoli
>    diversi e quali sono parti DISTINTE. Una feature visibile in una sola vista va comunque tenuta
>    (non scartarla perché le altre viste non la mostrano).
> 3. Per ogni parte: `id` univoco e parlante, `primitive_type` (la primitiva geometrica più vicina),
>    `parameters` **relativi/proporzionali** (forma e proporzioni, NON quote in mm), `relations`
>    verso altre parti (attacco/sopra/dentro/concentrico/a-filo/simmetria), `details` geometrici
>    (raccordi, fori, scanalature, smussi), `source_views` (dove l'hai vista), `confidence`.
> 4. Ciò che resta **ambiguo o non risolvibile** dalle viste → `open_questions` (testo libero).
>
> RITORNA come testo finale = valore SOLO il JSON del manifest (schema: `{"parts":[...],
> "open_questions":[...]}`). Niente altro testo. Scrivilo anche nel file manifest che ti è indicato.

## 2. PIANO (planner = Opus, lo fai TU nel main loop, thinking MEDIO)

Pianifica con **sforzo di ragionamento medio**, non estremo: la pianificazione è
decomposizione in step + scelta del modo di costruzione parametrico, non richiede thinking
massimo. Il budget di ragionamento più alto va semmai speso dagli executor sugli step rischiosi.

Il planner **consuma il manifest** (§1) come SSOT: ogni parte del manifest deve essere coperta
da almeno uno step. NON re-inventare l'inventario dalle immagini — le viste servono solo per
capire *come* realizzare le parti già elencate.

1. Leggi `manifest.json` e la descrizione, e **guarda le 4 viste** (`Read` su `refs/front.png`,
   `three_quarter.png`, `side.png`, `back.png`) per pianificare con il contesto visivo.
2. Produci il **piano**: decomposizione in step ordinati **coarse → fine**, ognuno
   espresso con il **paradigma parametrico di Fusion** (vedi Toolbox). Ogni step dichiara in
   `manifest_parts` gli `id` delle parti del manifest che realizza. Scrivi il piano in un file
   temporaneo e salvalo:
   ```
   uv run python -m src.fusion_build save-plan <build_dir> <plan.json>
   ```
   → ritorna `{"plan_path", "uncovered_parts"}`. Se `uncovered_parts` **NON è vuoto**, alcune
   parti del manifest non sono coperte da nessuno step: rivedi il piano prima di costruire
   (è un avviso, non un errore — ma di norma vanno coperte tutte).
   Schema `plan.json`:
   ```json
   {
     "source_session": "<name>",
     "strategy": "PRIORITÀ a geometria e simmetria: silhouette corretta, proporzioni, MIRROR feature sugli assi di simmetria; ESATTEZZA per costruzione (sketch fully-constrained → extrude/revolve, NON mesh); posizionamento tra parti via JOINTS/vincoli relativi (concentric/planar/rigid), MAI trasformazioni assolute; ogni parte = un Component proprio; dettagli (raccordi/fori/scanalature) = feature native; materiali/colori sono rifinitura finale opzionale; build incrementale coarse→fine in cm",
     "steps": [
       {
         "id": 1,
         "title": "Corpo base",
         "goal": "Volume principale che cattura la silhouette",
         "target": "Body",
         "manifest_parts": ["body"],
         "fusion_approach": "Sketch su piano XY (profilo metà-sezione) → REVOLVE attorno all'asse Z; design parametrico, quote in cm; Mirror feature se serve simmetria bilaterale",
         "features": ["sketch", "revolve", "fillet"],
         "joints": [],
         "reference_views": ["front", "side"],
         "depends_on": [],
         "acceptance": "silhouette frontale e laterale combaciano; corpo di rivoluzione attorno a Z; proporzioni ~1.5x altezza"
       }
     ]
   }
   ```
   Linee guida per il piano:
   - **Copri il manifest**: ogni parte ha almeno uno step che la cita in `manifest_parts`.
   - **Una parte = un Component.** Pensa l'oggetto come **assieme di componenti** posizionati con
     **joints**, non come un'unica mesh.
   - **Esattezza per costruzione**: ogni forma nasce da **sketch (vincolato e quotato) →
     extrude / revolve** (revolve per i solidi di rivoluzione: corpo teiera, bottiglia,
     coperchio). `shell` per cavità, `loft`/`sweep` per transizioni. **Niente mesh, niente
     editing per-vertice.**
   - **Priorità: forma e simmetria prima di tutto.** Cattura silhouette e proporzioni corrette;
     usa la **Mirror feature** sul piano di simmetria (analogo del MIRROR di Blender, default per
     gli oggetti simmetrici). Materiali e colori sono secondari e facoltativi.
   - **Posizionamento via JOINTS** (vedi §3 e tabella relazioni→joint): ogni step che crea una
     parte collegata dichiara in `joints` come la àncora alla parte target. **Mai** trasformazioni
     assolute.
   - **Dettagli** (`details` del manifest: raccordi, fori, scanalature) → feature native
     (Fillet/Hole/revolve-cut), tipicamente negli step finali.
   - 5–15 step tipicamente. Primo step = volume base; ultimi step = dettagli/raccordi.
   - Ogni step deve essere **applicabile da solo** sopra lo stato dello step precedente.
   - Indica `reference_views` pertinenti (quali viste verificare per quello step).

## 3. ESECUZIONE incrementale (executor = Sonnet, IN SEQUENZA)

**Sequenza rigorosa**: una sola connessione MCP e un solo documento Fusion condiviso mutabile ⇒
lancia **un** subagent per volta e **aspetta** che finisca prima del successivo. Mai in parallelo
(vedi vincoli HARD).

Per ogni step in ordine:

1. Calcola i path artefatti:
   ```
   uv run python -m src.fusion_build paths <build_dir> <step_id>
   ```
   → `{"f3d": "...abs...", "stl": "...", "png": "...", "json": "..."}`.
2. Lancia **un** subagent executor con il tool **Agent**:
   - `subagent_type: general-purpose` (ha il tool `mcp__fusion__execute_fusion_code` + Write)
   - `model: sonnet`
   - prompt = **template executor** sotto, con piano completo, step corrente, **le parti del
     manifest citate in `step.manifest_parts`** (estratte da `manifest.json`), i path delle
     viste di riferimento (`<build_dir>/refs/<view>.png`), e i path artefatti dello step.
3. Quando il subagent ritorna il record strutturato, registralo:
   ```
   uv run python -m src.fusion_build record <build_dir> <record.json>
   ```
4. Passa allo step successivo.

**Best-effort** (come blender-build / SCAD): se uno step non converge entro i **3 tentativi**,
salva il miglior tentativo, marca `status: "failed"` nel record con le note, e **prosegui**
— lo storico resta completo.

### Mappa relazioni del manifest → JOINTS / vincoli Fusion (il cuore della migrazione)

Realizza il `relations[].type` del manifest come **joint relativo**, NON come posizione assoluta.
Crea ogni componente **all'origine del suo sketch**, poi àncoralo al target con un joint che
**referenzia geometria** (preferisci **JointOrigin** su facce/spigoli/punti nominati: riferimenti
stabili che sopravvivono alla rigenerazione parametrica):

| relation (manifest)        | joint / vincolo Fusion                                              |
|----------------------------|---------------------------------------------------------------------|
| `concentric_with` / coaxial| **Cylindrical** o **Rigid** joint su spigoli/assi concentrici       |
| `on_top_of` / `flush_with` | **Planar** joint tra le facce a contatto (offset 0 = a filo)        |
| `attached_to`              | **Rigid** joint con JointOrigin coincidenti                         |
| `inside`                   | **concentric** + **planar** con offset per "sedere" dentro          |
| `symmetric_to`             | **Mirror feature** sul piano di simmetria (o coppia simmetrica)     |

**VIETATO**: impostare `occurrence.transform2` a una matrice mondo assoluta per "piazzare" una
parte — bakera la posizione assoluta e vanifica la migrazione. Una trasformazione transitoria è
ammessa **solo** come fallback ultimo ed **esplicitamente documentato** nelle `notes` del record.

### Render di verifica (pose ESATTE delle viste di riferimento)

Per restare apples-to-apples col gate, riproduci le **stesse pose** dei ref. Convenzione di build:
**up = +Z, "fronte" rivolto verso −Y**. Dentro lo script, per ogni vista in `reference_views`:
imposta `app.activeViewport.camera` (`eye`/`target`/`upVector`; `cameraType` ortografica per
front/right/back, isometrica fissa per three_quarter), `camera.isFitView = True` (o
`viewport.fit()`), riassegna `viewport.camera = camera`, poi
`viewport.saveAsImageFile(<png>, w, h)`. Le direzioni canoniche (verso di sguardo eye→target) sono
in `src/fusion_build.py` `VIEW_CAMERA` (front=+Y, side=−X, back=−Y, three_quarter=iso) — usa
**le stesse** negli step e nel gate, così i render combaciano con `refs/`.

### Template prompt per l'executor (Sonnet)

> Sei un executor specializzato in modellazione **PARAMETRICA** in Fusion 360 via MCP. Applichi
> **un solo step** di un piano a un documento Fusion già avviato, eseguendo codice `adsk.*`
> tramite `mcp__fusion__execute_fusion_code`. **Esattezza per costruzione**: sketch (vincolati e
> quotati) → extrude/revolve, feature native (fillet/hole/shell/mirror), MAI mesh o editing
> per-vertice. Il tuo focus è **geometria e simmetria** (forma, proporzioni, simmetria):
> materiali e colori sono secondari e facoltativi — NON spendere tentativi per perfezionarli.
>
> PIANO COMPLETO (contesto): {plan.json}
> STEP DA APPLICARE: {step}
> PARTI DEL MANIFEST DA REALIZZARE (single source of truth — costruisci QUESTE): {parts di step.manifest_parts}
> VISTE DI RIFERIMENTO: {refs/front.png, ...} (apri quelle in step.reference_views — servono solo
>   al CHECK VISIVO della forma, NON per inventare feature non presenti nel manifest)
> PATH ARTEFATTI (assoluti, usali esattamente): f3d={...} stl={...} png={...} json={...}
>
> VINCOLI HARD (non violarli):
> - **Unità = cm** (1 mm = 0.1 cm). Quote tonde e proporzionali.
> - **Una operazione logica per chiamata**, attesa; niente operazioni sovrapposte (crasha l'add-in).
> - **Budget ~30s per chiamata**: se lo step è pesante, spezzalo in più script e fai snapshot.
> - Per annullare: **riapri l'ultimo `.f3d` buono** (NON comandi di undo) o riporta indietro il
>   marker timeline e cancella le feature successive.
>
> Procedura:
> 1. Leggi lo stato: `app=adsk.core.Application.get(); design=app.activeProduct; root=design.rootComponent`.
> 2. **Crea la parte come Component proprio** (per poterla jointare): sketch su piano/faccia,
>    **vincola e quota** lo sketch, poi **extrude** (cubo/plane) o **revolve** (cilindro/cono/
>    sfera/toro / solidi di rivoluzione). Per simmetria bilaterale usa la **Mirror feature** sul
>    piano di simmetria. Nomina i component/feature come da `target`/piano.
> 3. **Posiziona con JOINTS** secondo `step.joints` e la mappa relazioni→joint: crea JointOrigin
>    su facce/spigoli/punti e un joint relativo (concentric/planar/rigid). **NON** usare
>    trasformazioni assolute (`transform2` matrice mondo) per piazzare la parte.
> 4. **Dettagli** dello step → feature native: **Fillet** (raccordi), **Hole** (fori), revolve-cut
>    (scanalature), **Shell** (cavità). Niente boolean su mesh.
> 5. Verifica: per ogni vista in `reference_views` imposta la camera con la posa canonica
>    (vedi `VIEW_CAMERA`: front=+Y, side=−X, back=−Y, three_quarter=iso; up=+Z), `viewport.fit()`,
>    poi `app.activeViewport.saveAsImageFile(<png>, w, h)`. Apri il png con Read e giudica **forma
>    e simmetria** (silhouette, proporzioni, simmetria) rispetto alla vista di riferimento e ad
>    `acceptance` — NON il colore/materiale.
> 6. RIPETI fino a un **massimo di 3 tentativi** finché il risultato non è soddisfacente
>    (soddisfacente = silhouette/simmetria combaciano con le `reference_views` e `acceptance` è
>    rispettata). A ogni retry: ricarica l'ultimo `.f3d` buono o aggiusta i parametri (è
>    parametrico: cambia le quote dello sketch / dell'extrude), poi ri-verifica via render. Solo
>    dopo il 3° tentativo non soddisfacente → procedi col miglior tentativo e marca
>    `status: "failed"`.
> 7. Su esito accettabile: **salva snapshot ed export** via codice — salva/esporta il design in
>    `<f3d>` (export manager, formato Fusion Archive `.f3d`) ed esporta la mesh STL in `<stl>`
>    (export manager STL del rootComponent/body). Salva il render di verifica in `<png>`.
>    Scrivi `<json>` con il record dello step.
> 8. RITORNA (come testo finale = valore) SOLO questo JSON record:
>    `{"id", "title", "status": "done|failed", "attempts": N, "f3d": "steps/step_NN.f3d",
>      "stl": "steps/step_NN.stl", "png": "steps/step_NN.png",
>      "components": [..nomi..], "joints": [..tipi/coppie..], "notes": "cosa è stato fatto / perché eventuale fail"}`
>    (path RELATIVI alla build dir nel record).

## 4. Chiusura

Dopo l'ultimo step, esporta i file finali (un subagent o tu via MCP):
```
uv run python -m src.fusion_build final-paths <build_dir>
```
poi via `mcp__fusion__execute_fusion_code`: salva il design come `final.f3d` (export Fusion
Archive), esporta **STEP** su `final.step` e **STL** su `final.stl` tramite l'export manager
(`design.exportManager.createSTEPExportOptions(...)` / `createMeshExportOptions(...)` per STL).
Infine:
Export fatti, **NON** riassumere ancora: prosegui col **gate di convergenza (§5)**. Lo stato
`done` lo imposta il gate (`record-eval`), non qui.

## 5. Gate di convergenza (confronto input↔output → stop o re-itera)

> Stadio **CAD-agnostico** nella struttura e nella regola di decisione (identico a blender-build):
> cambia solo *come* si calcolano le metriche (API Fusion in cm) e *come* si rendono le viste
> (`saveAsImageFile`).

Step finale **ingegneristico**: confronta gli input (descrizione + 4 viste `refs/` + manifest)
con l'output (il modello, ancora nel documento dopo la chiusura) in modo **misurabile** e decide
se fermarsi o ri-partire dal risultato. Confronto **ibrido**: metriche oggettive + **copertura
del manifest** + giudizio visivo per-vista, **focalizzato su geometria e simmetria** (NON colore).

1. Path artefatti del gate:
   ```
   uv run python -m src.fusion_build eval-paths <build_dir>
   ```
   → `{"iteration": N, "views": {front,three_quarter,side,back → png assoluti}, "verdict": "...abs..."}`
   (tutto sotto `eval/iter_NN/`).
2. **Metriche oggettive in Fusion** (`execute_fusion_code`, numeri non occhio):
   - bbox del modello: `body.boundingBox` / bbox del component (in **cm**) → ×10 = **mm** vs
     `target_mm` (quote-target dal prompt/`strategy`, es. 89×130×3) → `proportion_error_pct`.
   - **scarto di simmetria per asse**: misura lo scostamento rispetto al piano di simmetria
     (bbox simmetrica o confronto specchiato) → `symmetry_dev_mm` per asse (in mm).
   - `watertight`: esporta STL e verificalo con trimesh (come blender-build) e/o riporta la
     validità del solido B-rep (`body.isSolid`).
3. **Render pulito delle 4 viste** del modello: pose canoniche `VIEW_CAMERA` (front=+Y, side=−X,
   back=−Y, three_quarter=iso; up=+Z), ortografica per front/right/back, `viewport.fit()`, poi
   `saveAsImageFile` salvando in `views[...]` di `eval-paths`. **Stesse pose** usate negli step.
4. **Giudizio visivo per-vista + copertura manifest** — lancia **un** subagent giudice (Sonnet)
   col **template giudice** sotto, passandogli anche `manifest.json`: per ogni vista confronta il
   render con `refs/<view>.png` su **silhouette/forma/proporzioni/simmetria** (NON colore) →
   `score` 0–100 + note + `actionable_gaps` geometrici concreti; e per ogni parte del manifest
   verifica presenza/relazioni → `parts_check`.
5. Componi `verdict.json` nel path `verdict` (schema sotto: metriche + per-vista + `parts_check`
   + `overall_score` + `actionable_gaps`), poi:
   ```
   uv run python -m src.fusion_build record-eval <build_dir> <verdict.json>
   ```
   → ritorna la **raccomandazione deterministica** `{"recommendation":"stop|reiterate","reason":...,"overall_score":N,"iteration":N}`
   (regola nel helper: soglia `ACCEPT_THRESHOLD=85` e cap `MAX_ITERATIONS=2`; non delegata
   all'LLM — `parts_check` è **solo informativo**, non cambia la regola). Imposta `status=done`.
6. **Riporta all'utente e FERMATI** (sempre): tabella punteggi per-vista, metriche oggettive,
   **copertura del manifest** (`parts_check`: quali parti presenti / mancanti / relazioni ok),
   `actionable_gaps`, e la raccomandazione + motivazione. Se `reiterate`, di' all'utente che
   può rilanciare partendo dal risultato (§6) e quali gap verrebbero corretti.

Schema `verdict.json`:
```json
{
  "iteration": 1,
  "metrics": {"bbox_mm": [89.0,3.03,130.0], "target_mm": [89,130,3],
              "proportion_error_pct": 1.2, "symmetry_dev_mm": {"x":0.0,"z":0.4},
              "watertight": true},
  "parts_check": [{"id":"body","present":true,"relations_ok":true,"notes":"ok"},
                  {"id":"handle","present":false,"relations_ok":false,"notes":"manca del tutto"}],
  "views": [{"view":"front","score":92,"notes":"..."}, {"view":"side","score":88,"notes":"..."}],
  "overall_score": 90,
  "actionable_gaps": ["bordo inferiore ~2 mm troppo spesso vs ref", "manca la maniglia (part handle)"]
}
```

### Template prompt per il giudice (Sonnet)

> Sei un giudice di fedeltà geometrica. Confronti il MODELLO 3D renderizzato con le VISTE DI
> RIFERIMENTO e con il PART-MANIFEST di un oggetto, **solo su forma**: silhouette, proporzioni,
> simmetria, presenza/posizione delle feature geometriche. IGNORA colori, materiali,
> illuminazione, sfondo.
>
> Per ogni vista ti do due immagini: il render del modello e la `ref` corrispondente. Aprile
> con Read. Per ciascuna vista assegna uno `score` 0–100 (100 = silhouette/proporzioni/
> simmetria coincidono) e note brevi. Poi, usando il MANIFEST, per ogni parte verifica se è
> **presente** nel modello e se le sue **relazioni** sono rispettate → `parts_check`. Infine
> elenca `actionable_gaps`: differenze GEOMETRICHE concrete e correggibili (es. "bordo inferiore
> troppo spesso", "manca la parte handle", "spigolo non raccordato"), non estetiche.
>
> VISTE (render ↔ ref): {front: <render> ↔ <refs/front.png>, ...}
> MANIFEST (parti attese): {manifest.json}
> METRICHE OGGETTIVE (contesto): {bbox_mm, target_mm, symmetry_dev_mm, watertight}
>
> RITORNA come testo finale SOLO questo JSON:
> `{"views":[{"view":"front","score":N,"notes":"..."}, ...],
>   "parts_check":[{"id":"...","present":true|false,"relations_ok":true|false,"notes":"..."}, ...],
>   "overall_score":N, "actionable_gaps":["...", "..."]}`
> dove `overall_score` è la tua sintesi pesata (penalizza forte gli errori di proporzione e
> simmetria e le parti mancanti). Niente altro testo.

## 6. Re-iterazione (su richiesta utente — parte DAL risultato)

Se (e solo se) l'utente chiede di rilanciare dopo una raccomandazione `reiterate`, **NON
ripartire da zero**: si riparte dal modello ottenuto.
```
uv run python -m src.fusion_build reinit-from-result <prev_build_dir>
```
→ crea una NUOVA build con `seed.f3d` (= `final.f3d` precedente), gli **stessi `refs/`**
(input invariati), lo **stesso `manifest.json`** (l'inventario non cambia tra iterazioni),
`iteration` incrementata, `parent_build` e `planning_hints` (= gli `actionable_gaps` dell'ultimo
verdict).

Poi esegui la pipeline normale (§2–§5) con due differenze (il manifest è già pronto, salti §1):
- Il **piano è di rifinitura**: lo **step 1 NON crea geometria nuova** ma carica il modello —
  apri `<seed.f3d assoluto>` come documento attivo (import del Fusion Archive) — e gli step
  successivi **mirano ai `planning_hints`** (i gap da chiudere), restando su geometria/simmetria
  e sulle parti del manifest ancora carenti. Essendo parametrico, spesso basta **modificare le
  quote di sketch/feature** esistenti invece di ricostruire.
- Salta la pulizia del preflight (il seed È il documento di partenza).

A fine pipeline gira un nuovo gate (§5); le iterazioni restano tracciate in metadata
(`iteration`, `parent_build`, `evaluations`).

## Toolbox parametrico Fusion (preferenze — privilegiare SEMPRE l'esatto per costruzione)

- **Sketch + vincoli/quote**: profili 2D su un piano/faccia, **fully-constrained** (vincoli
  geometrici + dimensionali). La forma nasce qui, esatta.
- **Feature di volume**: **Extrude** (cubo/plane/profili estrusi), **Revolve** (cilindro/cono/
  sfera/toro e ogni solido di rivoluzione: corpo teiera, bottiglia, coperchio), **Loft**, **Sweep**.
- **Feature di modifica**: **Shell** (cavità a parete sottile), **Fillet** (raccordi),
  **Chamfer** (smussi), **Hole** (fori, native, con tipo/diametro), **Thread**, **Draft**.
- **Pattern/simmetria**: **Mirror** feature sul piano di simmetria (**default** per oggetti
  simmetrici — analogo del MIRROR di Blender), **Circular/Rectangular Pattern**.
- **Assieme**: ogni parte = un **Component**; posiziona con **Joints** (Rigid/Cylindrical/Planar/
  Revolute) e **JointOrigin** su geometria nominata. Relazioni del manifest → joints (vedi §3).
- **VIETATO** (salvo banalità): mesh / editing per-vertice; **trasformazioni assolute** per
  posizionare le parti (usa joints); boolean "sporchi" su mesh.

## Gotcha / principi (applicarli SEMPRE)

Principi generali (dettaglio + esempi nel **knowledge base versionato** `run/fusion_build_lessons.md`
— dalla cwd RUN è `fusion_build_lessons.md`). **Quando durante una run emerge un nuovo
problema/soluzione, AGGIUNGILO lì e committa**: il file è in repo e scrivibile dal contesto RUN
(non è sotto `run/.claude/skills/`), così la conoscenza resta versionata e condivisa. NON
aggiornare l'auto-memory fuori repo: la fonte canonica è il file in `run/`.
- **Esatto per costruzione > correggere dopo.** Sketch vincolati e quotati → extrude/revolve.
  La silhouette giusta nasce dai vincoli dello sketch, non da aggiustamenti a valle.
- **Posiziona con JOINTS, non con trasformazioni.** Crea il component all'origine, poi
  àncoralo con un joint relativo che referenzia geometria (JointOrigin). È il senso della
  migrazione: l'intento relativo sopravvive alle modifiche parametriche.
- **Simmetria = Mirror feature** sul piano di simmetria (default per oggetti simmetrici):
  modella metà e specchia → simmetria perfetta e parametrica.
- **Unità cm**: scrivi le quote in cm (1 mm = 0.1 cm), tonde e proporzionali; il gate converte
  cm→mm. Niente frazioni arbitrarie.
- **Una operazione per chiamata, ~30s di budget**: spezza gli step pesanti, fai snapshot
  `.f3d` tra l'uno e l'altro; mai operazioni sovrapposte.
- **Annullare = riaprire l'ultimo `.f3d`** (o rollback del marker timeline + cancella le
  feature successive): non affidarti a comandi di undo programmatici.
- **Riferimenti stabili**: per joints/feature usa JointOrigin / piani-feature / facce nominate,
  non indici di facce volatili che cambiano alla rigenerazione.
- **Diagnostica**: rendi le 4 pose canoniche (front/side/back ortografiche + 3/4 iso),
  `viewport.fit()`, e itera i parametri GUARDANDO i render salvati con `saveAsImageFile`.
- **Focus = geometria e simmetria, non materiali/colori.** Spendi i tentativi su silhouette,
  proporzioni e simmetria, non sull'estetica. Materiali/colori sono rifinitura finale opzionale.

## Note operative

- Esecuzione **dentro Claude Code**, niente app web. Nessuna chiamata Meshy/SCAD.
- **Fusion gira solo su macOS/Windows.** Setup add-in + MCP e checklist di verifica lato Mac:
  vedi **`SETUP_MACOS.md`** (in questa cartella skill).
- **Due contesti (DEV vs RUN)** — vedi `run/README.md`. La skill vive SOLO in
  `run/.claude/skills/`: i *run* della pipeline si lanciano da `run/` (ha il MCP `fusion` e
  i `src/` in sola lettura), lo *sviluppo* di `src/`+skill si fa da repo root (senza MCP CAD).
  Gli artefatti restano in `outputs/fusion/<ts-slug>/` (gitignored) qualunque sia la cwd.
- I path passati a Fusion devono essere **assoluti** (Fusion e Claude Code girano sulla
  stessa macchina): usali come tornano da `paths` / `final-paths`.
- Lo storico vive in `outputs/fusion/<ts-slug>/steps/` — uno `.f3d` apribile per step.
