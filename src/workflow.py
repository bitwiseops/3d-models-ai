import base64
import subprocess
import sys
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image

from . import config, grid_utils, mesh_repair, meshy_client, prompt_refiner, storage, usage as usage_mod


# STL per processing (single mesh fuso), GLB per preview <model-viewer> nella web UI.
# Meshy include il GLB nello stesso task: zero credit extra.
TARGET_FORMATS = ("stl", "glb")
# Grid 2x2 (4 viste in un'unica immagine), poi cropped lato Python.
IMAGE_ASPECT_RATIO = "1:1"

# Prompt per il pre-step opzionale di pulizia reference: normalizza sfondo,
# luce e risoluzione SENZA alterare forma/proporzioni del soggetto. Migliora
# l'accuratezza del mesh a valle (image-to-3d).
CLEAN_REFERENCE_PROMPT = (
    "Clean studio reference photo of the same subject: pure neutral white "
    "background, even diffuse lighting, no harsh shadows, single centered "
    "subject in sharp focus, high resolution. Preserve the subject's exact "
    "shape, proportions, colors and details — do not stylize or redesign."
)

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


def _meshy_grid(
    session: Path,
    prompt: str,
    *,
    references: list[Path] | None = None,
    label: str,
    on_progress: ProgressCallback = None,
    api_key: str | None = None,
) -> tuple[str, str, int]:
    """Genera una grid 2x2 via Meshy e ritorna `(image_url, task_id, credits)`.

    Se `references` è non vuota usa image-to-image (con eventuale pre-step di
    pulizia reference, come da config/metadata di sessione); altrimenti
    text-to-image. Le credit dell'eventuale clean vengono registrate qui
    (`add_credits image_clean`); le credit del task immagine sono ritornate al
    chiamante che decide in che bucket sommarle. Corpo condiviso tra le
    iterazioni Oggetti (`_generate_image`) e i componenti (`_generate_component_image`).
    """
    if references:
        print(
            f"\nGenerazione {label} via Meshy "
            f"image-to-image ({config.MESHY_IMAGE_MODEL}, {len(references)} reference)…"
        )
        ref_uris = [_to_data_uri_any(p) for p in references]
        meta = storage.load_metadata(session)
        clean = meta.get("clean_references", config.MESHY_CLEAN_REFERENCES)
        if clean:
            print("  [clean] pre-step pulizia reference via image-to-image…")
            clean_task_id = meshy_client.image_to_image(
                CLEAN_REFERENCE_PROMPT, reference_image_urls=ref_uris, api_key=api_key
            )
            clean_task = meshy_client.poll(
                "/image-to-image",
                clean_task_id,
                label=f"clean {label}",
                max_wait=600,
                on_progress=on_progress,
                api_key=api_key,
            )
            cleaned_urls = clean_task.get("image_urls") or []
            if cleaned_urls:
                ref_uris = cleaned_urls
            storage.update_metadata(
                session,
                add_credits={"image_clean": clean_task.get("consumed_credits", 0)},
            )
            print(f"  [clean] reference normalizzate ({len(ref_uris)})")
        img_task_id = meshy_client.image_to_image(
            prompt, reference_image_urls=ref_uris, api_key=api_key
        )
        endpoint = "/image-to-image"
    else:
        print(
            f"\nGenerazione {label} via Meshy "
            f"text-to-image ({config.MESHY_IMAGE_MODEL}, {IMAGE_ASPECT_RATIO})…"
        )
        img_task_id = meshy_client.text_to_image(
            prompt, aspect_ratio=IMAGE_ASPECT_RATIO, api_key=api_key
        )
        endpoint = "/text-to-image"
    task = meshy_client.poll(
        endpoint,
        img_task_id,
        label=label,
        max_wait=600,
        on_progress=on_progress,
        api_key=api_key,
    )
    image_url = task["image_urls"][0]
    credits = task.get("consumed_credits", 0)
    return image_url, img_task_id, credits


