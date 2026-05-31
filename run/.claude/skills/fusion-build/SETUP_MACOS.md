# fusion-build — setup macOS + checklist di verifica

Fusion 360 gira **solo su macOS/Windows**. La skill `fusion-build` è stata scritta e rivista
**staticamente su Linux**: nessun passo Fusion è stato eseguito né verificato lì. Questo
documento è la guida per **abilitare l'add-in + il server MCP sul Mac** e la **checklist da
spuntare tu** prima di considerare la pipeline funzionante. *Finché non spunti la checklist,
considera "non verificato" tutto ciò che tocca Fusion.*

## 1. Scegli e installa il server MCP Fusion

La skill usa **un solo** tool MCP: un esecutore di **codice Python `adsk.*` arbitrario** sulla
**coda main-thread** dell'add-in. Il render di verifica è `app.activeViewport.saveAsImageFile(...)`
DENTRO lo script — non serve un tool screenshot separato. Server candidati (verifica i README
correnti: la documentazione è sottile e cambia):

- **faust-machines/fusion360-mcp-server** — architettura socket-bridge stile BlenderMCP
  (add-in dentro Fusion + server MCP esterno), avvio tipo `uvx`. **Preferito SE** espone un
  tool di esecuzione codice (`adsk.*`) e `saveAsImageFile`. Parallelo a `uvx blender-mcp`.
- **AuraFriday Fusion-360-MCP-Server** — esecuzione Python arbitraria con work-queue main-thread
  esplicita (maturo, ~app store). Fallback solido se il primo non espone l'esecuzione codice.
- **cykonova/fusion-mcp** — minimale, sconsigliato (documentazione scarsa, tool ridotti).

> ⚠️ **Da confermare sul Mac (non verificabile su Linux):** il **nome esatto del tool** di
> esecuzione codice e che `saveAsImageFile` funzioni. La skill lo chiama
> `mcp__fusion__execute_fusion_code`. Se il tuo server lo espone con altro nome, **cambia QUESTO
> unico riferimento** in `SKILL.md` (compaiono: il box "Tool MCP di esecuzione", §0 preflight,
> §3 executor, §4 chiusura, §5 gate). L'alias del server in `run/.mcp.json` deve restare
> `fusion` così il prefisso tool è `mcp__fusion__`.

### Add-in dentro Fusion
1. Copia l'add-in del server scelto in:
   `~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns/`
   (è la cartella Add-Ins di Fusion su macOS).
2. In Fusion: **Utilities → ADD-INS → Scripts and Add-Ins** (oppure `Shift+S`), scheda
   **Add-Ins**, seleziona l'add-in → **Run** (e spunta *Run on Startup* se vuoi).
3. Verifica nella **Text Commands** di Fusion che il server sia in ascolto (es. log "listening").

### Config MCP nel contesto RUN
Il repo ha già un **placeholder** (scritto su Linux, innocuo lì perché non si connette):
- `run/.mcp.json` → server `fusion` (di default `uvx fusion360-mcp-server`). Se usi AuraFriday o
  un comando diverso, **aggiorna `command`/`args`** al launcher del tuo server.
- `run/.claude/settings.json` → `enabledMcpjsonServers` include `fusion`, e `permissions.allow`
  include `mcp__fusion__execute_fusion_code` (allinealo al nome reale del tool).

Dopo ogni modifica a MCP/skill/settings, **riavvia Claude dalla cwd `run/`** perché abbia effetto.

## 2. Checklist di verifica (da eseguire TU sul Mac)

- [ ] **Add-in attivo**: l'add-in del server MCP è copiato nella cartella Add-Ins ed è in **Run**;
      la Text Commands mostra il server in ascolto.
- [ ] **Server raggiungibile + nome tool**: da `run/`, Claude vede il tool MCP del server
      `fusion`. Conferma il **nome reale** del tool di esecuzione codice e, se ≠
      `mcp__fusion__execute_fusion_code`, aggiorna il riferimento in `SKILL.md` (vedi sopra).
- [ ] **Preflight**: uno script minimo (`adsk.core.Application.get(); ... rootComponent.name`)
      ritorna senza timeout. Se fallisce → add-in non in esecuzione / server non connesso.
