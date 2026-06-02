"""Test della scomposizione in componenti (Parte A), con Claude e Meshy mockati.

Esercita la VERA orchestrazione di workflow.breakdown_into_components /
regenerate_component (proposta → refine per pezzo → grid 2x2 → crop → salvataggio
del bundle components/components.json), sostituendo solo i due seam esterni:
le chiamate Claude (propose/refine) e la generazione immagine Meshy.
"""
import json
from pathlib import Path

import pytest
from PIL import Image

from src import storage, workflow


@pytest.fixture
def accepted_session(tmp_path: Path, monkeypatch) -> Path:
    """Sessione Oggetti con un'iterazione accettata (grid 2x2 reale + 4 crop)."""
    monkeypatch.setattr(storage.config, "OUTPUTS_ROOT", tmp_path)
    session = storage.create_session("teiera con coperchio e manico")

    # grid 2x2 fittizia + crop, come li produrrebbe _generate_image
    grid = session / "images" / "01_grid.png"
    Image.new("RGB", (64, 64), (200, 180, 160)).save(grid)
    from src import grid_utils

    crops = grid_utils.crop_grid_2x2(grid, prefix=grid.stem)
    storage.update_metadata(
        session,
        append_iteration={
            "version": 1,
            "prompt": "A squat ceramic teapot with a domed lid and a curved handle.",
            "image_task_id": "task-1",
            "image_path": "images/01_grid.png",
            "grid_path": "images/01_grid.png",
            "image_paths_grid": [str(c.relative_to(session)) for c in crops],
            "credits": 9,
        },
        accepted_iteration=1,
        image_task_id="task-1",
    )
    return session


def _mock_meshy(monkeypatch):
    """Sostituisce la generazione immagine: niente rete, scrive una grid 2x2 vera."""
    def fake_meshy_grid(session, prompt, *, references=None, label="", on_progress=None, api_key=None):
        return "fake://grid", "task-comp", 9

    def fake_save(session, component_id, version, url):
        path = storage.create_component_dir(session, component_id) / "images" / f"{version:02d}_grid.png"
        Image.new("RGB", (64, 64), (120, 120, 200)).save(path)
        return path

    monkeypatch.setattr(workflow, "_meshy_grid", fake_meshy_grid)
    monkeypatch.setattr(storage, "save_component_image_from_url", fake_save)


def _mock_claude(monkeypatch, *, components):
    monkeypatch.setattr(
        workflow.prompt_refiner,
        "propose_components",
        lambda subject, img: {"components": components, "open_questions": ["spout?"]},
    )
    monkeypatch.setattr(
        workflow.prompt_refiner,
        "refine_component",
        lambda subject, img, comp, siblings, feedback=None: (
            f"A {comp['id']} component."
            + (f" feedback={feedback}" if feedback else "")
        ),
    )
    # niente chiamate API per il tracking usage
    monkeypatch.setattr(
        workflow.prompt_refiner.usage, "record_response", lambda *a, **k: None
    )


COMPONENTS = [
    {
        "id": "body",
        "name": "Teapot body",
        "description": "Squat rounded body",
        "relations": [{"type": "concentric_with", "target_id": "lid", "detail": "collo riceve la flangia"}],
        "relative_scale": "~70% altezza",
        "interface": "apertura superiore Ø40",
    },
    {
        "id": "lid",
        "name": "Lid",
        "description": "Domed lid",
        "relations": [{"type": "on_top_of", "target_id": "body", "detail": "poggia sul collo"}],
        "relative_scale": "~15% altezza",
        "interface": "flangia che entra nel collo",
    },
    {
        "id": "handle",
        "name": "Handle",
        "description": "Curved handle",
        "relations": [{"type": "attached_to", "target_id": "body", "detail": "fianco"}],
        "relative_scale": "~30% altezza",
        "interface": "due punti di attacco al corpo",
    },
]


def test_breakdown_produces_bundle(accepted_session: Path, monkeypatch) -> None:
    _mock_meshy(monkeypatch)
    _mock_claude(monkeypatch, components=COMPONENTS)

    manifest = workflow.breakdown_into_components(accepted_session)

    assert len(manifest["components"]) == 3
    cj = accepted_session / "components" / "components.json"
    assert cj.exists()
    data = json.loads(cj.read_text())

    # contratto di handoff: prompt principale + viste design
    assert data["main_prompt"].startswith("A squat ceramic teapot")
    assert set(data["design_views"]) == {"front", "three_quarter", "side", "back"}

    for comp in data["components"]:
        # 4 viste per componente, esistenti su disco
        assert set(comp["views"]) == {"front", "three_quarter", "side", "back"}
        for rel in comp["views"].values():
            assert (accepted_session / rel).exists()
        assert comp["version"] == 1
        assert comp["approved"] is False
        # relations.target_id risolvibili
        ids = {c["id"] for c in data["components"]}
        for r in comp["relations"]:
            assert r["target_id"] in ids

    # sommario in metadata.json
    meta = storage.load_metadata(accepted_session)
    assert meta["components"]["count"] == 3
    assert meta["components"]["all_approved"] is False


def test_breakdown_requires_acceptance(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(storage.config, "OUTPUTS_ROOT", tmp_path)
    session = storage.create_session("oggetto non accettato")
    storage.update_metadata(
        session,
        append_iteration={"version": 1, "prompt": "x", "image_task_id": "t",
                          "image_path": "images/01_grid.png", "grid_path": "images/01_grid.png",
                          "image_paths_grid": [], "credits": 0},
    )
    with pytest.raises(RuntimeError, match="non accettata"):
        workflow.breakdown_into_components(session)


def test_breakdown_guard_already_done(accepted_session: Path, monkeypatch) -> None:
    _mock_meshy(monkeypatch)
    _mock_claude(monkeypatch, components=COMPONENTS)
    workflow.breakdown_into_components(accepted_session)
    with pytest.raises(RuntimeError, match="già scomposta"):
        workflow.breakdown_into_components(accepted_session)
    # force rigenera
    again = workflow.breakdown_into_components(accepted_session, force=True)
    assert len(again["components"]) == 3


def test_regenerate_component_bumps_version(accepted_session: Path, monkeypatch) -> None:
    _mock_meshy(monkeypatch)
    _mock_claude(monkeypatch, components=COMPONENTS)
    workflow.breakdown_into_components(accepted_session)

    workflow.regenerate_component(
        accepted_session, "lid", "rendi la cupola più alta", skip_open=True
    )
    data = json.loads((accepted_session / "components" / "components.json").read_text())
    lid = next(c for c in data["components"] if c["id"] == "lid")
    assert lid["version"] == 2
    assert lid["approved"] is False
    assert len(lid["history"]) == 2
    assert lid["history"][-1]["feedback"] == "rendi la cupola più alta"


def test_approve_component(accepted_session: Path, monkeypatch) -> None:
    _mock_meshy(monkeypatch)
    _mock_claude(monkeypatch, components=COMPONENTS)
    workflow.breakdown_into_components(accepted_session)
    workflow.approve_component(accepted_session, "body")
    data = json.loads((accepted_session / "components" / "components.json").read_text())
    body = next(c for c in data["components"] if c["id"] == "body")
    assert body["approved"] is True