def _generate_image(
    session: Path,
    version: int,
    prompt: str,
    *,
    on_progress: ProgressCallback = None,
    api_key: str | None = None,
) -> Path:
    references = _session_references(session)
    image_url, img_task_id, credits = _meshy_grid(
        session,
        prompt,
        references=references or None,
        label=f"image v{version}",
        on_progress=on_progress,
        api_key=api_key,
    )
    grid_path = storage.save_image_from_url(session, version, image_url)
    # Rename the saved file to highlight that it's the grid source.
    new_grid_path = grid_path.with_name(grid_path.stem + "_grid.png")
    grid_path.rename(new_grid_path)
    grid_path = new_grid_path
    quadrants = grid_utils.crop_grid_2x2(grid_path, prefix=grid_path.stem)
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


def prepare_session(
    idea: str,
    references: list[Path] | None = None,
    *,
    clean_references: bool | None = None,
) -> Path:
    """Crea la session dir + salva references. Veloce e sincrona — usato dal
    web layer per avere subito la session_id da redirectare l'utente, mentre
    il lavoro slow (Claude refine + Meshy image gen) parte in background.

    `clean_references`: se non None, persiste il toggle del pre-step di pulizia
    reference in metadata (override per-sessione di config.MESHY_CLEAN_REFERENCES).
    """
    session = storage.create_session(idea)
    print(f"Sessione: {session}")
    if clean_references is not None:
        storage.update_metadata(session, clean_references=bool(clean_references))
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
    def _on_claude(model: str, inp: int, out: int) -> None:
        storage.update_metadata(
            session,
            add_claude_usage={"model": model, "input_tokens": inp, "output_tokens": out},
        )
    with usage_mod.session_scope(_on_claude):
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
    def _on_claude(model: str, inp: int, out: int) -> None:
        storage.update_metadata(
            session,
            add_claude_usage={"model": model, "input_tokens": inp, "output_tokens": out},
        )
    with usage_mod.session_scope(_on_claude):
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

    src_for_repair = saved.get("stl")

    # Cross-check stampabilità via Meshy (GRATIS) + fallback repair server-side.
    # Best-effort: se l'API print fallisce, la pipeline locale prosegue invariata.
    try:
        printability = meshy_client.print_analyze(
            input_task_id=model_task_id, api_key=api_key
        )
        status = printability.get("status", "unknown")
        metrics = printability.get("metrics", {})
        msg = (
            f"printability Meshy: {status} "
            f"(watertight={metrics.get('is_watertight')}, "
            f"non_manifold={metrics.get('non_manifold_edges')}, "
            f"degenerate={metrics.get('degenerate_faces')}, "
            f"holes={metrics.get('holes')})"
        )
        print(f"[analyze] {msg}")
        if on_step:
            on_step(msg)
        storage.update_metadata(session, printability=printability)

        if status == "error":
            print("[analyze] status=error → repair server-side Meshy (10cr)…")
            if on_step:
                on_step("mesh non stampabile → repair server-side Meshy")
            repaired_url = meshy_client.print_repair(
                input_task_id=model_task_id, api_key=api_key
            )
            # Basename distinto da `model_repaired.*` del repair locale: altrimenti
            # mesh_repair.repair() (più sotto) sovrascriverebbe lo STL Meshy.
            glb_path = storage.save_3d_from_url(
                session, "glb", repaired_url, filename="model_meshy_repaired.glb"
            )
            # /print/repair con input_task_id restituisce GLB → converti in STL
            # per la pipeline locale (repair → decimate → flatten).
            import trimesh

            stl_path = session / "3d" / "model_meshy_repaired.stl"
            trimesh.load(str(glb_path), force="mesh").export(str(stl_path))
            src_for_repair = str(stl_path.relative_to(session))
            storage.update_metadata(
                session,
                meshy_repaired_files={
                    "glb": str(glb_path.relative_to(session)),
                    "stl": str(stl_path.relative_to(session)),
                },
                add_credits={"print_repair": 10},
            )
            print(f"[analyze] mesh riparato Meshy → {stl_path}")
    except Exception as e:
        print(f"[analyze] saltato (best-effort): {e}")

    repaired: dict[str, Path] | None = None
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


# --- Scomposizione in componenti -------------------------------------------
# Stadio post-accept: l'oggetto accettato viene scomposto in componenti, ognuno
# con un prompt context-aware e una propria grid 2x2 (4 viste). L'output è il
# bundle components/components.json letto dalla skill fusion-build-from-components.

