---
name: fusion-build-from-components
description: Costruisce in modo INCREMENTALE un modello 3D PARAMETRICO in Fusion 360 (live, via MCP) a partire da una sessione Oggetti già SCOMPOSTA in COMPONENTI (cartella `components/` con `components.json` + le viste 3/4 per ciascun componente, prodotta dallo stadio web `breakdown`). Variante di fusion-build con lo STESSO backend di costruzione (tool CAD granulari del server faust-machines + `execute_code` solo per render/bbox) e lo stesso gate, ma con un nuovo stadio di ACTION-PLAN (come fusion-build-from-transcript) prima della costruzione: l'inventario dei componenti SOSTITUISCE le sole viste dell'intero oggetto. Insight: ogni componente è una PARTE già identificata, con le SUE viste 3/4 e la SUA interfaccia di accoppiamento → il parsing è soprattutto mapping componenti→manifest + inferenza dei JOINT inter-componente dalle relazioni dichiarate e dall'immagine whole-object. Parte in AUTO MODE all'invocazione. Un parser Opus mappa i componenti su un PART-MANIFEST, un planner Opus mappa l'action-plan sui tool granulari Fusion costruendo ogni componente come Component separato, executor Sonnet in sequenza eseguono ogni step auto-verificandosi con render della viewport e assemblano i pezzi con JOINTS secondo l'assembly_order. Termina con un GATE DI CONVERGENZA che confronta il modello assemblato sia col design whole-object sia con le immagini per-componente, e raccomanda in modo deterministico stop o re-iterazione. USARE quando l'utente chiede di "costruire/modellare in Fusion DA(I) COMPONENTI" una sessione già scomposta. PRECONDIZIONE: Fusion 360 aperto su macOS/Windows, add-in MCP abilitato e server raggiungibile (setup in `run/README.md`); sessione sorgente con `components/components.json` (lanciare prima lo stadio `breakdown`).
---

# fusion-build-from-components — costruzione parametrica da oggetto scomposto in componenti

Pipeline a **più stadi** orchestrata da Claude Code (questo agente) sui **tool CAD granulari** del
server MCP Fusion (faust-machines): **action-plan → parsing → piano → esecuzione → chiusura → gate**.
Input: una sessione Oggetti **già scomposta** (`outputs/<ts-slug>/components/components.json` +, per
ogni componente, i crop delle sue viste 3/4). Output: modello Fusion **parametrico** (B-rep) in cui
**ogni componente è un Component** costruito dalle sue viste e **assemblato con JOINTS**, con
storico `.f3d` per ogni step.

> Le sessioni Oggetti correnti usano **due viste 3/4** (`front_3q`, `rear_3q`) per l'intero oggetto e
> per ogni componente (layout 1x2). I nomi e il numero delle viste **non sono cablati**: leggili da
> `metadata['views']` (whole) e `metadata['component_views']` (per-componente), e dai file in `refs/`
> e `refs/components/<id>/`. **Sessioni scomposte vecchie possono avere ancora 4 viste (schema
> legacy: front/three_quarter/side/back); leggi i nomi reali da components.json / metadata, non
> assumerli.**

> **Variante di `fusion-build`.** Tutto il layer di costruzione geometrica (sketch→extrude/revolve,
> feature native, joints), il render di verifica, le metriche e la regola del gate sono **identici**
> a fusion-build. Cambia l'**input** (un bundle di componenti invece delle sole viste dell'intero
> oggetto) e si aggiunge uno **stadio di ACTION-PLAN** (§0.5) prima del parsing — analogo all'action-log
> di `fusion-build-from-transcript`, ma **segmentato per componente** invece che derivato da un
> transcript piatto. `fusion-build` e `fusion-build-from-transcript` restano indipendenti e invariati.

Helper deterministico: `src/fusion_build_from_components.py` (CLI
`uv run python -m src.fusion_build_from_components ...`). Importa e riusa i pezzi CAD-agnostici da
`src/fusion_build.py` (save-manifest, save-plan, paths, record, eval, gate). Niente IO a mano: usa
sempre la CLI per init/action-plan/manifest/plan/paths/record/status.

Argomento della skill: il path della sessione Oggetti **scomposta** (es.
`outputs/20260602-...-teapot`). Se manca, chiedi all'utente quale sessione usare. Se la sessione non
ha `components/components.json`, **fermati** e di' all'utente di lanciare prima lo stadio `breakdown`
(CLI: `uv run python -m src.main breakdown <session>`, oppure il bottone "Scomponi in componenti"
nella web UI).

> **Sessione generata su un server remoto.** Se il `breakdown` è girato sull'app web remota, la
> session dir non è sul PC con Fusion. Tirala giù **prima** col bundle dell'handoff: in web, bottone
> "⬇ Scarica bundle (.zip)" nella sezione componenti (endpoint `GET /sessions/<id>/bundle.zip`);
> oppure da CLI `uv run python -m src.main pull <id> --from <url-server>` (token via `--token` o env
> `MODELS3D_PULL_TOKEN`), che ricostruisce `outputs/<id>/` in locale e stampa il path da passare a
> questa skill. Il bundle contiene solo gli input utili: `components.json` + viste whole/per-componente.

