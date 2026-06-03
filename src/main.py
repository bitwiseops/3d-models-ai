import argparse
import os
import sys
from pathlib import Path

from . import config, mesh_repair, storage, workflow

# Base URL di default per `pull` (il dominio ngrok statico assegnato). Override
# con --from o con la env MODELS3D_PULL_BASE.
DEFAULT_PULL_BASE = "https://unbribed-liv-empirical.ngrok-free.dev"


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
        # Neutralizza anche il fallback: in test mode niente chiavi reali, così il
        # failover non passerebbe mai a una key che consuma credit veri.
        os.environ.pop("MESHY_API_KEY_FALLBACK", None)
        config.MESHY_API_KEY_FALLBACK = None
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
    workflow.accept_and_breakdown(Path(args.session))
    return 0


def cmd_breakdown(args) -> int:
    _apply_test_mode(args.test_mode)
    _ensure_keys()
    manifest = workflow.breakdown_into_components(
        Path(args.session), force=args.force
    )
    print(f"\nCOMPONENTS={Path(args.session) / 'components' / 'components.json'}")
    print(f"COUNT={len(manifest.get('components') or [])}")
    return 0


def cmd_breakdown_regen(args) -> int:
    _apply_test_mode(args.test_mode)
    _ensure_keys()
    grid = workflow.regenerate_component(
        Path(args.session), args.component, args.feedback
    )
    print(f"\nIMAGE={grid}")
    return 0


def cmd_breakdown_approve(args) -> int:
    comp = workflow.approve_component(Path(args.session), args.component)
    print(f"APPROVED={comp.get('id')} (approved={comp.get('approved')})")
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


def cmd_cleanup(args) -> int:
    """Elenca/cancella gli intermedi 3D pesanti delle sessioni complete.
    Dry-run di default; `--apply` per cancellare davvero."""
    if args.sessions:
        sessions = [Path(s) for s in args.sessions]
    else:
        sessions = sorted(p for p in config.OUTPUTS_ROOT.iterdir() if p.is_dir())

    grand_total = 0
    cleaned = 0
    skipped = 0
    for session in sessions:
        if not (session / "metadata.json").exists():
            continue
        res = storage.cleanup_session(session, apply=args.apply)
        if res["skipped"]:
            skipped += 1
            continue
        if not res["files"]:
            continue
        cleaned += 1
        grand_total += res["total_bytes"]
        mb = res["total_bytes"] / (1024 * 1024)
        verb = "cancellati" if args.apply else "cancellabili"
        print(f"\n{session.name} — {mb:.1f} MB {verb}:")
        for rel, sz in res["files"]:
            print(f"    {rel:<32} {sz / (1024 * 1024):6.1f} MB")

    total_mb = grand_total / (1024 * 1024)
    print(
        f"\n{'Recuperati' if args.apply else 'Recuperabili'}: "
        f"{total_mb:.1f} MB su {cleaned} sessioni "
        f"({skipped} saltate: pipeline incompleta)."
    )
    if not args.apply and grand_total:
        print("Esecuzione di prova — rilancia con `--apply` per cancellare.")
    return 0


def _bundle_url_and_id(arg: str, base: str | None) -> tuple[str, str]:
    """Risolve (url_bundle, session_id_locale) dall'argomento di `pull`.
    Accetta: un id sessione (+ --from BASE), un URL di sessione (.../sessions/<id>)
    o un URL diretto a `.../bundle.zip`."""
    from urllib.parse import urlparse

    if arg.startswith(("http://", "https://")):
        u = arg.rstrip("/")
        if u.endswith(".zip"):
            parts = urlparse(u).path.rstrip("/").split("/")
            # .../sessions/<id>/bundle.zip → l'id è il penultimo segmento
            sid = parts[-2] if len(parts) >= 2 else "bundle"
            return u, sid
        sid = urlparse(u).path.rstrip("/").split("/")[-1]
        return f"{u}/bundle.zip", sid
    if not base:
        raise ValueError(
            "passa un URL completo, oppure l'id sessione insieme a --from BASE_URL."
        )
    sid = arg.strip("/")
    if not sid:
        raise ValueError("id sessione vuoto.")
    return f"{base.rstrip('/')}/sessions/{sid}/bundle.zip", sid