# Nomi dei 4 quadranti nell'ordine prodotto da grid_utils.crop_grid_2x2.
_QUADRANT_NAMES = [name for name, _, _ in grid_utils.GRID_QUADRANTS]


def _generate_component_image(
    session: Path,
    component_id: str,
    version: int,
    prompt: str,
    *,
    on_progress: ProgressCallback = None,
    api_key: str | None = None,
) -> dict:
    """Genera la grid 2x2 di un componente (text-to-image), la salva sotto
    components/<id>/images/ e la ritaglia nei 4 quadranti. Ritorna
    {grid_path, views:{view:rel}, task_id, credits} con path RELATIVI alla session.
    """
    image_url, img_task_id, credits = _meshy_grid(
        session,
        prompt,
        references=None,
        label=f"componente {component_id} v{version}",
        on_progress=on_progress,
        api_key=api_key,
    )
    grid_path = storage.save_component_image_from_url(
        session, component_id, version, image_url
    )
    quadrants = grid_utils.crop_grid_2x2(grid_path, prefix=grid_path.stem)
    storage.update_metadata(session, add_credits={"text_to_image": credits})
    views = {
        name: str(q.relative_to(session))
        for name, q in zip(_QUADRANT_NAMES, quadrants)
    }
    print(f"  componente {component_id}: grid {grid_path.name} (credits {credits})")
    return {
        "grid_path": str(grid_path.relative_to(session)),
        "views": views,
        "task_id": img_task_id,
        "credits": credits,
    }


def breakdown_into_components(
    session: Path,
    *,
    on_progress: ProgressCallback = None,
    on_step: StepCallback = None,
    api_key: str | None = None,
    skip_open: bool = False,
    force: bool = False,
) -> dict:
    """Scompone l'oggetto accettato in componenti e genera per ciascuno un prompt
    context-aware + una grid 2x2. Salva components/components.json (bundle di
    handoff). Ritorna il manifest dei componenti.

    Precondizione: la sessione deve avere un'iterazione accettata. Se i componenti
    esistono già solleva, salvo `force=True` (rigenera da capo).
    """
    meta = storage.load_metadata(session)
    iters = meta.get("iterations") or []
    if not iters:
        raise RuntimeError("Sessione senza iterazioni: usa prima `start`.")
    if meta.get("accepted_iteration") is None:
        raise RuntimeError(
            "Sessione non accettata: accetta un'iterazione prima di scomporla."
        )
    if meta.get("components") and not force:
        raise RuntimeError(
            "Sessione già scomposta in componenti (usa force=True per rigenerare)."
        )

    accepted = meta["accepted_iteration"]
    it = next((x for x in iters if x.get("version") == accepted), iters[-1])
    main_prompt = it["prompt"]
    main_subject = prompt_refiner._strip_staging(main_prompt)
    design_grid = session / (it.get("grid_path") or it["image_path"])
    design_views = {
        name: rel
        for name, rel in zip(_QUADRANT_NAMES, it.get("image_paths_grid") or [])
    }

    def _on_claude(model: str, inp: int, out: int) -> None:
        storage.update_metadata(
            session,
            add_claude_usage={"model": model, "input_tokens": inp, "output_tokens": out},
        )

    if on_step:
        on_step("proposta componenti con Claude…")
    print("\n[1/2] Proposta componenti con Claude…")
    with usage_mod.session_scope(_on_claude):
        proposal = prompt_refiner.propose_components(main_subject, design_grid)
    components_in = proposal.get("components") or []
    if not components_in:
        raise RuntimeError("Il proposer non ha prodotto componenti.")
    print(
        f"  {len(components_in)} componenti proposti: "
        f"{', '.join(str(c.get('id', '?')) for c in components_in)}"
    )

    out_components: list[dict] = []
    n = len(components_in)
    for i, comp in enumerate(components_in, start=1):
        cid = comp.get("id")
        siblings = [c for c in components_in if c.get("id") != cid]
        label = f"componente {i}/{n}: {cid}"
        if on_step:
            on_step(f"{label} — refine prompt")
        print(f"\n[2/2] {label} — refine prompt")
        with usage_mod.session_scope(_on_claude):
            comp_prompt = prompt_refiner.refine_component(
                main_subject, design_grid, comp, siblings
            )
        storage.create_component_dir(session, cid)
        storage.save_component_prompt(session, cid, 1, comp_prompt)
        if on_step:
            on_step(f"{label} — generazione immagine")
        img = _generate_component_image(
            session, cid, 1, comp_prompt, on_progress=on_progress, api_key=api_key
        )
        out_components.append(
            {
                "id": cid,
                "name": comp.get("name"),
                "description": comp.get("description"),
                "relations": comp.get("relations") or [],
                "relative_scale": comp.get("relative_scale"),
                "interface": comp.get("interface"),
                "prompt": comp_prompt,
                "grid_image": img["grid_path"],
                "views": img["views"],
                "version": 1,
                "status": "generated",
                "approved": False,
                "history": [{"version": 1, "prompt": comp_prompt, "feedback": None}],
            }
        )

    manifest = {
        "schema_version": 1,
        "source_session": session.name,
        "main_prompt": main_prompt,
        "main_subject": main_subject,
        "design_image": str(design_grid.relative_to(session)),
        "design_views": design_views,
        "components": out_components,
        "open_questions": proposal.get("open_questions") or [],
        "consumed_by": [],
    }
    path = storage.save_components_manifest(session, manifest)
    print(f"\nScomposizione completata: {len(out_components)} componenti → {path}")
    return manifest