> **Modello a tool granulari + eccezione `execute_code`** — identico a fusion-build. Il server
> `fusion` espone i tool CAD granulari `mcp__fusion__*` (`create_sketch`, `draw_*`, `add_constraint`,
> `add_dimension`, `extrude`, `revolve`, `fillet`, `shell`, `mirror`, `create_hole`,
> `create_component`, `add_joint`, `export_stl/step/f3d`, …): **la costruzione si fa con questi**, una
> operazione per chiamata, in sequenza. `mcp__fusion__execute_code` si usa **SOLO** per (a) render
> delle viste (`camera` + `viewport.fit()` + `saveAsImageFile`) e (b) lettura `body.boundingBox` per
> le metriche del gate (in cm). **Vietato** costruire geometria con `execute_code`.
>
> ⚠️ Le **firme dei parametri** dei tool granulari **non sono documentate**: fidati dello schema
> esposto dal server e, in caso di errore, leggi il messaggio del tool e correggi (self-heal del
> retry-loop). L'**API `adsk.*`** sottostante (che usi in `execute_code`) è documentata localmente:
> consultala col lookup lessicale (§"Documentazione API locale") prima di indovinare.

## Layout della build dir (importante)

`init` crea `outputs/fusion/<ts-slug>/` con:
- `refs/<view>.png` → le viste dell'**INTERO oggetto** (whole; correnti: `front_3q.png`,
  `rear_3q.png`). **Stessa posizione di fusion-build**, così il gate (eval render ↔ `refs/<view>.png`),
  make-lenses e reinit funzionano invariati. I nomi reali sono in `metadata['views']`.
- `refs/components/<id>/<view>.png` → le viste di **ogni componente** (riferimento PRIMARIO di forma
  per lo step che costruisce quel componente). I nomi reali sono in `metadata['component_views']`.
- `refs/lenses/` e `refs/components/<id>/lenses/` → lenti percettive (edges/silhouette/color).
- `components_source.json` → copia del bundle sorgente (per parser/planner).
- `metadata.json` con `components` (schede), `component_views`, `views` (whole), `status`.

## Auto mode (default all'invocazione)

Esegui l'**intera pipeline in autonomia** — preflight → action-plan → parsing/manifest → piano →
tutti gli step in sequenza → chiusura → **gate** — **senza fermarti a chiedere conferma** tra una
fase e l'altra. Al gate (§5) la pipeline **si ferma sempre** col verdetto; la re-iterazione (§6)
parte solo se l'utente la chiede.

Unici punti in cui ti fermi (sicurezza / dati / input):
- **Preflight fallito** (Fusion non connesso) → fermati e istruisci l'utente (§0).
- **`components/components.json` mancante** nella sessione sorgente → fermati, di' di lanciare prima `breakdown`.
- **Documento con lavoro esistente dell'utente** da cancellare → chiedi conferma (perdita dati).
- **Path sessione sorgente mancante** → chiedi quale sessione usare.

## Vincoli HARD di Fusion (valgono per OGNI chiamata MCP — identici a fusion-build)

- **API non thread-safe** → ogni operazione gira sulla coda main-thread dell'add-in.
- **Una sola chiamata alla volta, attesa.** Executor **rigorosamente in sequenza** (mai paralleli):
  un solo documento condiviso mutabile. Niente operazioni sovrapposte (crasha l'add-in).
- **Budget ~30s per chiamata.** Spezza gli step pesanti, snapshot `.f3d` tra l'uno e l'altro.
- **Unità interne = centimetri** (1 mm = 0.1 cm). Quote in cm, tonde e proporzionali; il gate
  converte la bbox cm→mm (×10).
- **Annullare**: tool `undo` per rollback puntuale; per scartare più operazioni, riapri l'ultimo
  `.f3d` buono. Niente loop non limitati che mutano geometria.

## 0. Preflight (OBBLIGATORIO)

1. **`mcp__fusion__ping`** (bridge add-in, TCP 9876). Se fallisce ⇒ Fusion non connesso: **fermati** e
   istruisci l'utente (apri Fusion 360, Shift+S → Add-Ins → Fusion360MCP → Run; verifica
   `~/fusion360mcp.log` "Server listening on localhost:9876"; riavvia Claude da `run/`. Setup in
   `run/README.md`). Non creare build session finché il preflight non passa.
2. **Verifica input**: che `<source_dir>/components/components.json` esista. Se manca → fermati e di'
   all'utente di lanciare prima lo stadio `breakdown`. (Puoi controllarlo con
   `uv run python -m src.fusion_build_from_components inputs <source_dir>`: se solleva, l'input non è
   pronto.)
3. **`mcp__fusion__get_scene_info`** / **`list_components`**: se il documento è vuoto/nuovo, in auto
   mode procedi; se contiene lavoro dell'utente, chiedi conferma (perdita dati) o crea un nuovo documento.

## 0.5 ACTION-PLAN (NUOVO stadio — sostituisce il pre-processing del transcript)

Insight: a differenza di un transcript grezzo (rumoroso, da ripulire con un fold chunk-per-chunk),
qui l'input è **già strutturato** (un inventario di componenti con relazioni e interfacce). Quindi
l'action-plan si produce con **una singola passata di consolidamento Opus** (niente chunking/fold).

