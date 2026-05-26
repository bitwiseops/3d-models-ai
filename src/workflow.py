import base64
import subprocess
import sys
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image

from . import config, grid_utils, mesh_repair, meshy_client, prompt_refiner, storage


# STL per processing (single mesh fuso), GLB per preview <model-viewer> nella web UI.
# Meshy include il GLB nello stesso task: zero credit extra.
TARGET_FORMATS = ("stl", "glb")
# Grid 2x2 (4 viste in un'unica immagine), poi cropped lato Python.
IMAGE_ASPECT_RATIO = "1:1"

# Tipi callback opzionali per web/SSE.
ProgressCallback = Callable[[int, str], None] | None  # (progress_pct, status)
StepCallback = Callable[[str], None] | None  # mesh_repair step labels


def _detect_center_band(
    img: Image.Image,
    flat_threshold_ratio: float = 0.2,
    min_band_pct: float = 0.03,
) -> tuple[int, int] | None:
    """Detect la banda monocolore centrale prodotta dal modello image-gen
    (Meshy genera 16:9 con front+back affiancate + gutter neutro).

    Strategia: per ogni colonna calcola std dei valori grayscale; mediana globale
    come reference. Colonne con std < `flat_threshold_ratio * mediana` sono
    "piatte" (background uniforme). Espande la regione flat dal centro verso
    l'esterno. Ritorna `(left_edge, right_edge)` della banda da rimuovere, o
    None se la banda è sotto `min_band_pct` della width.
    """
    arr = np.array(img.convert("L"))
    h, w = arr.shape
    col_std = arr.std(axis=0)
    median_std = float(np.median(col_std))
    if median_std == 0:
        return None
    is_flat = col_std < median_std * flat_threshold_ratio
    c = w // 2
    L = c
    while L > 0 and is_flat[L - 1]:
        L -= 1
    R = c
    while R < w - 1 and is_flat[R]:
        R += 1
    band_w = R - L
    if band_w < w * min_band_pct:
        return None
    # Force symmetric split: usa il min tra distanza-da-centro per non sbilanciare
    # front e back. Evita che una metà sia molto più stretta dell'altra.
    half = min(c - L, R - c)
    if half <= 0:
        return None
    return c - half, c + half


def _split_image(image_path: Path) -> tuple[Path, Path, Path | None]:
    """Split front+back dell'immagine combinata. Auto-detect la banda
    centrale di background uniforme e taglia escludendola. Crea anche un
    composite "pulito" (`<stem>_clean.png`) per il display in UI quando una
    banda viene effettivamente rilevata.

    Ritorna `(front_path, back_path, clean_path | None)`.
    """
    img = Image.open(image_path).convert("RGB")
    w, h = img.size

    band = _detect_center_band(img)
    if band is not None:
        left_edge, right_edge = band
    else:
        left_edge = right_edge = w // 2
    front = img.crop((0, 0, left_edge, h))
    back = img.crop((right_edge, 0, w, h))

    front_path = image_path.with_name(image_path.stem + "_front.png")
    back_path = image_path.with_name(image_path.stem + "_back.png")
    front.save(front_path, format="PNG")
    back.save(back_path, format="PNG")

    clean_path: Path | None = None
    if band is not None:
        gutter = 8
        clean_w = front.width + gutter + back.width
        clean = Image.new("RGB", (clean_w, h), (255, 255, 255))
        clean.paste(front, (0, 0))
        clean.paste(back, (front.width + gutter, 0))
        clean_path = image_path.with_name(image_path.stem + "_clean.png")
        clean.save(clean_path, format="PNG")

    return front_path, back_path, clean_path


def _to_data_uri(path: Path) -> str:
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{data}"


def _to_data_uri_any(path: Path) -> str:
    """Data URI con media type rilevato via magic bytes (riusa il detector
    di prompt_refiner). Evita mismatch se il file ha estensione bugiarda."""
    raw = path.read_bytes()
    media = prompt_refiner._detect_media_type(raw)
    data = base64.b64encode(raw).decode("ascii")
    return f"data:{media};base64,{data}"


