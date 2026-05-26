import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

import numpy as np
import pymeshfix
import trimesh

# Tipo del callback opzionale per progress UI/SSE.
StepCallback = Callable[[str], None] | None


def _emit(on_step: StepCallback, message: str) -> None:
    """Helper: chiama il callback se presente (no-op altrimenti). I `print()`
    nelle funzioni restano per la CLI; il callback è additivo per web/SSE."""
    if on_step is not None:
        on_step(message)


DEFAULT_TARGET_FACES = 30_000
MAX_REPAIR_INPUT_FACES = 100_000
MAX_PYMESHFIX_FACES = 50_000
DEFAULT_BASE_THICKNESS_PCT = 0.02
DEFAULT_CLOSE_MAX_HOLE_EDGES = 30
DEFAULT_MIN_COMPONENT_FACES = 50
DEFAULT_FINAL_CLOSE_HOLE_EDGES = 500
DEFAULT_VOXEL_REMESH_PCT = 0.005  # voxel size = 0.5% della max bbox dim

_BLENDER_CANDIDATES = [
    "/Applications/Blender.app/Contents/MacOS/Blender",
    "blender",
]

_BLENDER_CLEANUP_SCRIPT = '''
import bpy, sys

argv = sys.argv[sys.argv.index("--") + 1:]
input_path, output_path = argv[0], argv[1]

bpy.ops.wm.read_factory_settings(use_empty=True)

# Enable 3D-Print Toolbox extension (install una tantum a parte)
try:
    bpy.ops.preferences.addon_enable(module="bl_ext.blender_org.print3d_toolbox")
    has_print3d = True
except Exception:
    has_print3d = False

# Import STL
try:
    bpy.ops.wm.stl_import(filepath=input_path)
except AttributeError:
    bpy.ops.import_mesh.stl(filepath=input_path)

obj = bpy.context.selected_objects[0]
bpy.context.view_layer.objects.active = obj

bpy.ops.object.mode_set(mode="EDIT")

if has_print3d:
    # 3D-Print Toolbox: operator dedicato make-manifold con multi-pass
    # (merge doubles, fill holes, fix non-manifold, dissolve degenerate).
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.print3d_clean_non_manifold(threshold=0.0001, sides=0)
else:
    # Fallback: pipeline manuale (meno completa)
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.remove_doubles(threshold=0.0001)
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.dissolve_degenerate(threshold=0.0001)
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.fill_holes(sides=0)
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.remove_doubles(threshold=0.0001)

# Ricalcola normali coerenti verso l'esterno
bpy.ops.mesh.select_all(action="SELECT")
bpy.ops.mesh.normals_make_consistent(inside=False)

bpy.ops.object.mode_set(mode="OBJECT")

try:
    bpy.ops.wm.stl_export(filepath=output_path)
except AttributeError:
    bpy.ops.export_mesh.stl(filepath=output_path, use_selection=False)
'''


_BLENDER_VOXEL_REMESH_SCRIPT = '''
import bpy, sys

argv = sys.argv[sys.argv.index("--") + 1:]
input_path, output_path, voxel_size = argv[0], argv[1], float(argv[2])

bpy.ops.wm.read_factory_settings(use_empty=True)

try:
    bpy.ops.wm.stl_import(filepath=input_path)
except AttributeError:
    bpy.ops.import_mesh.stl(filepath=input_path)

obj = bpy.context.selected_objects[0]
bpy.context.view_layer.objects.active = obj

mod = obj.modifiers.new(name="VoxelRemesh", type="REMESH")
mod.mode = "VOXEL"
mod.voxel_size = voxel_size
mod.use_smooth_shade = False
mod.adaptivity = 0.0
bpy.ops.object.modifier_apply(modifier=mod.name)

# Voxel remesh in Blender talvolta lascia normali invertite — riallinea.
bpy.ops.object.mode_set(mode="EDIT")
bpy.ops.mesh.select_all(action="SELECT")
bpy.ops.mesh.normals_make_consistent(inside=False)
bpy.ops.object.mode_set(mode="OBJECT")

try:
    bpy.ops.wm.stl_export(filepath=output_path)
except AttributeError:
    bpy.ops.export_mesh.stl(filepath=output_path, use_selection=False)
'''


