"""SCAD workflow part-aware.

Stadio 1 (plan): Claude decompone l'idea in N parti coordinate da parametri
condivisi + recipe di assembly.
Stadio 2 (per-part): per ogni parte una chiamata Claude in parallelo, ognuna
produce un .scad/.stl/.glb/.png stampabile a sé.
Stadio 3 (assembly): preview composta via trimesh.Scene (niente openscad).

Refine globale rilancia tutti gli stadi; refine per-parte rigenera solo la
parte target e ricostruisce l'assembly. Iterazioni legacy (campo `stl_path`
piatto) restano leggibili dal template.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from . import (
    blender_render,
    config,
    mesh_validator,
    prompt_refiner,
    storage,
    usage as usage_tracker,
)

SCAD_ROOT = config.OUTPUTS_ROOT / "scad"

RENDER_SIZE = "800,800"
RENDER_COLORSCHEME = "Tomorrow"
OPENSCAD_TIMEOUT = 90
MAX_FIX_ATTEMPTS = 5
MAX_PARTS = 8
MAX_PARALLEL_PARTS = 4

_scad_locks: dict[Path, threading.RLock] = {}
_locks_guard = threading.Lock()


def _get_lock(scad_dir: Path) -> threading.RLock:
    key = scad_dir.resolve()
    with _locks_guard:
        lock = _scad_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _scad_locks[key] = lock
        return lock


# --- System prompts ---


SCAD_PLAN_SYSTEM_PROMPT = """<role>
Sei un architetto di modelli OpenSCAD parametrici per stampa 3D FDM.
Dato un oggetto, lo SCOMPONI in parti stampabili separatamente che combaciano
fisicamente quando assemblate.
</role>

<task>
Produci un PIANO di scomposizione, NON il codice. Il piano contiene:
- un breve ragionamento,
- un blocco SCAD di parametri condivisi (dimensioni globali, tolleranze, $fn),
- la lista delle PARTI (max 8) con nome snake_case e descrizione 1-2 frasi,
- la RECIPE di assembly: per ogni istanza, quale parte, translate [x,y,z] in mm,
  rotate [rx,ry,rz] in gradi. Una parte può comparire più volte (es. 4 piedini).
</task>

<part_design_rules>
- Ogni parte è autonoma in stampa: poggiata sul piano Z=0, manifold, senza
  supporti se evitabili.
- Le parti combaciano via i parametri condivisi (es. `body_w`, `wall`, `peg_d`).
  Definisci in `shared_scad` TUTTE le costanti che attraversano più parti.
- Scegli scomposizioni ragionevoli: per "scocca radio" → body, front_panel,
  knob, foot. NON spezzare un cubo banale in 6 facce. Min 1, max 8.
- Tolleranze FDM nei fit (clearance ≈ 0.2-0.3 mm).
</part_design_rules>

<shared_scad_rules>
- Solo definizioni (assignments) e $fn, niente moduli e niente top-level calls.
- Tutto in mm. Commenti brevi opzionali.
- Niente `include`/`use`.
- DEVI definire `min_wall = 1.5;` (mm) e `wall` ≥ `min_wall`. Ogni parete/guscio/
  rib generato da QUALSIASI parte deve avere spessore ≥ `min_wall`. Se l'oggetto
  ha pareti naturalmente sottili (es. tazza), comunque non scendere sotto 1.5 mm.
</shared_scad_rules>

<assembly_recipe_rules>
- translate e rotate in coordinate WORLD: come deve stare ogni parte nel
  modello finale assemblato.
- Una parte può comparire più volte con translate diversi (es. 4 piedini agli
  angoli).
- Se non hai bisogno di ruotare, metti `[0,0,0]`.
</assembly_recipe_rules>

<output_format>
Rispondi SOLO con un fence ```json ... ``` contenente esattamente questa shape:

```json
{
  "reasoning": "frase breve in italiano sulle scelte di scomposizione",
  "shared_scad": "// shared parameters\\nmin_wall = 1.5;\\nwall = 2.5;\\nbody_w = 80;\\n$fn = 64;\\n",
  "parts": [
    {"name": "body", "description": "scocca esterna trapezoidale con base piatta"},
    {"name": "knob", "description": "manopola cilindrica con base allargata"}
  ],
  "assembly": [
    {"part": "body", "translate": [0, 0, 0],    "rotate": [0, 0, 0]},
    {"part": "knob", "translate": [30, 20, 50], "rotate": [0, 0, 0]},
    {"part": "knob", "translate": [-30, 20, 50],"rotate": [0, 0, 0]}
  ]
}
```

Niente prosa fuori dal fence.
</output_format>
"""


SCAD_PART_SYSTEM_PROMPT = """<role>
Sei un esperto OpenSCAD per stampa 3D FDM. Generi UNA singola parte di un
assembly più grande. Il codice deve essere autonomo (compilabile da solo) e
produrre un oggetto manifold poggiato a Z=0.
</role>

<target_runtime>
OpenSCAD 2021.01, CGAL backend. Funzioni built-in: cube, sphere, cylinder,
polyhedron, square, circle, polygon, text, linear_extrude, rotate_extrude,
projection, surface, hull, minkowski, offset, translate, rotate, scale,
mirror, multmatrix, color, resize, union, difference, intersection, for,
intersection_for, if, let, each, module, function.
</target_runtime>

<hard_requirements>
- Unità: millimetri. Niente `include`/`use`.
- Il codice DEVE iniziare con il blocco di parametri condivisi che ti viene
  passato, INCLUSO ESATTAMENTE come fornito (così tutte le parti restano
  coerenti). Sotto puoi aggiungere parametri privati a questa parte.
- Definisci un singolo `module <part_name>() { … }` e termina il file con una
  sola chiamata top-level `<part_name>();`.
- Parte autonoma in stampa: poggiata su Z=0, manifold, niente parti sotto Z=0.
- Usa `$fn` adeguato (parti standard 64, piccoli dettagli 32, superfici di
  pregio 128).
- Niente echo/assert di debug.
- SPESSORI MINIMI (FDM 0.4 mm nozzle, vincolo HARD):
  - Pareti continue ≥ `min_wall` (1.5 mm); usa `wall` (≥ `min_wall`) o un altro
    nome derivato, MAI letterali sotto soglia.
  - Per shell cilindrici/gusci usa il pattern `outer - inner` con
    `inner_d = outer_d - 2*wall` (NON `2*min_wall`: lascia margine).
  - Ribs, alette, lamelle decorative ≥ 1.2 mm (`min_wall - 0.3`); se servono
    più sottili, omettile invece di farle fragili.
  - Solette/base ≥ 1.5 mm (= 4 layers a 0.4 mm).
  - Pin/peg di accoppiamento: diametro nominale ≥ 3 mm; clearance fori +0.3 mm
    sul diametro (`hole_d = pin_d + 0.3`).
  - Smussi e fillet non devono ridurre lo spessore locale sotto soglia: se un
    `offset(r=-x)` o un `minkowski` con `r` grande assottiglia, alza la base o
    cambia approccio.
