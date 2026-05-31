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

## Note

- Modifiche a MCP / skill / settings richiedono un **riavvio** di Claude dalla cwd giusta per
  avere effetto.
- Rischio aperto da verificare allo smoke test: Claude deve trattare `run/` come **project root
  a sé** (carica `run/.claude` + `run/.mcp.json`, non quelli di repo root). Se non risultasse
  isolato, il fallback è rendere `run/` una dir *sorella* fuori dal repo con la skill in symlink.
