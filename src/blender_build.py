"""Helper deterministici per la pipeline "Blender-build".

La pipeline gira dentro Claude Code (vedi `.claude/skills/blender-build/`): un
planner Opus produce `plan.json`, poi executor Sonnet in sequenza applicano ogni
step in Blender via MCP (`execute_code`) e salvano uno snapshot `.blend` per
modifica. Questo modulo tiene SOLO le parti deterministiche che non devono stare
in mano all'LLM: bootstrap della build session, copia degli input dalla sessione
Oggetti sorgente, e append atomico di piano/step su `metadata.json`.

Lo snapshot `.blend` e gli export GLB/STL li fa l'executor dentro Blender (servono
`bpy`), usando i path assoluti calcolati da `step_paths()` / `final_paths()`.
"""

import json
import os
import shutil
from datetime import datetime
from pathlib import Path

from . import config, image_lenses, storage

BLENDER_OUTPUTS_ROOT = config.OUTPUTS_ROOT / "blender"

# Gate di convergenza (vedi skill blender-build §4): soglia di accettazione del
# punteggio complessivo input↔output e numero massimo di iterazioni oltre il quale
# la raccomandazione è sempre "stop" (evita rifiniture all'infinito). Costanti qui
# così sono facili da ritoccare senza toccare la logica.
ACCEPT_THRESHOLD = 85
MAX_ITERATIONS = 2

# Quadranti del grid 2x2 prodotti dalla pipeline Oggetti (vedi src/grid_utils.py
# GRID_QUADRANTS). Sono le 4 viste di riferimento per la ricostruzione.
VIEW_NAMES = ("front", "three_quarter", "side", "back")

# Asse viewport di Blender (bpy.ops.view3d.view_axis) associato a ogni vista, per
# orientare lo screenshot di verifica. "three_quarter" non ha un asse canonico:
# l'executor userà una vista orbitata, qui ripieghiamo su FRONT come hint.
VIEW_AXIS = {
    "front": "FRONT",
    "three_quarter": "FRONT",
    "side": "RIGHT",
    "back": "BACK",
}


def _patch_metadata(build_dir: Path, mutate) -> dict:
    """Read-modify-write atomico su metadata.json, sotto il lock per-sessione di
    storage (stesso pattern di storage.update_metadata: write su .tmp + os.replace).
    `mutate(data)` modifica il dict in place; ritorna il dict aggiornato.
    """
    path = build_dir / "metadata.json"
    tmp = path.with_suffix(".json.tmp")
    with storage._get_lock(build_dir):
        data = json.loads(path.read_text(encoding="utf-8"))
        mutate(data)
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    return data


def _find_view_crops(source_dir: Path, meta: dict) -> dict[str, Path]:
    """Risolve i 4 crop del grid 2x2 nella sessione Oggetti sorgente.

    Preferisce i path dichiarati nell'iterazione accettata (o l'ultima), poi
    ripiega sul naming `images/NN_grid_<quadrant>.png` cercando la versione più
    recente. Solleva FileNotFoundError se manca anche una sola vista.
    """
    iters = meta.get("iterations") or []
    accepted = meta.get("accepted_iteration")
    chosen = None
    if accepted is not None:
        chosen = next((it for it in iters if it.get("version") == accepted), None)
    if chosen is None and iters:
        chosen = max(iters, key=lambda it: it.get("version", 0))

    views: dict[str, Path] = {}
    # 1) path espliciti dichiarati in metadata (image_paths_grid: lista ordinata
    #    front/three_quarter/side/back come GRID_QUADRANTS).
    declared = (chosen or {}).get("image_paths_grid") or []
    if len(declared) == len(VIEW_NAMES):
        for name, rel in zip(VIEW_NAMES, declared):
            p = source_dir / rel
            if p.exists():
                views[name] = p

    # 2) fallback: glob per quadrante, versione più alta.
    for name in VIEW_NAMES:
        if name in views:
            continue
        candidates = sorted(source_dir.glob(f"images/*_grid_{name}.png"))
        if candidates:
            views[name] = candidates[-1]

    missing = [n for n in VIEW_NAMES if n not in views]
    if missing:
        raise FileNotFoundError(
            f"Viste mancanti nella sessione sorgente {source_dir.name}: "
            f"{', '.join(missing)}. Serve una sessione Oggetti con grid 2x2 generato."
        )
    return views