1. Bootstrap della build session:
   ```
   uv run python -m src.fusion_build_from_components init <source_dir>
   ```
   → `{"build_dir": "outputs/fusion/<ts-slug>"}`. Copia le viste whole in `refs/`, le viste per
   componente in `refs/components/<id>/`, genera le lenti su entrambi, copia il bundle in
   `components_source.json`, scrive `metadata.json` (**status=preprocessing**).
2. Lancia **un** subagent **action-plan** (Agent, `subagent_type: general-purpose`, `model: opus`,
   sola percezione: `Read` + `Write`, **niente MCP**) col **template action-plan** sotto. Legge
   `components_source.json` + le viste whole (`refs/<view>.png`) + le viste di ogni componente
   (`refs/components/<id>/<view>.png`) + le lenti, e produce `action-plan.json` (schema sotto). Itera
   sulle viste che esistono in `metadata['views']`/`metadata['component_views']` (correnti: `front_3q`,
   `rear_3q`), non assumerne il numero.
3. Salva l'action-plan (porta lo stato `preprocessing → parsing`):
   ```
   uv run python -m src.fusion_build_from_components save-action-plan <build_dir> <action-plan.json>
   ```
   → `{"action_plan_path", "components", "n_joints", "open_questions"}`. **Logga le `open_questions`**.

