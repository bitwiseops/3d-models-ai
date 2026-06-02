import io
import json
import os
import re
import shutil
import threading
import unicodedata
import zipfile
from datetime import datetime
from pathlib import Path

import requests

from . import config

# Per-session reentrant lock per serializzare read-modify-write su metadata.json.
# RLock (non asyncio.Lock) perché update_metadata è chiamato sia da CLI sync
# che da worker thread del JobManager (entrambi sync context).
_session_locks: dict[Path, threading.RLock] = {}
_locks_guard = threading.Lock()


def _get_lock(session: Path) -> threading.RLock:
    """Lazy creation di un lock per-sessione. Thread-safe self."""
    key = session.resolve()
    with _locks_guard:
        lock = _session_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _session_locks[key] = lock
        return lock


# File intermedi 3D cancellabili da `cleanup_session`: pesanti e non serviti
# dalla UI Objects (viewer = model.glb, download = model_printable.*). Sono
# invece usati dai re-run CLI (decimate/flatten) e dai download Portraits,
# quindi la cancellazione è on-demand — mai automatica nella pipeline.
INTERMEDIATE_3D_FILES = (
    "3d/model.stl",
    "3d/model_repaired.stl",
    "3d/model_repaired.3mf",
    "3d/model_lowpoly.stl",
    "3d/model_lowpoly.3mf",
    "3d/model_meshy_repaired.glb",
    "3d/model_meshy_repaired.stl",
    "3d/model_repaired.glb",  # legacy: output Meshy /print/repair pre-rename
)


def _slugify(text: str, max_len: int = 40) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:max_len] or "session"


def create_session(idea: str) -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    session = config.OUTPUTS_ROOT / f"{ts}-{_slugify(idea)}"
    (session / "prompts").mkdir(parents=True, exist_ok=True)
    (session / "images").mkdir(parents=True, exist_ok=True)
    (session / "3d").mkdir(parents=True, exist_ok=True)
    (session / "prompts" / "00_idea.txt").write_text(idea, encoding="utf-8")
    metadata = {
        "created_at": ts,
        "idea": idea,
        "iterations": [],
        "accepted_iteration": None,
        "image_task_id": None,
        "model_task_id": None,
        "credits": {"text_to_image": 0, "image_to_3d": 0},
    }
    (session / "metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return session


def save_prompt(session: Path, version: int, text: str) -> Path:
    path = session / "prompts" / f"{version:02d}_refined.txt"
    path.write_text(text, encoding="utf-8")
    return path


def save_image_from_url(session: Path, version: int, url: str) -> Path:
    path = session / "images" / f"{version:02d}.png"
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    path.write_bytes(r.content)
    return path


def save_references(session: Path, paths: list[Path]) -> list[Path]:
    """Copia i file di reference nella session dir e ritorna i nuovi path."""
    if not paths:
        return []
    dst_dir = session / "references"
    dst_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for i, src in enumerate(paths, start=1):
        if not src.exists():
            raise FileNotFoundError(f"Reference non trovata: {src}")
        ext = src.suffix.lower() or ".png"
        dst = dst_dir / f"ref_{i:02d}{ext}"
        shutil.copy2(src, dst)
        saved.append(dst)
    return saved


def save_3d_from_url(session: Path, fmt: str, url: str, *, filename: str | None = None) -> Path:
    path = session / "3d" / (filename or f"model.{fmt}")
    r = requests.get(url, timeout=300)
    r.raise_for_status()
    path.write_bytes(r.content)
    return path


def update_metadata(session: Path, **patch) -> None:
    """Patch read-modify-write su metadata.json, atomico e thread-safe.

    Atomicità: scrive prima su `metadata.json.tmp` e poi `os.replace()` →
    su POSIX e Windows è una rename atomica, quindi il file non viene mai
    visto in stato parziale anche se il processo crasha mid-write.

    Thread-safety: tutto il read-modify-write sotto _get_lock(session).
    """
    path = session / "metadata.json"
    tmp = path.with_suffix(".json.tmp")
    with _get_lock(session):
        data = json.loads(path.read_text(encoding="utf-8"))
        for k, v in patch.items():
            if k == "append_iteration":
                data["iterations"].append(v)
            elif k == "add_credits":
                for bucket, amount in v.items():
                    data["credits"][bucket] = data["credits"].get(bucket, 0) + amount
            elif k == "add_claude_usage":
                cu = data.setdefault("claude_usage", {"by_model": {}})
                bm = cu.setdefault("by_model", {})
                m = bm.setdefault(v["model"], {"input_tokens": 0, "output_tokens": 0, "calls": 0})
                m["input_tokens"] += int(v.get("input_tokens", 0))
                m["output_tokens"] += int(v.get("output_tokens", 0))
                m["calls"] += 1
            else:
                data[k] = v
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)