- VERIFICA MENTALE prima di chiudere: in ogni `difference()` calcola lo spessore
  residuo (`outer - inner`) — deve essere ≥ `min_wall`. Se dubitoso, ingrossa.
</hard_requirements>

<common_pitfalls>
1. Facce coincidenti nelle sottrazioni: estendi di 0.01-0.02 mm.
2. `linear_extrude(scale=0)`: usa 0.01 minimo.
3. `minkowski()` su concavi: preferisci `offset()` 2D + `linear_extrude`.
4. `hull()` con molte primitive: O(n²), tieni sotto 6 punti.
5. Self-intersection in `union()`: se due parti devono toccarsi, falle
   compenetrare di 0.01 mm.
6. Niente numeri magici inline: usa costanti dello shared_scad o nominate
   subito sotto.
7. Tolleranze stampante: aggiungi 0.2-0.4 mm rispetto al nominale sui fit
   maschio/femmina.
</common_pitfalls>

<style_template>
```openscad
// === shared.scad (incluso così come fornito) ===
<shared block>

// --- private params (opzionali, solo per questa parte) ---
foot_h = 8;
foot_d = 18;

module foot() {
  difference() {
    cylinder(h=foot_h, d=foot_d);
    translate([0, 0, -0.01])
      cylinder(h=foot_h + 0.02, d=foot_d - 2*wall);
  }
}

foot();
```
</style_template>

<output_format>
Rispondi in due parti:
1. **Ragionamento** in italiano (1-3 frasi): scelte chiave per QUESTA parte.
2. **Codice** in un singolo fence ```openscad ... ```. Nient'altro fuori dal
   fence.