def _session_references(session: Path) -> list[Path]:
    meta = storage.load_metadata(session)
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


def _generate_image(
    session: Path,
    version: int,
    prompt: str,
    *,
    on_progress: ProgressCallback = None,
    api_key: str | None = None,
) -> Path:
    references = _session_references(session)
    if references:
        print(
            f"\nGenerazione immagine v{version} via Meshy "
            f"image-to-image ({config.MESHY_IMAGE_MODEL}, {len(references)} reference)…"
        )
        ref_uris = [_to_data_uri_any(p) for p in references]
        img_task_id = meshy_client.image_to_image(
            prompt, reference_image_urls=ref_uris, api_key=api_key
        )
        endpoint = "/image-to-image"
    else:
        print(
            f"\nGenerazione immagine v{version} via Meshy "
            f"text-to-image ({config.MESHY_IMAGE_MODEL}, {IMAGE_ASPECT_RATIO})…"
        )
        img_task_id = meshy_client.text_to_image(
            prompt, aspect_ratio=IMAGE_ASPECT_RATIO, api_key=api_key
        )
        endpoint = "/text-to-image"
    task = meshy_client.poll(
        endpoint,
        img_task_id,
        label=f"image v{version}",
        max_wait=600,
        on_progress=on_progress,
        api_key=api_key,
    )
    image_url = task["image_urls"][0]
    grid_path = storage.save_image_from_url(session, version, image_url)
    # Rename the saved file to highlight that it's the grid source.
    new_grid_path = grid_path.with_name(grid_path.stem + "_grid.png")
    grid_path.rename(new_grid_path)
    grid_path = new_grid_path
    quadrants = grid_utils.crop_grid_2x2(grid_path, prefix=grid_path.stem)
    credits = task.get("consumed_credits", 0)
    iter_record = {
        "version": version,
        "prompt": prompt,
        "image_task_id": img_task_id,
        # `image_path` rimane per compat con codice/template che lo cercano.
        "image_path": str(grid_path.relative_to(session)),
        "grid_path": str(grid_path.relative_to(session)),
        "image_paths_grid": [str(p.relative_to(session)) for p in quadrants],
        "credits": credits,
    }
    storage.update_metadata(
        session,
        append_iteration=iter_record,
        add_credits={"text_to_image": credits},
    )
    print(f"Grid 2x2 salvato: {grid_path}")
    print(f"  quadranti → {', '.join(p.name for p in quadrants)}")
    print(f"  credits: {credits}")
    return grid_path


def prepare_session(idea: str, references: list[Path] | None = None) -> Path:
    """Crea la session dir + salva references. Veloce e sincrona — usato dal
    web layer per avere subito la session_id da redirectare l'utente, mentre
    il lavoro slow (Claude refine + Meshy image gen) parte in background.
    """
    session = storage.create_session(idea)
    print(f"Sessione: {session}")
    if references:
        saved_refs = storage.save_references(session, references)
        rel_refs = [str(p.relative_to(session)) for p in saved_refs]
        storage.update_metadata(session, references=rel_refs)
        print(f"Reference salvate: {len(saved_refs)} file in references/")
    return session


def generate_first_iteration(
    session: Path,
    idea: str,
    *,
    on_progress: ProgressCallback = None,
    api_key: str | None = None,
    skip_open: bool = False,
) -> Path:
    """Raffina prompt v1 e genera image v1 su una session già preparata.
    Slow (Claude + Meshy polling). Wrappato in background job dal web layer.
    """
    print("\n[1/3] Raffinamento prompt iniziale con Claude…")
    refiner_refs = _session_references(session)
    prompt = prompt_refiner.refine_initial(
        idea, references=refiner_refs if refiner_refs else None
    )
    storage.save_prompt(session, 1, prompt)
    print(f"Prompt v1:\n{prompt}")

    image_path = _generate_image(
        session, 1, prompt, on_progress=on_progress, api_key=api_key
    )
    if not skip_open:
        _open_image(image_path)
    return image_path