Schema `action-plan.json` (segmentato per componente + joints inter-componente). Le quote sono
**relative/proporzionali** (whole-object: non c'è un riferimento metrico assoluto come nel transcript):
```json
{
  "schema_version": 1,
  "source_session": "<name>",
  "strategy": "Ogni componente = un Component Fusion costruito dalle sue viste 3/4 (sketch→extrude/revolve, esatto per costruzione, simmetria via mirror); poi assembla i componenti con JOINTS relativi secondo le relazioni dichiarate e l'assembly_order; quote in cm, proporzionali.",
  "components": [
    {
      "component_id": "body",
      "name": "Teapot body",
      "build_order": 1,
      "actions": [
        {"op": "sketch_revolve", "detail": "profilo metà-sezione sul piano XZ → revolve attorno a Z", "reference_views": ["front_3q", "rear_3q"]},
        {"op": "shell", "detail": "cavità interna, spessore ~2mm"},
        {"op": "fillet", "detail": "raccordo bordo superiore"}
      ],
      "interface": "apertura superiore ~Ø40mm; attacchi laterali per beccuccio e manico"
    }
  ],
  "joints": [
    {"from": "lid", "to": "body", "relation": "concentric_with", "joint_type": "cylindrical",
     "detail": "flangia del coperchio dentro il collo del corpo", "from_view_evidence": ["whole/front_3q"]}
  ],
  "assembly_order": ["body", "spout", "handle", "lid"],
  "open_questions": ["beccuccio: curva singola o composta?"]
}
```
`joints[].relation` usa lo **stesso vocabolario** del manifest; `joint_type` si mappa con la tabella
relazioni→joint (§3): concentric→cylindrical/rigid, on_top_of/flush→planar, attached_to→rigid,
inside→concentric+planar, symmetric_to→mirror.

### Template prompt per l'action-plan (Opus, sola percezione)

> Sei un pianificatore CAD. NON modelli nulla: produci un **action-plan** per costruire in Fusion un
> oggetto **già scomposto in componenti**, partendo dal bundle dei componenti e dalle immagini. Niente
> CAD, niente codice: solo analisi e struttura.
>
> BUNDLE COMPONENTI: {components_source.json} (id, name, description, relations, relative_scale,
>   interface per ogni componente; + main_subject + design_views)
> VISTE WHOLE (Read): {refs/<view>.png per ogni view in metadata['views'] — correnti: refs/front_3q.png,
>   refs/rear_3q.png} (+ lenti in refs/lenses/) — l'oggetto INTERO assemblato: usale per capire come i
>   pezzi si montano tra loro.
> VISTE PER COMPONENTE (Read): {refs/components/<id>/<view>.png per ogni view in
>   metadata['component_views']} (+ lenti) — ogni componente ISOLATO: usale per capire la forma/azioni
>   di costruzione del singolo pezzo.
>
> Procedura:
> 1. Per OGNI componente, decidi la **sequenza di azioni CAD** (`actions[]`) per costruirlo dalle sue
>    viste (NON assumere quante: usa quelle presenti): scegli `op` (sketch_extrude / sketch_revolve /
>    loft / sweep / shell / fillet / chamfer /
>    hole / mirror / pattern …), un `detail` conciso, e le `reference_views` pertinenti. Privilegia
>    l'**esatto per costruzione** (sketch vincolato → extrude/revolve), simmetria via mirror.
> 2. Assegna `build_order` (i pezzi target di un joint vanno costruiti prima dei pezzi che vi si
>    agganciano) e componi `assembly_order` coerente.
> 3. Inferisci/conferma i **joint inter-componente** (`joints[]`) incrociando le `relations` dichiarate
>    col modo in cui i pezzi combaciano nelle viste whole: `from`/`to` (component_id), `relation` (dal
>    vocabolario), `joint_type` (cylindrical/planar/rigid/revolute), `detail`, `from_view_evidence`.
> 4. Ciò che resta ambiguo → `open_questions`.
>
> RITORNA come testo finale = valore SOLO il JSON dell'action-plan (schema dato). Scrivilo anche nel
> file che ti è indicato. Niente altro testo.

## 1. Parsing → PART-MANIFEST (parser = Opus subagent, MAPPING, sola percezione)

Qui il parsing è **più leggero** che in fusion-build: i componenti sono **già identificati**, ognuno
con le sue viste 3/4. Il parser **mappa** ogni componente su una parte del manifest e **conferma/infierisce**
le relazioni inter-componente leggendo le viste whole. Il manifest resta la **single source of truth**
(schema identico a fusion-build) così tutti gli helper condivisi (save-manifest, plan_coverage, gate
`parts_check`) funzionano invariati.

1. Lancia **un** subagent **parser** (Agent, `subagent_type: general-purpose`, `model: opus`, sola
   percezione: `Read` + `Write`, **niente MCP**) col **template parser** sotto.
2. Salva il manifest (porta lo stato `parsing → planning`):
   ```
   uv run python -m src.fusion_build_from_components save-manifest <build_dir> <manifest.json>
   ```
   → `{"manifest_path", "part_ids", "open_questions"}`. **Logga le `open_questions`**.

Schema `manifest.json` (identico a fusion-build; parametri **relativi/proporzionali**):
```json
{
  "parts": [
    {
      "id": "body",
      "primitive_type": "cube|cylinder|sphere|cone|torus|plane|custom",
      "parameters": {"shape": "...", "proportions": "..."},
      "relations": [{"type": "attached_to|on_top_of|inside|concentric_with|flush_with|symmetric_to", "target_id": "lid", "detail": "..."}],
      "details": ["..."],
      "source_views": ["front_3q", "rear_3q"],
      "confidence": "high|medium|low"
    }
  ],
  "open_questions": ["..."]
}
```

### Template prompt per il parser (Opus, mapping + percezione)

> Sei un analista percettivo. NON modelli nulla: produci il **PART-MANIFEST** di un oggetto **già
> scomposto in componenti**. Ogni componente del bundle = una parte del manifest. Il tuo lavoro è
> soprattutto MAPPING (componente→parte) + inferenza delle relazioni inter-componente.
>
> BUNDLE COMPONENTI: {components_source.json}
> ACTION-PLAN (contesto): {action-plan.json}
> VISTE PER COMPONENTE (Read, + lenti): {refs/components/<id>/<view>.png} — usale per dedurre
>   `primitive_type`, `parameters` proporzionali e `details` di OGNI pezzo isolato.
> VISTE WHOLE (Read, + lenti): {refs/<view>.png} — usale per CONFERMARE/INFERIRE le `relations`
>   inter-componente (come i pezzi si toccano/montano).
>
> Procedura:
> 1. Per ogni componente del bundle crea UNA parte: `id` = component_id; `primitive_type` = la
>    primitiva più vicina dedotta dalle SUE viste 3/4; `parameters` proporzionali; `details` geometrici
>    (raccordi, fori, scanalature, interfaccia di accoppiamento); `source_views` = le viste del
>    componente; `confidence`.
> 2. `relations`: parti dalle relazioni dichiarate nel bundle e **verificale/completale** guardando le
>    viste whole (mantieni il vocabolario attached_to/on_top_of/inside/concentric_with/flush_with/symmetric_to).
> 3. Ciò che resta ambiguo → `open_questions`.
>
> RITORNA come testo finale = valore SOLO il JSON del manifest (`{"parts":[...], "open_questions":[...]}`).
> Scrivilo anche nel file indicato. Niente altro testo.

## 2. PIANO (planner = Opus, lo fai TU nel main loop, thinking MEDIO)

Il planner **consuma `action-plan.json` + `manifest.json`** e produce il `plan.json` (schema identico
a fusion-build). Il mapping è **meccanico**:
- ogni gruppo `actions[]` di un componente → uno o più `steps[]`, con `manifest_parts=[component_id]`,
  `fusion_approach` derivato dagli `op`, `reference_views` dalle azioni (PRIMARIE: le viste del
  componente `refs/components/<id>/...`; whole come contesto);
- i `joints[]` dell'action-plan → distribuiti nel campo `joints` dello step che crea il componente
  **dipendente** (quello che si aggancia), applicati secondo `assembly_order`;
- `depends_on` derivato dall'`assembly_order` (un pezzo dipende dai target dei suoi joint).

1. Leggi `action-plan.json`, `manifest.json`, e **guarda le viste** (whole + per-componente).
2. Salva il piano:
   ```
   uv run python -m src.fusion_build_from_components save-plan <build_dir> <plan.json>
   ```
   → `{"plan_path", "uncovered_parts"}`. Se `uncovered_parts` non è vuoto, rivedi il piano (ogni
   componente deve avere almeno uno step che lo cita in `manifest_parts`).

Schema `plan.json` (identico a fusion-build), esempio di step:
```json
{
  "source_session": "<name>",
  "strategy": "Ogni componente = un Component; esatto per costruzione (sketch→extrude/revolve); simmetria via mirror; posizionamento tra componenti SOLO via JOINTS relativi secondo assembly_order; quote in cm",
  "steps": [
    {
      "id": 1, "title": "Corpo (body)", "goal": "Volume principale del corpo",
      "target": "body", "manifest_parts": ["body"],
      "fusion_approach": "Sketch profilo metà-sezione su XZ → REVOLVE attorno a Z; shell ~2mm; fillet bordo",
      "features": ["sketch", "revolve", "shell", "fillet"],
      "joints": [],
      "reference_views": ["components/body/front_3q", "components/body/rear_3q", "front_3q"],
      "depends_on": [],
      "acceptance": "silhouette del corpo combacia con refs/components/body/*; corpo di rivoluzione attorno a Z"
    },
    {
      "id": 4, "title": "Coperchio (lid) + joint", "goal": "Coperchio e suo aggancio al corpo",
      "target": "lid", "manifest_parts": ["lid"],
      "fusion_approach": "Sketch+revolve del coperchio come Component separato; poi add_joint cylindrical al collo del corpo",
      "features": ["sketch", "revolve", "joint"],
      "joints": [{"target_part": "body", "joint_type": "cylindrical", "notes": "flangia coperchio concentrica al collo"}],
      "reference_views": ["components/lid/front_3q", "front_3q"],
      "depends_on": [1],
      "acceptance": "coperchio presente, concentrico al collo, poggiato sul bordo"
    }
  ]
}
```
Linee guida: copri il manifest; **una parte = un Component**; esatto per costruzione; simmetria via
**Mirror**; **posizionamento via JOINTS** (mai trasformazioni assolute); dettagli come feature native;
ordina gli step per `assembly_order` (target dei joint prima dei dipendenti).

## 3. ESECUZIONE incrementale (executor = Sonnet, IN SEQUENZA)

**Sequenza rigorosa** (un documento condiviso mutabile): lancia **un** subagent per volta e **aspetta**.

Per ogni step in ordine:

1. Path artefatti: `uv run python -m src.fusion_build_from_components paths <build_dir> <step_id>`
   → `{"f3d", "stl", "png", "json"}` (assoluti).
2. Lancia **un** subagent executor (Agent, `subagent_type: general-purpose`,
   tool `mcp__fusion__*` + `mcp__fusion__execute_code` *(solo render/bbox)* + Write, `model: sonnet`)
   col **template executor** sotto, passando: piano completo, step corrente, le parti del manifest in
   `step.manifest_parts`, **le viste del COMPONENTE** (`refs/components/<id>/<view>.png`, quelle in
   `metadata['component_views']`) come riferimento PRIMARIO di forma + le viste whole come contesto, e
   i path artefatti.
3. Registra: `uv run python -m src.fusion_build_from_components record <build_dir> <record.json>`.
4. Step successivo.

**Best-effort**: se uno step non converge entro **3 tentativi**, salva il miglior tentativo, marca
`status: "failed"` con note, e **prosegui**. A ogni retry: `undo`, oppure ri-quota
(`set_parameter`/`add_dimension`), oppure riapri l'ultimo `.f3d` buono; poi ri-verifica via render.

### Mappa relazioni → JOINTS / vincoli Fusion (identica a fusion-build)

Realizza ogni relazione come **joint relativo** (`add_joint`) che referenzia geometria stabile
(facce/spigoli/punti nominati), NON come posizione assoluta. Crea ogni componente con
`create_component`, poi àncoralo al target secondo l'`assembly_order`:

| relation                    | realizzazione faust-machines                                          |
|-----------------------------|----------------------------------------------------------------------|
| `concentric_with` / coaxial | `add_joint` tipo **cylindrical**/**rigid** su assi/spigoli concentrici |
| `on_top_of` / `flush_with`  | `add_joint` tipo **planar** tra le facce a contatto (offset 0 = a filo) |
| `attached_to`               | `add_joint` tipo **rigid** su origini/geometrie coincidenti          |
| `inside`                    | **concentric** + **planar** con offset (due joint)                   |
| `symmetric_to`              | **`mirror`** (feature) sul piano di simmetria — non un joint          |

**VIETATO** `move_body`/trasformazioni assolute per piazzare una parte (vanifica l'assemblaggio
relativo); ammesso solo come fallback ultimo documentato nelle `notes`.

### Render di verifica (pose ESATTE — identico a fusion-build)

Convenzione di build: **up = +Z**. Render = unico uso di `execute_code`: per ogni vista imposta
`app.activeViewport.camera`, `viewport.fit()`, poi `saveAsImageFile`. Le pose canoniche sono in
`src/view_schema.py` (`ViewSpec.fusion_camera`), risolte dallo schema della sessione, e in
`src/fusion_build.py` `VIEW_CAMERA` — **le stesse** negli step e nel gate. Le due viste 3/4 correnti:

| view      | view_dir      | up        | ortho |
|-----------|---------------|-----------|-------|
| `front_3q`| `[1, 1, -1]`  | `[0,0,1]` | False |
| `rear_3q` | `[-1, -1, -1]`| `[0,0,1]` | False |

Itera sulle viste reali della sessione (NON assumere il numero/i nomi). Per le firme `adsk` fai
lookup nella doc locale (§"Documentazione API locale").

### Template prompt per l'executor (Sonnet)

> Sei un executor di modellazione **PARAMETRICA** in Fusion 360 via MCP. Applichi **un solo step** di
> un piano, costruendo il componente indicato come **Component** e (se previsto) agganciandolo agli
> altri con **JOINTS**. Orchestri una **sequenza di tool granulari** `mcp__fusion__*` (una operazione
> per chiamata). Usi `execute_code` SOLO per il render di verifica. Esatto per costruzione (sketch
> vincolati → extrude/revolve, feature native), MAI mesh/editing per-vertice. Focus = **geometria e
> simmetria**; materiali/colori sono secondari. **Le firme dei tool non sono documentate**: fidati
> dello schema del server e correggi leggendo il messaggio d'errore.
>
> PIANO COMPLETO (contesto): {plan.json}
> STEP DA APPLICARE: {step}
> PARTI DEL MANIFEST DA REALIZZARE (SSOT): {parts di step.manifest_parts}
> VISTE DEL COMPONENTE (riferimento PRIMARIO di forma, Read): {refs/components/<id>/<view>.png per ogni
>   view della sessione — correnti: front_3q.png, rear_3q.png}
> VISTE WHOLE (contesto assemblaggio, Read): {refs/<view>.png — correnti: refs/front_3q.png, …}
> PATH ARTEFATTI (assoluti, usali esattamente): f3d={…} stl={…} png={…} json={…}
>
> VINCOLI HARD: unità cm (1 mm = 0.1 cm); una operazione per chiamata; budget ~30s (spezza se pesante);
> annulla con `undo` o riapri l'ultimo `.f3d`; `execute_code` SOLO per render; doc API locale
> (`uv run python fusion-docs/search.py "<keyword adsk>" -k 5` → Read del md) prima di indovinare `adsk.*`.
>
> Procedura (sequenza di chiamate granulari):
> 1. `get_scene_info`; se serve `set_design_type` → parametrico.
> 2. **Crea il componente come Component proprio** (`create_component`), poi sketch vincolato+quotato
>    (in cm) → `extrude`/`revolve`; `mirror` per simmetria; `shell`/`fillet`/`create_hole` per i dettagli.
>    Costruiscilo guardando le **viste del componente** (la sua forma isolata).
> 3. Se `step.joints` non è vuoto: **posiziona con `add_joint`** (mappa relazioni→joint) referenziando
>    geometria stabile; verifica nelle **viste whole** che combaci l'assemblaggio. **NON** `move_body`.
> 4. Verifica (via `execute_code`): rendi le `reference_views` con le pose canoniche (`fusion_camera`
>    dello schema / `VIEW_CAMERA`; front_3q=view_dir[1,1,-1], rear_3q=view_dir[-1,-1,-1], up=+Z),
>    `viewport.fit()`, `saveAsImageFile`; apri i png con Read e giudica forma/simmetria del componente
>    (vs le sue viste) e, se ha joint, dell'assemblaggio (vs whole) rispetto ad `acceptance`.
> 5. RIPETI fino a max 3 tentativi. A ogni retry: `undo`/ri-quota/riapri `.f3d`; poi ri-verifica.
> 6. Su esito accettabile: `export_f3d` → `<f3d>`, `export_stl` → `<stl>`, salva il render in `<png>`,
>    scrivi `<json>`.
> 7. RITORNA (testo finale = valore) SOLO il JSON record:
>    `{"id","title","status":"done|failed","attempts":N,"f3d":"steps/step_NN.f3d","stl":"steps/step_NN.stl",
>      "png":"steps/step_NN.png","components":[..nomi..],"joints":[..tipi/coppie..],"notes":"..."}`
>    (path RELATIVI alla build dir).

## 4. Chiusura

Dopo l'ultimo step, esporta i finali con i tool nativi:
```
uv run python -m src.fusion_build_from_components final-paths <build_dir>
```
poi `export_f3d` → `final.f3d`, `export_step` → `final.step`, `export_stl` → `final.stl` (path
assoluti). **NON** riassumere ancora: prosegui col gate (§5). Lo stato `done` lo imposta il gate.

## 5. Gate di convergenza (whole + per-componente → stop o re-itera)

Struttura e regola di decisione **identiche a fusion-build** (soglia `ACCEPT_THRESHOLD=85`, cap
`MAX_ITERATIONS=2`, calcolate dal helper, NON dall'LLM). La differenza: il giudizio visivo confronta
il modello assemblato **sia col design whole-object sia con le immagini per-componente**, e il
`parts_check` verifica presenza + correttezza dei **joint inter-componente**.

1. Path artefatti del gate:
   ```
   uv run python -m src.fusion_build_from_components eval-paths <build_dir>
   ```
   → `{"iteration": N, "views": {<view> → png per ogni vista whole della sessione — correnti front_3q,
   rear_3q}, "verdict": "...abs..."}`.
2. **Metriche oggettive** (via `execute_code`, in cm → ×10 = mm): bbox vs `target_mm`
   (`proportion_error_pct`), scarto di simmetria per asse (`symmetry_dev_mm`), `watertight` (export STL
   + trimesh e/o `body.isSolid`). Identiche a fusion-build.
3. **Render delle viste whole** del modello assemblato (tutte quelle della sessione, pose canoniche
   `fusion_camera`/`VIEW_CAMERA`; correnti front_3q/rear_3q) in `views[...]`, poi genera le lenti dei
   render:
   ```
   uv run python -m src.fusion_build_from_components make-lenses <build_dir> --iter N
   ```
   → `eval/iter_NN/lenses/<view>__{edges,silhouette}.png`. Le ref whole hanno le lenti in `refs/lenses/`.
4. **Giudizio visivo + copertura** — lancia **un** subagent giudice (Sonnet) col **template giudice**
   sotto, passandogli `manifest.json`, `components_source.json`, i render whole ↔ `refs/<view>.png` (con
   lenti) **e** le viste per-componente `refs/components/<id>/...`: per ogni vista whole → `score` 0–100;
   per ogni componente → presenza/fedeltà vs le sue viste + correttezza dei joint → `parts_check` +
   `actionable_gaps`.
5. Componi `verdict.json` e registra:
   ```
   uv run python -m src.fusion_build_from_components record-eval <build_dir> <verdict.json>
   ```
   → raccomandazione deterministica `{"recommendation":"stop|reiterate", "reason", "overall_score", "iteration"}`.
   Imposta `status=done`.
6. **Riporta all'utente e FERMATI**: punteggi per-vista whole, **check per-componente** (presenti /
   mancanti / joint ok), metriche, `actionable_gaps`, raccomandazione + motivazione. Se `reiterate`,
   di' che può rilanciare dal risultato (§6) e quali gap verrebbero corretti.

Schema `verdict.json` (come fusion-build; `parts_check` per-componente, con note sui joint):
```json
{
  "iteration": 1,
  "metrics": {"bbox_mm": [89.0,130.0,3.03], "target_mm": [89,130,3],
              "proportion_error_pct": 1.2, "symmetry_dev_mm": {"x":0.0,"z":0.4}, "watertight": true},
  "parts_check": [{"id":"body","present":true,"relations_ok":true,"notes":"ok"},
                  {"id":"lid","present":true,"relations_ok":false,"notes":"presente ma joint non concentrico"}],
  "views": [{"view":"front_3q","score":92,"notes":"..."}, {"view":"rear_3q","score":88,"notes":"..."}],
  "overall_score": 88,
  "actionable_gaps": ["coperchio non concentrico al collo (joint cylindrical da rivedere)"]
}
```

### Template prompt per il giudice (Sonnet)

> Sei un giudice di fedeltà geometrica. Confronti il MODELLO 3D assemblato, renderizzato, con (a) le
> VISTE WHOLE di riferimento e (b) le VISTE PER COMPONENTE, e col PART-MANIFEST. **Solo forma**:
> silhouette, proporzioni, simmetria, presenza/posizione delle parti e correttezza degli accoppiamenti.
> IGNORA colori, materiali, illuminazione, sfondo.
>
> Per ogni vista whole ti do il render del modello, la `ref` whole e le LENTI `edges`/`silhouette` di
> entrambi → assegna `score` 0–100 + note. Poi, usando il MANIFEST e le viste per-componente
> (`refs/components/<id>/...`), per ogni componente verifica: è **presente**? la sua forma combacia con
> le sue viste? i suoi **joint** (relazioni inter-componente) sono rispettati nell'assemblaggio whole?
> → `parts_check` (con `relations_ok` che copre la correttezza del joint). Infine `actionable_gaps`:
> differenze GEOMETRICHE concrete e correggibili (es. "coperchio non concentrico", "manca il manico",
> "beccuccio troppo corto").
>
> VISTE WHOLE (render ↔ ref, con lenti, una per ogni vista della sessione — correnti front_3q, rear_3q):
>   {front_3q: <render>+<lenses> ↔ <refs/front_3q.png>+<refs/lenses/...>, …}
> VISTE PER COMPONENTE (ref isolate): {refs/components/<id>/<view>.png}
> MANIFEST: {manifest.json}  BUNDLE: {components_source.json}
> METRICHE (contesto): {bbox_mm, target_mm, symmetry_dev_mm, watertight}
>
> RITORNA come testo finale SOLO il JSON:
> `{"views":[{"view":"front_3q","score":N,"notes":"..."}, ...],
>   "parts_check":[{"id":"...","present":bool,"relations_ok":bool,"notes":"..."}, ...],
>   "overall_score":N, "actionable_gaps":["..."]}`
> dove `overall_score` penalizza forte errori di proporzione/simmetria, parti mancanti e joint errati.

## 6. Re-iterazione (su richiesta utente — parte DAL risultato)

Solo se l'utente lo chiede dopo `reiterate`:
```
uv run python -m src.fusion_build_from_components reinit-from-result <prev_build_dir>
```
→ nuova build con `seed.f3d` (= `final.f3d` precedente), **tutti i `refs/` invariati** (whole +
componenti), lo **stesso `manifest.json`** e `action-plan.json`, `iteration` incrementata,
`planning_hints` = `actionable_gaps` dell'ultimo verdict.

Poi pipeline normale (§2–§5), saltando §0.5/§1 (action-plan e manifest già pronti):
- Piano di **rifinitura**: lo step 1 NON crea geometria nuova ma **carica `seed.f3d`** come documento
  attivo (via `execute_code` `app.documents.open(...)`; firma con lookup `documents open`). Gli step
  successivi mirano ai `planning_hints` (gap da chiudere: forma del componente carente o joint da
  correggere). Spesso basta **modificare le quote** o **rifare il joint** invece di ricostruire.
- Salta la pulizia del preflight (il seed È il documento di partenza).

A fine pipeline gira un nuovo gate (§5); le iterazioni restano tracciate in metadata.

## Documentazione API locale — lookup lessicale (usalo PRIMA di indovinare l'API)

Identico a fusion-build: knowledge base offline in `run/fusion-docs/` (markdown + indice FTS5/BM25).
Consultala prima di scrivere `adsk.*` in `execute_code` (camera/`saveAsImageFile`, `boundingBox`,
`isSolid`, `documents open`) e quando un tool granulare erra per firma sconosciuta:
```
uv run python fusion-docs/search.py "<identificatore o keyword>" -k 5
```
Cerca per **identificatore `adsk`** (es. `saveAsImageFile`, `boundingBox`, `Camera eye`, `Joints add`,
`Documents open`), NON per il nome snake_case del tool. Poi `Read` del path md assoluto stampato.
(Precondizione one-shot sul Mac per costruire l'indice: vedi `run/fusion-docs/README.md`.)

## Toolbox parametrico Fusion (preferenze — identico a fusion-build)

- **Sketch + vincoli/quote** → `create_sketch`, `draw_*`/`create_polygon`, `add_constraint`,
  `add_dimension`, `offset_curve`, `trim_curve`, `project_geometry`. Fully-constrained.
- **Volume** → `extrude`, `revolve` (solidi di rivoluzione), `loft`, `sweep` (+ primitive dirette solo
  per casi banali).
- **Modifica** → `shell`, `fillet`, `chamfer`, `create_hole`, `create_thread`, `draft_faces`,
  `split_body`, `scale_body`, `boolean_operation`.
- **Pattern/simmetria** → `mirror` (default per oggetti simmetrici), `circular_pattern`,
  `rectangular_pattern`.
- **Assieme** → ogni parte = un **Component** (`create_component`); posiziona con `add_joint`
  (rigid/cylindrical/planar/revolute), `create_as_built_joint`, `create_rigid_group`; piani/assi di
  costruzione. **Le relazioni inter-componente → joints** (§3).
- **Parametri** → `get_parameters`, `create_parameter`, `set_parameter`.
- **Export** → `export_f3d`, `export_step`, `export_stl`.
- **VIETATO** (salvo banalità): mesh / editing per-vertice; `move_body`/trasformazioni assolute per
  posizionare le parti (usa joints).

## Gotcha / principi (applicarli SEMPRE)

Condivide il **knowledge base versionato** `run/fusion_build_lessons.md` con fusion-build (stesso
backend CAD). Quando emerge un nuovo problema/soluzione durante una run, **aggiungilo lì e committa**.
Principi specifici di questa variante, oltre a quelli di fusion-build:
- **Un componente del bundle = un Component Fusion = uno (o più) step.** La scomposizione è già fatta:
  non re-inventare i pezzi, costruisci QUELLI.
- **Le viste del componente sono il riferimento PRIMARIO di forma**; le viste whole servono per i
  **joint** e per l'aspetto d'insieme. Non inventare feature non presenti nelle viste del componente.
- **Ordine = assembly_order.** Costruisci prima i pezzi target dei joint, poi i dipendenti, così
  l'`add_joint` referenzia geometria già esistente e stabile.
- **Joints, non trasformazioni** (come fusion-build): l'intento relativo tra componenti deve
  sopravvivere alle modifiche parametriche.
- **Esatto per costruzione, simmetria via Mirror, unità cm, una operazione per chiamata, undo/riapri
  `.f3d`, render+bbox solo via `execute_code`, firme tool non documentate → schema server + messaggio
  d'errore + doc locale** (tutti identici a fusion-build).

## Note operative

- Esecuzione **dentro Claude Code**, niente app web. La SCOMPOSIZIONE in componenti (`breakdown`)
  avviene PRIMA, lato web/CLI cross-platform; questa skill consuma il bundle prodotto.
- **Fusion gira solo su macOS/Windows.** Setup add-in + MCP: vedi `run/README.md`.
- **Due contesti (DEV vs RUN)**: la skill vive SOLO in `run/.claude/skills/`; i run si lanciano da
  `run/`. Artefatti in `outputs/fusion/<ts-slug>/` (gitignored).
- I path passati a Fusion devono essere **assoluti** (stessa macchina): usali come tornano da `paths`/
  `final-paths`.
- Lo storico vive in `outputs/fusion/<ts-slug>/steps/` — uno `.f3d` apribile per step.
