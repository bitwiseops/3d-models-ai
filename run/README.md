# `run/` — contesto di esecuzione della pipeline blender-build

Questo progetto ha **due contesti Claude Code distinti**, per separare lo *sviluppo* del codice
dalla sua *esecuzione* live su Blender. Sono due cwd diverse da cui lanci `claude`, ognuna con
i propri `.claude/` e `.mcp.json`.

## DEV — repo root (`cd <repo> && claude`)

Per **sviluppare**: modificare `src/`, la skill, i tool, i test.

- **Nessun MCP CAD**: `<repo>/.mcp.json` ha `mcpServers: {}` — niente Blender qui.
- La skill `blender-build` **non** è visibile da qui (vive solo in `run/.claude/skills/`).
- Niente di pericoloso da fare a mano sulla scena: questo contesto serve a scrivere codice.

## RUN — `run/` (`cd <repo>/run && claude`)

Per **eseguire** la pipeline contro un Blender aperto e connesso (porta 9876).

- **MCP `blender`** caricato da `run/.mcp.json` (`uvx blender-mcp`), abilitato in
  `run/.claude/settings.json` (`enabledMcpjsonServers: ["blender"]`).
- La skill `blender-build` è **qui** (`run/.claude/skills/blender-build/SKILL.md`) ed è l'unico
  posto in cui vive.
- Il **codice sorgente è in sola lettura**: `run/.claude/settings.json` `deny`-a Write/Edit su
  `src/`, `tools/`, `pyproject.toml` e sulla skill stessa. Durante un run non si tocca il codice
  per sbaglio — se serve modificarlo, si passa al contesto DEV.
- Permessi pre-concessi per scorrere senza prompt: `mcp__blender__execute_blender_code`,
  `mcp__blender__get_viewport_screenshot`, `Bash(uv run *)`.

## Artefatti

Gli output dei run **non** stanno sotto `run/` né sotto `src/`: vanno in `outputs/blender/<ts-slug>/`
(gitignored) come per le altre pipeline. L'helper risolve `BLENDER_OUTPUTS_ROOT =
config.OUTPUTS_ROOT/"blender"` indipendentemente dalla cwd, quindi funziona identico da `run/`.

## Sessione sorgente

La skill prende come argomento il path della sessione Oggetti sorgente. Lanciando da `run/`,
passalo **assoluto** oppure relativo alla cwd `run/` (es. `../outputs/<session>`).

## Skill sorella: `fusion-build` (Fusion 360, solo macOS/Windows)

Oltre a `blender-build`, in `run/.claude/skills/` vive `fusion-build`: una **copia-e-adatta**
della stessa pipeline con backend CAD = **Fusion 360** (geometria parametrica B-rep:
sketch→extrude/revolve, relazioni realizzate come **joints**, non trasformazioni). È una skill
**indipendente** — `blender-build` resta invariato. Il suo helper deterministico è
`src/fusion_build.py` (artefatti in `outputs/fusion/<ts-slug>/`) e usa il server MCP `fusion`
(in `run/.mcp.json`, abilitato in `run/.claude/settings.json`).

⚠️ Fusion gira solo su **macOS/Windows**: l'add-in, il bridge MCP e qualunque run reale **non
sono eseguibili né verificabili su Linux**.

**Setup MCP (Mac):** il server è **faust-machines/fusion360-mcp-server** (stdio,
`uvx fusion360-mcp-server --mode socket`, bridge add-in su TCP **9876**).
1. Installa l'add-in (`./scripts/install-addon.sh` del repo del server, o copia manuale in
   `~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns/Fusion360MCP`).
2. In Fusion: **Shift+S → Add-Ins → Fusion360MCP → Run**; il log `~/fusion360mcp.log` deve mostrare
   `"[MCP] Server listening on localhost:9876"`.
3. `run/.mcp.json` ha già `fusion` stdio e `settings.json` i permessi; **riavvia Claude da `run/`**.

La costruzione usa i **tool CAD granulari** (`mcp__fusion__create_sketch`, `extrude`, `revolve`,
`add_joint`, `export_*`…); `mcp__fusion__execute_code` serve **solo** per render viste e lettura
bbox (il server non ha tool di screenshot/bbox). ⚠️ Le firme dei tool **non sono documentate**:
introspezionale sul Mac (lista tool + schema) e annota in `run/fusion_build_lessons.md`. Attenzione
al **conflitto porta 9876 con blender-mcp** (non tenere i due add-in attivi insieme). Le lezioni
Fusion stanno in `run/fusion_build_lessons.md`.

### Variante: `fusion-build-from-transcript`

Sperimentale: stesso backend di costruzione di `fusion-build`, ma l'input è il **transcript di una
video-guida** (file di testo) invece delle 4 viste. Pre-elabora il transcript grezzo in un
**action-log** (split in chunk → fold sequenziale con un agente Sonnet per chunk che estende il log →
review/consolidamento Opus → `transcript/action_log.json`), poi parser/planner/executor come
fusion-build. Costruzione **esatta** seguendo le quote dette nel video. Verifica **semantica**: il
giudice confronta i render con `expected_summary` (descrizione attesa dal transcript), **senza
immagini di riferimento**. Helper: subcomandi `init-transcript` / `chunk-transcript` /
`save-action-log` in `src/fusion_build.py`. Gli stadi di testo (pre-elaborazione → piano) girano
**senza Fusion**; l'MCP serve solo dall'esecuzione in poi. Stessa precondizione MCP di `fusion-build`.

## Note

- Modifiche a MCP / skill / settings richiedono un **riavvio** di Claude dalla cwd giusta per
  avere effetto.
- Rischio aperto da verificare allo smoke test: Claude deve trattare `run/` come **project root
  a sé** (carica `run/.claude` + `run/.mcp.json`, non quelli di repo root). Se non risultasse
  isolato, il fallback è rendere `run/` una dir *sorella* fuori dal repo con la skill in symlink.