def _find_blender() -> str | None:
    for cand in _BLENDER_CANDIDATES:
        if Path(cand).exists():
            return cand
        which = shutil.which(cand)
        if which:
            return which
    return None


def _blender_clean(mesh: trimesh.Trimesh, timeout: float = 120.0) -> trimesh.Trimesh:
    """Cleanup mesh via Blender headless. Esegue remove_doubles, fill_holes,
    make_manifold via select_non_manifold+fill, dissolve_degenerate, recalc normals.
    Più completo di PyMeshLab/pymeshfix per output Meshy.

    Solleva RuntimeError se Blender non trovato o script fallisce.
    """
    blender = _find_blender()
    if blender is None:
        raise RuntimeError("Blender non trovato. Installa Blender o aggiungi al PATH.")

    with tempfile.TemporaryDirectory(prefix="blender_clean_") as td:
        td = Path(td)
        in_stl = td / "in.stl"
        out_stl = td / "out.stl"
        script = td / "clean.py"

        mesh.export(str(in_stl))
        script.write_text(_BLENDER_CLEANUP_SCRIPT)

        result = subprocess.run(
            [blender, "--background", "--python", str(script),
             "--", str(in_stl), str(out_stl)],
            capture_output=True, text=True, timeout=timeout,
        )
        if not out_stl.exists():
            tail = (result.stderr or result.stdout)[-800:]
            raise RuntimeError(f"Blender cleanup ha fallito.\n{tail}")

        cleaned = trimesh.load(str(out_stl), force="mesh", process=True)
        return cleaned


def _blender_voxel_remesh(
    mesh: trimesh.Trimesh,
    voxel_size_pct: float = DEFAULT_VOXEL_REMESH_PCT,
    timeout: float = 240.0,
) -> trimesh.Trimesh:
    """Applica Blender Remesh modifier in Voxel mode. Volumetric reconstruction:
    garantisce output manifold/watertight single-component, a costo di
    arrotondare gli spigoli vivi sotto la soglia del voxel.

    voxel_size = bbox.max() * voxel_size_pct (default 0.5% → ~10 units su mesh
    Meshy da ~2000 units, conserva dettagli >20 units).
    """
    blender = _find_blender()
    if blender is None:
        raise RuntimeError("Blender non trovato. Installa Blender o aggiungi al PATH.")

    bbox = mesh.bounds[1] - mesh.bounds[0]
    voxel_size = float(bbox.max() * voxel_size_pct)
    print(
        f"[voxel] bbox max={bbox.max():.2f}, "
        f"voxel_size={voxel_size:.4f} ({voxel_size_pct:.2%})"
    )

    with tempfile.TemporaryDirectory(prefix="blender_voxel_") as td:
        td = Path(td)
        in_stl = td / "in.stl"
        out_stl = td / "out.stl"
        script = td / "voxel.py"

        mesh.export(str(in_stl))
        script.write_text(_BLENDER_VOXEL_REMESH_SCRIPT)

        result = subprocess.run(
            [blender, "--background", "--python", str(script),
             "--", str(in_stl), str(out_stl), str(voxel_size)],
            capture_output=True, text=True, timeout=timeout,
        )
        if not out_stl.exists():
            tail = (result.stderr or result.stdout)[-800:]
            raise RuntimeError(f"Blender voxel remesh ha fallito.\n{tail}")

        return trimesh.load(str(out_stl), force="mesh", process=True)


