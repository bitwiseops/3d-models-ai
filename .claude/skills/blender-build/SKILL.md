---
name: blender-build
description: Costruisce in modo INCREMENTALE un modello 3D in Blender (live, via MCP) a partire dalla descrizione + 4 viste di una sessione Oggetti esistente. Un planner Opus crea il piano, poi executor Sonnet in sequenza applicano ogni step privilegiando gli strumenti automatici di Blender (primitive + modifier), si auto-verificano via screenshot e salvano uno snapshot .blend per modifica. USARE quando l'utente chiede di "costruire/modellare in Blender" un oggetto da una sessione esistente. PRECONDIZIONE: Blender aperto e connesso al server MCP (porta 9876).
---

# blender-build — costruzione 3D incrementale live in Blender

Pipeline a **2 passate** orchestrata da Claude Code (questo agente) sugli strumenti
`mcp__blender__*`. Input: una sessione Oggetti esistente (`outputs/<ts-slug>/`) con prompt
raffinato + i 4 crop del grid 2x2 (front / three_quarter / side / back). Output: modello
Blender costruito incrementalmente, con storico `.blend` per ogni modifica.

Helper deterministico: `src/blender_build.py` (CLI `uv run python -m src.blender_build ...`).
Niente IO a mano: usa sempre la CLI per init/plan/paths/record/status.

Argomento della skill: il path della sessione Oggetti sorgente (es.
`outputs/20260529-...-teapot`). Se manca, chiedi all'utente quale sessione usare.

## 0. Preflight (OBBLIGATORIO — la pipeline gira SOLO con Blender connesso)

1. Chiama `mcp__blender__get_scene_info`.
   - Se fallisce / va in timeout ⇒ Blender non è connesso. **Fermati** e di' all'utente:
     apri Blender, abilita l'addon `tools/blender_mcp_addon.py`, e nel panel laterale
     "BlenderMCP" clicca **Connect to MCP server** (porta 9876). Non creare nessuna build
     session finché il preflight non passa.
2. Se la scena contiene gli oggetti di default (Cube/Light/Camera) e l'utente parte da
   scena vuota, puoi ripulire via `execute_code`
   (`bpy.ops.object.select_all(action='SELECT'); bpy.ops.object.delete()`). Chiedi conferma
   se la scena contiene già lavoro dell'utente.

## 1. Passata 1 — PIANO (planner = Opus, lo fai TU nel main loop, "al massimo")

1. Bootstrap della build session:
   ```
   uv run python -m src.blender_build init <source_dir>
   ```
   → ritorna `{"build_dir": "outputs/blender/<ts-slug>"}`. Copia già le 4 viste in
   `refs/`, scrive `prompts/00_source.txt` e `metadata.json` (status=planning).
2. Leggi la descrizione e **guarda le 4 viste** (`Read` su `refs/front.png`,
   `three_quarter.png`, `side.png`, `back.png`) per pianificare con il contesto visivo.
