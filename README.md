<div align="center">

# 🧊 models3d-ai

**How do you go from a sentence to a 3D print that your slicer actually accepts?**
Claude refines the idea, Meshy generates the model, and a repair pipeline makes the mesh print-ready.

![Python](https://img.shields.io/badge/python-3.11-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
![Claude](https://img.shields.io/badge/LLM-Claude-d97757)
![Meshy](https://img.shields.io/badge/3D-Meshy-6250d6)
![FastAPI](https://img.shields.io/badge/web-FastAPI%20%2B%20HTMX-009688)
![Blender](https://img.shields.io/badge/repair-Blender%20(optional)-e87d0d)

</div>

---

## Table of contents

- [Overview](#overview)
- [Highlights](#highlights)
- [Quick start](#quick-start)
- [How it works](#how-it-works)
- [Why the repair step exists](#why-the-repair-step-exists)
- [CLI](#cli)
- [Web UI](#web-ui)
- [Project structure](#project-structure)
- [Limitations](#limitations)
- [License](#license)

## Overview

models3d-ai is a Python pipeline, with a CLI and a mobile-first web UI, that turns a **text idea** into a **print-ready STL/3MF** for Bambu Studio, PrusaSlicer or OrcaSlicer. You describe the object, iterate on the generated image until it looks right, and the pipeline does the rest.

> **The key point.** Meshy's raw output is not print-ready. The mesh-repair stage is what makes the result printable without a round-trip to an external repair service.

## Highlights

- ✍️ **Prompt refinement with Claude:** sharpens your idea and iterates on your feedback before any 3D credits are spent.
- 🖼️ **Front + back views:** Meshy text-to-image and image-to-image, then multi-image-to-3D.
- 🔧 **Print-ready repair:** cleanup, decimation, flat base and a guaranteed manifold mesh.
- 📱 **Web UI:** FastAPI and HTMX, live progress over SSE, inline `<model-viewer>` 3D preview. No JS toolchain.
- 🔁 **API-key failover:** automatic switch to a fallback Meshy key.
- 🧩 **Component breakdown:** decompose an accepted object into parts, each with its own context-aware prompt and 2×2 grid.
- ✅ **Tested:** `pytest` suite under `tests/`.

## Quick start

Requires Python 3.11 and [uv](https://docs.astral.sh/uv/). [Blender](https://www.blender.org) is optional but strongly recommended.

```bash
git clone https://github.com/bitwiseops/3d-models-ai.git
cd 3d-models-ai
uv sync
cp .env.example .env          # add ANTHROPIC_API_KEY and MESHY_API_KEY

uv run models3d run "a small dragon sitting on a stack of books"    # interactive CLI
uv run models3d-web                                                 # web UI on :8000
```

## How it works

```
text idea
  → Claude (prompt refiner)            refine the prompt, iterate on feedback
  → Meshy text/image-to-image          front + back views
  → Meshy multi-image-to-3D            raw mesh
  → mesh repair                        cleanup, decimation, flat base, manifold
  → model_printable.stl / .3mf         ready for the slicer
```

## Why the repair step exists

The raw Meshy mesh has boundary edges, intrinsic non-manifold edges (about 0.5 to 1 %), inconsistent winding and small floaters at the front/back seam. Slicers do not repair these in place and point you to an online repair tool. [`src/mesh_repair.py`](src/mesh_repair.py) removes that round-trip. With Blender installed, a headless voxel remesh at the end guarantees a watertight result. Without Blender the pipeline still runs, but may produce meshes that Bambu Studio rejects.

## CLI

| Command | What it does |
|---|---|
| `models3d run "<idea>"` | classic interactive loop |
| `models3d start` / `reject` / `accept` | create a session, iterate, accept the image |
| `models3d breakdown <session>` | split the accepted object into components |
| `models3d repair <session>` | re-apply mesh repair to an existing session |
| `models3d cleanup` | list or delete heavy intermediates (dry-run by default) |
| `models3d pull` | fetch a session's component bundle from a remote server |

## Web UI

`uv run models3d-web` serves a single-user app on `0.0.0.0:8000` (`MODELS3D_HOST`, `MODELS3D_PORT`, `MODELS3D_RELOAD=1`). A job manager runs the long tasks in a thread pool (`MODELS3D_MAX_WORKERS`, default 4) and streams progress to the page. It is meant to be reached from a phone through Tailscale or a Cloudflare Tunnel.

## Project structure

```
├── src/
│   ├── main.py             # CLI
│   ├── prompt_refiner.py   # Claude prompt refinement
│   ├── meshy_client.py     # Meshy API with key failover
│   ├── mesh_repair.py      # trimesh, pymeshfix, Blender passes
│   ├── mesh_validator.py
│   ├── workflow.py
│   └── web/                # FastAPI app, routes, Jinja2 templates, job manager
├── tools/                  # helper scripts, Blender MCP add-on
├── tests/                  # pytest
├── assets/hdri/            # lighting for renders
├── pyproject.toml          # dependencies (uv)
└── .env.example
```

## Limitations

The web UI is single-user with **no authentication**: put it behind a private tunnel and never expose it openly. Generation needs paid Anthropic and Meshy API keys. Repair quality is best with Blender installed. The repository's internal notes and code comments are in Italian.

## License

[MIT](LICENSE) © 2026 Flavio Cordari