def _pymeshlab_clean(
    mesh: trimesh.Trimesh,
    close_max_hole_edges: int = DEFAULT_CLOSE_MAX_HOLE_EDGES,
) -> trimesh.Trimesh:
    """Cleanup chirurgico via PyMeshLab. Workflow MeshLab 'Cleaning and Repairing'.

    Strategia: 'Remove Faces' su non-manifold edge (rimuove ~1% di facce
    problematiche), poi close_holes su holes piccoli per riparare i micro-buchi
    locali. NON usa 'Split Vertices' perché esplode il vertex count senza
    sanare effettivamente la topologia.
    """
    import pymeshlab as pml

    ms = pml.MeshSet()
    ms.add_mesh(
        pml.Mesh(np.asarray(mesh.vertices), np.asarray(mesh.faces)),
        "mesh",
    )

    # 1. Pulizia base
    ms.meshing_remove_duplicate_vertices()
    ms.meshing_remove_duplicate_faces()
    ms.meshing_remove_null_faces()
    ms.meshing_remove_unreferenced_vertices()

    # 2. Repair non-manifold edges via 'Remove Faces' — rimuove i triangoli
    #    che condividono edge non-manifold (tipicamente ~1% del totale).
    #    Iterativo per catturare effetti a cascata, converge in 1-2 pass.
    for _ in range(3):
        pre_f = ms.current_mesh().face_number()
        ms.meshing_repair_non_manifold_edges(method="Remove Faces")
        ms.meshing_repair_non_manifold_vertices()
        if ms.current_mesh().face_number() == pre_f:
            break

    # 3. Re-orient (ora il mesh è manifold)
    ms.meshing_re_orient_faces_coherently()

    # 4. Chiudi holes piccoli (residui locali dopo Remove Faces)
    ms.meshing_close_holes(maxholesize=close_max_hole_edges)

    out = ms.current_mesh()
    return trimesh.Trimesh(
        vertices=out.vertex_matrix(),
        faces=out.face_matrix(),
        process=True,
    )


def _drop_floaters(
    mesh: trimesh.Trimesh,
    min_faces: int = DEFAULT_MIN_COMPONENT_FACES,
    min_ratio_of_largest: float = 0.0,
) -> trimesh.Trimesh:
    """Rimuove componenti connesse minori (triangoli orfani dal cap dello slice,
    frammenti dal voxel remesh, ecc).

    Soglia: tiene un componente se `faces >= min_faces` AND
    `faces >= largest.faces * min_ratio_of_largest`. La componente AND consente
    di alzare la soglia in proporzione al body principale (es. 0.001 = 0.1%
    della componente più grande).
    """
    components = mesh.split(only_watertight=False)
    if len(components) <= 1:
        return mesh
    largest = max(len(c.faces) for c in components)
    threshold = max(min_faces, int(largest * min_ratio_of_largest))
    kept = [c for c in components if len(c.faces) >= threshold]
    dropped = len(components) - len(kept)
    if dropped == 0:
        return mesh
    if not kept:
        print(f"[floaters] ATTENZIONE: nessun componente >= {threshold} facce, "
              f"mantengo mesh originale.")
        return mesh
    print(
        f"[floaters] rimossi {dropped} componenti < {threshold} facce "
        f"({len(components)} → {len(kept)} componenti)"
    )
    return trimesh.util.concatenate(kept) if len(kept) > 1 else kept[0]