3. Produci il **piano**: decomposizione in step ordinati **coarse → fine**, ognuno
   espresso con **strumenti automatici di Blender** (vedi Toolbox). Scrivi il piano in un
   file temporaneo e salvalo:
   ```
   uv run python -m src.blender_build save-plan <build_dir> <plan.json>
   ```
   Schema `plan.json`:
   ```json
   {
     "source_session": "<name>",
     "strategy": "naming oggetti coerente col piano; modifier non-distruttivi tenuti vivi; build incrementale coarse→fine; NIENTE editing manuale per-vertice",
     "steps": [
       {
         "id": 1,
         "title": "Corpo base",
         "goal": "Volume principale che cattura la silhouette",
         "target": "Body",
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
   - 5–15 step tipicamente. Primo step = volume base; ultimi step = dettagli/smussi.
   - Ogni step deve essere **applicabile da solo** sopra lo stato dello step precedente.
   - Indica `reference_views` pertinenti (quali viste verificare per quello step).
   - Esprimi tutto in primitive + modifier (Toolbox). Evita di prescrivere coordinate di
     vertici: descrivi la forma e lo strumento, non la mesh a mano.

## 2. Passata 2 — ESECUZIONE incrementale (executor = Sonnet, IN SEQUENZA)

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
   - prompt = **template executor** sotto, con piano completo, step corrente, path delle
     viste di riferimento (`<build_dir>/refs/<view>.png`), e i path artefatti dello step.
3. Quando il subagent ritorna il record strutturato, registralo:
   ```
   uv run python -m src.blender_build record <build_dir> <record.json>
   ```
4. Passa allo step successivo.

**Best-effort** (come la pipeline SCAD): se uno step non converge entro i retry, salva il
miglior tentativo, marca `status: "failed"` nel record con le note, e **prosegui** — lo
storico resta completo.

### Template prompt per l'executor (Sonnet)

> Sei un executor specializzato in modellazione Blender via MCP. Applichi **un solo step**
> di un piano a una scena Blender già avviata. Usa SEMPRE strumenti automatici (primitive +
> modifier), MAI editing manuale coordinata-per-vertice.
>
> PIANO COMPLETO (contesto): {plan.json}
> STEP DA APPLICARE: {step}
> VISTE DI RIFERIMENTO: {refs/front.png, ...} (apri quelle in step.reference_views)
> PATH ARTEFATTI (assoluti, usali esattamente): blend={...} glb={...} png={...} json={...}
>
> Procedura:
> 1. `mcp__blender__get_scene_info` e, sugli oggetti rilevanti, `get_object_info`.
> 2. Applica SOLO questo step con `mcp__blender__execute_code` (Python `bpy`): primitive +
>    modifier dal toolbox. Nomina gli oggetti come da `target`. Tieni i modifier vivi
>    (non applicarli) salvo che il piano lo richieda.
> 3. Verifica: per ogni vista in `reference_views`, imposta l'angolo
>    (`bpy.ops.view3d.view_axis(type=...)` — FRONT/RIGHT/BACK; per three_quarter orbita una
>    vista 3/4), poi `mcp__blender__get_viewport_screenshot`. Confronta con la vista di
>    riferimento e con `acceptance`.
> 4. RETRY (max 3): se non combacia, correggi il codice (annulla/aggiusta) e riprova.
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

## 3. Chiusura

Dopo l'ultimo step, esporta i file finali (un subagent o tu via MCP):
```
uv run python -m src.blender_build final-paths <build_dir>
```
poi via `execute_code`: `save_as_mainfile(filepath=final.blend)`, export GLB su `final.glb`,
export STL su `final.stl` (`bpy.ops.export_mesh.stl(filepath=final.stl)` o l'export STL
disponibile nella versione di Blender). Infine:
```
uv run python -m src.blender_build status <build_dir> done
```
Riassumi all'utente: build dir, numero step, eventuali step falliti, path di `final.*`.

## Toolbox automatico Blender (preferenze — privilegiare SEMPRE l'automatico)

- **Primitive**: `mesh.primitive_cube_add`, `_uv_sphere_add`, `_cylinder_add`,
  `_cone_add`, `_torus_add`, `_plane_add`.
- **Modifier non-distruttivi**: `MIRROR`, `SUBSURF`, `BEVEL`, `SOLIDIFY`, `BOOLEAN`,
  `ARRAY`, `SCREW`, `SKIN`, `REMESH`, `SHRINKWRAP`, `LATTICE`, `CAST`, `SIMPLE_DEFORM`.
  (Aggiungi via `obj.modifiers.new(name, TYPE)` e configura le proprietà.)
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
- **Estetica premium**: matte (no metallic lucido), pochi toni; trame pulite > trame "spinose".

## Note operative

- Esecuzione **dentro Claude Code**, niente app web. Nessuna chiamata Meshy/SCAD.
- I path passati a Blender devono essere **assoluti** (Blender e Claude Code girano sulla
  stessa macchina): usali come tornano da `paths` / `final-paths`.
- Lo storico vive in `outputs/blender/<ts-slug>/steps/` — uno `.blend` apribile per step.