</output_format>
"""


_FENCE_SCAD_RE = re.compile(r"```(?:openscad|scad)?\s*\n(.+?)```", re.DOTALL)
_FENCE_JSON_RE = re.compile(r"```(?:json)?\s*\n(.+?)```", re.DOTALL)


def _extract_scad_code(reply: str) -> tuple[str, str]:
    m = _FENCE_SCAD_RE.search(reply)
    if not m:
        return "", reply.strip()
    return reply[: m.start()].strip(), m.group(1).rstrip()


def _extract_json(reply: str) -> dict:
    """Estrae il primo fence JSON. Solleva ValueError se non parsabile."""
    m = _FENCE_JSON_RE.search(reply)
    raw = m.group(1) if m else reply
    return json.loads(raw)


def _validate_plan(plan: dict) -> tuple[dict, list[str]]:
    """Normalizza e valida un piano. Ritorna (plan_pulito, lista_errori_non_fatali)."""
    warns: list[str] = []
    if not isinstance(plan, dict):
        raise ValueError("plan non è un dict")
    parts = plan.get("parts") or []
    if not isinstance(parts, list) or not parts:
        raise ValueError("plan.parts mancante o vuoto")
    if len(parts) > MAX_PARTS:
        warns.append(f"parts troncate da {len(parts)} a {MAX_PARTS}")
        parts = parts[:MAX_PARTS]
    seen_names: set[str] = set()
    norm_parts: list[dict] = []
    for p in parts:
        name = (p.get("name") or "").strip().lower()
        name = re.sub(r"[^a-z0-9_]+", "_", name).strip("_")
        if not name or name in seen_names:
            continue
        seen_names.add(name)
        norm_parts.append({"name": name, "description": (p.get("description") or "").strip()})
    if not norm_parts:
        raise ValueError("dopo normalizzazione, nessuna parte valida")
    plan["parts"] = norm_parts

    asm = plan.get("assembly") or []
    if not isinstance(asm, list):
        asm = []
    norm_asm: list[dict] = []
    valid_names = {p["name"] for p in norm_parts}
    for entry in asm:
        pn = (entry.get("part") or "").strip().lower()
        pn = re.sub(r"[^a-z0-9_]+", "_", pn).strip("_")
        if pn not in valid_names:
            warns.append(f"assembly entry per parte sconosciuta: {entry.get('part')!r}")
            continue
        t = entry.get("translate") or [0, 0, 0]
        r = entry.get("rotate") or [0, 0, 0]
        try:
            t = [float(x) for x in t][:3]
            r = [float(x) for x in r][:3]
        except (TypeError, ValueError):
            warns.append(f"translate/rotate non numerici per {pn}, uso zero")
            t, r = [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]
        while len(t) < 3: t.append(0.0)
        while len(r) < 3: r.append(0.0)
        norm_asm.append({"part": pn, "translate": t, "rotate": r})
    # Se la recipe è vuota, mettiamo le parti tutte all'origine (almeno la
    # prima istanza di ognuna), così l'utente vede qualcosa nell'assembly.
    if not norm_asm:
        warns.append("assembly vuoto, fallback: ogni parte all'origine")
        for p in norm_parts:
            norm_asm.append({"part": p["name"], "translate": [0, 0, 0], "rotate": [0, 0, 0]})
    plan["assembly"] = norm_asm

    shared = (plan.get("shared_scad") or "").strip() or "$fn = 64;"
    shared = _ensure_min_wall(shared)
    plan["shared_scad"] = shared
    plan["reasoning"] = (plan.get("reasoning") or "").strip()
    return plan, warns


_MIN_WALL_MM = 1.5  # tieni allineato con SCAD_PART_SYSTEM_PROMPT


def _ensure_min_wall(shared: str) -> str:
    """Garantisce che `shared_scad` contenga `min_wall` e che `wall` non scenda
    sotto soglia. Best-effort via regex su singoli assignment scalari.

    Casi:
    - manca `min_wall` → lo aggiunge in cima.
    - `min_wall` presente ma < soglia → riporta a soglia.
    - `wall` presente ma < `min_wall` → riporta a `min_wall`.
    """
    has_min_wall = re.search(r"(?m)^\s*min_wall\s*=", shared) is not None

    def _clamp_assignment(text: str, name: str, floor: float) -> str:
        pat = re.compile(rf"(?m)^(\s*{name}\s*=\s*)([0-9]*\.?[0-9]+)(\s*;.*)$")
        def repl(m: re.Match) -> str:
            try:
                v = float(m.group(2))
            except ValueError:
                return m.group(0)
            if v < floor:
                return f"{m.group(1)}{floor}{m.group(3)}"
            return m.group(0)
        return pat.sub(repl, text)

    if has_min_wall:
        shared = _clamp_assignment(shared, "min_wall", _MIN_WALL_MM)
    else:
        shared = f"min_wall = {_MIN_WALL_MM};  // hard floor FDM 0.4mm\n" + shared
    shared = _clamp_assignment(shared, "wall", _MIN_WALL_MM)
    return shared


# --- Claude calls ---


def _client_call(*, system: str, messages: list[dict], max_tokens: int = 4096):
    resp = prompt_refiner._client().messages.create(
        model=config.SCAD_MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=messages,
    )
    usage_tracker.record_response("scad", resp)
    return resp


def _claude_plan(
    idea: str,
    reference: Path | None,
    history: list[dict] | None = None,
    feedback: str | None = None,
) -> tuple[dict, str, dict]:
    """Ritorna (plan_normalizzato, raw_reply, usage_dict). Solleva RuntimeError
    se il JSON è irrecuperabile dopo un retry di fix."""
    intro = (
        f"Idea: {idea}\n\n"
        "Produci il piano di scomposizione secondo le regole del system."
    )
    intro_content: list[dict] = [{"type": "text", "text": intro}]
    if reference is not None:
        intro_content.append(prompt_refiner._image_block(reference))
    messages: list[dict] = [{"role": "user", "content": intro_content}]

    if history:
        for i, h in enumerate(history, start=1):
            messages.append({"role": "assistant", "content": json.dumps(h["plan"])})
            messages.append(
                {"role": "user", "content": f"(Iterazione {i} compilata.)"}
            )
    if feedback:
        messages.append(
            {
                "role": "user",
                "content": (
                    f"Feedback dell'utente sull'ultima iterazione: {feedback}\n"
                    "Rivedi il piano: stesse regole del system."
                ),
            }
        )

    resp = _client_call(system=SCAD_PLAN_SYSTEM_PROMPT, messages=messages, max_tokens=3072)
    reply = resp.content[0].text
    usage = _usage(resp)
    try:
        plan = _extract_json(reply)
        plan, _warns = _validate_plan(plan)
        return plan, reply, usage
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as e:
        fix_msgs = messages + [
            {"role": "assistant", "content": reply},
            {
                "role": "user",
                "content": (
                    f"La tua risposta non è un JSON valido secondo lo schema "
                    f"({e}). Restituisci SOLO il fence ```json ... ``` ben formato."
                ),
            },
        ]
        resp2 = _client_call(system=SCAD_PLAN_SYSTEM_PROMPT, messages=fix_msgs, max_tokens=3072)
        reply2 = resp2.content[0].text
        usage2 = _usage(resp2)
        plan = _extract_json(reply2)
        plan, _warns = _validate_plan(plan)
        return plan, reply2, {
            "input_tokens": usage["input_tokens"] + usage2["input_tokens"],
            "output_tokens": usage["output_tokens"] + usage2["output_tokens"],
        }


def _claude_part(
    *,
    idea: str,
    shared_scad: str,
    part_name: str,
    part_desc: str,
    other_parts: list[str],
    reference: Path | None,
    prior_code: str | None = None,
    prior_render: Path | None = None,
    feedback: str | None = None,
    use_previous_render: bool = True,
) -> tuple[str, str, dict]:
    """Genera (o raffina) il codice SCAD di UNA parte. Ritorna (reasoning, code, usage)."""
    intro_text = (
        f"Idea generale: {idea}\n\n"
        f"Stai generando la parte: **{part_name}** — {part_desc}\n\n"
        f"Le altre parti dell'assembly (per contesto): "
        f"{', '.join(other_parts) if other_parts else 'nessuna'}\n\n"
        f"Blocco di parametri CONDIVISI che devi incollare ESATTAMENTE in cima al file:\n"
        f"```openscad\n{shared_scad}\n```"
    )
    intro_content: list[dict] = [{"type": "text", "text": intro_text}]
    if reference is not None:
        intro_content.append(prompt_refiner._image_block(reference))
    messages: list[dict] = [{"role": "user", "content": intro_content}]

    if prior_code:
        messages.append(
            {"role": "assistant", "content": f"```openscad\n{prior_code}\n```"}
        )
        if use_previous_render and prior_render is not None and prior_render.exists():
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"Render attuale di {part_name}:"},
                        prompt_refiner._image_block(prior_render),
                    ],
                }
            )
        else:
            messages.append(
                {"role": "user", "content": f"({part_name} compilato, render non mostrato.)"}
            )

    if feedback:
        if use_previous_render and prior_render is not None and prior_render.exists():
            tail = (
                f"Feedback sull'attuale {part_name}: {feedback}\n"
                "Riscrivi il codice della parte tenendo conto del feedback e del "
                "render. Stesse regole del system."
            )
        else:
            tail = (
                f"Feedback sull'attuale {part_name}: {feedback}\n"
                "Riscrivi il codice solo sulla base del testo (non hai visto il "
                "render). Stesse regole del system."
            )
        messages.append({"role": "user", "content": [{"type": "text", "text": tail}]})

    resp = _client_call(system=SCAD_PART_SYSTEM_PROMPT, messages=messages, max_tokens=4096)
    reply = resp.content[0].text
    reasoning, code = _extract_scad_code(reply)
    return reasoning, code, _usage(resp)


def _claude_fix(prior_code: str, stderr: str) -> tuple[str, str, dict]:
    """Fix singolo turno dopo errore openscad. Stesso prompt del PART system."""
    messages = [
        {
            "role": "user",
            "content": (
                "Il codice OpenSCAD qui sotto ha prodotto un errore in compilazione "
                "(stderr di openscad CLI):\n\n"
                f"=== STDERR ===\n{stderr.strip()}\n\n"
                f"=== CODICE ===\n```openscad\n{prior_code}\n```\n\n"
                "Restituisci una versione CORRETTA del codice che risolve l'errore "
                "mantenendo l'intento. Stesse regole del system."
            ),
        }
    ]
    resp = _client_call(system=SCAD_PART_SYSTEM_PROMPT, messages=messages, max_tokens=4096)
    reply = resp.content[0].text
    _, code = _extract_scad_code(reply)
    return _, code, _usage(resp)


def _usage(resp) -> dict:
    u = getattr(resp, "usage", None)
    if u is None:
        return {"input_tokens": 0, "output_tokens": 0}
    return {
        "input_tokens": int(getattr(u, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(u, "output_tokens", 0) or 0),
    }


def _sum_usage(*usages: dict) -> dict:
    return {
        "input_tokens": sum(u.get("input_tokens", 0) for u in usages),
        "output_tokens": sum(u.get("output_tokens", 0) for u in usages),
    }


# --- OpenSCAD CLI runner ---


class ScadCompileError(RuntimeError):
    def __init__(self, attempts: int, errors: list[str]):
        super().__init__(
            f"OpenSCAD ha fallito dopo {attempts} tentativi.\n"
            + "\n---\n".join(errors[-2:])
        )
        self.attempts = attempts
        self.errors = errors


def _run_openscad(scad_path: Path, *, output: Path, kind: str) -> subprocess.CompletedProcess:
    cmd = [config.OPENSCAD_BIN, "-q", "-o", str(output)]
    if kind == "png":
        cmd += [
            f"--imgsize={RENDER_SIZE}",
            f"--colorscheme={RENDER_COLORSCHEME}",
            "--view=axes",
            "--projection=perspective",
            "--autocenter",
            "--viewall",
        ]
    cmd.append(str(scad_path))
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=OPENSCAD_TIMEOUT,
    )


def _stl_postprocess(stl_path: Path) -> tuple[Path | None, Path | None]:
    """Da uno STL produce un GLB (con materiale PLA arancione) e un 3MF
    (formato moderno, accettato da PrusaSlicer/Bambu Studio). Ritorna i path
    o None per ciò che fallisce."""
    import numpy as np
    import trimesh
    from trimesh.visual.material import PBRMaterial

    glb_path = stl_path.with_suffix(".glb")
    threemf_path = stl_path.with_suffix(".3mf")
    glb_out: Path | None = None
    threemf_out: Path | None = None
    try:
        mesh = trimesh.load(str(stl_path), force="mesh")
        material = PBRMaterial(
            name="pla_orange",
            baseColorFactor=[0.95, 0.45, 0.15, 1.0],
            metallicFactor=0.1,
            roughnessFactor=0.55,
        )
        uv = np.zeros((len(mesh.vertices), 2), dtype=np.float32)
        mesh.visual = trimesh.visual.TextureVisuals(uv=uv, material=material)
        mesh.export(str(glb_path), file_type="glb")
        glb_out = glb_path
    except Exception as e:
        print(f"[scad] STL→GLB FAIL ({stl_path.name}): {e}")
    try:
        mesh = trimesh.load(str(stl_path), force="mesh")
        mesh.export(str(threemf_path), file_type="3mf")
        threemf_out = threemf_path
    except Exception as e:
        print(f"[scad] STL→3MF FAIL ({stl_path.name}): {e}")
    return glb_out, threemf_out


def _stl_to_glb(stl_path: Path) -> Path | None:
    """Backwards-compat: solo GLB (per backfill script)."""
    glb, _ = _stl_postprocess(stl_path)
    return glb


def _compile_paths(
    scad_path: Path,
    stl_path: Path,
    png_path: Path,
    initial_code: str,
    *,
    on_step,
) -> tuple[Path, Path, int, list[str], Path | None, Path | None]:
    """Variante di _compile_and_render che accetta path espliciti.
    Ritorna (stl, png, attempts, errors, glb, threemf)."""
    scad_path.parent.mkdir(parents=True, exist_ok=True)
    stl_path.parent.mkdir(parents=True, exist_ok=True)
    png_path.parent.mkdir(parents=True, exist_ok=True)

    code = initial_code
    errors: list[str] = []
    fix_usages: list[dict] = []
    for attempt in range(1, MAX_FIX_ATTEMPTS + 1):
        scad_path.write_text(code, encoding="utf-8")
        on_step(f"{scad_path.parent.name}/{scad_path.stem} tentativo {attempt} — STL")
        try:
            r_stl = _run_openscad(scad_path, output=stl_path, kind="stl")
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            errors.append(f"[STL exec] {type(e).__name__}: {e}")
            raise ScadCompileError(attempt, errors) from e
        if r_stl.returncode != 0 or not stl_path.exists() or stl_path.stat().st_size == 0:
            err = (r_stl.stderr or r_stl.stdout or "(empty output)").strip()
            errors.append(f"[STL attempt {attempt}] {err}")
            if attempt >= MAX_FIX_ATTEMPTS:
                raise ScadCompileError(attempt, errors)
            on_step(f"errore STL, fix con Claude")
            _, code, u = _claude_fix(code, err)
            fix_usages.append(u)
            continue

        on_step(f"{scad_path.stem} — PNG render")
        try:
            r_png = _run_openscad(scad_path, output=png_path, kind="png")
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            errors.append(f"[PNG exec] {type(e).__name__}: {e}")
            glb_path, threemf_path = _stl_postprocess(stl_path)
            return stl_path, png_path, attempt, errors, glb_path, threemf_path
        if r_png.returncode != 0 or not png_path.exists():
            err = (r_png.stderr or r_png.stdout or "(empty output)").strip()
            errors.append(f"[PNG attempt {attempt}] {err}")
        glb_path, threemf_path = _stl_postprocess(stl_path)
        return stl_path, png_path, attempt, errors, glb_path, threemf_path

    raise ScadCompileError(MAX_FIX_ATTEMPTS, errors)


# --- Assembly synthesis ---


def _build_assembly_scad(parts: list[dict], recipe: list[dict]) -> str:
    """Sintetizza il testo di assembly.scad: import STL + translate/rotate."""
    lines = [
        "// Assembly auto-generato.",
        "// Compilabile con: openscad -o assembly.stl assembly.scad",
        "",
    ]
    valid = {p["name"] for p in parts}
    for entry in recipe:
        pn = entry["part"]
        if pn not in valid:
            continue
        t = entry.get("translate", [0, 0, 0])
        r = entry.get("rotate", [0, 0, 0])
        line = (
            f"translate([{t[0]}, {t[1]}, {t[2]}]) "
            f"rotate([{r[0]}, {r[1]}, {r[2]}]) "
            f'import("parts/{pn}.stl");'
        )
        lines.append(line)
    return "\n".join(lines) + "\n"


def _build_assembly_glb(
    iter_dir: Path, parts: list[dict], recipe: list[dict],
    *, exploded: bool = False, out_name: str = "assembly.glb",
    explode_factor: float = 0.6,
) -> Path | None:
    """Compone l'assembly GLB. Se `exploded=True`, sposta ogni istanza lungo il
    vettore dal centro del montaggio verso la sua posizione, di una distanza
    proporzionale al diametro del bbox dell'assembly (`explode_factor`)."""
    import numpy as np
    import trimesh

    out = iter_dir / out_name
    try:
        # Pre-calcola il centroide della recipe per esploso radiale.
        valid_entries = [
            e for e in recipe
            if (iter_dir / "parts" / f"{e['part']}.glb").exists()
        ]
        if not valid_entries:
            return None
        positions = np.array([e.get("translate", [0, 0, 0]) for e in valid_entries], dtype=float)
        center = positions.mean(axis=0) if len(positions) > 1 else np.zeros(3)
        spread = float(np.linalg.norm(positions.max(axis=0) - positions.min(axis=0)))
        # Distanza di esplosione: max tra spread e 50mm di default.
        explode_dist = max(spread, 50.0) * explode_factor if exploded else 0.0

        scene = trimesh.Scene()
        for entry in valid_entries:
            pn = entry["part"]
            glb = iter_dir / "parts" / f"{pn}.glb"
            geom = trimesh.load(str(glb), force="mesh")
            t = np.array(entry.get("translate", [0, 0, 0]), dtype=float)
            r = entry.get("rotate", [0, 0, 0])
            if exploded:
                direction = t - center
                norm = float(np.linalg.norm(direction))
                if norm > 1e-6:
                    t = t + (direction / norm) * explode_dist
                else:
                    # parte sul centro: alza in Z per separazione visiva.
                    t = t + np.array([0, 0, explode_dist * 0.5])
            transform = trimesh.transformations.euler_matrix(
                np.radians(r[0]), np.radians(r[1]), np.radians(r[2]), axes="sxyz",
            )
            transform[:3, 3] = t
            scene.add_geometry(geom, transform=transform, node_name=f"{pn}_{id(entry)}")
        scene.export(str(out), file_type="glb")
        return out
    except Exception as e:
        print(f"[scad] assembly GLB FAIL ({out_name}): {e}")
        return None


