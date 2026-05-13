"""A/B test: rigenera il 3D di una sessione usando SOLO la vista frontale
(image-to-3d single-view) invece del multi-image-to-3d front+back.

Output: outputs/<session>/3d_ab/single_view/{model.stl, model.glb}.

Usage:
    uv run python tools/ab_single_view.py <session_id> [iteration_version]

`iteration_version`: default = accepted_iteration; altrimenti un intero 1..N.
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

from src import config, meshy_client, storage


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    session_id = sys.argv[1]
    session = config.OUTPUTS_ROOT / session_id
    if not session.is_dir():
        print(f"Sessione non trovata: {session}", file=sys.stderr)
        return 2

    meta = storage.load_metadata(session)
    if len(sys.argv) >= 3:
        target_version = int(sys.argv[2])
    else:
        target_version = meta.get("accepted_iteration") or meta["iterations"][-1]["version"]

    iter_rec = next(it for it in meta["iterations"] if it["version"] == target_version)
    front_path = session / iter_rec["front_path"]
    if not front_path.exists():
        print(f"Front view non trovata: {front_path}", file=sys.stderr)
        return 2

    print(f"Session : {session_id}")
    print(f"Iter    : v{target_version}")
    print(f"Front   : {front_path.relative_to(session)} ({front_path.stat().st_size//1024} KB)")

    # Encode front come data URI
    raw = front_path.read_bytes()
    data_uri = f"data:image/png;base64,{base64.b64encode(raw).decode('ascii')}"

    api_key = config.MESHY_API_KEY
    if not api_key:
        print("MESHY_API_KEY mancante", file=sys.stderr)
        return 2

    print(f"\nLancio image-to-3d single-view su Meshy ({config.MESHY_3D_MODEL})…")
    task_id = meshy_client.image_to_3d(
        image_url=data_uri,
        target_formats=("stl", "glb"),
        enable_pbr=True,
        api_key=api_key,
    )
    print(f"task_id: {task_id}")

    task = meshy_client.poll(
        "/image-to-3d",
        task_id,
        label="3D single-view",
        max_wait=1800,
        api_key=api_key,
    )
    print()

    out_dir = session / "3d_ab" / "single_view"
    out_dir.mkdir(parents=True, exist_ok=True)
    model_urls = task.get("model_urls", {})
    saved = {}
    for fmt in ("stl", "glb"):
        url = model_urls.get(fmt)
        if not url:
            print(f"  attenzione: nessun URL per formato {fmt}")
            continue
        import requests
        r = requests.get(url, timeout=300)
        r.raise_for_status()
        path = out_dir / f"model.{fmt}"
        path.write_bytes(r.content)
        saved[fmt] = str(path.relative_to(session))
        print(f"  {fmt.upper()}: {path} ({path.stat().st_size//1024} KB)")

    credits = task.get("consumed_credits", 0)
    print(f"\ncredits consumati: {credits}")

    # Registra in metadata per riferimento futuro
    storage.update_metadata(
        session,
        ab_single_view={
            "version_used": target_version,
            "task_id": task_id,
            "model_files": saved,
            "credits": credits,
        },
        add_credits={"image_to_3d_ab": credits},
    )
    print(f"\nOK. Confronta:")
    print(f"  A (multi front+back): {session.name}/3d/model.glb")
    print(f"  B (single front)    : {session.name}/3d_ab/single_view/model.glb")
    return 0


if __name__ == "__main__":
    sys.exit(main())