def load_source_inputs(source_dir: Path) -> dict:
    """Estrae descrizione + 4 viste da una sessione Oggetti esistente.

    Ritorna {prompt, views: {front, three_quarter, side, back}, version,
    source_session}. Non copia nulla: i path puntano alla sessione sorgente.
    """
    source_dir = source_dir.resolve()
    meta = storage.load_metadata(source_dir)
    views = _find_view_crops(source_dir, meta)

    iters = meta.get("iterations") or []
    accepted = meta.get("accepted_iteration")
    chosen = None
    if accepted is not None:
        chosen = next((it for it in iters if it.get("version") == accepted), None)
    if chosen is None and iters:
        chosen = max(iters, key=lambda it: it.get("version", 0))
    version = (chosen or {}).get("version", 1)

    prompt = (chosen or {}).get("prompt")
    if not prompt:
        refined = sorted(source_dir.glob("prompts/*_refined.txt"))
        if refined:
            prompt = refined[-1].read_text(encoding="utf-8").strip()
    if not prompt:
        prompt = meta.get("idea", "")

    return {
        "prompt": prompt,
        "views": views,
        "version": version,
        "source_session": source_dir.name,
    }


def init_build_session(source_dir: Path) -> Path:
    """Crea outputs/blender/<ts-slug>/, copia le 4 viste in refs/, scrive
    prompts/00_source.txt e lo skeleton di metadata.json. Ritorna la build dir.
    """
    src = load_source_inputs(source_dir)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = storage._slugify(src["prompt"] or src["source_session"])
    build_dir = BLENDER_OUTPUTS_ROOT / f"{ts}-{slug}"
    (build_dir / "prompts").mkdir(parents=True, exist_ok=True)
    (build_dir / "refs").mkdir(parents=True, exist_ok=True)
    (build_dir / "steps").mkdir(parents=True, exist_ok=True)

    view_rel: dict[str, str] = {}
    copied: dict[str, Path] = {}
    for name, p in src["views"].items():
        dst = build_dir / "refs" / f"{name}.png"
        shutil.copy2(p, dst)
        view_rel[name] = f"refs/{name}.png"
        copied[name] = dst

    # Lenti percettive (edges/silhouette/color) delle 4 viste: il parser §1 le
    # legge accanto all'originale per un manifest più accurato (vedi image_lenses).
    ref_lenses = _rel_lenses(
        build_dir,
        image_lenses.generate_lenses(copied, build_dir / "refs" / "lenses"),
    )

    (build_dir / "prompts" / "00_source.txt").write_text(
        f"# source session: {src['source_session']} (iter {src['version']})\n\n"
        f"{src['prompt']}\n",
        encoding="utf-8",
    )

    metadata = {
        "created_at": ts,
        "kind": "blender_build",
        "status": "parsing",
        "source_session": src["source_session"],
        "prompt": src["prompt"],
        "manifest_path": "manifest.json",
        "plan_path": "plan.json",
        "views": view_rel,
        "ref_lenses": ref_lenses,
        "steps": [],
        "iteration": 1,
        "evaluations": [],
        "claude_usage": {"by_model": {}},
    }
    (build_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return build_dir


def save_manifest(build_dir: Path, manifest: dict) -> dict:
    """Salva il part-manifest (output dello stadio di parsing percettivo, §1) e
    porta lo stato 'parsing' → 'planning'. Il manifest è la SINGLE SOURCE OF TRUTH
    su COSA deve esistere: il planner e gli executor consumano questo, non
    ri-ragionano dalle immagini.

    Validazione deterministica leggera: `parts` lista non vuota, ogni parte con
    `id` e `primitive_type` (gli `id` devono essere unici). Ritorna gli id delle
    parti e le `open_questions` così l'orchestratore può loggarle.
    """
    parts = manifest.get("parts")
    if not isinstance(parts, list) or not parts:
        raise ValueError("manifest non valido: 'parts' deve essere una lista non vuota.")
    ids: list[str] = []
    for i, p in enumerate(parts):
        pid = p.get("id")
        if not pid or not p.get("primitive_type"):
            raise ValueError(
                f"manifest non valido: parte #{i} senza 'id' o 'primitive_type'."
            )
        if pid in ids:
            raise ValueError(f"manifest non valido: id duplicato '{pid}'.")
        ids.append(pid)

    path = build_dir / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    _patch_metadata(build_dir, lambda d: d.update(status="planning"))
    return {
        "manifest_path": str(path),
        "part_ids": ids,
        "open_questions": manifest.get("open_questions") or [],
    }


def plan_coverage(build_dir: Path, plan: dict) -> list[str]:
    """Cross-check di copertura piano↔manifest: ritorna gli id delle parti del
    manifest che NESSUNO step copre (campo `manifest_parts` dello step). Le parti
    irrisolte stanno in `open_questions` (testo libero, non id), quindi questo è un
    avviso — non un errore: feature potenzialmente droppate, da rivedere prima di
    costruire. Se non c'è manifest ritorna [] (retro-compatibile)."""
    manifest_path = build_dir / "manifest.json"
    if not manifest_path.exists():
        return []
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    part_ids = {p.get("id") for p in (manifest.get("parts") or []) if p.get("id")}
    covered: set = set()
    for step in plan.get("steps") or []:
        covered.update(step.get("manifest_parts") or [])
    return sorted(part_ids - covered)


def save_plan(build_dir: Path, plan: dict) -> Path:
    """Salva plan.json e porta lo stato a 'building'."""
    path = build_dir / "plan.json"
    path.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    _patch_metadata(build_dir, lambda d: d.update(status="building"))
    return path


def step_paths(build_dir: Path, step_id: int) -> dict[str, str]:
    """Path ASSOLUTI (str) degli artefatti di uno step, da passare all'executor
    perché Blender li scriva via execute_code/save_as_mainfile/export."""
    base = (build_dir / "steps").resolve()
    stem = f"step_{step_id:02d}"
    return {
        "blend": str(base / f"{stem}.blend"),
        "glb": str(base / f"{stem}.glb"),
        "png": str(base / f"{stem}.png"),
        "json": str(base / f"{stem}.json"),
    }


def final_paths(build_dir: Path) -> dict[str, str]:
    base = build_dir.resolve()
    return {
        "blend": str(base / "final.blend"),
        "glb": str(base / "final.glb"),
        "stl": str(base / "final.stl"),
    }


def _load_metadata(build_dir: Path) -> dict:
    return json.loads((build_dir / "metadata.json").read_text(encoding="utf-8"))


def eval_paths(build_dir: Path, iteration: int | None = None) -> dict:
    """Path ASSOLUTI degli artefatti del gate di convergenza per un'iterazione:
    le 4 viste renderizzate del modello (da confrontare con refs/) e verdict.json,
    sotto eval/iter_NN/. Crea la cartella. iteration default = metadata['iteration'].
    """
    if iteration is None:
        iteration = _load_metadata(build_dir).get("iteration", 1)
    base = (build_dir / "eval" / f"iter_{iteration:02d}").resolve()
    base.mkdir(parents=True, exist_ok=True)
    return {
        "iteration": iteration,
        "views": {name: str(base / f"{name}.png") for name in VIEW_NAMES},
        "verdict": str(base / "verdict.json"),
    }


def _rel_lenses(build_dir: Path, abs_map: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    """Converte la mappa {view: {lens: path_assoluto}} in path RELATIVI a build_dir
    (coerente con come `views` salva 'refs/front.png'): rende il metadata portabile."""
    base = build_dir.resolve()
    return {
        v: {lens: str(Path(p).resolve().relative_to(base)) for lens, p in lm.items()}
        for v, lm in abs_map.items()
    }


def make_lenses(build_dir: Path, iteration: int | None = None) -> dict:
    """Genera le lenti percettive (edges/silhouette/color) sulle immagini.

    - senza `iteration`: lenti delle 4 viste di riferimento (`refs/<view>.png`) →
      `refs/lenses/`, con TUTTE le lenti; aggiorna `metadata['ref_lenses']`.
    - con `iteration`: lenti dei render del gate (`eval/iter_NN/<view>.png`) →
      `eval/iter_NN/lenses/`, solo `edges`+`silhouette` (il giudice ignora i colori).

    Le lenti le legge il subagent (parser/giudice) ACCANTO all'originale per una
    descrizione/confronto più accurati. Ritorna {"lenses": {view: {lens: abs}}}.
    """
    build_dir = build_dir.resolve()
    if iteration is None:
        views = {n: build_dir / "refs" / f"{n}.png" for n in VIEW_NAMES}
        abs_map = image_lenses.generate_lenses(
            {n: p for n, p in views.items() if p.exists()},
            build_dir / "refs" / "lenses",
            lenses=image_lenses.DEFAULT_LENSES,
        )
        _patch_metadata(build_dir, lambda d: d.update(ref_lenses=_rel_lenses(build_dir, abs_map)))
    else:
        evd = (build_dir / "eval" / f"iter_{iteration:02d}").resolve()
        views = {n: evd / f"{n}.png" for n in VIEW_NAMES}
        abs_map = image_lenses.generate_lenses(
            {n: p for n, p in views.items() if p.exists()},
            evd / "lenses",
            lenses=image_lenses.GATE_LENSES,
        )
    return {"iteration": iteration, "lenses": abs_map}


def _decide(overall_score, actionable_gaps, iteration: int) -> dict:
    """Regola deterministica del gate: raccomanda 'reiterate' solo se il punteggio
    è sotto soglia E ci sono gap azionabili E non si è raggiunto il cap iterazioni;
    altrimenti 'stop' con motivazione esplicita. Ricalcolata qui, non delegata al
    giudizio LLM."""
    score = overall_score if isinstance(overall_score, (int, float)) else 0
    has_gaps = bool(actionable_gaps)
    if iteration >= MAX_ITERATIONS:
        return {"recommendation": "stop",
                "reason": f"cap iterazioni raggiunto (iteration={iteration} ≥ MAX_ITERATIONS={MAX_ITERATIONS})"}
    if score >= ACCEPT_THRESHOLD:
        return {"recommendation": "stop",
                "reason": f"soglia raggiunta (overall_score={score} ≥ ACCEPT_THRESHOLD={ACCEPT_THRESHOLD})"}
    if not has_gaps:
        return {"recommendation": "stop",
                "reason": f"nessun gap azionabile (overall_score={score})"}
    return {"recommendation": "reiterate",
            "reason": f"sotto soglia (overall_score={score} < {ACCEPT_THRESHOLD}) con {len(actionable_gaps)} gap azionabili"}


def record_evaluation(build_dir: Path, verdict: dict) -> dict:
    """Append atomico del verdict del gate a metadata['evaluations'], calcola la
    raccomandazione deterministica (stop|reiterate) e porta lo stato a 'done' (il
    gate si ferma sempre: è l'utente a decidere se rilanciare). Ritorna la decisione.
    """
    iteration = verdict.get("iteration")
    if iteration is None:
        iteration = _load_metadata(build_dir).get("iteration", 1)
        verdict = {**verdict, "iteration": iteration}
    decision = _decide(verdict.get("overall_score"), verdict.get("actionable_gaps") or [], iteration)
    verdict = {**verdict, **decision}

    def mutate(d: dict) -> None:
        evals = d.setdefault("evaluations", [])
        for i, e in enumerate(evals):
            if e.get("iteration") == iteration:
                evals[i] = verdict
                break
        else:
            evals.append(verdict)
        d["status"] = "done"

    _patch_metadata(build_dir, mutate)
    return {
        "recommendation": decision["recommendation"],
        "reason": decision["reason"],
        "overall_score": verdict.get("overall_score"),
        "iteration": iteration,
    }


def reinit_from_result(prev_build_dir: Path) -> dict:
    """Crea una NUOVA build session che PARTE dal risultato precedente, per una
    re-iterazione di rifinitura (su richiesta utente). Copia gli STESSI refs/ (input
    invariati) e prev/final.blend → seed.blend; eredita source_session, incrementa
    iteration, valorizza parent_build e planning_hints (gap dell'ultimo verdict).
    """
    prev_build_dir = prev_build_dir.resolve()
    prev_meta = _load_metadata(prev_build_dir)
    seed_src = prev_build_dir / "final.blend"
    if not seed_src.exists():
        raise FileNotFoundError(
            f"Manca {seed_src}: la build precedente non ha un final.blend da cui ripartire."
        )

    iteration = int(prev_meta.get("iteration", 1)) + 1
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = storage._slugify(prev_meta.get("prompt") or prev_meta.get("source_session") or "blender-build")
    build_dir = BLENDER_OUTPUTS_ROOT / f"{ts}-{slug}"
    (build_dir / "prompts").mkdir(parents=True, exist_ok=True)
    (build_dir / "refs").mkdir(parents=True, exist_ok=True)
    (build_dir / "steps").mkdir(parents=True, exist_ok=True)

    # input invariati: stessi 4 refs della build precedente
    view_rel: dict[str, str] = {}
    copied: dict[str, Path] = {}
    for name in VIEW_NAMES:
        src_ref = prev_build_dir / "refs" / f"{name}.png"
        if src_ref.exists():
            dst = build_dir / "refs" / f"{name}.png"
            shutil.copy2(src_ref, dst)
            view_rel[name] = f"refs/{name}.png"
            copied[name] = dst

    # rigenera le lenti percettive sulle refs (input invariati → stesse lenti)
    ref_lenses = _rel_lenses(
        build_dir,
        image_lenses.generate_lenses(copied, build_dir / "refs" / "lenses"),
    )

    # modello di partenza
    shutil.copy2(seed_src, build_dir / "seed.blend")

    # manifest invariato: il part-manifest descrive COSA deve esistere e non
    # cambia tra iterazioni (come i refs). Copialo se la build precedente ce l'ha.
    prev_manifest = prev_build_dir / "manifest.json"
    has_manifest = prev_manifest.exists()
    if has_manifest:
        shutil.copy2(prev_manifest, build_dir / "manifest.json")

    # gap dell'ultima valutazione → hint di pianificazione
    evals = prev_meta.get("evaluations") or []
    hints = (evals[-1].get("actionable_gaps") if evals else None) or []

    (build_dir / "prompts" / "00_source.txt").write_text(
        f"# source session: {prev_meta.get('source_session')} (build iter {iteration}, "
        f"da {prev_build_dir.name})\n\n{prev_meta.get('prompt', '')}\n",
        encoding="utf-8",
    )

    metadata = {
        "created_at": ts,
        "kind": "blender_build",
        "status": "planning",
        "source_session": prev_meta.get("source_session"),
        "prompt": prev_meta.get("prompt"),
        "manifest_path": "manifest.json" if has_manifest else None,
        "plan_path": "plan.json",
        "views": view_rel,
        "ref_lenses": ref_lenses,
        "steps": [],
        "iteration": iteration,
        "evaluations": [],
        "parent_build": str(prev_build_dir),
        "seed_blend": "seed.blend",
        "planning_hints": hints,
        "claude_usage": {"by_model": {}},
    }
    (build_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return {
        "build_dir": str(build_dir),
        "seed_blend": str((build_dir / "seed.blend").resolve()),
        "iteration": iteration,
        "hints": hints,
    }


def record_step(build_dir: Path, step_record: dict) -> None:
    """Append atomico di un record-step a metadata.json['steps'].

    Idempotente sull'id: se uno step con lo stesso id esiste già (re-run), lo
    sostituisce invece di duplicarlo.
    """
    sid = step_record.get("id")

    def mutate(d: dict) -> None:
        steps = d.setdefault("steps", [])
        for i, s in enumerate(steps):
            if s.get("id") == sid:
                steps[i] = step_record
                return
        steps.append(step_record)

    _patch_metadata(build_dir, mutate)


def add_usage(build_dir: Path, model: str, input_tokens: int, output_tokens: int) -> None:
    """Aggrega i token Claude per modello (stesso schema di storage.add_claude_usage)."""

    def mutate(d: dict) -> None:
        cu = d.setdefault("claude_usage", {"by_model": {}})
        bm = cu.setdefault("by_model", {})
        m = bm.setdefault(model, {"input_tokens": 0, "output_tokens": 0, "calls": 0})
        m["input_tokens"] += int(input_tokens)
        m["output_tokens"] += int(output_tokens)
        m["calls"] += 1

    _patch_metadata(build_dir, mutate)


def set_status(build_dir: Path, status: str) -> None:
    _patch_metadata(build_dir, lambda d: d.update(status=status))


# --- CLI sottile -----------------------------------------------------------
# Usata dall'orchestratore (Claude Code) nella skill blender-build per i passi
# deterministici. Output JSON su stdout così è facilmente parsabile.
def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="blender_build")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="crea build session da una sessione Oggetti")
    p.add_argument("source_dir")

    p = sub.add_parser("inputs", help="stampa prompt + path viste della sorgente")
    p.add_argument("source_dir")

    p = sub.add_parser("save-manifest", help="salva manifest.json (da file) e stato=planning")
    p.add_argument("build_dir")
    p.add_argument("manifest_file")

    p = sub.add_parser("save-plan", help="salva plan.json (da file) e stato=building")
    p.add_argument("build_dir")
    p.add_argument("plan_file")

    p = sub.add_parser("paths", help="path assoluti artefatti di uno step")
    p.add_argument("build_dir")
    p.add_argument("step_id", type=int)

    p = sub.add_parser("final-paths", help="path assoluti artefatti finali")
    p.add_argument("build_dir")

    p = sub.add_parser("record", help="append/replace di un record-step (da file json)")
    p.add_argument("build_dir")
    p.add_argument("record_file")

    p = sub.add_parser("status", help="imposta lo stato della build")
    p.add_argument("build_dir")
    p.add_argument("value")

    p = sub.add_parser("make-lenses", help="genera le lenti percettive (edges/silhouette/color)")
    p.add_argument("build_dir")
    p.add_argument("--iter", dest="iteration", type=int, default=None,
                   help="se presente: lenti dei render del gate eval/iter_NN/ (edges+silhouette)")

    p = sub.add_parser("eval-paths", help="path assoluti render+verdict del gate (iterazione)")
    p.add_argument("build_dir")
    p.add_argument("iteration", type=int, nargs="?", default=None)

    p = sub.add_parser("record-eval", help="registra il verdict del gate e ritorna la raccomandazione")
    p.add_argument("build_dir")
    p.add_argument("verdict_file")

    p = sub.add_parser("reinit-from-result", help="nuova build di rifinitura che parte da final.blend")
    p.add_argument("prev_build_dir")

    args = parser.parse_args(argv)

    if args.cmd == "init":
        out = {"build_dir": str(init_build_session(Path(args.source_dir)))}
    elif args.cmd == "inputs":
        src = load_source_inputs(Path(args.source_dir))
        out = {
            "prompt": src["prompt"],
            "version": src["version"],
            "source_session": src["source_session"],
            "views": {k: str(v) for k, v in src["views"].items()},
        }
    elif args.cmd == "save-manifest":
        manifest = json.loads(Path(args.manifest_file).read_text(encoding="utf-8"))
        out = save_manifest(Path(args.build_dir), manifest)
    elif args.cmd == "save-plan":
        plan = json.loads(Path(args.plan_file).read_text(encoding="utf-8"))
        build_dir = Path(args.build_dir)
        out = {
            "plan_path": str(save_plan(build_dir, plan)),
            "uncovered_parts": plan_coverage(build_dir, plan),
        }
    elif args.cmd == "paths":
        out = step_paths(Path(args.build_dir), args.step_id)
    elif args.cmd == "final-paths":
        out = final_paths(Path(args.build_dir))
    elif args.cmd == "record":
        rec = json.loads(Path(args.record_file).read_text(encoding="utf-8"))
        record_step(Path(args.build_dir), rec)
        out = {"ok": True, "id": rec.get("id")}
    elif args.cmd == "status":
        set_status(Path(args.build_dir), args.value)
        out = {"ok": True, "status": args.value}
    elif args.cmd == "make-lenses":
        out = make_lenses(Path(args.build_dir), args.iteration)
    elif args.cmd == "eval-paths":
        out = eval_paths(Path(args.build_dir), args.iteration)
    elif args.cmd == "record-eval":
        verdict = json.loads(Path(args.verdict_file).read_text(encoding="utf-8"))
        out = record_evaluation(Path(args.build_dir), verdict)
    elif args.cmd == "reinit-from-result":
        out = reinit_from_result(Path(args.prev_build_dir))
    else:  # pragma: no cover
        parser.error(f"comando sconosciuto: {args.cmd}")

    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