def start_session(
    idea: str,
    references: list[Path] | None = None,
    *,
    on_progress: ProgressCallback = None,
    api_key: str | None = None,
    skip_open: bool = False,
) -> tuple[Path, Path]:
    """Crea sessione, raffina prompt v1, genera image v1. Sincrona end-to-end
    (per CLI). Ritorna (session_dir, image_path).

    Web/SSE: usa invece `prepare_session()` + `generate_first_iteration()`
    per separare la creazione dir (immediata) dalla generazione (slow).
    """
    session = prepare_session(idea, references)
    image_path = generate_first_iteration(
        session,
        idea,
        on_progress=on_progress,
        api_key=api_key,
        skip_open=skip_open,
    )
    return session, image_path


def continue_with_feedback(
    session: Path,
    feedback: str,
    *,
    on_progress: ProgressCallback = None,
    api_key: str | None = None,
    skip_open: bool = False,
    use_previous_image: bool = True,
) -> Path:
    """Raffina con feedback, genera prossima immagine.
    Ritorna nuovo image_path. Se `use_previous_image=False`, Claude raffina
    senza vedere le immagini delle iterazioni precedenti."""
    meta = storage.load_metadata(session)
    if not meta["iterations"]:
        raise RuntimeError("Sessione senza iterazioni: usa prima `start`.")
    if meta.get("accepted_iteration") is not None:
        raise RuntimeError("Sessione già accettata: non si può rigenerare la 2D.")

    idea = meta["idea"]
    # Per le nuove iterazioni-grid usa `grid_path` (la composita 2x2) come cue
    # visivo singolo; fallback a `image_path` per iterazioni legacy.
    history = [
        {
            "prompt": it["prompt"],
            "image_path": session / (it.get("grid_path") or it["image_path"]),
        }
        for it in meta["iterations"]
    ]
    last = meta["iterations"][-1]
    last_image = session / (last.get("grid_path") or last["image_path"])

    print(
        f"\nRaffinamento prompt con feedback "
        f"(use_previous_image={use_previous_image})…"
    )
    new_prompt = prompt_refiner.refine_with_feedback(
        idea, history, last_image, feedback,
        use_previous_image=use_previous_image,
    )
    new_version = last["version"] + 1
    storage.save_prompt(session, new_version, new_prompt)
    print(f"Prompt v{new_version}:\n{new_prompt}")

    image_path = _generate_image(
        session, new_version, new_prompt, on_progress=on_progress, api_key=api_key
    )
    if not skip_open:
        _open_image(image_path)
    return image_path


def accept_and_generate_3d(
    session: Path,
    *,
    on_progress: ProgressCallback = None,
    on_step: StepCallback = None,
    api_key: str | None = None,
) -> dict:
    """Marca l'ultima iterazione come accettata e lancia la fase image-to-3d.

    Web/SSE: `on_progress(pct, status)` per il polling Meshy 3D (può durare
    10-30 min); `on_step(label)` per i messaggi granulari della mesh_repair
    pipeline (repair → decimate → flatten_base).
    """
    meta = storage.load_metadata(session)
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
    if last.get("image_paths_grid"):
        # Nuovo flusso: 4 quadranti del grid 2x2 → multi-image-to-3d.
        quadrant_paths = [session / p for p in last["image_paths_grid"]]
        image_urls = [_to_data_uri(p) for p in quadrant_paths]
        print(f"  input: {len(image_urls)} viste dal grid 2x2")
    else:
        # Legacy: front + back dalla vecchia split 16:9.
        front = session / last["front_path"]
        back = session / last["back_path"]
        image_urls = [_to_data_uri(front), _to_data_uri(back)]
        print("  input: legacy front + back (2 viste)")
    model_task_id = meshy_client.multi_image_to_3d(
        image_urls=image_urls,
        target_formats=TARGET_FORMATS,
        enable_pbr=True,
        api_key=api_key,
    )
    model_task = meshy_client.poll(
        "/multi-image-to-3d",
        model_task_id,
        label="3D model",
        max_wait=1800,
        on_progress=on_progress,
        api_key=api_key,
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
            repaired = mesh_repair.repair(
                session / src_for_repair, session / "3d", on_step=on_step
            )
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
                on_step=on_step,
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
                on_step=on_step,
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
