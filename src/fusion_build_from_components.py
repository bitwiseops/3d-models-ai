"""Helper deterministici per la pipeline "Fusion-build-from-components".

Variante di `fusion_build` il cui INPUT è una sessione Oggetti già SCOMPOSTA in
componenti (cartella `components/` con `components.json` + 4 viste per componente,
prodotta da `workflow.breakdown_into_components`). A differenza di fusion-build
(che parte dalle sole 4 viste dell'intero oggetto) qui ogni componente è una
PARTE pre-identificata con le sue 4 viste: il parsing è soprattutto mapping
componenti→manifest + inferenza delle relazioni/joint inter-componente.

Per MASSIMIZZARE il riuso (decisione: "import condiviso, aggiungi i delta"), le
parti CAD-agnostiche sono importate da `fusion_build` e usate invariate:
save_manifest, plan_coverage, save_plan, step_paths, final_paths, eval_paths,
record_step, record_evaluation, add_usage, set_status, e le costanti del gate.
Qui vivono SOLO i pezzi specifici dell'input-a-componenti: lettura del bundle,
bootstrap della build session (refs whole + refs per-componente + lenti), e lo
stadio di ACTION-PLAN (analogo all'action-log di fusion-build-from-transcript,
ma segmentato per componente).

Convenzione di layout (scelta per compatibilità coi helper condivisi):
- refs/<view>.png            → le 4 viste dell'INTERO oggetto (whole). Stessa
  posizione di fusion-build, così il gate (eval vs refs/<view>.png), make-lenses
  default e reinit funzionano invariati.
- refs/components/<id>/<view>.png → le 4 viste di OGNI componente.
"""

import json
import shutil
from datetime import datetime
from pathlib import Path

from . import image_lenses, storage
from .fusion_build import (  # riuso diretto dei pezzi CAD-agnostici
    FUSION_OUTPUTS_ROOT,
    VIEW_NAMES,
    _load_metadata,
    _patch_metadata,
    _rel_lenses,
    add_usage,
    eval_paths,
    final_paths,
    plan_coverage,
    record_evaluation,
    record_step,
    save_manifest,
    save_plan,
    set_status,
    step_paths,
)
from .fusion_build import make_lenses as _make_lenses_shared


def load_components_inputs(source_dir: Path) -> dict:
    """Legge components/components.json di una sessione Oggetti e risolve i path
    relativi → assoluti. Valida che ESISTANO le 4 viste whole e le 4 viste di ogni
    componente. Ritorna {main_prompt, main_subject, design_views, components,
    open_questions, source_session}.
    """
    source_dir = source_dir.resolve()
    manifest = storage.load_components_manifest(source_dir)

    design_views_rel = manifest.get("design_views") or {}
    design_views: dict[str, Path] = {}
    for name in VIEW_NAMES:
        rel = design_views_rel.get(name)
        p = (source_dir / rel) if rel else None
        if not p or not p.exists():
            raise FileNotFoundError(
                f"Vista whole '{name}' mancante nella sessione {source_dir.name}."
            )
        design_views[name] = p

    components: list[dict] = []
    for c in manifest.get("components") or []:
        cid = c.get("id")
        views_rel = c.get("views") or {}
        views: dict[str, Path] = {}
        for name in VIEW_NAMES:
            rel = views_rel.get(name)
            p = (source_dir / rel) if rel else None
            if not p or not p.exists():
                raise FileNotFoundError(
                    f"Componente '{cid}': vista '{name}' mancante "
                    f"({rel}). Rigenera il componente prima della build."
                )
            views[name] = p
        components.append({**c, "_view_paths": views})

    if not components:
        raise ValueError(
            f"Sessione {source_dir.name} senza componenti: lancia prima `breakdown`."
        )

    return {
        "main_prompt": manifest.get("main_prompt", ""),
        "main_subject": manifest.get("main_subject") or manifest.get("main_prompt", ""),
        "design_views": design_views,
        "components": components,
        "open_questions": manifest.get("open_questions") or [],
        "source_session": source_dir.name,
    }


