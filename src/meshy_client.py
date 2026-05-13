import sys
import time
from typing import Iterable

import requests

from . import config


class MeshyError(RuntimeError):
    pass


class MeshyTaskFailed(MeshyError):
    def __init__(self, status: str, detail):
        super().__init__(f"Meshy task {status}: {detail}")
        self.status = status
        self.detail = detail


def _headers() -> dict:
    if not config.MESHY_API_KEY:
        raise MeshyError("MESHY_API_KEY non configurata.")
    return {
        "Authorization": f"Bearer {config.MESHY_API_KEY}",
        "Content-Type": "application/json",
    }


def text_to_image(
    prompt: str,
    *,
    ai_model: str = config.MESHY_IMAGE_MODEL,
    aspect_ratio: str = "1:1",
) -> str:
    body = {"prompt": prompt, "ai_model": ai_model, "aspect_ratio": aspect_ratio}
    r = requests.post(
        f"{config.MESHY_BASE}/text-to-image", json=body, headers=_headers(), timeout=60
    )
    if not r.ok:
        raise MeshyError(f"text-to-image POST {r.status_code}: {r.text}")
    return r.json()["result"]


def image_to_image(
    prompt: str,
    reference_image_urls: list[str],
    *,
    ai_model: str = config.MESHY_IMAGE_MODEL,
) -> str:
    """reference_image_urls: 1–5 voci, URL pubblici o data URI base64."""
    if not 1 <= len(reference_image_urls) <= 5:
        raise MeshyError(
            f"image-to-image richiede 1-5 reference (ne hai {len(reference_image_urls)})."
        )
    body = {
        "prompt": prompt,
        "ai_model": ai_model,
        "reference_image_urls": reference_image_urls,
    }
    r = requests.post(
        f"{config.MESHY_BASE}/image-to-image",
        json=body,
        headers=_headers(),
        timeout=60,
    )
    if not r.ok:
        raise MeshyError(f"image-to-image POST {r.status_code}: {r.text}")
    return r.json()["result"]


DEFAULT_TARGET_POLYCOUNT = 300_000  # massimo Meshy image-to-3d


def image_to_3d(
    *,
    input_task_id: str,
    target_formats: Iterable[str] = ("glb", "stl"),
    enable_pbr: bool = True,
    ai_model: str = config.MESHY_3D_MODEL,
    target_polycount: int = DEFAULT_TARGET_POLYCOUNT,
) -> str:
    body = {
        "input_task_id": input_task_id,
        "ai_model": ai_model,
        "enable_pbr": enable_pbr,
        "target_formats": list(target_formats),
        "target_polycount": target_polycount,
    }
    r = requests.post(
        f"{config.MESHY_BASE}/image-to-3d", json=body, headers=_headers(), timeout=60
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
) -> str:
    """image_urls: 1–4 voci, ognuna URL pubblico o data URI base64.

    NB: `target_polycount` viene applicato solo se `should_remesh=True`.
    Meshy-6 ha `should_remesh=False` di default, quindi forziamo True per
    avere controllo sulla risoluzione e topologia uniforme.
    """
    body = {
        "image_urls": image_urls,
        "ai_model": ai_model,
        "enable_pbr": enable_pbr,
        "target_formats": list(target_formats),
        "target_polycount": target_polycount,
        "should_remesh": should_remesh,
    }
    r = requests.post(
        f"{config.MESHY_BASE}/multi-image-to-3d",
        json=body,
        headers=_headers(),
        timeout=60,
    )
    if not r.ok:
        raise MeshyError(f"multi-image-to-3d POST {r.status_code}: {r.text}")
    return r.json()["result"]


def poll(
    endpoint: str,
    task_id: str,
    *,
    interval: float = 5.0,
    max_wait: float = 900.0,
    label: str = "task",
) -> dict:
    url = f"{config.MESHY_BASE}{endpoint}/{task_id}"
    start = time.monotonic()
    last_progress = -1
    while True:
        r = requests.get(url, headers=_headers(), timeout=30)
        if not r.ok:
            raise MeshyError(f"poll GET {r.status_code}: {r.text}")
        task = r.json()
        status = task.get("status")
        progress = task.get("progress", 0)
        if progress != last_progress:
            sys.stdout.write(f"\r  {label}: {status} {progress}%   ")
            sys.stdout.flush()
            last_progress = progress
        if status == "SUCCEEDED":
            sys.stdout.write("\n")
            return task
        if status in ("FAILED", "CANCELED"):
            sys.stdout.write("\n")
            raise MeshyTaskFailed(status, task.get("task_error") or task)
        if time.monotonic() - start > max_wait:
            raise MeshyError(f"poll timeout dopo {max_wait}s (ultimo status: {status})")
        time.sleep(interval)
