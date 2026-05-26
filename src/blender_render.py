"""Wrapper headless Blender per hero render PBR con HDR environment.

Se `blender` non è installato, `render_hero` torna `False` e l'app degrada
graziosamente al render PNG di openscad. La script blender vive inline qui per
non avere file orfani.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import config

# Override esplicito via env (per AppImage / installazioni non in PATH).
_BIN_OVERRIDE = os.environ.get("BLENDER_BIN")
if _BIN_OVERRIDE and Path(_BIN_OVERRIDE).is_file() and os.access(_BIN_OVERRIDE, os.X_OK):
    BLENDER_BIN = _BIN_OVERRIDE
else:
    BLENDER_BIN = shutil.which("blender")
HDR_DEFAULT = config.PROJECT_ROOT / "assets" / "hdri" / "studio_small.hdr"

# Polyhaven CC0, 1k, ~1.2 MB. URL diretto, no auth richiesta.
HDR_URL = "https://dl.polyhaven.org/file/ph-assets/HDRIs/hdr/1k/studio_small_03_1k.hdr"

DEFAULT_TIMEOUT = 180
DEFAULT_SAMPLES = 48
DEFAULT_SIZE = (1024, 1024)


def is_available() -> bool:
    return BLENDER_BIN is not None


def ensure_hdr(target: Path = HDR_DEFAULT) -> Path | None:
    """Scarica l'HDR CC0 se assente. Ritorna il path se ora disponibile."""
    if target.exists() and target.stat().st_size > 100_000:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        import requests
        r = requests.get(HDR_URL, timeout=20)
        if r.ok:
            target.write_bytes(r.content)
            return target
    except Exception as e:
        print(f"[blender] HDR download fallito: {e}")
    return target if target.exists() else None


_BLENDER_SCRIPT = r"""
import sys, os, bpy

# Args dopo `--`
argv = sys.argv[sys.argv.index('--') + 1:]
STL_PATH, OUT_PATH, HDR_PATH, SAMPLES, SIZE_W, SIZE_H = (
    argv[0], argv[1], argv[2], int(argv[3]), int(argv[4]), int(argv[5])
)

# Reset scena
bpy.ops.wm.read_factory_settings(use_empty=True)

# Import mesh (STL o GLB)
ext = os.path.splitext(STL_PATH)[1].lower()
if ext == '.stl':
    try:
        bpy.ops.wm.stl_import(filepath=STL_PATH)  # blender 4.x
    except AttributeError:
        bpy.ops.import_mesh.stl(filepath=STL_PATH)  # blender 3.x
elif ext == '.glb' or ext == '.gltf':
    bpy.ops.import_scene.gltf(filepath=STL_PATH)
else:
    raise SystemExit(f'unsupported ext: {ext}')

# Center e fit camera
import mathutils
objs = [o for o in bpy.context.scene.objects if o.type == 'MESH']
if not objs:
    raise SystemExit('nessuna mesh importata')
# bbox globale
mins = mathutils.Vector(( 1e30,)*3)
maxs = mathutils.Vector((-1e30,)*3)
for o in objs:
    for v in o.bound_box:
        wv = o.matrix_world @ mathutils.Vector(v)
        mins = mathutils.Vector((min(mins.x,wv.x), min(mins.y,wv.y), min(mins.z,wv.z)))
        maxs = mathutils.Vector((max(maxs.x,wv.x), max(maxs.y,wv.y), max(maxs.z,wv.z)))
center = (mins + maxs) * 0.5
size = max((maxs - mins).x, (maxs - mins).y, (maxs - mins).z, 1.0)

# Sposta tutto in modo che la base poggi a Z=0 e centro X/Y sull'origine
for o in objs:
    o.location.x -= center.x
    o.location.y -= center.y
    o.location.z -= mins.z

# Materiale PBR arancione PLA su tutte le mesh
mat = bpy.data.materials.new(name='pla_orange')
mat.use_nodes = True
bsdf = mat.node_tree.nodes.get('Principled BSDF')
if bsdf:
    bsdf.inputs['Base Color'].default_value = (0.95, 0.45, 0.15, 1.0)
    # Le label degli input variano tra versioni Blender; tentiamo entrambi.
    for key in ('Roughness',):
        if key in bsdf.inputs:
            bsdf.inputs[key].default_value = 0.55
    for key in ('Metallic',):
        if key in bsdf.inputs:
            bsdf.inputs[key].default_value = 0.1
for o in objs:
    if o.data.materials:
        o.data.materials[0] = mat
    else:
        o.data.materials.append(mat)

# World con HDR
world = bpy.data.worlds.new('w') if not bpy.context.scene.world else bpy.context.scene.world
world.use_nodes = True
bpy.context.scene.world = world
nt = world.node_tree
nt.nodes.clear()
bg = nt.nodes.new('ShaderNodeBackground')
env = nt.nodes.new('ShaderNodeTexEnvironment')
out = nt.nodes.new('ShaderNodeOutputWorld')
if os.path.exists(HDR_PATH):
    env.image = bpy.data.images.load(HDR_PATH)
    nt.links.new(env.outputs[0], bg.inputs[0])
nt.links.new(bg.outputs[0], out.inputs[0])
bg.inputs[1].default_value = 1.0  # strength

# Camera 3/4 isometrica
cam_data = bpy.data.cameras.new('cam')
cam = bpy.data.objects.new('cam', cam_data)
bpy.context.scene.collection.objects.link(cam)
d = size * 2.2
cam.location = (d, -d, d * 0.9)
# Punta al baricentro spostato (origine X/Y, metà altezza)
center_after = mathutils.Vector((0.0, 0.0, (maxs.z - mins.z) * 0.5))
direction = center_after - cam.location
rot = direction.to_track_quat('-Z', 'Y').to_euler()
cam.rotation_euler = rot
cam_data.lens = 50
bpy.context.scene.camera = cam

# Render
scene = bpy.context.scene
scene.render.engine = 'CYCLES'
try:
    scene.cycles.device = 'GPU'
except Exception:
    pass
scene.cycles.samples = SAMPLES
scene.cycles.use_denoising = True
scene.render.resolution_x = SIZE_W
scene.render.resolution_y = SIZE_H
scene.render.resolution_percentage = 100
scene.render.image_settings.file_format = 'PNG'
scene.render.image_settings.color_mode = 'RGBA'
scene.render.film_transparent = False
scene.render.filepath = OUT_PATH
bpy.ops.render.render(write_still=True)
print('OK', OUT_PATH)
"""