def _gen_ref_lenses(build_dir: Path) -> tuple[dict, dict]:
    """Genera le lenti percettive su refs/<view>.png (whole) e su ogni
    refs/components/<id>/<view>.png. Ritorna (whole_rel, {id: component_rel})."""
    whole_views = {n: build_dir / "refs" / f"{n}.png" for n in VIEW_NAMES}
    whole_abs = image_lenses.generate_lenses(
        {n: p for n, p in whole_views.items() if p.exists()},
        build_dir / "refs" / "lenses",
    )
    whole_rel = _rel_lenses(build_dir, whole_abs)

    comp_rel: dict[str, dict] = {}
    comp_root = build_dir / "refs" / "components"
    if comp_root.exists():
        for cdir in sorted(p for p in comp_root.iterdir() if p.is_dir()):
            views = {n: cdir / f"{n}.png" for n in VIEW_NAMES}
            abs_map = image_lenses.generate_lenses(
                {n: p for n, p in views.items() if p.exists()},
                cdir / "lenses",
            )
            comp_rel[cdir.name] = _rel_lenses(build_dir, abs_map)
    return whole_rel, comp_rel


def init_components_session(source_dir: Path) -> Path:
    """Crea outputs/fusion/<ts-slug>/ per una build-from-components: copia le 4
    viste whole in refs/, le 4 viste di ogni componente in refs/components/<id>/,
    genera le lenti su entrambi, copia components.json come riferimento e scrive
    lo skeleton di metadata.json (status='preprocessing', lo stadio action-plan).
    """
    src = load_components_inputs(source_dir)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = storage._slugify(src["main_subject"] or src["source_session"])
    build_dir = FUSION_OUTPUTS_ROOT / f"{ts}-{slug}"
    (build_dir / "prompts").mkdir(parents=True, exist_ok=True)
    (build_dir / "refs" / "components").mkdir(parents=True, exist_ok=True)
    (build_dir / "steps").mkdir(parents=True, exist_ok=True)

    # viste whole → refs/<view>.png (posizione standard, compatibile coi helper)
    whole_rel: dict[str, str] = {}
    for name, p in src["design_views"].items():
        dst = build_dir / "refs" / f"{name}.png"
        shutil.copy2(p, dst)
        whole_rel[name] = f"refs/{name}.png"

    # viste per-componente → refs/components/<id>/<view>.png
    component_views: dict[str, dict[str, str]] = {}
    components_meta: list[dict] = []
    for c in src["components"]:
        cid = c["id"]
        cdir = build_dir / "refs" / "components" / storage._slugify(cid)
        cdir.mkdir(parents=True, exist_ok=True)
        view_rel: dict[str, str] = {}
        for name, p in c["_view_paths"].items():
            dst = cdir / f"{name}.png"
            shutil.copy2(p, dst)
            view_rel[name] = f"refs/components/{cdir.name}/{name}.png"
        component_views[cid] = view_rel
        # scheda componente (senza i path assoluti interni) per parser/planner
        components_meta.append(
            {k: v for k, v in c.items() if not k.startswith("_")}
        )

    whole_lenses, comp_lenses = _gen_ref_lenses(build_dir)

    (build_dir / "prompts" / "00_source.txt").write_text(
        f"# source session: {src['source_session']} (scomposta in {len(components_meta)} componenti)\n\n"
        f"{src['main_subject']}\n\n"
        "Componenti:\n"
        + "\n".join(
            f"- {c.get('id')}: {c.get('name')} — {c.get('description', '')}".strip()
            for c in components_meta
        )
        + "\n",
        encoding="utf-8",
    )

    # copia il bundle sorgente per riferimento del parser
    shutil.copy2(
        source_dir / "components" / "components.json",
        build_dir / "components_source.json",
    )

    metadata = {
        "created_at": ts,
        "kind": "fusion_build_components",
        "status": "preprocessing",
        "source_session": src["source_session"],
        "prompt": src["main_subject"],
        "main_prompt": src["main_prompt"],
        "action_plan_path": "action-plan.json",
        "manifest_path": "manifest.json",
        "plan_path": "plan.json",
        "views": whole_rel,
        "component_views": component_views,
        "ref_lenses": whole_lenses,
        "component_ref_lenses": comp_lenses,
        "components": components_meta,
        "open_questions": src["open_questions"],
        "steps": [],
        "iteration": 1,
        "evaluations": [],
        "claude_usage": {"by_model": {}},
    }
    (build_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return build_dir


def save_action_plan(build_dir: Path, action_plan: dict) -> dict:
    """Salva action-plan.json (output dello stadio di consolidamento Opus) e porta
    lo stato 'preprocessing' → 'parsing'.

    Validazione leggera: `components` lista non vuota, ogni elemento con
    `component_id` e `actions` (lista); ogni `joints[]` con `from`/`to` validi
    rispetto ai component_id presenti.
    """
    comps = action_plan.get("components")
    if not isinstance(comps, list) or not comps:
        raise ValueError("action-plan non valido: 'components' deve essere una lista non vuota.")
    ids: list[str] = []
    for i, c in enumerate(comps):
        cid = c.get("component_id")
        if not cid:
            raise ValueError(f"action-plan non valido: componente #{i} senza 'component_id'.")
        if not isinstance(c.get("actions"), list) or not c.get("actions"):
            raise ValueError(f"action-plan non valido: componente '{cid}' senza 'actions'.")
        ids.append(cid)
    id_set = set(ids)
    for j in action_plan.get("joints") or []:
        a, b = j.get("from"), j.get("to")
        if a not in id_set or b not in id_set:
            raise ValueError(
                f"action-plan non valido: joint con component_id sconosciuto ({a} → {b})."
            )

    path = build_dir / "action-plan.json"
    path.write_text(json.dumps(action_plan, indent=2, ensure_ascii=False), encoding="utf-8")
    _patch_metadata(build_dir, lambda d: d.update(status="parsing"))
    return {
        "action_plan_path": str(path),
        "components": ids,
        "n_joints": len(action_plan.get("joints") or []),
        "open_questions": action_plan.get("open_questions") or [],
    }


def make_lenses(build_dir: Path, iteration: int | None = None) -> dict:
    """Lenti percettive. Senza `iteration`: rigenera le lenti dei refs whole
    (refs/<view>.png) E di ogni componente (refs/components/<id>/), aggiornando
    metadata. Con `iteration`: delega al helper condiviso (render del gate sotto
    eval/iter_NN/, edges+silhouette)."""
    build_dir = build_dir.resolve()
    if iteration is None:
        whole_lenses, comp_lenses = _gen_ref_lenses(build_dir)
        _patch_metadata(
            build_dir,
            lambda d: d.update(ref_lenses=whole_lenses, component_ref_lenses=comp_lenses),
        )
        return {"iteration": None, "whole": whole_lenses, "components": comp_lenses}
    return _make_lenses_shared(build_dir, iteration)


def reinit_from_result(prev_build_dir: Path) -> dict:
    """Nuova build di rifinitura che parte da final.f3d, mantenendo TUTTI i refs
    (whole + componenti), il manifest e i gap dell'ultima valutazione come hint.
    Variante components-aware di fusion_build.reinit_from_result (qui i refs
    includono anche refs/components/)."""
    prev_build_dir = prev_build_dir.resolve()
    prev_meta = _load_metadata(prev_build_dir)
    seed_src = prev_build_dir / "final.f3d"
    if not seed_src.exists():
        raise FileNotFoundError(
            f"Manca {seed_src}: la build precedente non ha un final.f3d da cui ripartire."
        )

    iteration = int(prev_meta.get("iteration", 1)) + 1
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = storage._slugify(prev_meta.get("prompt") or prev_meta.get("source_session") or "fusion-components")
    build_dir = FUSION_OUTPUTS_ROOT / f"{ts}-{slug}"
    # evita collisione col build precedente (stesso secondo/slug)
    suffix = 2
    while build_dir.exists():
        build_dir = FUSION_OUTPUTS_ROOT / f"{ts}-{slug}-{suffix}"
        suffix += 1
    (build_dir / "prompts").mkdir(parents=True, exist_ok=True)
    (build_dir / "steps").mkdir(parents=True, exist_ok=True)

    # refs invariati: copia l'intero albero refs/ (whole + componenti + lenti)
    if (prev_build_dir / "refs").exists():
        shutil.copytree(prev_build_dir / "refs", build_dir / "refs")
    # rigenera le lenti per sicurezza (input invariati)
    whole_lenses, comp_lenses = _gen_ref_lenses(build_dir)

    shutil.copy2(seed_src, build_dir / "seed.f3d")

    prev_manifest = prev_build_dir / "manifest.json"
    has_manifest = prev_manifest.exists()
    if has_manifest:
        shutil.copy2(prev_manifest, build_dir / "manifest.json")
    prev_ap = prev_build_dir / "action-plan.json"
    if prev_ap.exists():
        shutil.copy2(prev_ap, build_dir / "action-plan.json")
    prev_bundle = prev_build_dir / "components_source.json"
    if prev_bundle.exists():
        shutil.copy2(prev_bundle, build_dir / "components_source.json")

    evals = prev_meta.get("evaluations") or []
    hints = (evals[-1].get("actionable_gaps") if evals else None) or []

    (build_dir / "prompts" / "00_source.txt").write_text(
        f"# source session: {prev_meta.get('source_session')} (build iter {iteration}, "
        f"da {prev_build_dir.name})\n\n{prev_meta.get('prompt', '')}\n",
        encoding="utf-8",
    )

    metadata = {
        "created_at": ts,
        "kind": "fusion_build_components",
        "status": "planning",
        "source_session": prev_meta.get("source_session"),
        "prompt": prev_meta.get("prompt"),
        "main_prompt": prev_meta.get("main_prompt"),
        "action_plan_path": "action-plan.json",
        "manifest_path": "manifest.json" if has_manifest else None,
        "plan_path": "plan.json",
        "views": prev_meta.get("views", {}),
        "component_views": prev_meta.get("component_views", {}),
        "ref_lenses": whole_lenses,
        "component_ref_lenses": comp_lenses,
        "components": prev_meta.get("components", []),
        "open_questions": prev_meta.get("open_questions", []),
        "steps": [],
        "iteration": iteration,
        "evaluations": [],
        "parent_build": str(prev_build_dir),
        "seed_f3d": "seed.f3d",
        "planning_hints": hints,
        "claude_usage": {"by_model": {}},
    }
    (build_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return {
        "build_dir": str(build_dir),
        "seed_f3d": str((build_dir / "seed.f3d").resolve()),
        "iteration": iteration,
        "hints": hints,
    }


# --- CLI sottile -----------------------------------------------------------
# Usata dall'orchestratore (Claude Code) nella skill fusion-build-from-components.
# I comandi specifici (init, save-action-plan, make-lenses, reinit) vivono qui;
# gli altri delegano agli helper condivisi importati da fusion_build.
def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="fusion_build_from_components")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="crea build session da una sessione Oggetti scomposta")
    p.add_argument("source_dir")

    p = sub.add_parser("inputs", help="stampa prompt + path viste whole/componenti")
    p.add_argument("source_dir")

    p = sub.add_parser("save-action-plan", help="salva action-plan.json e stato=parsing")
    p.add_argument("build_dir")
    p.add_argument("action_plan_file")

    p = sub.add_parser("save-manifest", help="salva manifest.json e stato=planning")
    p.add_argument("build_dir")
    p.add_argument("manifest_file")

    p = sub.add_parser("save-plan", help="salva plan.json e stato=building")
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

    p = sub.add_parser("make-lenses", help="genera le lenti percettive (whole + componenti)")
    p.add_argument("build_dir")
    p.add_argument("--iter", dest="iteration", type=int, default=None)

    p = sub.add_parser("eval-paths", help="path assoluti render+verdict del gate")
    p.add_argument("build_dir")
    p.add_argument("iteration", type=int, nargs="?", default=None)

    p = sub.add_parser("record-eval", help="registra il verdict del gate e ritorna la raccomandazione")
    p.add_argument("build_dir")
    p.add_argument("verdict_file")

    p = sub.add_parser("reinit-from-result", help="nuova build di rifinitura che parte da final.f3d")
    p.add_argument("prev_build_dir")

    args = parser.parse_args(argv)

    if args.cmd == "init":
        out = {"build_dir": str(init_components_session(Path(args.source_dir)))}
    elif args.cmd == "inputs":
        src = load_components_inputs(Path(args.source_dir))
        out = {
            "main_subject": src["main_subject"],
            "source_session": src["source_session"],
            "design_views": {k: str(v) for k, v in src["design_views"].items()},
            "components": [
                {
                    "id": c["id"],
                    "name": c.get("name"),
                    "views": {k: str(v) for k, v in c["_view_paths"].items()},
                }
                for c in src["components"]
            ],
        }
    elif args.cmd == "save-action-plan":
        ap = json.loads(Path(args.action_plan_file).read_text(encoding="utf-8"))
        out = save_action_plan(Path(args.build_dir), ap)
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
