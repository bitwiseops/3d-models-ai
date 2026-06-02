"""Smoke test web della scomposizione in componenti (Parte A), offline.

Mocka la generazione (Claude + Meshy) come fa test_components_breakdown, ma
esercita gli endpoint FastAPI: breakdown, fragment card, approve.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from src import config, storage, workflow
from src.web import app as web_app


@pytest.fixture
def tmp_outputs(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(config, "OUTPUTS_ROOT", tmp_path)
    return tmp_path


@pytest.fixture
def client(tmp_outputs: Path):
    app = web_app.create_app()
    with TestClient(app) as c:
        yield c


def _accepted_session(name: str) -> Path:
    from src import grid_utils

    session = storage.create_session(name)
    grid = session / "images" / "01_grid.png"
    Image.new("RGB", (64, 64), (200, 180, 160)).save(grid)
    crops = grid_utils.crop_grid_2x2(grid, prefix=grid.stem)
    storage.update_metadata(
        session,
        append_iteration={
            "version": 1,
            "prompt": "A squat ceramic teapot.",
            "image_task_id": "t",
            "image_path": "images/01_grid.png",
            "grid_path": "images/01_grid.png",
            "image_paths_grid": [str(c.relative_to(session)) for c in crops],
            "credits": 9,
        },
        accepted_iteration=1,
        image_task_id="t",
    )
    return session


def _session_with_iteration(name: str) -> Path:
    """Come _accepted_session ma SENZA accettazione: ha solo l'iterazione v1."""
    from src import grid_utils

    session = storage.create_session(name)
    grid = session / "images" / "01_grid.png"
    Image.new("RGB", (64, 64), (200, 180, 160)).save(grid)
    crops = grid_utils.crop_grid_2x2(grid, prefix=grid.stem)
    storage.update_metadata(
        session,
        append_iteration={
            "version": 1,
            "prompt": "A squat ceramic teapot.",
            "image_task_id": "t",
            "image_path": "images/01_grid.png",
            "grid_path": "images/01_grid.png",
            "image_paths_grid": [str(c.relative_to(session)) for c in crops],
            "credits": 9,
        },
    )
    return session


def _mock_generation(monkeypatch):
    def fake_meshy_grid(session, prompt, *, references=None, label="", on_progress=None, api_key=None):
        return "fake://grid", "task", 9

    def fake_save(session, component_id, version, url):
        path = storage.create_component_dir(session, component_id) / "images" / f"{version:02d}_grid.png"
        Image.new("RGB", (64, 64), (120, 120, 200)).save(path)
        return path

    monkeypatch.setattr(workflow, "_meshy_grid", fake_meshy_grid)
    monkeypatch.setattr(storage, "save_component_image_from_url", fake_save)
    monkeypatch.setattr(
        workflow.prompt_refiner, "propose_components",
        lambda subject, img: {"components": [
            {"id": "body", "name": "Body", "description": "corpo",
             "relations": [{"type": "on_top_of", "target_id": "lid", "detail": "x"}],
             "relative_scale": "70%", "interface": "apertura"},
            {"id": "lid", "name": "Lid", "description": "coperchio",
             "relations": [], "relative_scale": "15%", "interface": "flangia"},
        ], "open_questions": []},
    )
    monkeypatch.setattr(
        workflow.prompt_refiner, "refine_component",
        lambda subject, img, comp, siblings, feedback=None: f"A {comp['id']}.",
    )
    monkeypatch.setattr(workflow.prompt_refiner.usage, "record_response", lambda *a, **k: None)