def _build_bed_packing_png(
    iter_dir: Path, part_records: list[dict], *, bed_mm: tuple[int, int] = (220, 220),
) -> Path | None:
    """Genera un PNG che mostra il piatto stampante con le parti disposte via
    shelf-packing semplice. Annota se una parte non entra."""
    try:
        import colorsys
        from PIL import Image, ImageDraw, ImageFont
        import trimesh
    except ImportError as e:
        print(f"[scad] bed packing: import fallito ({e})")
        return None

    out = iter_dir / "bed.png"
    margin_mm = 5  # clearance tra parti
    bed_w, bed_h = bed_mm
    px_per_mm = 3
    img_w = bed_w * px_per_mm + 60
    img_h = bed_h * px_per_mm + 60
    img = Image.new("RGB", (img_w, img_h), (245, 245, 244))
    draw = ImageDraw.Draw(img)
    # Bed
    bed_x0, bed_y0 = 30, 30
    bed_x1 = bed_x0 + bed_w * px_per_mm
    bed_y1 = bed_y0 + bed_h * px_per_mm
    draw.rectangle([bed_x0, bed_y0, bed_x1, bed_y1], fill=(230, 230, 228), outline=(120, 120, 120), width=2)
    # Griglia ogni 50 mm
    for x in range(0, bed_w + 1, 50):
        px = bed_x0 + x * px_per_mm
        draw.line([(px, bed_y0), (px, bed_y1)], fill=(200, 200, 200), width=1)
    for y in range(0, bed_h + 1, 50):
        py = bed_y0 + y * px_per_mm
        draw.line([(bed_x0, py), (bed_x1, py)], fill=(200, 200, 200), width=1)

    # Raccoglie bbox 2D delle parti dai relativi STL
    items = []
    for i, p in enumerate(part_records):
        if not p.get("stl_path"):
            continue
        stl_full = iter_dir.parent / p["stl_path"]
        if not stl_full.exists():
            continue
        try:
            mesh = trimesh.load(str(stl_full), force="mesh")
            bbox = mesh.bounds[1] - mesh.bounds[0]
            w_mm = float(bbox[0]) + margin_mm
            h_mm = float(bbox[1]) + margin_mm
            items.append({"name": p["name"], "w": w_mm, "h": h_mm, "idx": i})
        except Exception:
            continue
    # Shelf packing: ordina per altezza desc, riempi righe
    items.sort(key=lambda x: x["h"], reverse=True)
    placements = []
    cur_x, cur_y, shelf_h = 0.0, 0.0, 0.0
    unplaced = []
    for it in items:
        if it["w"] > bed_w or it["h"] > bed_h:
            unplaced.append(it["name"])
            continue
        if cur_x + it["w"] > bed_w:
            cur_x = 0.0
            cur_y += shelf_h
            shelf_h = 0.0
        if cur_y + it["h"] > bed_h:
            unplaced.append(it["name"])
            continue
        placements.append((it, cur_x, cur_y))
        cur_x += it["w"]
        shelf_h = max(shelf_h, it["h"])

    # Disegna placements colorati
    for (it, x, y) in placements:
        hue = (it["idx"] * 0.18) % 1.0
        rgb = tuple(int(c * 255) for c in colorsys.hsv_to_rgb(hue, 0.45, 0.92))
        px0 = bed_x0 + int(x * px_per_mm)
        py0 = bed_y0 + int(y * px_per_mm)
        px1 = bed_x0 + int((x + it["w"] - margin_mm) * px_per_mm)
        py1 = bed_y0 + int((y + it["h"] - margin_mm) * px_per_mm)
        draw.rectangle([px0, py0, px1, py1], fill=rgb, outline=(80, 80, 80), width=1)
        try:
            draw.text((px0 + 4, py0 + 4), it["name"], fill=(30, 30, 30))
        except Exception:
            pass

    # Header text
    bed_label = f"piatto {bed_w}×{bed_h} mm — {len(placements)}/{len(items)} parti"
    if unplaced:
        bed_label += f" — non entrano: {', '.join(unplaced)}"
    try:
        draw.text((bed_x0, 6), bed_label, fill=(60, 60, 60))
    except Exception:
        pass

    img.save(out, format="PNG")
    return out