def render_hero(
    src_path: Path,
    out_png: Path,
    *,
    hdr_path: Path | None = None,
    samples: int = DEFAULT_SAMPLES,
    size: tuple[int, int] = DEFAULT_SIZE,
    timeout: float = DEFAULT_TIMEOUT,
) -> bool:
    """Renderizza `src_path` (STL o GLB) come PNG fotorealistico in `out_png`.
    Ritorna True se il file è stato prodotto, False altrimenti (blender assente,
    timeout, errore Cycles, ecc.)."""
    if BLENDER_BIN is None:
        return False
    if not src_path.exists():
        return False
    hdr = hdr_path or ensure_hdr()
    out_png.parent.mkdir(parents=True, exist_ok=True)

    # Script su file temp per evitare quoting infernale.
    with tempfile.NamedTemporaryFile(
        "w", suffix=".py", delete=False, encoding="utf-8"
    ) as f:
        f.write(_BLENDER_SCRIPT)
        script_path = Path(f.name)

    cmd = [
        BLENDER_BIN, "--background", "--factory-startup",
        "--python", str(script_path),
        "--",
        str(src_path), str(out_png), str(hdr or ""), str(samples),
        str(size[0]), str(size[1]),
    ]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "BLENDER_USER_CONFIG": "/tmp/.blender-tmp"},
        )
    except subprocess.TimeoutExpired:
        print(f"[blender] timeout {timeout}s su {src_path.name}")
        return False
    except FileNotFoundError:
        return False
    finally:
        script_path.unlink(missing_ok=True)

    if r.returncode != 0:
        print(f"[blender] FAIL ({src_path.name}): rc={r.returncode} {r.stderr[-300:]}")
        return False
    if not out_png.exists() or out_png.stat().st_size == 0:
        print(f"[blender] niente file di output per {src_path.name}")
        return False
    return True