def load_metadata(session: Path) -> dict:
    """Legge metadata.json sotto lock (consistent con writer concorrenti)."""
    with _get_lock(session):
        return json.loads((session / "metadata.json").read_text(encoding="utf-8"))


# --- Componenti ------------------------------------------------------------
# Dopo l'accettazione del design, l'oggetto viene scomposto in componenti
# (vedi workflow.breakdown_into_components). Ogni componente vive sotto
# components/<id>/ con i suoi prompt e le sue immagini; components/components.json
# è il CONTRATTO di handoff letto dalla skill fusion-build-from-components.

def create_component_dir(session: Path, component_id: str) -> Path:
    """Crea components/<slug>/{prompts,images} e ritorna la dir del componente.
    Lo slug è derivato dall'id (già slug dal proposer, ri-sanificato per sicurezza)."""
    slug = _slugify(component_id)
    comp_dir = session / "components" / slug
    (comp_dir / "prompts").mkdir(parents=True, exist_ok=True)
    (comp_dir / "images").mkdir(parents=True, exist_ok=True)
    return comp_dir


def save_component_prompt(session: Path, component_id: str, version: int, text: str) -> Path:
    slug = _slugify(component_id)
    path = session / "components" / slug / "prompts" / f"{version:02d}_refined.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def save_component_image_from_url(
    session: Path, component_id: str, version: int, url: str
) -> Path:
    """Scarica la grid 2x2 del componente in components/<id>/images/NN_grid.png.
    Il crop nei 4 quadranti lo fa il workflow (riusa grid_utils.crop_grid_2x2)."""
    slug = _slugify(component_id)
    path = session / "components" / slug / "images" / f"{version:02d}_grid.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    path.write_bytes(r.content)
    return path


def save_components_manifest(session: Path, manifest: dict) -> Path:
    """Valida e salva components/components.json (SSOT di handoff) e aggiorna
    metadata.json con un sommario.

    Validazione: `components` lista non vuota, `id` univoci, ogni `relations.target_id`
    deve riferirsi a un id presente. Solleva ValueError se non valido.
    """
    components = manifest.get("components")
    if not isinstance(components, list) or not components:
        raise ValueError("manifest componenti non valido: 'components' lista non vuota richiesta.")
    ids: list[str] = []
    for i, c in enumerate(components):
        cid = c.get("id")
        if not cid:
            raise ValueError(f"componente #{i} senza 'id'.")
        if cid in ids:
            raise ValueError(f"id componente duplicato: '{cid}'.")
        ids.append(cid)
    id_set = set(ids)
    for c in components:
        for r in c.get("relations") or []:
            tgt = r.get("target_id")
            if tgt and tgt not in id_set:
                raise ValueError(
                    f"componente '{c.get('id')}': relazione verso id inesistente '{tgt}'."
                )

    comp_root = session / "components"
    comp_root.mkdir(parents=True, exist_ok=True)
    path = comp_root / "components.json"
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    update_metadata(
        session,
        components={
            "count": len(ids),
            "ids": ids,
            "manifest_path": "components/components.json",
            "all_approved": all(c.get("approved") for c in components),
        },
    )
    return path