def _try_render_assembly_png(iter_dir: Path) -> Path | None:
    """Compila assembly.scad → PNG via openscad. Best-effort: None se fallisce."""
    scad = iter_dir / "assembly.scad"
    png = iter_dir / "assembly.png"
    if not scad.exists():
        return None
    try:
        r = _run_openscad(scad, output=png, kind="png")
        if r.returncode == 0 and png.exists():
            return png
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


# --- Public API ---


def create_scad_session(idea: str) -> Path:
    SCAD_ROOT.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    sdir = SCAD_ROOT / f"{ts}-{storage._slugify(idea or 'scad')}"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "prompts").mkdir(parents=True, exist_ok=True)
    (sdir / "prompts" / "00_idea.txt").write_text(idea, encoding="utf-8")
    meta = {
        "created_at": ts,
        "idea": idea,
        "reference": None,
        "iterations": [],
        "accepted_iteration": None,
        "status": "draft",
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }
    _write_meta(sdir, meta)
    return sdir


def save_reference(scad_dir: Path, src_path: Path) -> Path:
    ext = src_path.suffix.lower() or ".png"
    dst = scad_dir / f"reference{ext}"
    shutil.copy2(src_path, dst)
    update_metadata(scad_dir, reference=dst.name)
    return dst


def _iter_dir(scad_dir: Path, version: int) -> Path:
    return scad_dir / f"iter_{version:02d}"