def cmd_pull(args) -> int:
    """Scarica da un server remoto il bundle componenti di una sessione e lo
    ricostruisce in outputs/<id> in locale, pronto per la skill PC
    fusion-build-from-components."""
    import requests

    try:
        url, sid = _bundle_url_and_id(args.session, args.from_url)
    except ValueError as e:
        print(f"Errore: {e}", file=sys.stderr)
        return 2

    # Header innocuo per server normali: salta l'interstitial HTML del free tier
    # ngrok, che altrimenti arriverebbe al posto dello zip e farebbe fallire l'estrazione.
    headers = {"ngrok-skip-browser-warning": "1"}
    token = args.token or os.environ.get("MODELS3D_PULL_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    print(f"→ Scarico bundle da {url}")
    try:
        resp = requests.get(url, headers=headers, timeout=60)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"Errore download: {e}", file=sys.stderr)
        return 1

    dest = (Path(args.out) if args.out else config.OUTPUTS_ROOT) / sid
    try:
        storage.extract_components_bundle(resp.content, dest, force=args.force)
    except (FileExistsError, ValueError) as e:
        print(f"Errore: {e}", file=sys.stderr)
        return 1

    print(f"\nPULLED={dest}")
    print("Lancia la skill su questa sessione, es.:")
    print(f"    /fusion-build-from-components {dest}")
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
        "accept", help="Accetta l'ultima immagine e scompone in componenti."
    )
    p_accept.add_argument("session", help="Path alla session dir.")
    p_accept.set_defaults(func=cmd_accept)

    p_breakdown = sub.add_parser(
        "breakdown",
        help="Scompone l'oggetto accettato in componenti: per ognuno genera un "
        "prompt context-aware + una grid 2x2. Produce components/components.json.",
    )
    p_breakdown.add_argument("session", help="Path alla session dir (accettata).")
    p_breakdown.add_argument(
        "--force",
        action="store_true",
        help="Rigenera i componenti anche se già presenti.",
    )
    p_breakdown.set_defaults(func=cmd_breakdown)

    p_bd_regen = sub.add_parser(
        "breakdown-regen",
        help="Rigenera un singolo componente con feedback testuale.",
    )
    p_bd_regen.add_argument("session", help="Path alla session dir.")
    p_bd_regen.add_argument("component", help="id del componente da rigenerare.")
    p_bd_regen.add_argument("feedback", help="Feedback testuale per il refinement.")
    p_bd_regen.set_defaults(func=cmd_breakdown_regen)

    p_bd_approve = sub.add_parser(
        "breakdown-approve",
        help="Marca un componente come approvato in components.json.",
    )
    p_bd_approve.add_argument("session", help="Path alla session dir.")
    p_bd_approve.add_argument("component", help="id del componente da approvare.")
    p_bd_approve.set_defaults(func=cmd_breakdown_approve)

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

    p_cleanup = sub.add_parser(
        "cleanup",
        help="Elenca/cancella gli intermedi 3D pesanti (model.stl, "
        "model_repaired.*, model_lowpoly.*, model_meshy_repaired.*) delle "
        "sessioni complete, tenendo model.glb + model_printable.*. "
        "Dry-run di default.",
    )
    p_cleanup.add_argument(
        "sessions",
        nargs="*",
        help="Path delle session dir (default: tutte sotto outputs/).",
    )
    p_cleanup.add_argument(
        "--apply",
        action="store_true",
        help="Cancella davvero i file (senza, è solo un'anteprima dry-run).",
    )
    p_cleanup.set_defaults(func=cmd_cleanup)

    p_pull = sub.add_parser(
        "pull",
        help="Scarica da un server remoto il bundle componenti di una sessione "
        "(components.json + viste whole/per-componente) e lo ricostruisce in "
        "outputs/<id> in locale, pronto per la skill fusion-build-from-components.",
    )
    p_pull.add_argument(
        "session",
        help="id sessione (con --from) oppure URL completo (di sessione o di bundle.zip).",
    )
    p_pull.add_argument(
        "--from",
        dest="from_url",
        default=os.environ.get("MODELS3D_PULL_BASE", DEFAULT_PULL_BASE),
        help=f"Base URL del server (default: {DEFAULT_PULL_BASE}, "
        "override con env MODELS3D_PULL_BASE).",
    )
    p_pull.add_argument(
        "--token",
        help="Bearer token se il server è protetto (o env MODELS3D_PULL_TOKEN).",
    )
    p_pull.add_argument(
        "--out",
        help="Root di destinazione (default: outputs/).",
    )
    p_pull.add_argument(
        "--force",
        action="store_true",
        help="Sovrascrive la session dir locale se già esistente e non vuota.",
    )
    p_pull.set_defaults(func=cmd_pull)

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