def load_components_manifest(session: Path) -> dict:
    path = session / "components" / "components.json"
    if not path.exists():
        raise FileNotFoundError(
            f"Nessun components.json in {session}: lancia prima `breakdown`."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def collect_components_bundle(session: Path) -> list[tuple[Path, str]]:
    """Raccoglie i file dell'handoff componenti — gli UNICI input che servono
    alla skill PC `fusion-build-from-components`: `components/components.json` +
    tutte le immagini referenziate (le 4 viste whole + le 4 viste di ogni
    componente) + `metadata.json` per provenienza.

    Ritorna una lista di `(path_assoluto, arcname)` con l'**arcname relativo alla
    session dir** così la struttura (`components/...`, `images/...`) resta intatta
    e, una volta scompattata sul PC, `load_components_inputs` risolve i path
    relativi senza modifiche. Valida che ogni referenziato esista (FileNotFoundError
    altrimenti): il bundle non è MAI parziale.
    """
    manifest = load_components_manifest(session)  # FileNotFoundError se manca
    session = session.resolve()
    members: list[tuple[Path, str]] = []
    seen: set[str] = set()

    def add(rel: str, *, required: bool = True) -> None:
        rel = str(rel).replace("\\", "/").lstrip("/")
        if not rel or rel in seen:
            return
        p = (session / rel).resolve()
        try:
            p.relative_to(session)
        except ValueError:
            raise ValueError(f"path fuori dalla session nel manifest: {rel}")
        if not p.exists():
            if required:
                raise FileNotFoundError(
                    f"file referenziato mancante nel bundle: {rel} "
                    "(rigenera il componente prima di esportare)."
                )
            return
        seen.add(rel)
        members.append((p, rel))

    add("components/components.json")
    for rel in (manifest.get("design_views") or {}).values():
        add(rel)
    for c in manifest.get("components") or []:
        for rel in (c.get("views") or {}).values():
            add(rel)
    add("metadata.json", required=False)
    return members


def write_components_bundle(session: Path) -> bytes:
    """Serializza il bundle componenti (vedi collect_components_bundle) in un
    archivio .zip in memoria. Usato sia dall'endpoint web sia dai test."""
    members = collect_components_bundle(session)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for abs_path, arcname in members:
            zf.write(abs_path, arcname=arcname)
    return buf.getvalue()


def extract_components_bundle(data: bytes, dest: Path, *, force: bool = False) -> Path:
    """Scompatta un bundle componenti (.zip prodotto da write_components_bundle)
    in `dest` (la nuova session dir locale). Guard anti path-traversal sugli
    arcname. Rifiuta una dest non vuota salvo `force`. Verifica che dopo
    l'estrazione esista `components/components.json`. Ritorna `dest`.
    """
    if dest.exists() and any(dest.iterdir()) and not force:
        raise FileExistsError(
            f"{dest} esiste già e non è vuota: usa --force per sovrascrivere."
        )
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            if name.startswith("/") or ".." in Path(name).parts:
                raise ValueError(f"arcname non sicuro nel bundle: {name}")
        dest.mkdir(parents=True, exist_ok=True)
        zf.extractall(dest)
    if not (dest / "components" / "components.json").exists():
        raise ValueError("bundle non valido: manca components/components.json")
    return dest


def cleanup_session(session: Path, *, apply: bool = False) -> dict:
    """Elenca (o cancella, se apply=True) gli intermedi 3D pesanti di una
    sessione, tenendo `model.glb` (preview viewer) e `model_printable.{stl,3mf}`
    (download finali) + images/references/prompts/metadata.

    Guard: agisce solo se la pipeline è completa (`3d/model_printable.stl`
    esiste), altrimenti ritorna `skipped=True` senza toccare nulla — protegge
    sessioni image-only o pipeline parziali.

    Ritorna `{"skipped": bool, "files": [(rel, bytes)...], "total_bytes": int}`.
    Con `apply=True` cancella i file e pota i path stale da metadata.json.
    """
    if not (session / "3d" / "model_printable.stl").exists():
        return {"skipped": True, "files": [], "total_bytes": 0}

    found: list[tuple[str, int]] = []
    for rel in INTERMEDIATE_3D_FILES:
        p = session / rel
        if p.exists():
            found.append((rel, p.stat().st_size))
    total = sum(sz for _, sz in found)

    if apply and found:
        for rel, _ in found:
            (session / rel).unlink()
        # Pota i riferimenti diventati stale (la UI Objects non li usa).
        meta = load_metadata(session)
        model_files = {
            k: v for k, v in (meta.get("model_files") or {}).items() if k != "stl"
        }
        update_metadata(
            session,
            model_files=model_files,
            repaired_files={},
            lowpoly_files={},
            meshy_repaired_files={},
        )

    return {"skipped": False, "files": found, "total_bytes": total}
