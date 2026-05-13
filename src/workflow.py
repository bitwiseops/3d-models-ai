import base64
import json
import subprocess
import sys
from pathlib import Path

from PIL import Image

from . import config, mesh_repair, meshy_client, prompt_refiner, storage


TARGET_FORMATS = ("stl",)
IMAGE_ASPECT_RATIO = "16:9"


def _split_image(image_path: Path) -> tuple[Path, Path]:
    img = Image.open(image_path).convert("RGB")
    w, h = img.size
    mid = w // 2
    front = img.crop((0, 0, mid, h))
    back = img.crop((mid, 0, w, h))
    front_path = image_path.with_name(image_path.stem + "_front.png")
    back_path = image_path.with_name(image_path.stem + "_back.png")
    front.save(front_path, format="PNG")
    back.save(back_path, format="PNG")
    return front_path, back_path


def _to_data_uri(path: Path) -> str:
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{data}"


def _to_data_uri_any(path: Path) -> str:
    suffix = path.suffix.lower()
    media = "image/jpeg" if suffix in (".jpg", ".jpeg") else "image/png"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{media};base64,{data}"


def _session_references(session: Path) -> list[Path]:
    meta = _load_metadata(session)
    return [session / p for p in meta.get("references", [])]


def _open_image(path: Path) -> None:
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        elif sys.platform.startswith("linux"):
            subprocess.run(["xdg-open", str(path)], check=False)
        elif sys.platform == "win32":
            subprocess.run(["cmd", "/c", "start", "", str(path)], check=False)
    except FileNotFoundError:
        print(f"  (impossibile aprire viewer di sistema, l'immagine è qui: {path})")


def _load_metadata(session: Path) -> dict:
    return json.loads((session / "metadata.json").read_text(encoding="utf-8"))


def _generate_image(session: Path, version: int, prompt: str) -> Path:
    references = _session_references(session)
    if references:
        print(
            f"\nGenerazione immagine v{version} via Meshy "
            f"image-to-image ({config.MESHY_IMAGE_MODEL}, {len(references)} reference)…"
        )
        ref_uris = [_to_data_uri_any(p) for p in references]
        img_task_id = meshy_client.image_to_image(
            prompt, reference_image_urls=ref_uris
        )
        endpoint = "/image-to-image"
    else:
        print(
            f"\nGenerazione immagine v{version} via Meshy "
            f"text-to-image ({config.MESHY_IMAGE_MODEL}, {IMAGE_ASPECT_RATIO})…"
        )
        img_task_id = meshy_client.text_to_image(
            prompt, aspect_ratio=IMAGE_ASPECT_RATIO
        )
        endpoint = "/text-to-image"
    task = meshy_client.poll(
        endpoint, img_task_id, label=f"image v{version}", max_wait=600
    )
    image_url = task["image_urls"][0]
    image_path = storage.save_image_from_url(session, version, image_url)
    front_path, back_path = _split_image(image_path)
    credits = task.get("consumed_credits", 0)
    storage.update_metadata(
        session,
        append_iteration={
            "version": version,
            "prompt": prompt,
            "image_task_id": img_task_id,
            "image_path": str(image_path.relative_to(session)),
            "front_path": str(front_path.relative_to(session)),
            "back_path": str(back_path.relative_to(session)),
            "credits": credits,
        },
        add_credits={"text_to_image": credits},
    )
    print(f"Immagine combinata salvata: {image_path}")
    print(f"  split → {front_path.name} + {back_path.name}")
    print(f"  credits: {credits}")
    return image_path


def start_session(
    idea: str, references: list[Path] | None = None
) -> tuple[Path, Path]:
    """Crea sessione, raffina prompt v1, genera image v1.
    Se references presenti, vengono salvate in session/references/ e usate
    per tutte le iterazioni (image-to-image invece di text-to-image).
    Ritorna (session_dir, image_path)."""
    session = storage.create_session(idea)
    print(f"Sessione: {session}")

    if references:
        saved_refs = storage.save_references(session, references)
        rel_refs = [str(p.relative_to(session)) for p in saved_refs]
        storage.update_metadata(session, references=rel_refs)
        print(f"Reference salvate: {len(saved_refs)} file in references/")

    print("\n[1/3] Raffinamento prompt iniziale con Claude…")
    refiner_refs = _session_references(session)
    prompt = prompt_refiner.refine_initial(
        idea, references=refiner_refs if refiner_refs else None
    )
    storage.save_prompt(session, 1, prompt)
    print(f"Prompt v1:\n{prompt}")

    image_path = _generate_image(session, 1, prompt)
    _open_image(image_path)
    return session, image_path