- [ ] **Smoke test esecuzione + render** (conferma il cuore del design):
      uno script che (a) crea uno sketch rettangolo, (b) lo estrude (in **cm**), (c) imposta la
      camera FRONT (ortografica, up=+Z, sguardo +Y), `viewport.fit()`, (d)
      `app.activeViewport.saveAsImageFile("/tmp/fusion_smoke.png", 800, 600)`.
      **Atteso**: `/tmp/fusion_smoke.png` esiste e mostra un parallelepipedo. Conferma
      esecuzione codice **+** render posato end-to-end.
- [ ] **Vincoli HARD reggono**: una sola operazione per chiamata (niente batch → niente crash);
      gli script restano sotto ~30s; per "annullare" riapri un `.f3d` salvato (no undo).
- [ ] **Joints vivi**: uno script che crea due component e un **Joint** (es. concentric) ritorna
      ok e il joint compare nel browser/timeline (verifica che NON sia una trasformazione baked).
- [ ] **Export**: salvataggio `.f3d` + export **STEP** e **STL** del rootComponent vanno a buon
      fine sui path assoluti.
- [ ] **Run end-to-end** su un oggetto semplice (es. una tazza/mug):
      `fusion-build <oggetti-session>` e verifica:
      - manifest prodotto (parser), piano che copre tutte le parti;
      - parti costruite come **Component** via **sketch→extrude/revolve** (non mesh);
      - relazioni realizzate come **joints/vincoli** (ispeziona browser/timeline — *non*
        trasformazioni assolute);
      - 4 render posati che combaciano con i `refs/` (pose front/side/back/iso coerenti);
      - gate: verdetto per-vista + metriche (bbox in **mm**, simmetria, watertight) +
        `parts_check` + raccomandazione stop/reiterate stampata.
- [ ] **Lezioni**: ogni nuovo gotcha Fusion emerso → aggiungilo a `run/fusion_build_lessons.md`
      e committa (la fonte canonica è quel file versionato, non l'auto-memory).

## 3. Cosa NON è stato verificato su Linux (rischi noti)

1. **Joint da relazioni qualitative** (rischio più alto): le `relations` del manifest sono
   vaghe ("centrato"); l'executor deve scegliere geometria di joint valida su facce/spigoli
   appena creati. Mitigazione in skill: JointOrigin su geometria nominata + tabella
   relazioni→joint + fallback documentato. Da validare sul campo.
2. **Nome tool di esecuzione + `saveAsImageFile`**: l'intero design dipende da questo — è il
   **primo** controllo (vedi smoke test).
3. **Convenzione assi dei render vs i ref**: deve essere internamente coerente (orientamento di
   build ↔ camere). Ritocca le pose `VIEW_CAMERA` sul Mac finché i render somigliano ai ref.
4. **Semantica di "annullamento"** (riapri `.f3d` vs rollback timeline) col server scelto.
5. **Timeout 30s con ricomputo dell'intera timeline**: può servire una granularità di step più
   fine che in Blender.
6. **watertight/simmetria** su B-rep: tenuti via trimesh-su-STL per parità di calcolo col gate
   di blender-build (più `body.isSolid` come indicatore B-rep).

## 4. Verifica fattibile su Linux (già possibile, niente Fusion)

L'helper deterministico è puro file-IO e gira ovunque:
```
uv run python -m src.fusion_build init <oggetti-session>
uv run python -m src.fusion_build save-manifest <build_dir> <manifest.json>
uv run python -m src.fusion_build save-plan <build_dir> <plan.json>
uv run python -m src.fusion_build paths <build_dir> 1
uv run python -m src.fusion_build eval-paths <build_dir>
uv run python -m src.fusion_build record-eval <build_dir> <verdict.json>
```
Conferma che la struttura della build (`outputs/fusion/<ts-slug>/`), gli stati, la copertura
piano↔manifest e la regola del gate (soglia 85 / cap 2) si comportino come in blender-build.
**Questo non sostituisce** la verifica Fusion sul Mac: valida solo il core CAD-agnostico.