def regenerate_component(
    session: Path,
    component_id: str,
    feedback: str,
    *,
    on_progress: ProgressCallback = None,
    api_key: str | None = None,
    skip_open: bool = False,
) -> Path:
    """Rigenera un singolo componente con feedback (Claude vede la scheda del
    componente + il design whole-object). Bumpa la version, appende alla history,
    riscrive components.json. Vietato se i componenti sono già stati consumati da
    una build. Ritorna il path della nuova grid."""
    manifest = storage.load_components_manifest(session)
    if manifest.get("consumed_by"):
        raise RuntimeError(
            "Componenti già consumati da una build Fusion: rigenerazione bloccata."
        )
    components = manifest.get("components") or []
    comp = next((c for c in components if c.get("id") == component_id), None)
    if comp is None:
        raise RuntimeError(f"Componente '{component_id}' non trovato.")

    main_subject = manifest.get("main_subject") or prompt_refiner._strip_staging(
        manifest.get("main_prompt", "")
    )
    design_grid = session / manifest["design_image"]
    siblings = [c for c in components if c.get("id") != component_id]

    def _on_claude(model: str, inp: int, out: int) -> None:
        storage.update_metadata(
            session,
            add_claude_usage={"model": model, "input_tokens": inp, "output_tokens": out},
        )

    print(f"\nRigenerazione componente '{component_id}' con feedback…")
    with usage_mod.session_scope(_on_claude):
        new_prompt = prompt_refiner.refine_component(
            main_subject, design_grid, comp, siblings, feedback=feedback
        )
    new_version = int(comp.get("version", 1)) + 1
    storage.save_component_prompt(session, component_id, new_version, new_prompt)
    img = _generate_component_image(
        session,
        component_id,
        new_version,
        new_prompt,
        on_progress=on_progress,
        api_key=api_key,
    )
    comp.update(
        {
            "prompt": new_prompt,
            "grid_image": img["grid_path"],
            "views": img["views"],
            "version": new_version,
            "status": "generated",
            "approved": False,
        }
    )
    comp.setdefault("history", []).append(
        {"version": new_version, "prompt": new_prompt, "feedback": feedback}
    )
    storage.save_components_manifest(session, manifest)
    grid_path = session / img["grid_path"]
    if not skip_open:
        _open_image(grid_path)
    return grid_path


def approve_component(session: Path, component_id: str, approved: bool = True) -> dict:
    """Marca un componente come approvato (o meno) in components.json."""
    manifest = storage.load_components_manifest(session)
    comp = next(
        (c for c in (manifest.get("components") or []) if c.get("id") == component_id),
        None,
    )
    if comp is None:
        raise RuntimeError(f"Componente '{component_id}' non trovato.")
    comp["approved"] = bool(approved)
    storage.save_components_manifest(session, manifest)
    return comp


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
