---
name: fusion-build-from-transcript
description: Costruisce in modo INCREMENTALE un modello 3D PARAMETRICO in Fusion 360 (live, via MCP) a partire dal TRANSCRIPT di una video-guida YouTube, invece che dalle 4 viste di una sessione Oggetti. Variante della pipeline fusion-build con stesso backend di costruzione (tool CAD granulari del server faust-machines + `execute_code` solo per render/bbox) e stesso gate, ma con un nuovo stadio di PRE-ELABORAZIONE del transcript grezzo e una verifica SEMANTICA (niente immagini di riferimento). Insight: il transcript di un tutorial è già una sequenza ordinata di azioni con QUOTE ESATTE → la costruzione segue le quote dette nel video (esatta, non proporzionale). Parte in AUTO MODE all'invocazione. Pre-elaborazione: il transcript viene spezzato in chunk e un agente Sonnet per chunk (in sequenza) ESTENDE un action-log coerentemente, poi una review Opus lo consolida. Un parser Opus legge l'action-log (+ il transcript originale come link) e produce un PART-MANIFEST + un expected_summary (la descrizione attesa dell'oggetto). Un planner Opus mappa l'action-log sui tool granulari Fusion (sketch→extrude/revolve, joints), executor Sonnet eseguono ogni step in sequenza auto-verificandosi con render della viewport (via `execute_code`), e salvano uno snapshot .f3d per modifica. Termina con un GATE DI CONVERGENZA che confronta il modello renderizzato con `expected_summary` (giudizio semantico) + metriche oggettive e raccomanda in modo deterministico stop o re-iterazione. USARE quando l'utente chiede di "costruire/modellare in Fusion da un transcript / da una guida YouTube". PRECONDIZIONE per gli stadi MCP: Fusion 360 aperto su macOS/Windows, add-in MCP faust-machines abilitato e server raggiungibile (setup in `run/README.md`). Gli stadi di testo (pre-elaborazione, parsing, piano) girano SENZA Fusion.
---

# fusion-build-from-transcript — ricostruzione parametrica in Fusion da un transcript video

Pipeline a **più stadi** orchestrata da Claude Code (questo agente). L'input è il **transcript di
una video-guida** (testo) che mostra come costruire un oggetto in Fusion 360; l'output è il modello
**parametrico** (B-rep) costruito incrementalmente, con storico `.f3d` per ogni modifica.

> **Variante di `fusion-build`.** Il **backend di costruzione è identico**: i tool CAD granulari del
> server MCP faust-machines (`mcp__fusion__create_sketch`, `extrude`, `revolve`, `add_joint`,
> `export_*`…), con `mcp__fusion__execute_code` usato **solo** per render delle viste
> (`saveAsImageFile`) e lettura `body.boundingBox`. Cambiano **solo** l'INGRESSO (un transcript invece
> delle 4 viste), lo stadio di **pre-elaborazione** (transcript→action-log) e il **gate** (giudizio
> **semantico** contro una descrizione attesa, non contro immagini di riferimento). `fusion-build`
> resta indipendente e invariato. Per i dettagli del backend di costruzione vedi anche
> `run/.claude/skills/fusion-build/SKILL.md`.

Helper deterministico: `src/fusion_build.py` (CLI `uv run python -m src.fusion_build ...`).
Niente IO a mano: usa sempre la CLI per init/chunk/action-log/manifest/plan/paths/record/status.

Argomento della skill: il **path del file di transcript** (es. `/tmp/tutorial.txt`). Se manca, chiedi
all'utente il file (o di incollare il testo, che salverai in un file).

