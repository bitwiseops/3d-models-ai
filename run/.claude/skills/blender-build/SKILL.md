---
name: blender-build
description: Costruisce in modo INCREMENTALE un modello 3D in Blender (live, via MCP) a partire dalla descrizione + 4 viste di una sessione Oggetti esistente. Parte in AUTO MODE all'invocazione (gira end-to-end senza chiedere conferme). Prima un parser Opus (subagent, sola percezione, niente MCP) estrae un PART-MANIFEST — l'inventario strutturato di COSA deve esistere (parti, primitive, relazioni); è la single source of truth. Poi un planner Opus (thinking medio) crea il piano focalizzato su GEOMETRIA e SIMMETRIA consumando il manifest, e executor Sonnet in sequenza applicano ogni step privilegiando gli strumenti automatici di Blender (primitive + modifier), si auto-verificano via screenshot ripetendo fino a 3 volte se il risultato non è soddisfacente, e salvano uno snapshot .blend per modifica. Termina con un GATE DI CONVERGENZA che confronta input↔output (metriche oggettive + copertura del manifest + giudizio visivo sulle 4 viste) e raccomanda in modo deterministico se fermarsi o ri-partire dal risultato; si ferma sempre lasciando la decisione all'utente. USARE quando l'utente chiede di "costruire/modellare in Blender" un oggetto da una sessione esistente. PRECONDIZIONE: Blender aperto e connesso al server MCP (porta 9876).
---

# blender-build — costruzione 3D incrementale live in Blender

Pipeline a **più stadi** orchestrata da Claude Code (questo agente) sugli strumenti
`mcp__blender__*`: **parsing → piano → esecuzione → chiusura → gate**. Input: una sessione
Oggetti esistente (`outputs/<ts-slug>/`) con prompt raffinato + i 4 crop del grid 2x2
(front / three_quarter / side / back). Output: modello Blender costruito incrementalmente,
con storico `.blend` per ogni modifica.

Helper deterministico: `src/blender_build.py` (CLI `uv run python -m src.blender_build ...`).
Niente IO a mano: usa sempre la CLI per init/manifest/plan/paths/record/status.

Argomento della skill: il path della sessione Oggetti sorgente (es.
`outputs/20260529-...-teapot`). Se manca, chiedi all'utente quale sessione usare.

## Auto mode (default all'invocazione)

Quando la skill viene invocata, esegui l'**intera pipeline in autonomia** —
preflight → parsing/manifest → piano → tutti gli step in sequenza → chiusura → **gate di
convergenza** — **senza fermarti a chiedere conferma** tra una fase e l'altra né tra uno step
e il successivo. Non chiedere "vuoi che proceda?": procedi. Al gate (§5) la pipeline **si ferma
sempre** col verdetto e la raccomandazione: NON riavvia da sola; la re-iterazione (§6) parte
solo se l'utente la chiede.

Restano gli unici punti in cui ti fermi (sicurezza / perdita dati):
- **Preflight fallito** (Blender non connesso) → fermati e istruisci l'utente (vedi §0.1).
- **Scena con lavoro esistente dell'utente** da cancellare → chiedi conferma (perdita dati).
  Scena con soli oggetti di default → pulisci senza chiedere (vedi §0.2).
- **Path sessione sorgente mancante** → chiedi quale sessione usare.

## 0. Preflight (OBBLIGATORIO — la pipeline gira SOLO con Blender connesso)

1. Chiama `mcp__blender__get_scene_info`.
   - Se fallisce / va in timeout ⇒ Blender non è connesso. **Fermati** e di' all'utente:
     apri Blender, abilita l'addon `tools/blender_mcp_addon.py`, e nel panel laterale
     "BlenderMCP" clicca **Connect to MCP server** (porta 9876). Non creare nessuna build
     session finché il preflight non passa.
2. Se la scena contiene SOLO gli oggetti di default (Cube/Light/Camera), in auto mode
   ripuliscila senza chiedere via `execute_code`
   (`bpy.ops.object.select_all(action='SELECT'); bpy.ops.object.delete()`). Chiedi conferma
   SOLO se la scena contiene già lavoro dell'utente (perdita dati).

