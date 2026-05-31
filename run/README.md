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
sono eseguibili né verificabili su Linux**. Setup add-in + MCP e la **checklist di verifica lato
Mac** sono in `run/.claude/skills/fusion-build/SETUP_MACOS.md`. Il placeholder MCP nel repo
(`uvx fusion360-mcp-server`) e il nome tool `mcp__fusion__execute_fusion_code` vanno **confermati
e, se serve, corretti sul Mac** in base al server scelto. Le lezioni Fusion stanno in
`run/fusion_build_lessons.md`.

## Note

- Modifiche a MCP / skill / settings richiedono un **riavvio** di Claude dalla cwd giusta per
  avere effetto.
- Rischio aperto da verificare allo smoke test: Claude deve trattare `run/` come **project root
  a sé** (carica `run/.claude` + `run/.mcp.json`, non quelli di repo root). Se non risultasse
  isolato, il fallback è rendere `run/` una dir *sorella* fuori dal repo con la skill in symlink.
