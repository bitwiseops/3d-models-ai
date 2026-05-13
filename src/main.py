import argparse
import os
import sys
from pathlib import Path

from . import config, mesh_repair, storage, workflow


def _ensure_keys() -> None:
    try:
        config.assert_keys_present()
    except RuntimeError as e:
        print(f"Errore: {e}", file=sys.stderr)
        sys.exit(2)


def _apply_test_mode(flag: bool) -> None:
    if flag:
        os.environ["MESHY_API_KEY"] = config.MESHY_TEST_KEY
        config.MESHY_API_KEY = config.MESHY_TEST_KEY
    if config.is_test_mode():
        print("⚠️  Meshy in TEST MODE: niente credit, output dummy.\n")


def cmd_run(args) -> int:
    _apply_test_mode(args.test_mode)
    _ensure_keys()
    idea = args.idea or input("Idea: ").strip()
    if not idea:
        print("Nessuna idea fornita.", file=sys.stderr)
        return 2
    workflow.run(idea)
    return 0


def cmd_start(args) -> int:
    _apply_test_mode(args.test_mode)
    _ensure_keys()
    refs = [Path(p) for p in (args.reference or [])]
    session, image_path = workflow.start_session(args.idea, references=refs or None)
    print(f"\nSESSION={session}")
    print(f"IMAGE={image_path}")
    return 0


def cmd_reject(args) -> int:
    _apply_test_mode(args.test_mode)
    _ensure_keys()
    image_path = workflow.continue_with_feedback(Path(args.session), args.feedback)
    print(f"\nIMAGE={image_path}")
    return 0


def cmd_accept(args) -> int:
    _apply_test_mode(args.test_mode)
    _ensure_keys()
    workflow.accept_and_generate_3d(Path(args.session))
    return 0


def cmd_repair(args) -> int:
    session = Path(args.session)
    stl = session / "3d" / "model.stl"
    glb = session / "3d" / "model.glb"
    src = stl if stl.exists() else glb
    if not src.exists():
        print(f"Nessun mesh raw trovato in {session}/3d/", file=sys.stderr)
        return 2
    repaired = mesh_repair.repair(src, session / "3d")
    storage.update_metadata(
        session,
        repaired_files={k: str(v.relative_to(session)) for k, v in repaired.items()},
    )
    return 0


def cmd_decimate(args) -> int:
    session = Path(args.session)
    src = session / "3d" / "model_repaired.stl"
    if not src.exists():
        print(
            f"STL repaired non trovato: {src}. Lancia prima `repair`.",
            file=sys.stderr,
        )
        return 2
    lowpoly = mesh_repair.decimate(src, session / "3d", target_faces=args.target)
    storage.update_metadata(
        session,
        lowpoly_files={k: str(v.relative_to(session)) for k, v in lowpoly.items()},
    )
    return 0


def cmd_flatten(args) -> int:
    session = Path(args.session)
    src = session / "3d" / "model_lowpoly.stl"
    if not src.exists():
        src = session / "3d" / "model_repaired.stl"
    if not src.exists():
        print(
            f"Nessuno STL trovato per il flatten in {session}/3d/. "
            f"Lancia prima `repair`/`decimate`.",
            file=sys.stderr,
        )
        return 2
    printable = mesh_repair.flatten_base(
        src, session / "3d", base_thickness_pct=args.thickness
    )
    storage.update_metadata(
        session,
        printable_files={k: str(v.relative_to(session)) for k, v in printable.items()},
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Workflow Meshy + Claude per generare modelli 3D."
    )
    parser.add_argument(
        "--test-mode",
        action="store_true",
        help=f"Forza la dummy key Meshy ({config.MESHY_TEST_KEY}).",
    )

    sub = parser.add_subparsers(dest="cmd")

    p_run = sub.add_parser("run", help="Loop interattivo classico (default).")
    p_run.add_argument("idea", nargs="?")
    p_run.set_defaults(func=cmd_run)

    p_start = sub.add_parser("start", help="Crea sessione e genera la prima immagine.")
    p_start.add_argument("idea")
    p_start.add_argument(
        "--reference",
        action="append",
        metavar="PATH",
        help="Path a un'immagine di reference (1-5, ripetibile). "
        "Se presente, Meshy usa image-to-image invece di text-to-image, "
        "e Claude vede le reference durante il refinement.",
    )
    p_start.set_defaults(func=cmd_start)

    p_reject = sub.add_parser(
        "reject", help="Rigenera con feedback testuale (Claude vede l'ultima immagine)."
    )
    p_reject.add_argument("session", help="Path alla session dir.")
    p_reject.add_argument("feedback", help="Feedback testuale per il refinement.")
    p_reject.set_defaults(func=cmd_reject)

    p_accept = sub.add_parser(
        "accept", help="Accetta l'ultima immagine e genera il modello 3D."
    )
    p_accept.add_argument("session", help="Path alla session dir.")
    p_accept.set_defaults(func=cmd_accept)

    p_repair = sub.add_parser(
        "repair",
        help="Riapplica la pipeline di mesh repair (trimesh + pymeshfix) "
        "sul GLB scaricato di una sessione esistente.",
    )
    p_repair.add_argument("session", help="Path alla session dir.")
    p_repair.set_defaults(func=cmd_repair)

    p_decimate = sub.add_parser(
        "decimate",
        help="Riduzione facce (Quadric Error Metrics) di model_repaired.stl "
        "in una sessione esistente.",
    )
    p_decimate.add_argument("session", help="Path alla session dir.")
    p_decimate.add_argument(
        "--target",
        type=int,
        default=mesh_repair.DEFAULT_TARGET_FACES,
        help=f"Target di facce (default: {mesh_repair.DEFAULT_TARGET_FACES}).",
    )
    p_decimate.set_defaults(func=cmd_decimate)

    p_flatten = sub.add_parser(
        "flatten",
        help="Taglia la base inferiore con un piano orizzontale e la tappa: "
        "produce model_printable.{stl,3mf} con base piatta a z=0.",
    )
    p_flatten.add_argument("session", help="Path alla session dir.")
    p_flatten.add_argument(
        "--thickness",
        type=float,
        default=mesh_repair.DEFAULT_BASE_THICKNESS_PCT,
        help=f"Frazione dell'altezza da tagliare in basso "
        f"(default: {mesh_repair.DEFAULT_BASE_THICKNESS_PCT}, cioè 2%).",
    )
    p_flatten.set_defaults(func=cmd_flatten)

    parser.add_argument(
        "idea_positional",
        nargs="?",
        help=argparse.SUPPRESS,
    )

    args = parser.parse_args()

    if args.cmd is None:
        idea = args.idea_positional
        ns = argparse.Namespace(idea=idea, test_mode=args.test_mode)
        return cmd_run(ns)

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