def continue_with_feedback(session: Path, feedback: str) -> Path:
    """Raffina con feedback, genera prossima immagine.
    Ritorna nuovo image_path."""
    meta = _load_metadata(session)
    if not meta["iterations"]:
        raise RuntimeError("Sessione senza iterazioni: usa prima `start`.")
    if meta.get("accepted_iteration") is not None:
        raise RuntimeError("Sessione già accettata: non si può rigenerare la 2D.")

    idea = meta["idea"]
    history = [
        {"prompt": it["prompt"], "image_path": session / it["image_path"]}
        for it in meta["iterations"]
    ]
    last = meta["iterations"][-1]
    last_image = session / last["image_path"]

    print("\nRaffinamento prompt con feedback…")
    new_prompt = prompt_refiner.refine_with_feedback(
        idea, history, last_image, feedback
    )
    new_version = last["version"] + 1
    storage.save_prompt(session, new_version, new_prompt)
    print(f"Prompt v{new_version}:\n{new_prompt}")

    image_path = _generate_image(session, new_version, new_prompt)
    _open_image(image_path)
    return image_path


def accept_and_generate_3d(session: Path) -> dict:
    """Marca l'ultima iterazione come accettata e lancia la fase image-to-3d."""
    meta = _load_metadata(session)
    if not meta["iterations"]:
        raise RuntimeError("Sessione senza iterazioni.")
    if meta.get("model_task_id"):
        raise RuntimeError("Fase 3D già completata per questa sessione.")

    last = meta["iterations"][-1]
    storage.update_metadata(
        session,
        accepted_iteration=last["version"],
        image_task_id=last["image_task_id"],
    )

    print(f"\n[3/3] Generazione 3D multi-view via Meshy ({config.MESHY_3D_MODEL})…")
    front = session / last["front_path"]
    back = session / last["back_path"]
    image_urls = [_to_data_uri(front), _to_data_uri(back)]
    model_task_id = meshy_client.multi_image_to_3d(
        image_urls=image_urls,
        target_formats=TARGET_FORMATS,
        enable_pbr=True,
    )
    model_task = meshy_client.poll(
        "/multi-image-to-3d", model_task_id, label="3D model", max_wait=1800
    )
    model_urls = model_task.get("model_urls", {})
    saved = {}
    for fmt in TARGET_FORMATS:
        url = model_urls.get(fmt)
        if not url:
            print(f"  attenzione: nessun URL per formato {fmt}")
            continue
        path = storage.save_3d_from_url(session, fmt, url)
        saved[fmt] = str(path.relative_to(session))
        print(f"  {fmt.upper()} salvato: {path}")

    storage.update_metadata(
        session,
        model_task_id=model_task_id,
        model_files=saved,
        add_credits={"image_to_3d": model_task.get("consumed_credits", 0)},
    )

    repaired: dict[str, Path] | None = None
    src_for_repair = saved.get("stl")
    if src_for_repair:
        print("\n[repair] post-processing per stampa…")
        try:
            repaired = mesh_repair.repair(session / src_for_repair, session / "3d")
            storage.update_metadata(
                session,
                repaired_files={
                    k: str(v.relative_to(session)) for k, v in repaired.items()
                },
            )
        except Exception as e:
            print(f"[repair] FALLITO: {e}")

    lowpoly: dict[str, Path] | None = None
    if repaired and "stl" in repaired:
        print("\n[decimate] riduzione mesh per portabilità…")
        try:
            lowpoly = mesh_repair.decimate(
                repaired["stl"],
                session / "3d",
                target_faces=mesh_repair.DEFAULT_TARGET_FACES,
            )
            storage.update_metadata(
                session,
                lowpoly_files={
                    k: str(v.relative_to(session)) for k, v in lowpoly.items()
                },
            )
        except Exception as e:
            print(f"[decimate] FALLITO: {e}")

    if lowpoly and "stl" in lowpoly:
        print("\n[flatten] taglio base piatta per stampa…")
        try:
            printable = mesh_repair.flatten_base(
                lowpoly["stl"],
                session / "3d",
                base_thickness_pct=mesh_repair.DEFAULT_BASE_THICKNESS_PCT,
            )
            storage.update_metadata(
                session,
                printable_files={
                    k: str(v.relative_to(session)) for k, v in printable.items()
                },
            )
        except Exception as e:
            print(f"[flatten] FALLITO: {e}")

    print(f"\nFatto. Output: {session}")
    return saved


def run(idea: str) -> Path:
    """Loop interattivo (input() da terminale). Per modalità non-interattiva
    usa i sub-comandi start/reject/accept."""
    session, _ = start_session(idea)
    while True:
        decision, feedback = _prompt_decision()
        if decision == "accept":
            break
        continue_with_feedback(session, feedback)
    accept_and_generate_3d(session)
    return session


def _prompt_decision() -> tuple[str, str]:
    while True:
        raw = input(
            "\nDecisione  [a]ccetto  |  r: <feedback per rigenerare>\n> "
        ).strip()
        if not raw:
            continue
        if raw.lower() in ("a", "accept", "accetta", "ok"):
            return "accept", ""
        if raw.lower().startswith("r"):
            _, _, feedback = raw.partition(":")
            feedback = feedback.strip()
            if not feedback:
                print("  Devi specificare un feedback dopo 'r:'.")
                continue
            return "reject", feedback
        print("  Risposta non valida. Usa 'a' per accettare o 'r: <feedback>'.")