## 1. Parsing percettivo — PART-MANIFEST (parser = Opus subagent, sola percezione)

Prima di pianificare, estrai un **inventario strutturato di COSA deve esistere**. Il manifest è
la **single source of truth**: planner ed executor consumano QUESTO, non ri-ragionano dalle
immagini. Separare *percezione* (cosa c'è) da *costruzione* (come farlo) evita che feature
viste in una sola vista vengano "dimenticate" durante la pianificazione.

1. Bootstrap della build session:
   ```
   uv run python -m src.blender_build init <source_dir>
   ```
   → ritorna `{"build_dir": "outputs/blender/<ts-slug>"}`. Copia già le 4 viste in
   `refs/`, scrive `prompts/00_source.txt` e `metadata.json` (**status=parsing**).
2. Lancia **un** subagent **parser** con il tool **Agent**:
   - `subagent_type: general-purpose`, `model: opus` — **sola percezione**: gli servono solo
     `Read` (le 4 viste) e `Write` (il manifest). **NIENTE MCP / niente Blender** in questo stadio.
   - prompt = **template parser** sotto, con la descrizione (`prompts/00_source.txt`) e i path
     delle 4 viste `refs/<view>.png`.
3. Salva il manifest prodotto dal parser (porta lo stato `parsing → planning`):
   ```
   uv run python -m src.blender_build save-manifest <build_dir> <manifest.json>
   ```
   → ritorna `{"manifest_path", "part_ids", "open_questions"}`. **Logga le `open_questions`**
   (ambiguità che il piano dovrà risolvere).

Schema `manifest.json` (parametri **relativi/proporzionali**, non quote assolute: le quote sono
compito del planner/gate):
```json
{
  "parts": [
    {
      "id": "body",
      "primitive_type": "cube|cylinder|sphere|cone|torus|plane|custom",
      "parameters": {"shape": "tronco-conico", "proportions": "altezza ~1.5x larghezza"},
      "relations": [{"type": "attached_to|on_top_of|inside|symmetric_to", "target_id": "base", "detail": "centrato"}],
      "details": ["scanalatura orizzontale a metà altezza"],
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
> (front / three_quarter / side / back). Niente Blender, niente codice: solo osservazione.
>
> DESCRIZIONE: {prompts/00_source.txt}
> VISTE (aprile tutte con Read): {refs/front.png, refs/three_quarter.png, refs/side.png, refs/back.png}
>
> Procedura:
> 1. **Enumera le parti per ciascuna vista** separatamente (cosa vedi in front, poi in side, ecc.).
> 2. **Riconcilia tra le viste**: decidi quali osservazioni sono la STESSA parte vista da angoli
>    diversi e quali sono parti DISTINTE. Una feature visibile in una sola vista va comunque tenuta
>    (non scartarla perché le altre viste non la mostrano).
> 3. Per ogni parte: `id` univoco e parlante, `primitive_type` (la primitiva Blender più vicina),
>    `parameters` **relativi/proporzionali** (forma e proporzioni, NON quote in mm), `relations`
>    verso altre parti (attacco/sopra/dentro/simmetria), `details` geometrici, `source_views`
>    (dove l'hai vista), `confidence`.
> 4. Ciò che resta **ambiguo o non risolvibile** dalle viste → `open_questions` (testo libero).
>
> RITORNA come testo finale = valore SOLO il JSON del manifest (schema: `{"parts":[...],
> "open_questions":[...]}`). Niente altro testo. Scrivilo anche nel file manifest che ti è indicato.

## 2. PIANO (planner = Opus, lo fai TU nel main loop, thinking MEDIO)

Pianifica con **sforzo di ragionamento medio**, non estremo: la pianificazione è
decomposizione in step + scelta degli strumenti automatici, non richiede thinking massimo.
Il budget di ragionamento più alto va semmai speso dagli executor sugli step rischiosi.

Il planner **consuma il manifest** (§1) come SSOT: ogni parte del manifest deve essere coperta
da almeno uno step. NON re-inventare l'inventario dalle immagini — le viste servono solo per
capire *come* realizzare le parti già elencate.

1. Leggi `manifest.json` e la descrizione, e **guarda le 4 viste** (`Read` su `refs/front.png`,
   `three_quarter.png`, `side.png`, `back.png`) per pianificare con il contesto visivo.
2. Produci il **piano**: decomposizione in step ordinati **coarse → fine**, ognuno
   espresso con **strumenti automatici di Blender** (vedi Toolbox). Ogni step dichiara in
   `manifest_parts` gli `id` delle parti del manifest che realizza. Scrivi il piano in un file
   temporaneo e salvalo:
   ```
   uv run python -m src.blender_build save-plan <build_dir> <plan.json>
   ```
   → ritorna `{"plan_path", "uncovered_parts"}`. Se `uncovered_parts` **NON è vuoto**, alcune
   parti del manifest non sono coperte da nessuno step: rivedi il piano prima di costruire
   (è un avviso, non un errore — ma di norma vanno coperte tutte).
   Schema `plan.json`:
   ```json
   {
     "source_session": "<name>",
     "strategy": "PRIORITÀ a geometria e simmetria: silhouette corretta, proporzioni, MIRROR sugli assi di simmetria; materiali/colori sono rifinitura finale opzionale, non il focus. Naming oggetti coerente col piano; modifier non-distruttivi tenuti vivi; build incrementale coarse→fine; NIENTE editing manuale per-vertice",
     "steps": [
       {
         "id": 1,
         "title": "Corpo base",
         "goal": "Volume principale che cattura la silhouette",
         "target": "Body",
         "manifest_parts": ["body"],
         "blender_approach": "primitive_cube_add → Mirror(X) + Subdivision Surface + Bevel; scala a proporzioni ~1.5x altezza",
         "tools": ["primitive_cube_add", "modifier:MIRROR", "modifier:SUBSURF", "modifier:BEVEL"],
         "reference_views": ["front", "side"],
         "depends_on": [],
         "acceptance": "silhouette frontale e laterale combaciano; simmetria su X"
       }
     ]
   }
   ```
   Linee guida per il piano:
   - **Copri il manifest**: ogni parte ha almeno uno step che la cita in `manifest_parts`.
   - **Priorità: forma e simmetria prima di tutto.** Cattura silhouette e proporzioni
     corrette, usa `MIRROR` sugli assi di simmetria dell'oggetto. Materiali e colori sono
     secondari e facoltativi (eventuale step finale): non costruire il piano attorno ad essi.
   - 5–15 step tipicamente. Primo step = volume base; ultimi step = dettagli/smussi.
   - Ogni step deve essere **applicabile da solo** sopra lo stato dello step precedente.
   - Indica `reference_views` pertinenti (quali viste verificare per quello step).
   - Esprimi tutto in primitive + modifier (Toolbox). Evita di prescrivere coordinate di
     vertici: descrivi la forma e lo strumento, non la mesh a mano.

## 3. ESECUZIONE incrementale (executor = Sonnet, IN SEQUENZA)

**Sequenza rigorosa**: una sola connessione MCP e scena condivisa mutabile ⇒ lancia **un**
subagent per volta e **aspetta** che finisca prima del successivo. Mai in parallelo.

Per ogni step in ordine:

1. Calcola i path artefatti:
   ```
   uv run python -m src.blender_build paths <build_dir> <step_id>
   ```
   → `{"blend": "...abs...", "glb": "...", "png": "...", "json": "..."}`.
2. Lancia **un** subagent executor con il tool **Agent**:
   - `subagent_type: general-purpose` (ha gli strumenti `mcp__blender__*` + Write)
   - `model: sonnet`
   - prompt = **template executor** sotto, con piano completo, step corrente, **le parti del
     manifest citate in `step.manifest_parts`** (estratte da `manifest.json`), i path delle
     viste di riferimento (`<build_dir>/refs/<view>.png`), e i path artefatti dello step.
3. Quando il subagent ritorna il record strutturato, registralo:
   ```
   uv run python -m src.blender_build record <build_dir> <record.json>
   ```
4. Passa allo step successivo.

**Best-effort** (come la pipeline SCAD): se uno step non converge entro i **3 tentativi**,
salva il miglior tentativo, marca `status: "failed"` nel record con le note, e **prosegui**
— lo storico resta completo.

### Template prompt per l'executor (Sonnet)

> Sei un executor specializzato in modellazione Blender via MCP. Applichi **un solo step**
> di un piano a una scena Blender già avviata. Usa SEMPRE strumenti automatici (primitive +
> modifier), MAI editing manuale coordinata-per-vertice. Il tuo focus è **geometria e
> simmetria** (forma, proporzioni, MIRROR sugli assi): materiali e colori sono secondari e
> facoltativi — NON spendere tentativi per perfezionarli.
>
> PIANO COMPLETO (contesto): {plan.json}
> STEP DA APPLICARE: {step}
> PARTI DEL MANIFEST DA REALIZZARE (single source of truth — costruisci QUESTE): {parts di step.manifest_parts}
> VISTE DI RIFERIMENTO: {refs/front.png, ...} (apri quelle in step.reference_views — servono solo
>   al CHECK VISIVO della forma, NON per inventare feature non presenti nel manifest)
> PATH ARTEFATTI (assoluti, usali esattamente): blend={...} glb={...} png={...} json={...}
>
> Procedura:
> 1. `mcp__blender__get_scene_info` e, sugli oggetti rilevanti, `get_object_info`.
> 2. Applica SOLO questo step con `mcp__blender__execute_code` (Python `bpy`): primitive +
>    modifier dal toolbox. Nomina gli oggetti come da `target`. Tieni i modifier vivi
>    (non applicarli) salvo che il piano lo richieda.
> 3. Verifica: per ogni vista in `reference_views`, imposta l'angolo
>    (`bpy.ops.view3d.view_axis(type=...)` — FRONT/RIGHT/BACK; per three_quarter orbita una
>    vista 3/4), poi `mcp__blender__get_viewport_screenshot`. Giudica **forma e simmetria**
>    (silhouette, proporzioni, simmetria sugli assi) rispetto alla vista di riferimento e a
>    `acceptance` — NON il colore/materiale.
> 4. RIPETI fino a un **massimo di 3 tentativi** finché il risultato non è soddisfacente
>    (soddisfacente = silhouette/simmetria combaciano con le `reference_views` e `acceptance`
>    è rispettata). A ogni retry: ricarica l'ultimo `.blend` buono (NON `bpy.ops.ed.undo`) o
>    aggiusta i parametri, poi ri-verifica via screenshot. Solo dopo il 3° tentativo non
>    soddisfacente → procedi col miglior tentativo e marca `status: "failed"`.
> 5. Su esito accettabile: salva snapshot e export via `execute_code`:
>    `bpy.ops.wm.save_as_mainfile(filepath=<blend>, copy=True)` e
>    export GLB su `<glb>` (`bpy.ops.export_scene.gltf(filepath=<glb>)`). Salva lo
>    screenshot di verifica su `<png>` (passa `filepath=<png>` a get_viewport_screenshot).
>    Scrivi `<json>` con il record dello step.
> 6. RITORNA (come testo finale = valore) SOLO questo JSON record:
>    `{"id", "title", "status": "done|failed", "attempts": N, "blend": "steps/step_NN.blend",
>      "glb": "steps/step_NN.glb", "png": "steps/step_NN.png",
>      "objects": [..nomi..], "notes": "cosa è stato fatto / perché eventuale fail"}`
>    (path RELATIVI alla build dir nel record).

## 4. Chiusura

Dopo l'ultimo step, esporta i file finali (un subagent o tu via MCP):
```
uv run python -m src.blender_build final-paths <build_dir>
```
poi via `execute_code`: `save_as_mainfile(filepath=final.blend)`, export GLB su `final.glb`,
export STL su `final.stl` (`bpy.ops.export_mesh.stl(filepath=final.stl)` o l'export STL
disponibile nella versione di Blender). Infine:
Export fatti, **NON** riassumere ancora: prosegui col **gate di convergenza (§5)**. Lo stato
`done` lo imposta il gate (`record-eval`), non qui.

## 5. Gate di convergenza (confronto input↔output → stop o re-itera)

Step finale **ingegneristico**: confronta gli input (descrizione + 4 viste `refs/` + manifest)
con l'output (il modello, ancora in scena dopo la chiusura) in modo **misurabile** e decide se
fermarsi o ri-partire dal risultato. Confronto **ibrido**: metriche oggettive + **copertura del
manifest** + giudizio visivo per-vista, **focalizzato su geometria e simmetria** (NON colore).

1. Path artefatti del gate:
   ```
   uv run python -m src.blender_build eval-paths <build_dir>
   ```
   → `{"iteration": N, "views": {front,three_quarter,side,back → png assoluti}, "verdict": "...abs..."}`
   (tutto sotto `eval/iter_NN/`).
2. **Metriche oggettive in Blender** (`execute_code`, numeri non occhio):
   - bbox in mm vs `target_mm` (le quote-target dal prompt/`strategy`, es. 89×130×3) →
     `proportion_error_pct`.
   - **scarto di simmetria per asse**: specchia la mesh sull'asse di simmetria e misura lo
     scostamento (mm) → `symmetry_dev_mm` per asse.
   - `watertight` (già verificato in chiusura con trimesh): riportalo.
3. **Render pulito delle 4 viste** del modello: SOLID, overlay/gizmo OFF, orientamento da
   `view_axis` (FRONT/RIGHT/BACK; per `three_quarter` orbita una 3/4), `view_selected`, poi
   `get_viewport_screenshot` salvando in `views[...]` di `eval-paths`.
4. **Giudizio visivo per-vista + copertura manifest** — lancia **un** subagent giudice (Sonnet)
   col **template giudice** sotto, passandogli anche `manifest.json`: per ogni vista confronta il
   render con `refs/<view>.png` su **silhouette/forma/proporzioni/simmetria** (NON colore) →
   `score` 0–100 + note + `actionable_gaps` geometrici concreti; e per ogni parte del manifest
   verifica presenza/relazioni → `parts_check`.
5. Componi `verdict.json` nel path `verdict` (schema sotto: metriche + per-vista + `parts_check`
   + `overall_score` + `actionable_gaps`), poi:
   ```
   uv run python -m src.blender_build record-eval <build_dir> <verdict.json>
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
> troppo spesso", "manca la parte handle", "spigolo non smussato"), non estetiche.
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
uv run python -m src.blender_build reinit-from-result <prev_build_dir>
```
→ crea una NUOVA build con `seed.blend` (= `final.blend` precedente), gli **stessi `refs/`**
(input invariati), lo **stesso `manifest.json`** (l'inventario non cambia tra iterazioni),
`iteration` incrementata, `parent_build` e `planning_hints` (= gli `actionable_gaps` dell'ultimo
verdict).

Poi esegui la pipeline normale (§2–§5) con due differenze (il manifest è già pronto, salti §1):
- Il **piano è di rifinitura**: lo **step 1 NON crea primitive** ma carica il modello —
  `bpy.ops.wm.open_mainfile(filepath=<seed.blend assoluto>)` — e gli step successivi
  **mirano ai `planning_hints`** (i gap da chiudere), restando su geometria/simmetria e sulle
  parti del manifest ancora carenti.
- Salta la pulizia scena del preflight (il seed È la scena di partenza).

A fine pipeline gira un nuovo gate (§5); le iterazioni restano tracciate in metadata
(`iteration`, `parent_build`, `evaluations`).

## Toolbox automatico Blender (preferenze — privilegiare SEMPRE l'automatico)

- **Primitive**: `mesh.primitive_cube_add`, `_uv_sphere_add`, `_cylinder_add`,
  `_cone_add`, `_torus_add`, `_plane_add`.
- **Modifier non-distruttivi**: `MIRROR`, `SUBSURF`, `BEVEL`, `SOLIDIFY`, `BOOLEAN`,
  `ARRAY`, `SCREW`, `SKIN`, `REMESH`, `SHRINKWRAP`, `LATTICE`, `CAST`, `SIMPLE_DEFORM`.
  (Aggiungi via `obj.modifiers.new(name, TYPE)` e configura le proprietà.)
  **`MIRROR` è il default per ogni oggetto simmetrico**: modella metà e specchia sull'asse
  → simmetria perfetta e gratuita, niente disallineamenti.
- **Operatori**: `object.join`, `duplicate`, parent/transform, `object.shade_smooth`,
  auto-smooth, proportional edit; per forme organiche `sculpt` + remesh.
- **VIETATO** (salvo banalità): posizionare/spostare vertici singoli a mano. Per forma fine
  usa deform/lattice/sculpt+remesh, non coordinate di vertici scritte a mano.

## Gotcha / principi (applicarli SEMPRE)

Principi generali (dettaglio + esempi in memoria `reference_blender_build_lessons`). Aggiornarli quando emergono nuovi problemi/soluzioni.
- **Boolean = operandi puliti, NON auto-intersecanti.** Un cutter che si auto-interseca (prismi sovrapposti; prismi radiali che convergono verso l'asse con fori grandi) rende EXACT degenere e può SVUOTARE l'oggetto → un boolean per gruppo non-sovrapposto in sequenza, o self-union del cutter. Forare vicino a un rim aperto lascia schegge → confina i tagli lontano dal bordo.
- **Modellare il positivo > sottrarre il negativo.** Per reticoli/pattern fitti e uniformi: maglia di facce + **WIREFRAME modifier** (fili uniformi a ogni scala), non boolean.
- **`BOOLEAN UNION` preserva i materiali solo con `material_mode='TRANSFER'`** (assegnali PRIMA di unire); parti "connesse" = compenetrare + union (1 componente).
- **Deform (TWIST/BEND) ruotano attorno all'ORIGINE oggetto** (decentrata dopo `join` → splay): usa `mod.origin=empty` al centro; per coerenza unisci le parti e deforma l'insieme.
- **Layout regolari con loop Python** (theta) + `join`, non con ARRAY Object-Offset (fragile). Tassellazioni con le formule corrette, non a occhio.
- **Scala**: mm tondi e proporzionali (`scale_length=0.001`), niente frazioni arbitrarie.
- **NON usare `bpy.ops.ed.undo`** (crasha l'MCP): per annullare ricarica l'ultimo `.blend`.
- **Diagnostica**: 3 viste (FRONT/RIGHT/3-4), viewport pulito; itera i parametri estetici GUARDANDO gli screenshot. Material Preview sovraespone → abbassa `studiolight_intensity`.
- **Manifold**: EDIT → `remove_doubles` + `normals_make_consistent` + `select_non_manifold`→`fill_holes`; verifica contando gli edge non-manifold.
- **Focus = geometria e simmetria, non materiali/colori.** Spendi i tentativi su silhouette,
  proporzioni e simmetria (MIRROR sugli assi), non sull'estetica dei materiali. Materiali e
  colori sono una rifinitura finale opzionale. *Se e quando* li applichi: matte (no metallic
  lucido), pochi toni; trame pulite > trame "spinose" — ma è secondario.

## Note operative

- Esecuzione **dentro Claude Code**, niente app web. Nessuna chiamata Meshy/SCAD.
- **Due contesti (DEV vs RUN)** — vedi `run/README.md`. La skill vive SOLO in
  `run/.claude/skills/`: i *run* della pipeline si lanciano da `run/` (ha il MCP `blender` e
  i `src/` in sola lettura), lo *sviluppo* di `src/`+skill si fa da repo root (senza MCP CAD).
  Gli artefatti restano comunque in `outputs/blender/<ts-slug>/` (gitignored) qualunque sia la cwd.
- I path passati a Blender devono essere **assoluti** (Blender e Claude Code girano sulla
  stessa macchina): usali come tornano da `paths` / `final-paths`.
- Lo storico vive in `outputs/blender/<ts-slug>/steps/` — uno `.blend` apribile per step.