def _generate_full(
    scad_dir: Path,
    version: int,
    plan: dict,
    *,
    reference: Path | None,
    idea: str,
    on_step,
    on_progress=None,
) -> tuple[dict, dict]:
    """Genera tutte le parti in parallelo e l'assembly. Ritorna (iter_record, usage)."""
    iter_dir = _iter_dir(scad_dir, version)
    iter_dir.mkdir(parents=True, exist_ok=True)
    (iter_dir / "parts").mkdir(parents=True, exist_ok=True)

    # Persist shared + plan
    (iter_dir / "shared.scad").write_text(plan["shared_scad"], encoding="utf-8")
    (iter_dir / "plan.json").write_text(
        json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    parts = plan["parts"]
    part_names = [p["name"] for p in parts]
    if on_progress:
        on_progress(20, f"Claude → {len(parts)} parti in parallelo")

    def _run_part(p: dict) -> tuple[dict, dict]:
        other = [n for n in part_names if n != p["name"]]
        on_step(f"part {p['name']} — Claude code")
        reasoning, code, usage = _claude_part(
            idea=idea, shared_scad=plan["shared_scad"],
            part_name=p["name"], part_desc=p["description"],
            other_parts=other, reference=reference,
        )
        if not code:
            raise RuntimeError(f"Claude non ha prodotto codice per parte {p['name']}")
        scad_p = iter_dir / "parts" / f"{p['name']}.scad"
        stl_p  = iter_dir / "parts" / f"{p['name']}.stl"
        png_p  = iter_dir / "parts" / f"{p['name']}.png"
        try:
            stl_path, png_path, attempts, errors, glb_path, threemf_path = _compile_paths(
                scad_p, stl_p, png_p, code, on_step=on_step,
            )
            on_step(f"{p['name']} — validazione mesh")
            validation = mesh_validator.validate(stl_path) if stl_path else None
            # Se pymeshfix ha sistemato lo STL, ri-esporta GLB+3MF dal nuovo.
            if validation and validation.get("auto_fixed"):
                glb_path, threemf_path = _stl_postprocess(stl_path)
            rec = {
                "name": p["name"],
                "description": p["description"],
                "reasoning": reasoning,
                "scad_path": str(scad_p.relative_to(scad_dir)),
                "stl_path": str(stl_path.relative_to(scad_dir)) if stl_path else None,
                "render_path": str(png_path.relative_to(scad_dir)) if png_path.exists() else None,
                "glb_path": str(glb_path.relative_to(scad_dir)) if glb_path else None,
                "threemf_path": str(threemf_path.relative_to(scad_dir)) if threemf_path else None,
                "hero_path": None,
                "validation": validation,
                "claude_attempts": attempts,
                "errors": errors,
            }
            return rec, usage
        except ScadCompileError as e:
            rec = {
                "name": p["name"],
                "description": p["description"],
                "reasoning": reasoning,
                "scad_path": str(scad_p.relative_to(scad_dir)),
                "stl_path": None, "render_path": None, "glb_path": None,
                "threemf_path": None, "hero_path": None, "validation": None,
                "claude_attempts": e.attempts,
                "errors": e.errors,
            }
            return rec, usage

    with ThreadPoolExecutor(
        max_workers=min(MAX_PARALLEL_PARTS, max(1, len(parts))),
        thread_name_prefix="scad-part",
    ) as ex:
        results = list(ex.map(_run_part, parts))

    part_records = [r[0] for r in results]
    parts_usage = _sum_usage(*[r[1] for r in results])

    # Assembly
    if on_progress:
        on_progress(80, "assembly preview")
    on_step("assembly — sintesi e GLB")
    (iter_dir / "assembly.scad").write_text(
        _build_assembly_scad(parts, plan["assembly"]), encoding="utf-8"
    )
    glb_asm = _build_assembly_glb(iter_dir, parts, plan["assembly"])
    glb_asm_exploded = _build_assembly_glb(
        iter_dir, parts, plan["assembly"], exploded=True,
        out_name="assembly_exploded.glb",
    )
    png_asm = _try_render_assembly_png(iter_dir)
    bed_png = _build_bed_packing_png(iter_dir, part_records)

    # Hero render Blender (per-parte + assembly) se disponibile. In parallelo
    # con max 2 worker per non saturare RAM.
    hero_paths: dict[str, Path | None] = {}
    if blender_render.is_available():
        if on_progress:
            on_progress(88, "Blender hero render")
        on_step("blender — hero render in parallelo")
        blender_render.ensure_hdr()
        def _render(p_rec: dict) -> tuple[str, Path | None]:
            stl = scad_dir / p_rec["stl_path"] if p_rec.get("stl_path") else None
            if stl is None or not stl.exists():
                return p_rec["name"], None
            out = iter_dir / "parts" / f"{p_rec['name']}_hero.png"
            ok = blender_render.render_hero(stl, out)
            return p_rec["name"], (out if ok else None)
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="scad-hero") as ex:
            for name, path in ex.map(_render, part_records):
                hero_paths[name] = path
        # Assembly hero (su STL composito generato da openscad assembly.scad)
        asm_src = png_asm  # fallback se openscad assembly.scad render manca → skip
        # In assenza di STL composito comodo, renderizziamo direttamente il GLB
        glb_in = iter_dir / "assembly.glb"
        if glb_in.exists():
            out_asm = iter_dir / "assembly_hero.png"
            if blender_render.render_hero(glb_in, out_asm):
                hero_paths["__assembly__"] = out_asm
        for rec in part_records:
            h = hero_paths.get(rec["name"])
            if h:
                rec["hero_path"] = str(h.relative_to(scad_dir))

    iter_record = {
        "version": version,
        "kind": "full",
        "changed_part": None,
        "reasoning": plan["reasoning"],
        "shared_scad_path": str((iter_dir / "shared.scad").relative_to(scad_dir)),
        "plan_path": str((iter_dir / "plan.json").relative_to(scad_dir)),
        "parts": part_records,
        "assembly": {
            "scad_path": str((iter_dir / "assembly.scad").relative_to(scad_dir)),
            "glb_path": str(glb_asm.relative_to(scad_dir)) if glb_asm else None,
            "exploded_glb_path": str(glb_asm_exploded.relative_to(scad_dir)) if glb_asm_exploded else None,
            "render_path": str(png_asm.relative_to(scad_dir)) if png_asm else None,
            "hero_path": str(hero_paths["__assembly__"].relative_to(scad_dir))
                if hero_paths.get("__assembly__") else None,
            "bed_png_path": str(bed_png.relative_to(scad_dir)) if bed_png else None,
            "recipe": plan["assembly"],
        },
    }
    return iter_record, parts_usage


def generate_initial(
    scad_dir: Path,
    *,
    on_progress=None,
    on_step=None,
    api_key: str | None = None,
) -> Path:
    meta = load_metadata(scad_dir)
    idea = meta["idea"]
    reference = scad_dir / meta["reference"] if meta.get("reference") else None

    def _step(s: str) -> None:
        print(f"[scad] {s}")
        if on_step: on_step(s)

    if on_progress: on_progress(5, "Claude → piano scomposizione")
    _step("plan → Claude")
    plan, _raw, plan_usage = _claude_plan(idea, reference)
    _step(f"plan: {len(plan['parts'])} parti = {', '.join(p['name'] for p in plan['parts'])}")

    iter_record, parts_usage = _generate_full(
        scad_dir, version=1, plan=plan,
        reference=reference, idea=idea, on_step=_step, on_progress=on_progress,
    )

    _append_iteration_record(scad_dir, iter_record)
    _add_usage(scad_dir, _sum_usage(plan_usage, parts_usage))
    update_metadata(scad_dir, status="draft")
    if on_progress: on_progress(100, "done")
    # Path "qualcosa" da ritornare per compat con la API esistente.
    asm = iter_record["assembly"].get("glb_path") or iter_record["assembly"].get("scad_path")
    return scad_dir / asm if asm else scad_dir


