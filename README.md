# models3d-ai

**From a text idea to a print-ready 3D model.** A Python pipeline (CLI + mobile-first web UI) that chains **Claude** and **Meshy** and then repairs the generated mesh so slicers such as Bambu Studio, PrusaSlicer or OrcaSlicer accept it as-is.

```
text idea
  → Claude (prompt refiner): sharpens the prompt, iterates on your feedback
  → Meshy text-to-image / image-to-image: front + back views
  → Meshy multi-image-to-3D: raw mesh
  → mesh repair: cleanup, decimation, flat base, guaranteed manifold
  → model_printable.stl / .3mf
```

## Why the repair step exists

Meshy output is **not** print-ready: it has boundary edges, intrinsic non-manifold edges (~0.5–1 %), inconsistent winding and small floaters at the front/back seam. Slicers don't fix these in place and send you to an online repair service. [`src/mesh_repair.py`](src/mesh_repair.py) removes that round-trip, optionally finishing with a headless **Blender** voxel remesh to guarantee a watertight result.

## Features

- Interactive CLI and a **FastAPI + HTMX** web UI with live progress over SSE and an inline `<model-viewer>` 3D preview. No JS toolchain; designed to be reached from a phone through a tunnel
- Background job manager with progress and logs
- Automatic failover between Meshy API keys
- Mesh validation and a test suite (`tests/`)

## Setup

Requires Python 3.11 and [uv](https://docs.astral.sh/uv/). Blender is optional but strongly recommended.

```bash
uv sync
cp .env.example .env     # add ANTHROPIC_API_KEY and MESHY_API_KEY

uv run models3d run "a small dragon sitting on a stack of books"   # CLI
uv run models3d-web                                                # web UI on :8000
```

The web UI is single-user with no authentication. If you expose it, put it behind Tailscale or a Cloudflare Tunnel.

## Stack

Python · Anthropic API · Meshy API · trimesh / pymeshfix / pymeshlab · Blender (headless) · FastAPI · HTMX · uv

## License

MIT, see [LICENSE](LICENSE).
