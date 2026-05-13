# AGENTS.md

Documentazione per agenti AI che lavorano su questo progetto. Linguaggio: italiano (in linea con commenti/log del codice).

## Cos'è

Pipeline CLI in Python che genera modelli 3D **print-ready** partendo da un'idea testuale:

```
idea testuale
  → Claude (prompt_refiner): raffina prompt + iterazione con feedback utente
  → Meshy text-to-image / image-to-image: genera immagine front+back
  → Meshy multi-image-to-3d: genera mesh 3D
  → mesh_repair: cleanup + decimazione + base piatta + manifold-garantito
  → outputs/.../3d/model_printable.{stl,3mf} pronto per Bambu/Prusa/Orca
```

Il punto chiave: **l'output di Meshy non è print-ready**. Ha boundary edges, non-manifold edges intrinseci (~0.5-1%), winding incoerente, piccoli floater dai join front/back. Bambu Studio non ripara in-loco questi problemi: ti rimanda a `formware.co/onlinestlrepair`. La pipeline `mesh_repair.py` esiste apposta per evitare questo round-trip esterno.

## Setup

Gestione dipendenze via [`uv`](https://docs.astral.sh/uv/). Python pinned a 3.11 (`.python-version`).

```bash
# prima volta: uv crea .venv + installa tutte le deps dal lock
uv sync

# variabili d'ambiente
cp .env.example .env   # poi compila ANTHROPIC_API_KEY e MESHY_API_KEY

# CLI interattiva
uv run models3d run "idea di partenza"

# Web UI (FastAPI + HTMX, mobile-first)
uv run models3d-web
# default: 0.0.0.0:8000 — esponi via Tailscale/Cloudflare Tunnel per accesso mobile remoto
```

Aggiunta dipendenze: `uv add <pkg>` (runtime) o `uv add --group dev <pkg>` (dev). `pyproject.toml` + `uv.lock` sono la fonte di verità — niente `pip install` manuale.

Dipendenze esterne non-pip:
- **Blender** (opzionale ma molto consigliato). Cercato in `/Applications/Blender.app/Contents/MacOS/Blender` o sul `$PATH`. Serve per i pass headless `print3d_clean_non_manifold` (richiede l'estensione "3D-Print Toolbox" installata in Blender, una tantum) e per il **Voxel Remesh** finale che garantisce manifold/watertight.
- Senza Blender la pipeline gira ugualmente ma può produrre mesh che Bambu rifiuta.

## Web UI (FastAPI + HTMX, mobile-first)

Avvio: `uv run models3d-web` (default `0.0.0.0:8000`). Single-user, no auth — pensata per accesso da remoto via tunnel (Tailscale, Cloudflare Tunnel, ngrok). Niente Node/JS toolchain: Tailwind via CDN, HTMX + estensione SSE, `<model-viewer>` di Google per il preview 3D inline.

Variabili d'ambiente specifiche:
- `MODELS3D_HOST` (default `0.0.0.0`), `MODELS3D_PORT` (default `8000`), `MODELS3D_RELOAD=1` per auto-reload in dev.
- `MODELS3D_MAX_WORKERS` (default `4`): worker thread del `ThreadPoolExecutor` per i job background.

Architettura:
- `src/web/app.py`: factory FastAPI + lifespan che istanzia `ThreadPoolExecutor` + `JobManager` shared via `app.state`.
- `src/web/jobs.py`: `JobManager` registry in-memory dei task background. Ogni job ha `progress: int`, `log: list[str]`, `status: pending|running|done|failed`. Iniezione automatica di `on_progress` e `on_step` callback nelle funzioni target via introspection (`inspect.signature`).
- `src/web/routes/`: sessions, iterations, events (SSE), files (serve session dir).
- `src/web/templates/`: Jinja2 mobile-first.

Endpoint principali:
- `GET /` — lista sessioni (con auto-refresh HTMX ogni 5s).
- `POST /sessions` — multipart con `idea` + `references[]`. Crea la session dir **sincronamente** (~10ms) e enqueue il job di image-gen, poi redirect a `/sessions/{id}`.
- `GET /sessions/{id}` — pagina dettaglio con iterazioni, form feedback, bottone accept, eventuale 3D viewer.
- `POST /sessions/{id}/feedback` — enqueue rigenerazione immagine.
- `POST /sessions/{id}/accept` — enqueue pipeline 3D completa.
- `GET /events/{job_id}` — SSE: stream di HTML fragments di `_job_progress_inner.html` ad ogni progress/log update.
- `GET /events/sessions/{id}` — SSE livello sessione: emette `iteration_added` / `3d_ready` alla fine del job attivo.

Concorrenza & sicurezza:
- `storage.update_metadata()` thread-safe (RLock per-sessione + atomic write via `tmp + os.replace()`).
- `meshy_client.*` accetta `api_key` esplicito (no mutazione globale `config.MESHY_API_KEY` in contesto web).
- `mesh_repair.repair/decimate/flatten_base` accettano `on_step` opzionale per progress SSE granulare (label tipo `flatten 4/8: Blender print3d_clean_non_manifold`).
- File serving con check path traversal (`Path(path).parts` non deve contenere `..`, e `full.resolve().relative_to(session.resolve())` deve riuscire).
- Niente auth: la sicurezza è demandata al tunnel. Non esporre il porto direttamente su rete pubblica.

## CLI

Entry point: `uv run models3d` (o `uv run python -m src.main`). Modalità interattiva (default) o sub-comandi per non-interattiva.

| Sub-comando | Cosa fa |
|---|---|
| (nessuno) o `run "idea"` | Loop interattivo classico: prompt → immagine → feedback → ... → accetta → 3D + repair |
| `start "idea" [--reference path]` | Crea sessione + genera prima immagine (text-to-image o image-to-image se ci sono reference) |
| `reject <session> "feedback"` | Rigenera immagine con feedback (Claude vede l'ultima immagine + history) |
| `accept <session>` | Accetta ultima immagine → multi-image-to-3d Meshy → pipeline repair completa |
| `repair <session>` | Solo `mesh_repair.repair()` su `model.stl` raw |
| `decimate <session> [--target N]` | QEM decimation su `model_repaired.stl` |
| `flatten <session> [--thickness pct]` | `mesh_repair.flatten_base()` → `model_printable.{stl,3mf}` |
| `--test-mode` | Usa la dummy key Meshy (`MESHY_TEST_KEY`): niente credit, output fake |

## Architettura

```
src/
├── main.py             # argparse + dispatch sub-comandi
├── workflow.py         # orchestrazione end-to-end (start/reject/accept)
├── meshy_client.py     # wrapper API Meshy (text-to-image, multi-image-to-3d, poll)
├── prompt_refiner.py   # wrapper Claude per refinement prompt (system prompt curato)
├── storage.py          # session dir layout, save image/3d, update metadata.json
├── mesh_repair.py      # repair() + decimate() + flatten_base() + helper Blender/PyMeshLab
└── config.py           # env var loading + costanti modello (claude-opus-4-7, meshy-6)
```

Ogni sessione vive in `outputs/<timestamp>-<slug-idea>/` con:
- `metadata.json` — stato + history iterazioni + credits Meshy consumati
- `prompts/00_idea.txt`, `prompts/NN_refined.txt`
- `references/*.png` (se l'utente ha fornito reference per image-to-image)
- `images/01.png`, `01_front.png`, `01_back.png` (split front+back per multi-image-to-3d)
- `3d/model.stl` (raw Meshy, 300k facce remeshate), poi i derivati post-processati

## Pipeline mesh repair (la parte non-ovvia)

Tre stadi sequenziali in `accept_and_generate_3d`:

### 1. `repair()` — `model.stl` → `model_repaired.{stl,3mf}`
Pre-decim QEM a 100k se serve → trimesh light fix → fallback `pymeshfix` solo se ≤50k facce con safety check (scarta se collassa <50%).

### 2. `decimate()` — `model_repaired.stl` → `model_lowpoly.{stl,3mf}`
`simplify_quadric_decimation(face_count=30_000)` + light fix. Quello che la maggior parte dei tool gestisce comodamente.

### 3. `flatten_base()` — `model_lowpoly.stl` → `model_printable.{stl,3mf}`
**Pipeline incrementale con early-exit**. Step mandatori (sempre):
1. PyMeshLab pre-cleanup (prepara lo slice)
2. `slice_mesh_plane(cap=True)` @ z = z_min + 2% altezza (base piatta)
3. PyMeshLab post-slice (ripulisce il join cap/walls)

Poi step opzionali in ordine cheap→costoso, **ognuno preceduto da un check `_is_print_ready()`**. Esce alla prima soglia raggiunta:

4. Blender `print3d_clean_non_manifold` (3D-Print Toolbox)
5. `_drop_floaters` (rimuove componenti < 50 facce — triangoli orfani dal cap)
6. `_fix_winding_per_component` (inverte isole con `volume<0`)
7. PyMeshLab `close_holes(maxholesize=500)` aggressivo
8. **Voxel remesh Blender** (last resort, *lossy*): `voxel_size = 0.5% bbox`. Garantisce manifold/watertight ma esplode il face count (~30k → 400k) e arrotonda gli spigoli vivi sotto la soglia voxel. Seguito da `_drop_floaters(min_ratio_of_largest=0.001)` perché il voxel grid può disconnettere parti sottili.

`_is_print_ready()` controlla: `is_watertight` + `is_winding_consistent` + zero edges non-manifold + nessun componente con `volume<0`.

### Quirk specifici da non riapprendere

- **Meshy multi-image-to-3d** ha output non-manifold intrinseco (~0.5-1% degli edge) anche con `should_remesh=True, target_polycount=300_000`. Non è eliminabile con tool standard senza distruggere geometria.
- **`pymeshfix.clean_from_arrays`** è un remesher globale aggressivo, NON un repair locale. Su mesh multi-component collassa a ~50% delle facce. Usare solo con safety check, mai come tool primario.
- **PyMeshLab `meshing_repair_non_manifold_edges(method='Split Vertices')`** esplode il vertex count (es. 14k → 2.1M) senza sanare la topologia. Usare sempre `method='Remove Faces'`.
- **Blender `normals_make_consistent`** NON corregge componenti con winding invertito perché propaga da una face seme sull'isola, propagando il verso sbagliato. Serve fix per-componente con `if c.volume<0: c.invert()`.
- **`slice_mesh_plane(cap=True)`** su mesh sottili genera triangoli orfani (coppie 2-face) che vanno droppati esplicitamente. `print3d_clean_non_manifold` di Blender non li elimina.
- **Voxel remesh** può creare piccoli body staccati: serve un secondo `drop_floaters` con soglia relativa al body principale, non assoluta.

## Convenzioni

- **Lingua**: codice e log in italiano. Commenti spiegano il *perché*, non il *cosa*.
- **Output utente**: `print(f"[modulo] …")` con prefisso per disambiguare stage. Le cifre di facce/vertici formattate con `:,`.
- **Errori**: la pipeline è "best-effort" — ogni step opzionale è in `try/except` e logga `FALLITO`, ma non interrompe il workflow. Solo gli step mandatori (slice) propagano l'eccezione.
- **Costanti** del repair in cima a `mesh_repair.py` (`DEFAULT_TARGET_FACES`, `DEFAULT_BASE_THICKNESS_PCT`, ecc), parametrizzabili da CLI.
- **Test mode**: `--test-mode` setta `MESHY_API_KEY` a una dummy key. Meshy risponde con stub e non addebita credit. **Non** evita comunque la pipeline 3D in locale.

## Reference esterne

- Meshy API: `https://api.meshy.ai/openapi/v1` — modelli usati: `nano-banana-pro` (image), `meshy-6` (3D).
- Anthropic API: modello `claude-opus-4-7` per il prompt refinement.
- `should_remesh=True` su Meshy-6: forzato perché di default è `False` e produrrebbe topologia non controllata.