def continue_with_feedback(
    scad_dir: Path,
    feedback: str,
    *,
    use_previous_render: bool = True,
    on_progress=None,
    on_step=None,
    api_key: str | None = None,
) -> Path:
    """Refine globale: nuovo plan + tutte le parti + assembly."""
    meta = load_metadata(scad_dir)
    if not meta.get("iterations"):
        raise RuntimeError("Nessuna iterazione di partenza.")
    if meta.get("accepted_iteration") is not None:
        raise RuntimeError("Sessione già accettata.")

    idea = meta["idea"]
    reference = scad_dir / meta["reference"] if meta.get("reference") else None

    def _step(s: str) -> None:
        print(f"[scad] {s}")
        if on_step: on_step(s)

    # History per il plan: solo i plan precedenti (no immagini).
    history: list[dict] = []
    for it in meta["iterations"]:
        if not it.get("plan_path"):
            continue
        try:
            history.append(
                {"plan": json.loads((scad_dir / it["plan_path"]).read_text(encoding="utf-8"))}
            )
        except OSError:
            pass

    if on_progress: on_progress(5, "Claude → re-plan")
    _step("re-plan con feedback")
    plan, _raw, plan_usage = _claude_plan(idea, reference, history=history, feedback=feedback)
    _step(f"plan: {len(plan['parts'])} parti")

    next_version = meta["iterations"][-1]["version"] + 1
    iter_record, parts_usage = _generate_full(
        scad_dir, version=next_version, plan=plan,
        reference=reference, idea=idea, on_step=_step, on_progress=on_progress,
    )
    _append_iteration_record(scad_dir, iter_record)
    _add_usage(scad_dir, _sum_usage(plan_usage, parts_usage))
    if on_progress: on_progress(100, "done")
    asm = iter_record["assembly"].get("glb_path") or iter_record["assembly"].get("scad_path")
    return scad_dir / asm if asm else scad_dir


def continue_with_part_feedback(
    scad_dir: Path,
    part_name: str,
    feedback: str,
    *,
    use_previous_render: bool = True,
    on_progress=None,
    on_step=None,
    api_key: str | None = None,
) -> Path:
    """Refine puntuale: rigenera SOLO la parte target, copia le altre."""
    meta = load_metadata(scad_dir)
    if not meta.get("iterations"):
        raise RuntimeError("Nessuna iterazione di partenza.")
    if meta.get("accepted_iteration") is not None:
        raise RuntimeError("Sessione già accettata.")

    prev = meta["iterations"][-1]
    if not prev.get("parts"):
        raise RuntimeError("Iterazione corrente è legacy (no parts): usa refine globale.")
    part = next((p for p in prev["parts"] if p["name"] == part_name), None)
    if part is None:
        raise RuntimeError(f"Parte sconosciuta: {part_name}")

    idea = meta["idea"]
    reference = scad_dir / meta["reference"] if meta.get("reference") else None
    shared_scad = (scad_dir / prev["shared_scad_path"]).read_text(encoding="utf-8")
    other_parts = [p["name"] for p in prev["parts"] if p["name"] != part_name]
    prior_code = (scad_dir / part["scad_path"]).read_text(encoding="utf-8") if part.get("scad_path") else None
    prior_render = scad_dir / part["render_path"] if part.get("render_path") else None

    def _step(s: str) -> None:
        print(f"[scad] {s}")
        if on_step: on_step(s)

    next_version = prev["version"] + 1
    iter_dir = _iter_dir(scad_dir, next_version)
    iter_dir.mkdir(parents=True, exist_ok=True)
    (iter_dir / "parts").mkdir(parents=True, exist_ok=True)

    # Copia shared + plan dall'iter precedente.
    shutil.copy2(scad_dir / prev["shared_scad_path"], iter_dir / "shared.scad")
    if prev.get("plan_path"):
        shutil.copy2(scad_dir / prev["plan_path"], iter_dir / "plan.json")
    # Copia tutte le altre parti (incluso 3MF e hero render)
    for p in prev["parts"]:
        if p["name"] == part_name: continue
        for key, ext in (("scad_path", ".scad"), ("stl_path", ".stl"),
                          ("glb_path", ".glb"), ("render_path", ".png"),
                          ("threemf_path", ".3mf"), ("hero_path", "_hero.png")):
            src_rel = p.get(key)
            if not src_rel: continue
            src = scad_dir / src_rel
            if not src.exists(): continue
            dst_name = f"{p['name']}{ext}" if not ext.startswith("_") else f"{p['name']}{ext}"
            dst = iter_dir / "parts" / dst_name
            shutil.copy2(src, dst)

    if on_progress: on_progress(20, f"Claude refine '{part_name}'")
    _step(f"refine part: {part_name}")
    reasoning, code, part_usage = _claude_part(
        idea=idea, shared_scad=shared_scad,
        part_name=part_name, part_desc=part.get("description", ""),
        other_parts=other_parts, reference=reference,
        prior_code=prior_code, prior_render=prior_render,
        feedback=feedback, use_previous_render=use_previous_render,
    )
    if not code:
        raise RuntimeError("Claude non ha prodotto codice estraibile per la parte.")

    scad_p = iter_dir / "parts" / f"{part_name}.scad"
    stl_p  = iter_dir / "parts" / f"{part_name}.stl"
    png_p  = iter_dir / "parts" / f"{part_name}.png"
    new_part_rec: dict
    try:
        stl_path, png_path, attempts, errors, glb_path, threemf_path = _compile_paths(
            scad_p, stl_p, png_p, code, on_step=_step,
        )
        validation = mesh_validator.validate(stl_path) if stl_path else None
        if validation and validation.get("auto_fixed"):
            glb_path, threemf_path = _stl_postprocess(stl_path)
        hero_path: Path | None = None
        if blender_render.is_available() and stl_path:
            blender_render.ensure_hdr()
            out_hero = iter_dir / "parts" / f"{part_name}_hero.png"
            if blender_render.render_hero(stl_path, out_hero):
                hero_path = out_hero
        new_part_rec = {
            "name": part_name,
            "description": part.get("description", ""),
            "reasoning": reasoning,
            "scad_path": str(scad_p.relative_to(scad_dir)),
            "stl_path": str(stl_path.relative_to(scad_dir)) if stl_path else None,
            "render_path": str(png_path.relative_to(scad_dir)) if png_path.exists() else None,
            "glb_path": str(glb_path.relative_to(scad_dir)) if glb_path else None,
            "threemf_path": str(threemf_path.relative_to(scad_dir)) if threemf_path else None,
            "hero_path": str(hero_path.relative_to(scad_dir)) if hero_path else None,
            "validation": validation,
            "claude_attempts": attempts,
            "errors": errors,
        }
    except ScadCompileError as e:
        new_part_rec = {
            "name": part_name, "description": part.get("description", ""),
            "reasoning": reasoning,
            "scad_path": str(scad_p.relative_to(scad_dir)),
            "stl_path": None, "render_path": None, "glb_path": None,
            "threemf_path": None, "hero_path": None, "validation": None,
            "claude_attempts": e.attempts, "errors": e.errors,
        }

    # Compose parts list mantenendo ordine originale ma sostituendo la parte target.
    new_parts = []
    for p in prev["parts"]:
        if p["name"] == part_name:
            new_parts.append(new_part_rec)
        else:
            np_rec = dict(p)
            for key, ext in (("scad_path", ".scad"), ("stl_path", ".stl"),
                              ("glb_path", ".glb"), ("render_path", ".png"),
                              ("threemf_path", ".3mf"), ("hero_path", "_hero.png")):
                if p.get(key):
                    np_rec[key] = str((iter_dir / "parts" / f"{p['name']}{ext}").relative_to(scad_dir))
            new_parts.append(np_rec)

    if on_progress: on_progress(80, "assembly preview")
    _step("assembly — sintesi e GLB")
    recipe = prev.get("assembly", {}).get("recipe") or []
    parts_meta_for_asm = [{"name": p["name"]} for p in new_parts]
    (iter_dir / "assembly.scad").write_text(
        _build_assembly_scad(parts_meta_for_asm, recipe), encoding="utf-8"
    )
    glb_asm = _build_assembly_glb(iter_dir, parts_meta_for_asm, recipe)
    glb_asm_exploded = _build_assembly_glb(
        iter_dir, parts_meta_for_asm, recipe, exploded=True,
        out_name="assembly_exploded.glb",
    )
    png_asm = _try_render_assembly_png(iter_dir)
    bed_png = _build_bed_packing_png(iter_dir, new_parts)
    hero_asm = None
    if blender_render.is_available() and glb_asm and glb_asm.exists():
        out_asm = iter_dir / "assembly_hero.png"
        if blender_render.render_hero(glb_asm, out_asm):
            hero_asm = out_asm

    iter_record = {
        "version": next_version,
        "kind": "partial",
        "changed_part": part_name,
        "reasoning": f"Rigenerata solo parte '{part_name}' con feedback utente.",
        "shared_scad_path": str((iter_dir / "shared.scad").relative_to(scad_dir)),
        "plan_path": prev.get("plan_path"),
        "parts": new_parts,
        "assembly": {
            "scad_path": str((iter_dir / "assembly.scad").relative_to(scad_dir)),
            "glb_path": str(glb_asm.relative_to(scad_dir)) if glb_asm else None,
            "exploded_glb_path": str(glb_asm_exploded.relative_to(scad_dir)) if glb_asm_exploded else None,
            "render_path": str(png_asm.relative_to(scad_dir)) if png_asm else None,
            "hero_path": str(hero_asm.relative_to(scad_dir)) if hero_asm else None,
            "bed_png_path": str(bed_png.relative_to(scad_dir)) if bed_png else None,
            "recipe": recipe,
        },
    }
    _append_iteration_record(scad_dir, iter_record)
    _add_usage(scad_dir, part_usage)
    if on_progress: on_progress(100, "done")
    return scad_dir / iter_record["assembly"].get("glb_path", "")