def _fix_winding_per_component(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    """Inverte le facce dei componenti con volume negativo (winding invertito).
    L'output di Meshy può avere isole con normali invertite che il pass Blender
    `normals_make_consistent` non riallinea perché lavora sul mesh unito.
    """
    components = mesh.split(only_watertight=False)
    if len(components) <= 1:
        if mesh.volume < 0:
            mesh.invert()
            print("[winding] mesh con volume negativo → invertito")
        return mesh
    inverted = 0
    for c in components:
        if c.volume < 0:
            c.invert()
            inverted += 1
    if inverted:
        print(
            f"[winding] invertiti {inverted}/{len(components)} componenti "
            f"con volume negativo"
        )
        return trimesh.util.concatenate(components)
    return mesh


def _stats(mesh: trimesh.Trimesh) -> str:
    return (
        f"{len(mesh.vertices):,} vert, {len(mesh.faces):,} faces, "
        f"watertight={mesh.is_watertight}, winding_consistent={mesh.is_winding_consistent}"
    )


def _is_print_ready(mesh: trimesh.Trimesh, verbose: bool = True) -> bool:
    """Verifica se il mesh soddisfa i criteri Bambu Studio / slicer moderni
    senza richiedere repair esterno:
    - watertight (chiuso, no boundary edges)
    - winding consistente (normali coerenti)
    - zero edges non-manifold (count != 2)
    - nessun componente con volume negativo (no cavità interne)
    """
    if not mesh.is_watertight:
        if verbose:
            print("  [check] non watertight → continuo cleanup")
        return False
    if not mesh.is_winding_consistent:
        if verbose:
            print("  [check] winding inconsistente → continuo cleanup")
        return False
    edges_sorted = np.sort(mesh.edges, axis=1)
    _, counts = np.unique(edges_sorted, axis=0, return_counts=True)
    nm = int((counts != 2).sum())
    if nm > 0:
        if verbose:
            print(f"  [check] {nm} edges non-manifold → continuo cleanup")
        return False
    for c in mesh.split(only_watertight=False):
        if c.volume < 0:
            if verbose:
                print("  [check] componente con vol<0 (cavità interna) → continuo")
            return False
    if verbose:
        print("  [check] mesh print-ready ✓")
    return True


def repair(
    glb_path: Path,
    out_dir: Path,
    *,
    on_step: StepCallback = None,
) -> dict[str, Path]:
    """Carica GLB, applica repair watertight, esporta STL + 3MF print-ready.

    Pipeline:
      1. Carica con `force='mesh'` per fondere tutte le geometrie in un singolo mesh.
      2. Pass leggero con trimesh: fix winding/normali, fill holes.
      3. Se ancora non-watertight, fallback aggressivo a pymeshfix (MeshFix).
    """
    _emit(on_step, f"repair: caricamento {glb_path.name}")
    print(f"[repair] caricamento {glb_path.name}…")
    mesh = trimesh.load(str(glb_path), force="mesh", process=True)
    if not isinstance(mesh, trimesh.Trimesh):
        raise RuntimeError(
            f"Atteso Trimesh, ottenuto {type(mesh).__name__}: GLB senza geometria utile?"
        )
    print(f"[repair] iniziale     → {_stats(mesh)}")

    if len(mesh.faces) > MAX_REPAIR_INPUT_FACES:
        _emit(on_step, f"repair: pre-decimazione QEM a {MAX_REPAIR_INPUT_FACES:,} facce")
        print(
            f"[repair] mesh oltre la soglia ({MAX_REPAIR_INPUT_FACES:,}), "
            f"pre-decimazione QEM…"
        )
        mesh = mesh.simplify_quadric_decimation(face_count=MAX_REPAIR_INPUT_FACES)
        print(f"[repair] post-pre-dec → {_stats(mesh)}")

    _emit(on_step, "repair: trimesh fix winding/normali/fill_holes")
    mesh.update_faces(mesh.unique_faces())
    mesh.remove_unreferenced_vertices()
    mesh.merge_vertices()
    trimesh.repair.fix_inversion(mesh)
    trimesh.repair.fix_winding(mesh)
    trimesh.repair.fix_normals(mesh)
    trimesh.repair.fill_holes(mesh)
    print(f"[repair] post-trimesh → {_stats(mesh)}")

    if not mesh.is_watertight:
        if len(mesh.faces) > MAX_PYMESHFIX_FACES:
            print(
                f"[repair] mesh > {MAX_PYMESHFIX_FACES:,} facce, skip pymeshfix "
                f"(rischio runtime non polinomiale su multi-component). "
                f"Mantengo il pass trimesh."
            )
        else:
            _emit(on_step, "repair: fallback pymeshfix")
            print("[repair] non watertight, fallback a pymeshfix (non distruttivo)…")
            pre_faces = len(mesh.faces)
            new_v, new_f = pymeshfix.clean_from_arrays(
                np.asarray(mesh.vertices),
                np.asarray(mesh.faces),
                joincomp=True,
                remove_smallest_components=False,
            )
            candidate = trimesh.Trimesh(vertices=new_v, faces=new_f, process=True)
            print(f"[repair] candidate    → {_stats(candidate)}")
            if len(candidate.faces) < pre_faces * 0.5:
                print(
                    f"[repair] pymeshfix ha collassato la mesh "
                    f"({pre_faces:,} → {len(candidate.faces):,} facce). "
                    f"Scarto il risultato e mantengo il pass trimesh."
                )
            else:
                mesh = candidate
                print(f"[repair] post-meshfix → {_stats(mesh)}")

    if not mesh.is_watertight:
        print("[repair] ATTENZIONE: mesh ancora non-watertight dopo entrambi i pass.")

    out: dict[str, Path] = {}
    for fmt in ("stl", "3mf"):
        path = out_dir / f"model_repaired.{fmt}"
        mesh.export(str(path))
        out[fmt] = path
        size_mb = path.stat().st_size / (1024 * 1024)
        print(f"[repair] esportato {fmt.upper()}: {path.name} ({size_mb:.1f} MB)")
    return out


def decimate(
    mesh_path: Path,
    out_dir: Path,
    target_faces: int = DEFAULT_TARGET_FACES,
    suffix: str = "lowpoly",
    *,
    on_step: StepCallback = None,
) -> dict[str, Path]:
    """Decima un mesh esistente via QEM (Quadric Error Metrics) e lo esporta STL+3MF.

    Pipeline:
      1. Carica con `force='mesh'` (accetta STL, GLB, 3MF...).
      2. Se le facce >= target, applica `simplify_quadric_decimation`.
      3. Light fix post-decimazione (normali, fill_holes residui).
      4. Esporta `model_{suffix}.{stl,3mf}` in `out_dir`.
    """
    _emit(on_step, f"decimate: caricamento {mesh_path.name}")
    print(f"[decimate] caricamento {mesh_path.name}…")
    mesh = trimesh.load(str(mesh_path), force="mesh", process=True)
    if not isinstance(mesh, trimesh.Trimesh):
        raise RuntimeError(
            f"Atteso Trimesh, ottenuto {type(mesh).__name__} da {mesh_path}"
        )
    print(f"[decimate] iniziale     → {_stats(mesh)}")

    if len(mesh.faces) <= target_faces:
        print(f"[decimate] mesh già <= {target_faces:,} facce, skip decimazione.")
        decimated = mesh
    else:
        _emit(on_step, f"decimate: QEM target {target_faces:,} facce")
        decimated = mesh.simplify_quadric_decimation(face_count=target_faces)
        print(f"[decimate] post-QEM     → {_stats(decimated)}")
        trimesh.repair.fix_normals(decimated)
        trimesh.repair.fill_holes(decimated)
        print(f"[decimate] post-fix     → {_stats(decimated)}")

    out: dict[str, Path] = {}
    for fmt in ("stl", "3mf"):
        path = out_dir / f"model_{suffix}.{fmt}"
        decimated.export(str(path))
        out[fmt] = path
        size_mb = path.stat().st_size / (1024 * 1024)
        print(f"[decimate] esportato {fmt.upper()}: {path.name} ({size_mb:.1f} MB)")
    return out


def crop_bust(
    mesh_path: Path,
    out_dir: Path,
    keep_top_pct: float = 0.55,
    suffix: str = "bust",
    *,
    on_step: StepCallback = None,
) -> dict[str, Path]:
    """Taglia il mesh con un piano orizzontale per mantenere solo la porzione
    superiore (mezzo busto): `keep_top_pct` è la frazione dell'altezza
    bbox tenuta dall'alto verso il basso. Default 55% → testa + collo + spalle
    + porzione superiore del torace; il resto (torso/gambe/jeans) eliminato.

    Tappa la sezione di taglio per ottenere un mesh watertight con base piatta.
    Esporta `model_{suffix}.{stl,3mf}` in `out_dir`.
    """
    _emit(on_step, f"crop bust: caricamento {mesh_path.name}")
    print(f"[crop_bust] caricamento {mesh_path.name}…")
    mesh = trimesh.load(str(mesh_path), force="mesh", process=True)
    if not isinstance(mesh, trimesh.Trimesh):
        raise RuntimeError(f"Atteso Trimesh, ottenuto {type(mesh).__name__}")
    z_min, z_max = float(mesh.bounds[0][2]), float(mesh.bounds[1][2])
    height = z_max - z_min
    # Taglio orizzontale a (z_max - keep_top_pct * height): tutto sopra resta.
    cut_z = z_max - keep_top_pct * height
    _emit(on_step, f"crop bust: taglio z={cut_z:.2f} (keep top {keep_top_pct*100:.0f}%)")
    print(f"[crop_bust] bbox z=[{z_min:.2f}, {z_max:.2f}] h={height:.2f} → cut_z={cut_z:.2f}")

    # `slice_plane` taglia + tappa la sezione (cap=True) → watertight.
    cut = mesh.slice_plane(
        plane_origin=[0, 0, cut_z],
        plane_normal=[0, 0, 1],
        cap=True,
    )
    if cut is None or len(cut.faces) == 0:
        raise RuntimeError(f"slice_plane ha eliminato tutto il mesh (cut_z={cut_z})")
    print(f"[crop_bust] post-cut    → {_stats(cut)}")
    trimesh.repair.fix_normals(cut)
    trimesh.repair.fill_holes(cut)

    out: dict[str, Path] = {}
    for fmt in ("stl", "3mf"):
        path = out_dir / f"model_{suffix}.{fmt}"
        cut.export(str(path))
        out[fmt] = path
        size_mb = path.stat().st_size / (1024 * 1024)
        print(f"[crop_bust] esportato {fmt.upper()}: {path.name} ({size_mb:.1f} MB)")
    return out


def flatten_base(
    mesh_path: Path,
    out_dir: Path,
    base_thickness_pct: float = DEFAULT_BASE_THICKNESS_PCT,
    suffix: str = "printable",
    *,
    on_step: StepCallback = None,
) -> dict[str, Path]:
    """Taglia la base inferiore del mesh con un piano orizzontale (z = z_min + h*pct),
    tappa la sezione per creare una base piatta watertight, e trasla il risultato
    in modo che la nuova base sia a z=0 (pronto per il print bed).

    Assume convenzione z-up. `base_thickness_pct` è la frazione dell'altezza totale
    da tagliare in basso (default 2% — taglia le piccole protrusioni e rende la
    base perfettamente piatta).
    """
    _emit(on_step, f"flatten: caricamento {mesh_path.name}")
    print(f"[flatten] caricamento {mesh_path.name}…")
    mesh = trimesh.load(str(mesh_path), force="mesh", process=True)
    if not isinstance(mesh, trimesh.Trimesh):
        raise RuntimeError(f"Atteso Trimesh, ottenuto {type(mesh).__name__}")
    print(f"[flatten] iniziale    → {_stats(mesh)}")

    # Pre-cleanup PyMeshLab del mesh INTERO: rimuove non-manifold edges
    # interni della lowpoly (residui della decimazione QEM) PRIMA dello slice.
    # Lo slice di trimesh con cap=True funziona pulito solo su input manifold.
    _emit(on_step, "flatten 1/8: pre-cleanup PyMeshLab")
    print("[flatten] pre-cleanup PyMeshLab del mesh intero…")
    try:
        mesh = _pymeshlab_clean(mesh, close_max_hole_edges=DEFAULT_CLOSE_MAX_HOLE_EDGES)
        print(f"[flatten] post-pre-cleanup → {_stats(mesh)}")
    except Exception as e:
        print(f"[flatten] pre-cleanup FALLITO ({e}), procedo con mesh originale.")

    z_min, z_max = float(mesh.bounds[0, 2]), float(mesh.bounds[1, 2])
    height = z_max - z_min
    cut_z = z_min + height * base_thickness_pct
    print(
        f"[flatten] altezza={height:.3f}, z_min={z_min:.3f}, z_max={z_max:.3f}, "
        f"taglio @ z={cut_z:.3f} ({base_thickness_pct:.1%} dal fondo)"
    )

    # Slice CON cap: ora il mesh in input è quasi-manifold, il cap di trimesh
    # produce una base pulita.
    _emit(on_step, f"flatten 2/8: slice base @ z={cut_z:.2f}")
    sliced = trimesh.intersections.slice_mesh_plane(
        mesh,
        plane_normal=[0.0, 0.0, 1.0],
        plane_origin=[0.0, 0.0, cut_z],
        cap=True,
    )
    if sliced is None or len(sliced.faces) == 0:
        raise RuntimeError("slice_mesh_plane ha restituito mesh vuoto")
    sliced.apply_translation([0.0, 0.0, -cut_z])
    print(f"[flatten] post-slice  → {_stats(sliced)}")

    # Post-slice cleanup: sempre, perché lo slice introduce non-manifold sul
    # join cap/walls. È il primo punto utile per testare print-readiness.
    _emit(on_step, "flatten 3/8: post-slice cleanup PyMeshLab")
    print("[flatten] post-slice cleanup PyMeshLab…")
    pre_faces = len(sliced.faces)
    try:
        cleaned = _pymeshlab_clean(sliced, close_max_hole_edges=DEFAULT_CLOSE_MAX_HOLE_EDGES)
        print(
            f"[flatten] post-cleanup → {_stats(cleaned)} "
            f"({pre_faces:,} → {len(cleaned.faces):,} facce)"
        )
        sliced = cleaned
    except Exception as e:
        print(f"[flatten] PyMeshLab cleanup FALLITO ({e}), mantengo pre-cleanup.")

    # Da qui in poi: pipeline incrementale. Dopo ogni step controlla se il
    # mesh è già print-ready; se sì esce e preserva la topologia originale
    # (spigoli vivi, face count basso). Solo i casi davvero rotti arrivano
    # al voxel remesh lossy.

    # Step: Blender 3D-Print Toolbox (make_manifold + fill_holes + dissolve)
    if not _is_print_ready(sliced) and _find_blender() is not None:
        _emit(on_step, "flatten 4/8: Blender print3d_clean_non_manifold")
        print("[flatten] step: Blender print3d_clean_non_manifold…")
        pre_b = len(sliced.faces)
        try:
            blender_cleaned = _blender_clean(sliced)
            print(
                f"[flatten] post-blender → {_stats(blender_cleaned)} "
                f"({pre_b:,} → {len(blender_cleaned.faces):,} facce)"
            )
            sliced = blender_cleaned
        except Exception as e:
            print(f"[flatten] Blender cleanup FALLITO ({e}), continuo.")

    # Step: drop floater + fix winding per-componente
    if not _is_print_ready(sliced):
        _emit(on_step, "flatten 5/8: drop floater + fix winding")
        print("[flatten] step: drop floater + fix winding per-componente…")
        sliced = _drop_floaters(sliced)
        sliced = _fix_winding_per_component(sliced)
        print(f"[flatten] post-drop/winding → {_stats(sliced)}")

    # Step: close_holes aggressivo (chiude buchi residui medi)
    if not _is_print_ready(sliced):
        _emit(on_step, f"flatten 6/8: close_holes(max {DEFAULT_FINAL_CLOSE_HOLE_EDGES})")
        print(f"[flatten] step: close_holes(max {DEFAULT_FINAL_CLOSE_HOLE_EDGES})…")
        try:
            sliced = _pymeshlab_clean(
                sliced, close_max_hole_edges=DEFAULT_FINAL_CLOSE_HOLE_EDGES
            )
            print(f"[flatten] post-close_holes → {_stats(sliced)}")
        except Exception as e:
            print(f"[flatten] close_holes FALLITO ({e}), continuo.")

    # Step finale: voxel remesh (lossy ma garantito manifold). Si raggiunge
    # solo se nessuno dei passi precedenti ha prodotto un mesh print-ready.
    if not _is_print_ready(sliced):
        if _find_blender() is not None:
            _emit(on_step, "flatten 7/8: voxel remesh (last resort, lossy)")
            print("[flatten] step: voxel remesh (last resort, lossy)…")
            pre_v = len(sliced.faces)
            try:
                remeshed = _blender_voxel_remesh(sliced)
                print(
                    f"[flatten] post-voxel → {_stats(remeshed)} "
                    f"({pre_v:,} → {len(remeshed.faces):,} facce)"
                )
                sliced = _drop_floaters(remeshed, min_ratio_of_largest=0.001)
            except Exception as e:
                print(f"[flatten] voxel remesh FALLITO ({e}), mantengo pre-voxel.")
        else:
            print("[flatten] Blender non trovato — voxel remesh skippato.")
    else:
        _emit(on_step, "flatten: mesh print-ready early (spigoli vivi preservati)")
        print("[flatten] mesh print-ready prima del voxel remesh → spigoli vivi preservati ✓")

    _emit(on_step, "flatten 8/8: export STL + 3MF")

    out: dict[str, Path] = {}
    for fmt in ("stl", "3mf"):
        path = out_dir / f"model_{suffix}.{fmt}"
        sliced.export(str(path))
        out[fmt] = path
        size_mb = path.stat().st_size / (1024 * 1024)
        print(f"[flatten] esportato {fmt.upper()}: {path.name} ({size_mb:.1f} MB)")
    return out