> **Costruzione ESATTA.** Un transcript di tutorial dà quote precise ("rettangolo 50×30, estrudi
> 10 mm, raccordo 2 mm"). Riproducile **esattamente** (non proporzionalmente). Unità interne Fusion =
> **cm** (1 mm = 0.1 cm): converti le quote del video da mm a cm scrivendole nei tool.

## Auto mode (default all'invocazione)

Quando la skill viene invocata, esegui l'**intera pipeline in autonomia** — pre-elaborazione →
parsing/manifest → piano → preflight → tutti gli step in sequenza → chiusura → **gate di
convergenza** — **senza fermarti a chiedere conferma** tra una fase e l'altra. Al gate (§6) la
pipeline **si ferma sempre** col verdetto; la re-iterazione (§7) parte solo se l'utente la chiede.

Ti fermi solo per (sicurezza / dati):
- **Transcript mancante** → chiedi il file.
- **Preflight fallito** (Fusion non connesso) quando stai per eseguire (§4) → fermati e istruisci.
- **Documento con lavoro esistente dell'utente** da cancellare → chiedi conferma. Documento
  vuoto/nuovo → procedi.
- **Salvataggio lezioni** (§6.1): al gate proponi i pattern validati e chiedi conferma prima di
  scrivere in `fusion_build_lessons.md` (file versionato, condiviso con fusion-build). È l'unico
  altro punto di conferma, e cade comunque al gate dove la pipeline si ferma già.

## Vincoli HARD di Fusion (valgono per OGNI chiamata MCP — non violarli)

> Identici a `fusion-build`. Valgono dallo stadio di **esecuzione** (§4) in poi; gli stadi di testo
> (§1–§3) non toccano Fusion.

- **Coda main-thread.** Ogni operazione gira sulla coda main-thread dell'add-in (lo gestisce il server).
- **Una sola chiamata alla volta, attesa.** Gli executor sono **rigorosamente in sequenza**: un solo
  documento condiviso mutabile. **Niente batch/operazioni sovrapposte** (crasha l'add-in).
- **Budget ~30s per chiamata.** Spezza gli step pesanti in più chiamate, snapshot tra l'uno e l'altro.
- **Unità interne = cm.** 1 mm = 0.1 cm. Le quote del transcript sono in mm: scrivile in cm. Il gate
  converte la bbox cm→mm (×10).
- **Annullare.** Tool **`undo`** per rollback puntuale dell'ultima operazione; per scartare di più,
  **riapri l'ultimo `.f3d` buono**.
- **Render/bbox solo via `execute_code`.** Il server non ha tool di screenshot né di bbox: usali SOLO
  per render viste e `body.boundingBox`, mai per costruire geometria.

## 1. Pre-elaborazione del transcript → action-log (fold sequenziale, NO MCP)

Il transcript grezzo (auto-caption) è rumoroso: niente punteggiatura, errori ASR sui termini CAD
("fill it" → *fillet*, "loft" → *loafed*), filler, timestamp. Trasformalo in un **action-log
strutturato** PRIMA di pianificare, con un **fold sequenziale incrementale**.

> La **narrazione didattica/best-practice** del tutore ("conviene lavorare in parametrico", "vincola
> sempre lo sketch", "usa i joint, non spostare i corpi") è *meta-conoscenza*, NON un'azione: **non
> forzarla nell'action-log** (che resta operazioni concrete con quote). Viene raccolta al §2 come
> `candidate_lessons` e, se si rivela utile nel build, proposta per `fusion_build_lessons.md` (§6).

1. Bootstrap della build session dal file di transcript:
   ```
   uv run python -m src.fusion_build init-transcript <transcript_file> [--title "<titolo>"]
   ```
   → `{"build_dir": "outputs/fusion/<ts-slug>"}`. Copia il grezzo in `transcript/source.txt`,
   scrive `metadata.json` (**status=preprocessing**, `views={}`).
2. Spezza il transcript in chunk (split deterministico con overlap):
   ```
   uv run python -m src.fusion_build chunk-transcript <build_dir>
   ```
   → `{"chunks": [<path in ordine>], "count": N, "single_pass": bool}`. Se `single_pass` è true c'è
   un solo chunk (transcript corto): il fold degenera in un solo passaggio.
3. **Fold sequenziale** sui chunk **in ordine**. Mantieni un **action-log corrente** (parti da
   `{"actions": [], "open_questions": []}`). Per ogni chunk lancia **un** subagent **Sonnet** con il
   tool **Agent** (`subagent_type: general-purpose`, `model: sonnet`; solo `Read` sul chunk + capacità
   di restituire JSON — **niente MCP / niente Fusion**), passandogli l'**action-log corrente** e il
   path del chunk. Il subagent ritorna l'action-log **esteso**; usalo come "corrente" per il chunk
   successivo. Template sotto.
4. **Review/consolidamento Opus**: quando hai l'action-log completo, fai TU (main loop, thinking
   medio) un passaggio di consolidamento — ordina per `n`, deduplica eventuali doppioni nati dagli
   overlap, normalizza le unità (tutto in **mm**), verifica la continuità (azioni a cavallo di chunk),
   e raccogli le `open_questions`. Salvalo:
   ```
   uv run python -m src.fusion_build save-action-log <build_dir> <action_log.json>
   ```
   → `{"action_log_path", "n_actions", "open_questions"}`. Porta lo stato `preprocessing → parsing`.
   **Logga le `open_questions`** (ambiguità che il parser/planner dovranno gestire).

Schema `action_log.json` (azioni atomiche ordinate, quote **esatte in mm**):
```json
{
  "actions": [
    {
      "n": 1,
      "op": "sketch|rectangle|circle|line|arc|dimension|constraint|extrude|revolve|fillet|chamfer|hole|shell|mirror|pattern|component|joint|...",
      "target": "piano/faccia/sketch/body su cui agisce (es. 'piano top', 'faccia superiore base')",
      "params": {"size_mm": [50, 30], "depth_mm": 10, "radius_mm": 2},
      "refs": "riferimenti a geometria precedente (es. 'spigoli superiori del corpo')",
      "source": {"chunk": 1, "quote": "estrai per dieci millimetri"},
      "confidence": "high|medium|low",
      "note": "ambiguità/assunzione"
    }
  ],
  "open_questions": ["la quota dello smusso non è detta nel video"]
}
```

### Template prompt per il fold (Sonnet, sola lettura testo, NIENTE MCP)

> Sei un trascrittore tecnico esperto di Fusion 360. Ricevi un **action-log corrente** (azioni CAD già
> estratte dai chunk precedenti del transcript) e **un nuovo chunk** del transcript grezzo di un video
> tutorial. Il tuo compito: **ESTENDERE** l'action-log con le azioni implicate da QUESTO chunk,
> coerentemente con quanto già presente.
>
> ACTION-LOG CORRENTE (JSON): {action_log corrente}
> CHUNK DA INTEGRARE (apri con Read): {path del chunk}
>
> Regole (CRITICHE):
> - **Correggi** terminologia/unità CAD e numeri mal trascritti (es. "fill it"→fillet, "loaft"→loft,
>   "extreude"→extrude, "for milimeters"→4 mm). Togli filler ("um", "so", "okay"), ripetizioni e
>   timestamp.
> - **NON riassumere e NON perdere nessuna operazione né alcuna quota.** Ogni sketch, quota, estrusione,
>   raccordo, foro, joint detto nel video deve diventare un'azione. Le quote vanno in **mm** esatti.
> - **Estendi, non riscrivere**: non toccare le azioni già nell'action-log se non per correggere un
>   errore evidente di continuità (es. un'azione spezzata a metà chunk che ora si completa).
> - Se il chunk inizia un'azione che continuerà nel prossimo, registra ciò che è chiaro e segna il
>   resto in `note`/`open_questions`.
> - Numera le nuove azioni con `n` crescente; valorizza `target`, `params` (mm), `refs`, `source`
>   (chunk + breve citazione), `confidence`. Ciò che resta ambiguo → `open_questions`.
>
> RITORNA come testo finale = valore SOLO il JSON dell'action-log COMPLETO ed esteso (stesso schema:
> `{"actions":[...], "open_questions":[...]}`). Niente altro testo.

## 2. Parsing — PART-MANIFEST + expected_summary (parser = Opus subagent, NO MCP)

Dall'action-log estrai l'**inventario di COSA deve esistere** e la **descrizione attesa dell'oggetto
finale**. Il manifest è la **single source of truth** per il planner e per il `parts_check` del gate;
`expected_summary` è il riferimento del **giudizio semantico** (sostituisce le 4 immagini). In più,
estrai i **candidate_lessons**: i **pattern di buon uso generalizzabili** che il tutorial *insegna*
(non i fatti specifici dell'oggetto) — saranno validati e, se utili, salvati in
`fusion_build_lessons.md` (§6).

1. Lancia **un** subagent **parser** con il tool **Agent** (`subagent_type: general-purpose`,
   `model: opus`; `Read` su `transcript/action_log.json` e, se serve disambiguare, su
   `transcript/source.txt`; `Write` per il manifest — **niente MCP**). Template sotto.
2. Salva il manifest (porta lo stato `parsing → planning`):
   ```
   uv run python -m src.fusion_build save-manifest <build_dir> <manifest.json>
   ```
   → `{"manifest_path", "part_ids", "open_questions"}`. **Logga le `open_questions`.**

Schema `manifest.json` (quote **esatte in mm**; `primitive_type` neutro come in fusion-build —
l'executor lo mappa al modo di costruzione Fusion: cilindro/cono/sfera/toro → **revolve**; cubo/plane
→ **extrude**):
```json
{
  "parts": [
    {
      "id": "base",
      "primitive_type": "cube|cylinder|sphere|cone|torus|plane|custom",
      "parameters": {"size_mm": [50, 30, 10], "shape": "prisma rettangolare"},
      "relations": [{"type": "on_top_of|inside|concentric_with|flush_with|attached_to|symmetric_to", "target_id": "...", "detail": "..."}],
      "details": ["raccordo 2 mm sui bordi superiori"],
      "source_actions": [1, 2, 3, 8],
      "confidence": "high|medium|low"
    }
  ],
  "expected_summary": "Descrizione dell'oggetto finale: forma, parti, proporzioni, feature salienti — così come risulterebbe seguendo il tutorial. È il riferimento del giudice semantico.",
  "candidate_lessons": [
    {
      "id": "L1",
      "pattern": "Preferisci il parametrico al direct modeling",
      "rationale": "In parametrico timeline/feature/joint/pattern funzionano in modo normale; in direct mode molte operazioni vanno aggirate",
      "scope": "fusion-backend|general",
      "applies_to_actions": [2, 4],
      "source": {"quote": "always start in parametric mode", "kind": "narration|demonstrated"},
      "confidence": "high|medium|low"
    }
  ],
  "open_questions": ["..."]
}
```
`candidate_lessons` è **opzionale** (`[]` se il tutorial non insegna pattern generalizzabili);
`save-manifest` lo persiste as-is. NON metterci fatti specifici dell'oggetto (quelli stanno in
`parts`/`expected_summary`): solo pattern di **metodo** riusabili in qualunque build.

### Template prompt per il parser (Opus, NO MCP)

> Sei un analista CAD. A partire dall'**action-log** estratto dal transcript di un tutorial Fusion 360
> (e dal transcript originale), produci: (a) l'**inventario delle parti** che il tutorial costruisce,
> (b) una **descrizione testuale dell'oggetto finale atteso**, (c) i **candidate_lessons**: i pattern
> di buon uso generalizzabili che il tutorial insegna.
>
> ACTION-LOG (apri con Read): {transcript/action_log.json}
> TRANSCRIPT ORIGINALE (apri con Read: per disambiguare le parti E per cogliere i pattern insegnati
>   nella narrazione): {transcript/source.txt}
>
> Procedura:
> 1. Raggruppa le azioni in **parti** (body/component) coerenti: ogni parte = un `id` parlante,
>    `primitive_type` (la primitiva più vicina), `parameters` con **quote esatte in mm**, `relations`
>    verso altre parti, `details` (raccordi/fori/smussi/scanalature), `source_actions` (gli `n` delle
>    azioni che la realizzano), `confidence`.
> 2. Scrivi `expected_summary`: cosa dovrebbe risultare alla fine (forma, parti, proporzioni, feature).
>    È il metro del giudice: sii concreto e geometrico, non vago.
> 3. Estrai i **candidate_lessons**: SOLO pattern di **metodo generalizzabili** — riusabili in
>    qualunque build Fusion — che il tutore *insegna* o *dimostra* (es. "lavora in parametrico", "vincola
>    a fondo lo sketch prima di estrudere", "posiziona con i joint, non spostando i corpi", "revolve per
>    i solidi di rivoluzione", "modella metà e specchia"). Per ognuno: `pattern` (1 frase imperativa),
>    `rationale` (il *perché* detto/implicito), `scope` (`fusion-backend` o `general`),
>    `applies_to_actions` (gli `n` delle azioni dove si applica), `source` (`quote` breve + `kind`
>    narration/demonstrated), `confidence`. **ESCLUDI** i fatti specifici dell'oggetto (quote, nomi
>    parti, scelte estetiche del progetto): quelli NON sono lezioni. Se non ce ne sono, `[]`.
> 4. Ciò che resta ambiguo → `open_questions`.
>
> RITORNA come testo finale = valore SOLO il JSON del manifest (schema:
> `{"parts":[...], "expected_summary":"...", "candidate_lessons":[...], "open_questions":[...]}`).
> Scrivilo anche nel file manifest indicato. Niente altro testo.

## 3. PIANO (planner = Opus, lo fai TU nel main loop, thinking MEDIO, NO MCP)

Il planner **consuma l'action-log + il manifest** e produce gli **step granulari ordinati** per la
costruzione in Fusion. L'action-log è già sequenziale: il tuo lavoro è raggrupparlo/pulirlo in step
**applicabili uno alla volta**, mappando ciascuna azione sui **tool granulari faust-machines**, con
le quote in **cm**.

1. Leggi `transcript/action_log.json` e `manifest.json`. Produci il **piano** e salvalo:
   ```
   uv run python -m src.fusion_build save-plan <build_dir> <plan.json>
   ```
   → `{"plan_path", "uncovered_parts"}`. Se `uncovered_parts` non è vuoto, alcune parti del manifest
   non sono coperte: rivedi il piano (di norma vanno coperte tutte).

Schema `plan.json` (come fusion-build, ma gli step derivano dalle azioni del video):
```json
{
  "source": "transcript",
  "strategy": "Costruzione ESATTA seguendo le quote del video: sketch fully-constrained → extrude/revolve, dettagli con feature native (fillet/hole/shell), posizionamento tra parti via JOINTS relativi (concentric/planar/rigid) MAI trasformazioni assolute, ogni parte = un Component, MIRROR per simmetria, build incrementale coarse→fine in cm",
  "steps": [
    {
      "id": 1,
      "title": "Base rettangolare",
      "goal": "Volume base 50x30x10 mm",
      "target": "Component 'base'",
      "manifest_parts": ["base"],
      "source_actions": [1, 2, 3],
      "fusion_approach": "create_component → create_sketch su piano XY → draw_rectangle → add_dimension 5.0x3.0 cm → extrude 1.0 cm",
      "features": ["sketch", "extrude"],
      "joints": [],
      "acceptance": "prisma 50x30x10 mm; spigoli vivi (raccordo in step successivo)",
      "depends_on": []
    }
  ]
}
```
Linee guida: **copri il manifest** (ogni parte citata in `manifest_parts` di almeno uno step); **una
parte = un Component**; **esatto per costruzione** (sketch vincolato/quotato → extrude/revolve);
**simmetria = `mirror`**; **posizionamento via `add_joint`** (mai trasformazioni assolute); dettagli
(`fillet`/`create_hole`/`shell`) negli step finali; quote in **cm**; 5–15 step tipicamente; ogni step
applicabile da solo sopra lo stato precedente; `acceptance` derivato dall'azione del video.

## 0/§3.5 Preflight (OBBLIGATORIO prima dell'esecuzione)

Solo ora serve Fusion. Chiama **`mcp__fusion__ping`**.
- Se fallisce/timeout ⇒ Fusion non connesso o add-in non in esecuzione. **Fermati** e istruisci:
  apri Fusion 360, esegui l'add-in **Fusion360MCP** (Shift+S → Add-Ins → Fusion360MCP → Run), verifica
  nel log `~/fusion360mcp.log` "[MCP] Server listening on localhost:9876", riavvia Claude da `run/`.
  Setup in `run/README.md`. Non eseguire nessuno step finché il preflight non passa.
- Stato documento con **`get_scene_info`**/`list_components`: vuoto → procedi; con lavoro utente →
  chiedi conferma (perdita dati). In dubbio, nuovo documento (via `execute_code` `app.documents.add`).

## 4. ESECUZIONE incrementale (executor = Sonnet, IN SEQUENZA)

> Identica a `fusion-build` §3, **tranne** la verifica render: non ci sono `refs/`, quindi il render
> serve a controllare la forma rispetto all'`acceptance` dello step e a `expected_summary` (del
> manifest), non rispetto a immagini di input.

**Sequenza rigorosa**: un solo documento condiviso ⇒ lancia **un** subagent per volta e **aspetta**.

Per ogni step in ordine:
1. Calcola i path artefatti: `uv run python -m src.fusion_build paths <build_dir> <step_id>` →
   `{"f3d", "stl", "png", "json"}` (assoluti).
2. Lancia **un** subagent executor (`subagent_type: general-purpose`, `model: sonnet`; ha i tool
   `mcp__fusion__*` granulari **+** `mcp__fusion__execute_code` *(solo render/bbox)* + Write).
   Template sotto, con piano completo, step corrente, **le parti del manifest** citate in
   `step.manifest_parts`, `expected_summary`, i **candidate_lessons pertinenti** (quelli il cui
   `applies_to_actions` interseca `step.source_actions`), e i path artefatti.
3. Registra il record: `uv run python -m src.fusion_build record <build_dir> <record.json>`.
4. Step successivo. **Best-effort**: se uno step non converge in **3 tentativi**, salva il migliore,
   marca `status:"failed"` con note, prosegui.

### Mappa relazioni del manifest → JOINTS / vincoli Fusion (con `add_joint`)

| relation (manifest)         | realizzazione faust-machines                                            |
|-----------------------------|-------------------------------------------------------------------------|
| `concentric_with` / coaxial | `add_joint` tipo **cylindrical**/**rigid** su assi/spigoli concentrici  |
| `on_top_of` / `flush_with`  | `add_joint` tipo **planar** tra le facce a contatto (offset 0 = a filo)  |
| `attached_to`               | `add_joint` tipo **rigid** su origini/geometrie coincidenti             |
| `inside`                    | **concentric** + **planar** con offset (`add_joint`, due joint)         |
| `symmetric_to`              | **`mirror`** (feature) sul piano di simmetria — non un joint            |

**VIETATO** `move_body`/trasformazioni assolute per "piazzare" una parte. Firme dei riferimenti dei
tool granulari *[da confermare sul Mac]*: in caso d'errore leggi il messaggio del tool e correggi;
per l'API `adsk` sottostante (es. `Joints add`/`JointInput`) usa il lookup della doc locale
(§"Documentazione API locale").

### Render di verifica (pose canoniche, via execute_code)

Unico uso ammesso di `execute_code`: dentro una chiamata, per ogni vista utile imposta
`app.activeViewport.camera` con le pose canoniche `VIEW_CAMERA` di `src/fusion_build.py`
(front=+Y, side=−X, back=−Y, three_quarter=iso; up=+Z), `viewport.fit()`, poi
`saveAsImageFile(<png>, w, h)`. Apri il PNG con Read e giudica forma/simmetria rispetto ad
`acceptance` e `expected_summary`. Per le firme `adsk` del render (`Camera.eye`/`target`/
`upVector`, `Viewport.saveAsImageFile`) fai lookup nella doc locale se incerto.

### Template prompt per l'executor (Sonnet)

> Sei un executor di modellazione **PARAMETRICA** in Fusion 360 via MCP. Applichi **un solo step**
> orchestrando una **sequenza di tool CAD granulari** `mcp__fusion__*` (una operazione per chiamata).
> Usi `mcp__fusion__execute_code` **SOLO** per il render di verifica (camera + `saveAsImageFile`).
> **Esattezza per costruzione**: sketch vincolati e quotati → extrude/revolve, feature native, MAI mesh.
> Le firme dei tool non sono documentate: fidati dello schema esposto e, in caso d'errore, leggi il
> messaggio del tool e correggi; per l'API `adsk` sottostante e per il Python di `execute_code` fai
> lookup nella doc locale (`fusion-docs/search.py`, vedi §"Documentazione API locale"). Focus su
> **geometria e simmetria**; materiali/colori secondari.
>
> PIANO COMPLETO (contesto): {plan.json}
> STEP DA APPLICARE: {step}
> PARTI DEL MANIFEST DA REALIZZARE: {parts di step.manifest_parts}
> OGGETTO ATTESO (per il check visivo della forma): {manifest.expected_summary}
> PATTERN SUGGERITI DAL TUTORIAL (candidate_lessons pertinenti): {candidate_lessons con applies_to_actions ∩ step.source_actions}
> PATH ARTEFATTI (assoluti): f3d={...} stl={...} png={...} json={...}
>
> VINCOLI HARD: unità **cm** (1 mm = 0.1 cm); **una operazione per chiamata** (batch crasha); ~30s a
> chiamata (spezza, snapshot); annullare = `undo` o riapri `.f3d`; `execute_code` solo per render.
> **Doc API locale — non indovinare se puoi verificare**: prima di scrivere `adsk.*` in
> `execute_code`, o se un tool granulare erra per firma sconosciuta, fai lookup lessicale
> `uv run python fusion-docs/search.py "<keyword adsk>" -k 5` → `Read` del md (cerca per
> identificatore, es. `saveAsImageFile`/`boundingBox`/`Joints add`, non per nome del tool).
>
> Procedura (sequenza granulare, una chiamata per volta):
> 1. Stato con `get_scene_info`; se serve, `set_design_type` → parametrico.
> 2. **Component** (`create_component`), poi `create_sketch` → `draw_*`/`create_polygon` →
>    `add_constraint` + `add_dimension` (cm), poi `extrude`/`revolve`. Simmetria con `mirror`.
> 3. **Posiziona con `add_joint`** secondo `step.joints` e la mappa relazioni→joint. MAI trasformazioni
>    assolute.
> 4. **Dettagli**: `fillet`/`create_hole`/`shell`/extrude-cut/`boolean_operation`.
> 5. Verifica via `execute_code`: render delle pose utili (`VIEW_CAMERA`), `viewport.fit()`,
>    `saveAsImageFile(<png>,w,h)`; apri il PNG con Read e giudica forma/simmetria vs `acceptance` ed
>    `expected_summary`.
> 6. RIPETI fino a **3 tentativi**. A ogni retry: `undo`, oppure ri-quota (`set_parameter`/
>    `add_dimension`), oppure riapri l'ultimo `.f3d`; poi ri-verifica. Dopo il 3° → miglior tentativo +
>    `status:"failed"`.
> 7. Snapshot/export nativi: `export_f3d` → `<f3d>`, `export_stl` → `<stl>`. Render in `<png>`.
>    Scrivi `<json>` col record.
> 8. RITORNA (testo finale = valore) SOLO il JSON record:
>    `{"id","title","status":"done|failed","attempts":N,"f3d":"steps/step_NN.f3d",
>      "stl":"steps/step_NN.stl","png":"steps/step_NN.png","components":[...],"joints":[...],
>      "applied_lessons":["L1", ...],"notes":"..."}`
>    (path RELATIVI alla build dir). In **`applied_lessons`** elenca gli `id` dei PATTERN SUGGERITI
>    che hai **effettivamente seguito** in questo step (lista vuota se nessuno); se un pattern ha
>    aiutato o NON ha funzionato, dillo in `notes`. Seguire i pattern non è obbligatorio: è
>    tracciamento onesto di cosa hai usato, serve a validarli a fine pipeline.

## 5. Chiusura

Dopo l'ultimo step, export finali con i tool nativi:
```
uv run python -m src.fusion_build final-paths <build_dir>
```
→ `export_f3d` → `final.f3d`, `export_step` → `final.step`, `export_stl` → `final.stl` (path assoluti).
**NON** riassumere: prosegui col **gate (§6)**. Lo stato `done` lo imposta il gate.

## 6. Gate di convergenza — giudizio SEMANTICO (no immagini di riferimento)

Confronta l'output con ciò che il tutorial avrebbe prodotto, in modo **misurabile + semantico**.

1. Path artefatti: `uv run python -m src.fusion_build eval-paths <build_dir>` →
   `{"iteration", "views": {front,three_quarter,side,back → png assoluti}, "verdict": "...abs..."}`.
2. **Metriche oggettive** (via `execute_code`): bbox `body.boundingBox` (cm) → ×10 = mm; se il
   manifest implica una bbox attesa dalle quote del video usala come `target_mm` (sanity →
   `proportion_error_pct`); `symmetry_dev_mm` per asse (specchiatura); `watertight` via `export_stl` +
   trimesh (e `body.isSolid`). Per le firme `adsk` (`BRepBody.boundingBox`, `BoundingBox3D`,
   `BRepBody.isSolid`) fai lookup nella doc locale se incerto.
3. **Render 4 viste** dell'output (pose `VIEW_CAMERA`, via `execute_code`), salvate nei `views[...]`.
4. **Giudizio semantico** — lancia **un** subagent giudice (Sonnet) col template sotto: NON ci sono
   immagini di riferimento; il giudice confronta i 4 render con `expected_summary` + `manifest` →
   `views[].score` (quanto il modello corrisponde all'oggetto descritto) + `parts_check` +
   `actionable_gaps`.
5. Componi `verdict.json` e registra:
   ```
   uv run python -m src.fusion_build record-eval <build_dir> <verdict.json>
   ```
   → raccomandazione deterministica (`ACCEPT_THRESHOLD=85`, `MAX_ITERATIONS=2`). Imposta `status=done`.
6. **Riporta all'utente e FERMATI** (sempre): punteggi per-vista, metriche, copertura manifest,
   `actionable_gaps`, raccomandazione + motivazione. Se `reiterate`, di' che può rilanciare (§7).
7. **Distillazione lezioni dal tutorial** (sotto, §6.1): includila nel report del gate e, su
   conferma utente, salva i pattern validati in `fusion_build_lessons.md`.

Schema `verdict.json`:
```json
{
  "iteration": 1,
  "metrics": {"bbox_mm": [50.0,30.0,10.0], "target_mm": [50,30,10], "proportion_error_pct": 0.0,
              "symmetry_dev_mm": {"x":0.0}, "watertight": true},
  "parts_check": [{"id":"base","present":true,"relations_ok":true,"notes":"ok"}],
  "views": [{"view":"front","score":90,"notes":"silhouette coerente con la descrizione"}],
  "overall_score": 90,
  "actionable_gaps": ["manca il raccordo 2 mm sui bordi superiori"]
}
```

### Template prompt per il giudice (Sonnet, semantico)

> Sei un giudice di fedeltà geometrica. NON hai immagini di riferimento: hai i **render del modello**
> (4 viste) e una **descrizione testuale dell'oggetto atteso** (`expected_summary`) + il **manifest**.
> Valuta, **solo su forma** (silhouette, proporzioni, simmetria, presenza/posizione delle feature),
> quanto il modello renderizzato corrisponde alla descrizione. IGNORA colori/materiali/sfondo.
>
> RENDER (apri con Read): {front, three_quarter, side, back}
> OGGETTO ATTESO: {manifest.expected_summary}
> MANIFEST (parti attese): {manifest.json}
> METRICHE (contesto): {bbox_mm, target_mm, symmetry_dev_mm, watertight}
>
> Per ogni vista assegna `score` 0–100 (100 = combacia con la descrizione) + note. Per ogni parte del
> manifest verifica presenza/relazioni → `parts_check`. Elenca `actionable_gaps`: differenze
> GEOMETRICHE concrete e correggibili (es. "manca il raccordo", "la base è troppo alta vs descritto").
>
> RITORNA come testo finale SOLO il JSON:
> `{"views":[{"view":"front","score":N,"notes":"..."}, ...],
>   "parts_check":[{"id":"...","present":true|false,"relations_ok":true|false,"notes":"..."}, ...],
>   "overall_score":N, "actionable_gaps":["...", "..."]}`
> dove `overall_score` è la sintesi pesata (penalizza forte proporzioni/simmetria errate e parti
> mancanti). Niente altro testo.

### 6.1 Distillazione lezioni dal tutorial (al gate, scrittura SU CONFERMA)

I tutorial insegnano pattern di buon uso del backend (es. "parametrico > direct", "vincola lo
sketch", "joint non trasformazioni"). Salva nel knowledge base **solo quelli che si sono
rivelati utili in QUESTO run** — non ogni affermazione del video. Lo fai TU (main loop) come
ultimo passo del gate, prima di chiudere il report.

1. **Raccogli**: `manifest.candidate_lessons`, tutti i record step (`applied_lessons`, `status`,
   `notes`), il `verdict` (`overall_score`, `actionable_gaps`), e `fusion_build_lessons.md` (Read,
   per il dedup).
2. **Filtro evidence-based** — tieni un candidate-pattern SOLO se TUTTE:
   - **generalizzabile** (`scope` general/fusion-backend; non un fatto specifico dell'oggetto);
   - **applicato**: il suo `id` compare in `applied_lessons` di ≥1 step (o è chiaramente realizzato
     da uno step `done` a cui mappa via `applies_to_actions`);
   - quegli step sono **`status: done`**;
   - **ha funzionato**: `overall_score` decente (≳ `ACCEPT_THRESHOLD` o comunque alto) **oppure** il
     pattern ha chiuso un gap (non compare più tra gli `actionable_gaps`).
   Scarta tutto il resto **con motivazione** (mai applicato / step failed / specifico dell'oggetto /
   gate scarso / **già presente** nel file lezioni).
   > Esempi: `"parametrico > direct"` applicato in step 2,4 (done) + gate 88 → **SALVA**;
   > `"usa material X"` mai in `applied_lessons` → **SCARTA** (mai applicato);
   > `"sketch fully-constrained"` ma già nel file lezioni → **SCARTA** (duplicato).
3. **Proponi nel report del gate**: elenca i pattern superstiti con la **prova d'uso** (step + esito
   + `overall_score`) e **chiedi all'utente** se salvarli (è l'unico punto di conferma nuovo).
4. **Su conferma** (e solo allora): aggiungi in coda a `fusion_build_lessons.md` una **sottosezione
   datata e tracciabile**, poi committa (come per le altre lezioni):
   ```
   ## 📚 Pattern da tutorial — <slug> (<YYYY-MM-DD>, gate <overall_score>)
   - **<pattern>** — <rationale>. _Validato_: step <ids> (done); fonte: "<quote>".
   ```
   Su **no**: non scrivere nulla.

Se l'utente chiede di rilanciare dopo `reiterate`:
```
uv run python -m src.fusion_build reinit-from-result <prev_build_dir>
```
→ nuova build con `seed.f3d` (= `final.f3d` precedente), **stesso `manifest.json`** (con
`expected_summary` invariato), `planning_hints` = `actionable_gaps`. (`refs/` semplicemente assenti.)
Esegui poi piano (di rifinitura, mirato ai gap) → esecuzione → gate. Salti pre-elaborazione e parsing
(action-log e manifest già pronti); lo step 1 NON crea geometria ma apre `<seed.f3d>` (via
`execute_code` `app.documents.open` *[da verificare sul Mac]*; per la firma esatta:
`fusion-docs/search.py "documents open"` → `Documents.open`).

## Documentazione API locale — lookup lessicale (usalo PRIMA di indovinare l'API)

C'è una **knowledge base offline dell'API Fusion** in `run/fusion-docs/` (markdown + indice
lessicale SQLite FTS5/BM25, estratta dal CHM ufficiale). Serve a scrivere il Python `adsk.*`
**giusto al primo colpo** invece di tirare a indovinare. **Consultala** in due casi:
- **prima** di scrivere `adsk.*` dentro `mcp__fusion__execute_code` (camera/`saveAsImageFile`,
  `boundingBox`, `isSolid`, `app.documents.open`, proprietà fisiche…);
- **quando un tool granulare erra per firma sconosciuta** → cerca l'API `adsk.*` sottostante per
  capire parametri/tipi (è un punto del self-heal del retry-loop).

Comando (dalla cwd RUN = `run/`), poi `Read` del path md **assoluto** stampato:
```
uv run python fusion-docs/search.py "<identificatore o keyword>" -k 5
```
È **lessicale**: cerca per **identificatore/nome** di classe o membro (o poche keyword), NON per
frase concettuale lunga e **NON per il nome snake_case del tool MCP**. Per trovare l'API dietro a
un tool granulare, cerca il **concetto `adsk`**, non il nome del tool:

| serve…                         | query efficace            | pagina top |
|--------------------------------|---------------------------|------------|
| render vista                   | `saveAsImageFile`         | `Viewport.saveAsImageFile` |
| posa camera                    | `Camera eye`              | `Camera.eye` / `CameraTypes` |
| bbox per il gate               | `boundingBox`             | `BRepBody.boundingBox` |
| watertight / solido            | `isSolid`                 | `BRepBody.isSolid` |
| aprire il seed in re-iterazione| `documents open`          | `Documents.open` |
| joint (tool `add_joint`)       | `Joints add`              | `Joints.add` / `JointInput` |
| sketch (tool `create_sketch`)  | `Sketches add`            | `Sketches.add` |

Resta **grounding**, non oracolo: per i tool granulari la firma autorevole è lo **schema esposto
dal server** + il messaggio d'errore; la doc spiega l'API `adsk` che quei tool incapsulano e
quella che usi in `execute_code`.

> **Precondizione (una tantum, sul Mac):** la doc è un artefatto **gitignored**. Se `search.py`
> dà `no matches` o manca `fusion-docs/docs.db`, costruiscila una volta:
> `bash fusion-docs/extract_chm.sh && uv run python fusion-docs/parse_html.py && uv run python fusion-docs/build_index.py`
> (dettagli in `fusion-docs/README.md`). `Bash(uv run *)` è già pre-concesso in RUN: nessun prompt.

## Toolbox parametrico Fusion (preferenze — privilegiare l'esatto per costruzione)

- **Sketch+vincoli/quote** → `create_sketch`, `draw_rectangle/circle/line/arc/spline`, `create_polygon`,
  `add_constraint`, `add_dimension`, `offset_curve`, `trim_curve`, `project_geometry`.
- **Volume** → `extrude`, `revolve`, `loft`, `sweep` (+ primitive dirette per casi banali).
- **Modifica** → `shell`, `fillet`, `chamfer`, `create_hole`, `split_body`, `boolean_operation`.
- **Pattern/simmetria** → `mirror`, `circular_pattern`, `rectangular_pattern`.
- **Assieme** → `create_component` + `add_joint` (rigid/cylindrical/planar/revolute),
  `create_as_built_joint`, `create_rigid_group`; `create_construction_plane`/`create_construction_axis`.
- **Parametri** → `get_parameters`, `create_parameter`, `set_parameter`.
- **Export** → `export_f3d`, `export_step`, `export_stl`.
- **VIETATO**: mesh/per-vertice; `move_body`/trasformazioni assolute per posizionare; boolean "sporchi".
- **Firma o parametri incerti** (tool granulare o API `adsk` in `execute_code`)? → **lookup lessicale**
  della doc locale (§"Documentazione API locale"): `uv run python fusion-docs/search.py "<keyword>"`.

## Gotcha / principi

- **Pre-elaborazione fedele**: il rischio #1 è perdere operazioni/quote durante la pulizia. Il fold
  Sonnet **non riassume mai**; la review Opus controlla che ogni azione del video sia rappresentata.
- **Quote esatte**: segui i mm detti nel video (in cm nei tool). Non inventare proporzioni.
- **Esatto per costruzione**: sketch vincolati/quotati → extrude/revolve. Posiziona con `add_joint`,
  non con trasformazioni. Simmetria con `mirror`.
- **Una operazione per chiamata, ~30s**: spezza, snapshot `.f3d`; annulla con `undo` o riapri `.f3d`.
- **Porta 9876 condivisa con blender-mcp**: non tenere i due add-in attivi insieme.
- **Render/bbox solo via `execute_code`**; tutta la geometria via tool granulari.
- **Doc API locale**: per il Python `adsk.*` di `execute_code` e per capire l'API dietro ai tool
  (firme MCP non documentate) usa il **lookup lessicale** `fusion-docs/search.py` (cerca per
  identificatore `adsk`, non per nome snake_case del tool) — vedi §"Documentazione API locale".
- **Knowledge base**: gotcha Fusion confermati in `run/fusion_build_lessons.md` (condiviso con
  fusion-build) — aggiungi lì i nuovi e committa. Inoltre, i run da transcript **propongono al
  gate** (§6.1) i pattern di buon uso *insegnati dal tutorial e validati nel build* (applicati +
  funzionanti), e li salvano lì **su conferma** dell'utente — non ogni affermazione del video.

## Note operative

- Esecuzione **dentro Claude Code**, niente app web. Nessuna chiamata Meshy/SCAD.
- Gli stadi **§1–§3 girano su Linux/qualsiasi OS** (solo testo + LLM); Fusion serve solo da §4 (macOS/
  Windows). Setup MCP: sezione `fusion-build` in `run/README.md`.
- **Due contesti (DEV vs RUN)**: la skill vive in `run/.claude/skills/`; i run si lanciano da `run/`
  (MCP `fusion` + `src/` read-only). Gli artefatti restano in `outputs/fusion/<ts-slug>/` (gitignored).
- I path passati a Fusion devono essere **assoluti** (stessa macchina): usali come tornano da `paths`/
  `final-paths`.
