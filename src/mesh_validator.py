"""Validazione + auto-fix di un STL post-OpenSCAD.

Best-effort: ogni controllo è protetto da try/except, in caso di errore
imprevisto torna comunque un dict con `warnings` popolato.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import trimesh

# Soglia minima HARD per stampa FDM 0.4 mm nozzle (sotto = rosso/warn).
MIN_WALL_MM = 0.8
# Soglia TARGET (sotto = info/giallo, oltre warn): allinearsi con scad.py
# min_wall=1.5; teniamo 1.2 per non spammare info su bordi/lamelle ok.
WARN_WALL_MM = 1.2
BED_MM = 220.0
SAMPLE_POINTS = 1500  # più samples → p5 stabile, costo trascurabile (~0.3s)


def _empty(warning: str) -> dict[str, Any]:
    return {
        "is_watertight": False,
        "is_winding_consistent": False,
        "is_manifold": False,
        "euler_number": None,
        "volume_mm3": None,
        "surface_area_mm2": None,
        "bbox_mm": None,
        "fits_bed_220": None,
        "min_thickness_mm": None,
        "p5_thickness_mm": None,
        "warnings": [warning],
        "infos": [],
        "auto_fixed": False,
    }


def _try_pymeshfix(stl_path: Path) -> bool:
    """Tenta auto-repair via pymeshfix. Sovrascrive lo STL se va a buon fine.
    Ritorna True se l'STL ora è watertight; False altrimenti."""
    try:
        import pymeshfix
    except ImportError:
        return False
    try:
        mesh = trimesh.load(str(stl_path), force="mesh")
        v, f = np.asarray(mesh.vertices), np.asarray(mesh.faces)
        mf = pymeshfix.MeshFix(v, f)
        mf.repair(verbose=False, joincomp=True, remove_smallest_components=False)
        new = trimesh.Trimesh(vertices=mf.v, faces=mf.f, process=True)
        if not new.is_watertight:
            return False
        new.export(str(stl_path), file_type="stl")
        return True
    except Exception:
        return False


def _thickness_stats(mesh: trimesh.Trimesh) -> tuple[float | None, float | None]:
    """Probe approssimata dello spessore: per N punti random sulla superficie
    si lancia un raggio interno e si misura la distanza alla prima intersezione
    opposta. Ritorna `(min, p5)`: min è il valore peggiore (sensibile a edge
    sfortunati), p5 è il 5° percentile (resistente agli outlier — è la metrica
    affidabile per decidere il warning). Ritorna `(None, None)` se non valutabile."""
    if not mesh.is_watertight or len(mesh.faces) == 0:
        return None, None
    try:
        sample, face_idx = trimesh.sample.sample_surface_even(mesh, SAMPLE_POINTS)
        if len(sample) == 0:
            return None, None
        normals = mesh.face_normals[face_idx]
        # Spara raggi verso l'interno (lato opposto della normale).
        origins = sample - normals * 1e-3  # piccolo offset per evitare hit zero
        directions = -normals
        locs, ray_idx, _ = mesh.ray.intersects_location(
            ray_origins=origins, ray_directions=directions, multiple_hits=False,
        )
        if len(locs) == 0:
            return None, None
        hit_origins = origins[ray_idx]
        dists = np.linalg.norm(locs - hit_origins, axis=1)
        # Filtra zero-length (auto-intersezioni numeriche).
        dists = dists[dists > 1e-3]
        if len(dists) == 0:
            return None, None
        return float(np.min(dists)), float(np.percentile(dists, 5))
    except Exception:
        return None, None


def validate(stl_path: Path, *, attempt_fix: bool = True) -> dict[str, Any]:
    """Valida un STL. Se non-manifold e `attempt_fix=True`, prova pymeshfix
    e ri-valida. Ritorna sempre un dict (anche su errore catastrofico)."""
    if not stl_path.exists() or stl_path.stat().st_size == 0:
        return _empty("STL mancante o vuoto")

    try:
        mesh = trimesh.load(str(stl_path), force="mesh")
    except Exception as e:
        return _empty(f"trimesh.load fallita: {e}")

    warnings: list[str] = []
    infos: list[str] = []
    auto_fixed = False

    is_watertight = bool(mesh.is_watertight)
    is_winding = bool(mesh.is_winding_consistent)
    is_manifold = is_watertight and is_winding

    if not is_manifold and attempt_fix:
        if _try_pymeshfix(stl_path):
            try:
                mesh = trimesh.load(str(stl_path), force="mesh")
                is_watertight = bool(mesh.is_watertight)
                is_winding = bool(mesh.is_winding_consistent)
                is_manifold = is_watertight and is_winding
                auto_fixed = True
                warnings.append("auto-riparato con pymeshfix")
            except Exception:
                pass

    if not is_manifold:
        if not is_watertight:
            warnings.append("non-watertight (buchi nel mesh)")
        if not is_winding:
            warnings.append("winding non consistente (normali invertite)")

    try:
        volume = float(abs(mesh.volume)) if mesh.is_volume or is_watertight else None
    except Exception:
        volume = None
    try:
        area = float(mesh.area)
    except Exception:
        area = None

    try:
        bbox = (mesh.bounds[1] - mesh.bounds[0]).tolist()
        bbox = [round(float(x), 2) for x in bbox]
    except Exception:
        bbox = None

    fits_bed = None
    if bbox is not None:
        fits_bed = bbox[0] <= BED_MM and bbox[1] <= BED_MM
        if not fits_bed:
            warnings.append(f"supera piatto {int(BED_MM)}×{int(BED_MM)} mm (bbox {bbox[0]}×{bbox[1]})")

    min_thick, p5_thick = _thickness_stats(mesh) if is_manifold else (None, None)
    # Decisione basata su p5 (resistente a outlier su edge sfortunati): se il
    # 5° percentile è sotto la soglia HARD → warning vero. Se è oltre la HARD
    # ma sotto la TARGET → info (giallo soft, non bloccante).
    if p5_thick is not None:
        if p5_thick < MIN_WALL_MM:
            warnings.append(
                f"wall sotto soglia: p5 {p5_thick:.2f} mm < {MIN_WALL_MM} mm "
                f"(min assoluto {min_thick:.2f} mm)"
            )
        elif p5_thick < WARN_WALL_MM:
            infos.append(
                f"wall borderline: p5 {p5_thick:.2f} mm < target {WARN_WALL_MM} mm "
                f"(min assoluto {min_thick:.2f} mm) — stampabile ma fragile"
            )

    try:
        euler = int(mesh.euler_number)
    except Exception:
        euler = None

    return {
        "is_watertight": is_watertight,
        "is_winding_consistent": is_winding,
        "is_manifold": is_manifold,
        "euler_number": euler,
        "volume_mm3": round(volume, 2) if volume is not None else None,
        "surface_area_mm2": round(area, 2) if area is not None else None,
        "bbox_mm": bbox,
        "fits_bed_220": fits_bed,
        "min_thickness_mm": round(min_thick, 3) if min_thick is not None else None,
        "p5_thickness_mm": round(p5_thick, 3) if p5_thick is not None else None,
        "warnings": warnings,
        "infos": infos,
        "auto_fixed": auto_fixed,
    }


def severity(validation: dict | None) -> str:
    """Mappa il validation dict a un livello: 'ok', 'info', 'warn', 'fail'."""
    if not validation:
        return "fail"
    if not validation.get("is_manifold"):
        return "fail"
    if validation.get("warnings"):
        return "warn"
    if validation.get("infos"):
        return "info"
    return "ok"
