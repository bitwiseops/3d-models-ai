import sys
import time
from typing import Callable, Iterable

import requests

from . import config


class MeshyError(RuntimeError):
    pass


class MeshyTaskFailed(MeshyError):
    def __init__(self, status: str, detail):
        super().__init__(f"Meshy task {status}: {detail}")
        self.status = status
        self.detail = detail


def _headers(api_key: str | None = None) -> dict:
    """Header Bearer per Meshy. `api_key` esplicito ha priorità su `config.MESHY_API_KEY`
    (mutabile globale, da CLI _apply_test_mode). Il web layer passa sempre `api_key`
    per evitare race su mutazioni runtime."""
    key = api_key or config.MESHY_API_KEY
    if not key:
        raise MeshyError("MESHY_API_KEY non configurata.")
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def text_to_image(
    prompt: str,
    *,
    ai_model: str = config.MESHY_IMAGE_MODEL,
    aspect_ratio: str = "1:1",
    api_key: str | None = None,
) -> str:
    body = {"prompt": prompt, "ai_model": ai_model, "aspect_ratio": aspect_ratio}
    r = requests.post(
        f"{config.MESHY_BASE}/text-to-image",
        json=body,
        headers=_headers(api_key),
        timeout=60,
    )
    if not r.ok:
        raise MeshyError(f"text-to-image POST {r.status_code}: {r.text}")
    return r.json()["result"]


def image_to_image(
    prompt: str,
    reference_image_urls: list[str],
    *,
    ai_model: str = config.MESHY_IMAGE_MODEL,
    generate_multi_view: bool = False,
    api_key: str | None = None,
) -> str:
    """reference_image_urls: 1–5 voci, URL pubblici o data URI base64.

    Con `generate_multi_view=True`, l'API genera 3 viste del soggetto da
    angolazioni diverse: il task SUCCEEDED avrà `image_urls` di lunghezza 3
    invece di 1. Default False per non rompere i call site che si aspettano
    una singola immagine.
    """
    if not 1 <= len(reference_image_urls) <= 5:
        raise MeshyError(
            f"image-to-image richiede 1-5 reference (ne hai {len(reference_image_urls)})."
        )
    body: dict = {
        "prompt": prompt,
        "ai_model": ai_model,
        "reference_image_urls": reference_image_urls,
    }
    if generate_multi_view:
        body["generate_multi_view"] = True
    r = requests.post(
        f"{config.MESHY_BASE}/image-to-image",
        json=body,
        headers=_headers(api_key),
        timeout=60,
    )
    if not r.ok:
        raise MeshyError(f"image-to-image POST {r.status_code}: {r.text}")
    return r.json()["result"]


DEFAULT_TARGET_POLYCOUNT = 300_000  # massimo Meshy image-to-3d


def image_to_3d(
    *,
    input_task_id: str | None = None,
    image_url: str | None = None,
    target_formats: Iterable[str] = ("glb", "stl"),
    enable_pbr: bool = True,
    ai_model: str = config.MESHY_3D_MODEL,
    target_polycount: int = DEFAULT_TARGET_POLYCOUNT,
    should_remesh: bool = True,
    api_key: str | None = None,
) -> str:
    """Genera un mesh 3D da UNA singola immagine. Specificare uno tra:
    - `input_task_id`: task ID di una text-to-image / image-to-image Meshy
    - `image_url`: URL pubblico o data URI base64 dell'immagine
    """
    if (input_task_id is None) == (image_url is None):
        raise MeshyError(
            "image_to_3d richiede esattamente uno tra input_task_id e image_url."
        )
    body: dict = {
        "ai_model": ai_model,
        "enable_pbr": enable_pbr,
        "target_formats": list(target_formats),
        "target_polycount": target_polycount,
        "should_remesh": should_remesh,
    }
    if input_task_id is not None:
        body["input_task_id"] = input_task_id
    else:
        body["image_url"] = image_url
    r = requests.post(
        f"{config.MESHY_BASE}/image-to-3d",
        json=body,
        headers=_headers(api_key),
        timeout=60,
    )
    if not r.ok:
        raise MeshyError(f"image-to-3d POST {r.status_code}: {r.text}")
    return r.json()["result"]


def multi_image_to_3d(
    *,
    image_urls: list[str],
    target_formats: Iterable[str] = ("glb", "stl"),
    enable_pbr: bool = True,
    ai_model: str = config.MESHY_3D_MODEL,
    target_polycount: int = DEFAULT_TARGET_POLYCOUNT,
    should_remesh: bool = True,
    symmetry_mode: str | None = None,  # "off" | "auto" | "on"
    api_key: str | None = None,
) -> str:
    """image_urls: 1–4 voci, ognuna URL pubblico o data URI base64.

    NB: `target_polycount` viene applicato solo se `should_remesh=True`.
    Meshy-6 ha `should_remesh=False` di default, quindi forziamo True per
    avere controllo sulla risoluzione e topologia uniforme.

    `symmetry_mode`: se passato, forza la simmetria sull'asse verticale.
    Utile per visi/busti. Valori: "off" (no), "auto" (Meshy decide),
    "on" (forza). Omesso → Meshy usa il suo default.
    """
    body = {
        "image_urls": image_urls,
        "ai_model": ai_model,
        "enable_pbr": enable_pbr,
        "target_formats": list(target_formats),
        "target_polycount": target_polycount,
        "should_remesh": should_remesh,
    }
    if symmetry_mode is not None:
        body["symmetry_mode"] = symmetry_mode
    r = requests.post(
        f"{config.MESHY_BASE}/multi-image-to-3d",
        json=body,
        headers=_headers(api_key),
        timeout=60,
    )
    if not r.ok:
        raise MeshyError(f"multi-image-to-3d POST {r.status_code}: {r.text}")
    return r.json()["result"]