def accept(scad_dir: Path) -> None:
    meta = load_metadata(scad_dir)
    if not meta.get("iterations"):
        raise RuntimeError("Niente da accettare.")
    last = meta["iterations"][-1]
    update_metadata(scad_dir, accepted_iteration=last["version"], status="done")


def list_scad_sessions() -> list[dict]:
    if not SCAD_ROOT.exists():
        return []
    out: list[dict] = []
    for d in SCAD_ROOT.iterdir():
        if not d.is_dir() or not (d / "metadata.json").exists():
            continue
        try:
            meta = load_metadata(d)
        except Exception:
            continue
        thumb = None
        if meta.get("iterations"):
            last = meta["iterations"][-1]
            # nuovo schema → assembly.render_path, poi prima parte, poi legacy.
            if last.get("assembly"):
                thumb = last["assembly"].get("render_path")
            if not thumb and last.get("parts"):
                for p in last["parts"]:
                    if p.get("render_path"):
                        thumb = p["render_path"]
                        break
            thumb = thumb or last.get("render_path")
        out.append({
            "id": d.name,
            "idea": meta.get("idea", ""),
            "created_at": meta.get("created_at", ""),
            "iterations": len(meta.get("iterations", [])),
            "status": meta.get("status", "draft"),
            "accepted": meta.get("accepted_iteration"),
            "thumb": thumb,
            "cost_usd": usage_tracker.session_cost_scad(meta),
        })
    out.sort(key=lambda x: x["created_at"], reverse=True)
    return out


def resolve_scad(scad_id: str) -> Path:
    if "/" in scad_id or ".." in scad_id or scad_id.startswith("."):
        raise ValueError("scad_id non valido")
    p = (SCAD_ROOT / scad_id).resolve()
    p.relative_to(SCAD_ROOT.resolve())
    if not p.exists() or not (p / "metadata.json").exists():
        raise FileNotFoundError(scad_id)
    return p


# --- metadata I/O ---


def _write_meta(scad_dir: Path, data: dict) -> None:
    path = scad_dir / "metadata.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def load_metadata(scad_dir: Path) -> dict:
    with _get_lock(scad_dir):
        return json.loads((scad_dir / "metadata.json").read_text(encoding="utf-8"))


def update_metadata(scad_dir: Path, **patch) -> None:
    with _get_lock(scad_dir):
        data = json.loads((scad_dir / "metadata.json").read_text(encoding="utf-8"))
        data.update(patch)
        _write_meta(scad_dir, data)


def _append_iteration_record(scad_dir: Path, record: dict) -> None:
    with _get_lock(scad_dir):
        data = json.loads((scad_dir / "metadata.json").read_text(encoding="utf-8"))
        data["iterations"].append(record)
        _write_meta(scad_dir, data)


def _add_usage(scad_dir: Path, usage: dict) -> None:
    with _get_lock(scad_dir):
        data = json.loads((scad_dir / "metadata.json").read_text(encoding="utf-8"))
        cur = data.get("usage") or {"input_tokens": 0, "output_tokens": 0}
        cur["input_tokens"] += int(usage.get("input_tokens", 0))
        cur["output_tokens"] += int(usage.get("output_tokens", 0))
        data["usage"] = cur
        _write_meta(scad_dir, data)