def _wait_components(session: Path, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        meta = storage.load_metadata(session)
        if meta.get("components"):
            return meta
        time.sleep(0.05)
    return storage.load_metadata(session)


def test_breakdown_endpoint_and_cards(client: TestClient, monkeypatch) -> None:
    _mock_generation(monkeypatch)
    session = _accepted_session("teiera web")
    sid = session.name

    # la detail mostra il bottone Scomponi (post-accept, niente componenti)
    r = client.get(f"/sessions/{sid}")
    assert r.status_code == 200
    assert "Scomponi in componenti" in r.text

    # avvia il breakdown
    r = client.post(f"/sessions/{sid}/breakdown")
    assert r.status_code == 200
    assert "job-progress-card" in r.text

    meta = _wait_components(session)
    assert meta["components"]["count"] == 2

    # fragment card
    r = client.get(f"/sessions/{sid}/components")
    assert r.status_code == 200
    assert "Body" in r.text and "Lid" in r.text
    assert "Approva" in r.text


def test_approve_endpoint(client: TestClient, monkeypatch) -> None:
    _mock_generation(monkeypatch)
    session = _accepted_session("teiera approve")
    sid = session.name
    workflow.breakdown_into_components(session)

    r = client.post(f"/sessions/{sid}/components/body/approve")
    assert r.status_code == 200
    assert "Approvato" in r.text

    data = storage.load_components_manifest(session)
    body = next(c for c in data["components"] if c["id"] == "body")
    assert body["approved"] is True


def test_accept_triggers_breakdown(client: TestClient, monkeypatch) -> None:
    """Accettare un'idea avvia subito la scomposizione, senza generare 3D."""
    _mock_generation(monkeypatch)
    session = _session_with_iteration("teiera accept")
    sid = session.name

    # la detail mostra il bottone Accetta (non ancora accettata)
    r = client.get(f"/sessions/{sid}")
    assert r.status_code == 200
    assert "Accetta e scomponi" in r.text

    # accetta → il job avvia il breakdown
    r = client.post(f"/sessions/{sid}/accept")
    assert r.status_code == 200
    assert "job-progress-card" in r.text

    meta = _wait_components(session)
    assert meta["accepted_iteration"] == 1
    assert meta["components"]["count"] == 2
    # niente generazione 3D negli Oggetti
    assert not meta.get("model_files")
    assert not meta.get("model_task_id")


def test_breakdown_accept_gate(client: TestClient, monkeypatch) -> None:
    """Il gate globale 'Accetta decomposizione' chiude il cerchio."""
    _mock_generation(monkeypatch)
    session = _accepted_session("teiera gate")
    sid = session.name
    workflow.breakdown_into_components(session)

    # prima della chiusura: bottone gate presente, niente badge finale
    r = client.get(f"/sessions/{sid}/components")
    assert r.status_code == 200
    assert "Accetta decomposizione" in r.text
    assert "cerchio chiuso" not in r.text

    # chiusura
    r = client.post(f"/sessions/{sid}/breakdown/accept")
    assert r.status_code == 200
    assert "cerchio chiuso" in r.text
    assert "Accetta decomposizione" not in r.text

    meta = storage.load_metadata(session)
    assert meta.get("breakdown_accepted") is True


def test_no_3d_in_objects(client: TestClient, monkeypatch) -> None:
    """Le sessioni Oggetti non mostrano viewer 3D e la rotta A/B è rimossa."""
    _mock_generation(monkeypatch)
    session = _accepted_session("teiera no3d")
    sid = session.name

    r = client.get(f"/sessions/{sid}")
    assert r.status_code == 200
    assert "<model-viewer" not in r.text

    # rotta A/B rimossa
    r = client.get(f"/sessions/{sid}/ab")
    assert r.status_code == 404


def test_bundle_endpoint_roundtrip(
    client: TestClient, tmp_outputs: Path, monkeypatch
) -> None:
    """L'endpoint bundle.zip impacchetta l'handoff (components.json + viste) e,
    una volta scompattato altrove, load_components_inputs risolve tutto: prova
    server→PC end-to-end."""
    import io
    import zipfile

    from src.fusion_build_from_components import load_components_inputs

    _mock_generation(monkeypatch)
    session = _accepted_session("teiera bundle")
    sid = session.name
    workflow.breakdown_into_components(session)

    # collect: components.json + immagini, arcname relativi alla session, esistenti
    members = storage.collect_components_bundle(session)
    arcs = {arc for _, arc in members}
    assert "components/components.json" in arcs
    assert any(a.endswith(".png") for a in arcs)
    assert all(p.exists() for p, _ in members)
    assert all(not a.startswith("/") and ".." not in a for a in arcs)

    # endpoint → zip
    r = client.get(f"/sessions/{sid}/bundle.zip")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert "components/components.json" in set(zf.namelist())

    # round-trip: estrai in un root diverso e risolvi come fa la skill PC
    dest = tmp_outputs / "_pulled" / sid
    storage.extract_components_bundle(r.content, dest)
    src = load_components_inputs(dest)
    assert src["components"]
    assert all(p.exists() for p in src["design_views"].values())
    for c in src["components"]:
        assert all(p.exists() for p in c["_view_paths"].values())


def test_bundle_endpoint_404_without_components(
    client: TestClient, monkeypatch
) -> None:
    session = _accepted_session("teiera nuda")
    r = client.get(f"/sessions/{session.name}/bundle.zip")
    assert r.status_code == 404


def test_bundle_url_and_id() -> None:
    from src.main import _bundle_url_and_id

    assert _bundle_url_and_id("ses-1", "http://h:8000") == (
        "http://h:8000/sessions/ses-1/bundle.zip",
        "ses-1",
    )
    assert _bundle_url_and_id("https://h/x/sessions/abc", None) == (
        "https://h/x/sessions/abc/bundle.zip",
        "abc",
    )
    assert _bundle_url_and_id("https://h/sessions/abc/bundle.zip", None) == (
        "https://h/sessions/abc/bundle.zip",
        "abc",
    )
    with pytest.raises(ValueError):
        _bundle_url_and_id("bare-id", None)


def test_extract_bundle_rejects_traversal(tmp_path: Path) -> None:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../evil.txt", "x")
    with pytest.raises(ValueError):
        storage.extract_components_bundle(buf.getvalue(), tmp_path / "d")