def print_analyze(
    *,
    input_task_id: str | None = None,
    model_url: str | None = None,
    api_key: str | None = None,
) -> dict:
    """Check stampabilità FDM via /print/analyze. GRATIS (0 crediti).

    Specificare esattamente uno tra:
    - `input_task_id`: task Meshy-6/Preview che ha prodotto il mesh
    - `model_url`: URL pubblico del modello (GLB/STL)

    Ritorna il blocco `printability` del task: dict con `status`
    ("healthy"|"warning"|"error"|"unknown"), `metrics` (is_watertight, volume,
    non_manifold_edges, degenerate_faces, holes) e `issue_count`. Dict vuoto se
    il task non lo popola.
    """
    if (input_task_id is None) == (model_url is None):
        raise MeshyError(
            "print_analyze richiede esattamente uno tra input_task_id e model_url."
        )
    body = (
        {"input_task_id": input_task_id}
        if input_task_id is not None
        else {"model_url": model_url}
    )
    r = requests.post(
        f"{config.MESHY_BASE}/print/analyze",
        json=body,
        headers=_headers(api_key),
        timeout=60,
    )
    if not r.ok:
        raise MeshyError(f"print/analyze POST {r.status_code}: {r.text}")
    task = poll(
        "/print/analyze",
        r.json()["result"],
        label="print analyze",
        max_wait=300,
        api_key=api_key,
    )
    return task.get("printability") or {}


def print_repair(
    *,
    input_task_id: str | None = None,
    model_url: str | None = None,
    api_key: str | None = None,
) -> str:
    """Repair server-side della topologia (non-manifold, degenere, buchi) via
    /print/repair. Costo 10 crediti.

    Specificare esattamente uno tra `input_task_id` (output GLB) e `model_url`
    (output STL). Ritorna l'URL del mesh riparato (primo `model_urls` non-vuoto).
    NB: il repair preserva la geometria, non le texture.
    """
    if (input_task_id is None) == (model_url is None):
        raise MeshyError(
            "print_repair richiede esattamente uno tra input_task_id e model_url."
        )
    body = (
        {"input_task_id": input_task_id}
        if input_task_id is not None
        else {"model_url": model_url}
    )
    r = requests.post(
        f"{config.MESHY_BASE}/print/repair",
        json=body,
        headers=_headers(api_key),
        timeout=60,
    )
    if not r.ok:
        raise MeshyError(f"print/repair POST {r.status_code}: {r.text}")
    task = poll(
        "/print/repair",
        r.json()["result"],
        label="print repair",
        max_wait=600,
        api_key=api_key,
    )
    urls = task.get("model_urls") or {}
    repaired = next((u for u in urls.values() if u), None)
    if not repaired:
        raise MeshyError("print/repair non ha restituito alcun model_url.")
    return repaired


def poll(
    endpoint: str,
    task_id: str,
    *,
    interval: float = 5.0,
    max_wait: float = 900.0,
    label: str = "task",
    on_progress: Callable[[int, str], None] | None = None,
    api_key: str | None = None,
) -> dict:
    """Polling fino a SUCCEEDED/FAILED/CANCELED o timeout.

    Se `on_progress` è fornito, viene chiamato `(progress_pct, status_str)` ad
    ogni cambio di progress E nessun output va su stdout (modalità web/SSE).
    Senza callback: print su stdout come prima (modalità CLI).
    """
    url = f"{config.MESHY_BASE}{endpoint}/{task_id}"
    start = time.monotonic()
    last_progress = -1
    while True:
        r = requests.get(url, headers=_headers(api_key), timeout=30)
        if not r.ok:
            raise MeshyError(f"poll GET {r.status_code}: {r.text}")
        task = r.json()
        status = task.get("status")
        progress = task.get("progress", 0)
        if progress != last_progress:
            if on_progress is not None:
                on_progress(int(progress), str(status or ""))
            else:
                sys.stdout.write(f"\r  {label}: {status} {progress}%   ")
                sys.stdout.flush()
            last_progress = progress
        if status == "SUCCEEDED":
            if on_progress is None:
                sys.stdout.write("\n")
            return task
        if status in ("FAILED", "CANCELED"):
            if on_progress is None:
                sys.stdout.write("\n")
            raise MeshyTaskFailed(status, task.get("task_error") or task)
        if time.monotonic() - start > max_wait:
            raise MeshyError(f"poll timeout dopo {max_wait}s (ultimo status: {status})")
        time.sleep(interval)
