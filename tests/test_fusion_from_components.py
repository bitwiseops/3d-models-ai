"""Test degli helper deterministici di fusion_build_from_components (Parte B),
offline (niente Fusion/MCP). Costruisce un bundle componenti finto e verifica
init → save-action-plan → save-manifest → eval-paths.
"""
import json
from pathlib import Path

import pytest
from PIL import Image

from src import fusion_build, fusion_build_from_components as fbc, storage


def _make_bundle(session: Path) -> None:
    """Crea components/components.json + le 4 viste whole e per-componente."""
    def png(p: Path):
        p.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (32, 32), (180, 180, 180)).save(p)

    views = {}
    for name in fbc.VIEW_NAMES:
        rel = f"images/01_grid_{name}.png"
        png(session / rel)
        views[name] = rel

    components = []
    for cid in ("body", "lid"):
        cviews = {}
        for name in fbc.VIEW_NAMES:
            rel = f"components/{cid}/images/01_grid_{name}.png"
            png(session / rel)
            cviews[name] = rel
        components.append({
            "id": cid, "name": cid.title(), "description": f"{cid} desc",
            "relations": ([{"type": "on_top_of", "target_id": "body", "detail": "x"}] if cid == "lid" else []),
            "relative_scale": "x", "interface": "iface",
            "prompt": f"A {cid}.", "grid_image": f"components/{cid}/images/01_grid.png",
            "views": cviews, "version": 1, "status": "generated", "approved": True,
            "history": [],
        })

    manifest = {
        "schema_version": 1, "source_session": session.name,
        "main_prompt": "A teapot.", "main_subject": "A teapot.",
        "design_image": "images/01_grid.png", "design_views": views,
        "components": components, "open_questions": [], "consumed_by": [],
    }
    (session / "components").mkdir(parents=True, exist_ok=True)
    (session / "components" / "components.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )


@pytest.fixture
def bundle_session(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(storage.config, "OUTPUTS_ROOT", tmp_path)
    monkeypatch.setattr(fusion_build, "FUSION_OUTPUTS_ROOT", tmp_path / "fusion")
    monkeypatch.setattr(fbc, "FUSION_OUTPUTS_ROOT", tmp_path / "fusion")
    session = storage.create_session("teapot from components")
    _make_bundle(session)
    return session


def test_init_creates_build_dir(bundle_session: Path) -> None:
    build_dir = fbc.init_components_session(bundle_session)
    assert build_dir.exists()
    # viste whole in refs/<view>.png
    for name in fbc.VIEW_NAMES:
        assert (build_dir / "refs" / f"{name}.png").exists()
    # viste per-componente
    for cid in ("body", "lid"):
        for name in fbc.VIEW_NAMES:
            assert (build_dir / "refs" / "components" / cid / f"{name}.png").exists()
    # lenti generate (whole + componenti)
    assert (build_dir / "refs" / "lenses").exists()
    assert (build_dir / "refs" / "components" / "body" / "lenses").exists()
    # bundle copiato
    assert (build_dir / "components_source.json").exists()
    meta = json.loads((build_dir / "metadata.json").read_text())
    assert meta["status"] == "preprocessing"
    assert meta["kind"] == "fusion_build_components"
    assert len(meta["components"]) == 2
    assert set(meta["component_views"]) == {"body", "lid"}


def test_action_plan_and_manifest_flow(bundle_session: Path) -> None:
    build_dir = fbc.init_components_session(bundle_session)

    action_plan = {
        "schema_version": 1, "source_session": bundle_session.name,
        "strategy": "componenti separati + joints",
        "components": [
            {"component_id": "body", "name": "Body", "build_order": 1,
             "actions": [{"op": "sketch_revolve", "detail": "x", "reference_views": ["front"]}],
             "interface": "apertura"},
            {"component_id": "lid", "name": "Lid", "build_order": 2,
             "actions": [{"op": "extrude", "detail": "y"}], "interface": "flangia"},
        ],
        "joints": [{"from": "lid", "to": "body", "relation": "on_top_of",
                    "joint_type": "planar", "detail": "poggia"}],
        "assembly_order": ["body", "lid"], "open_questions": [],
    }
    out = fbc.save_action_plan(build_dir, action_plan)
    assert out["components"] == ["body", "lid"]
    assert out["n_joints"] == 1
    assert json.loads((build_dir / "metadata.json").read_text())["status"] == "parsing"
    assert (build_dir / "action-plan.json").exists()

    # manifest (schema fusion-build) → save condiviso, stato planning
    manifest = {
        "parts": [
            {"id": "body", "primitive_type": "cylinder", "parameters": {}, "relations": []},
            {"id": "lid", "primitive_type": "cylinder", "parameters": {},
             "relations": [{"type": "on_top_of", "target_id": "body"}]},
        ],
        "open_questions": [],
    }
    res = fbc.save_manifest(build_dir, manifest)
    assert set(res["part_ids"]) == {"body", "lid"}
    assert json.loads((build_dir / "metadata.json").read_text())["status"] == "planning"

    # plan → coverage completa
    plan = {"source_session": bundle_session.name, "strategy": "x", "steps": [
        {"id": 1, "title": "body", "manifest_parts": ["body"]},
        {"id": 2, "title": "lid", "manifest_parts": ["lid"]},
    ]}
    from src.fusion_build import plan_coverage
    fbc.save_plan(build_dir, plan)
    assert plan_coverage(build_dir, plan) == []

    # eval-paths crea la cartella iterazione
    ev = fbc.eval_paths(build_dir)
    assert ev["iteration"] == 1
    assert set(ev["views"]) == set(fbc.VIEW_NAMES)


def test_save_action_plan_rejects_bad_joint(bundle_session: Path) -> None:
    build_dir = fbc.init_components_session(bundle_session)
    bad = {
        "components": [{"component_id": "body", "actions": [{"op": "x"}]}],
        "joints": [{"from": "ghost", "to": "body"}],
    }
    with pytest.raises(ValueError, match="component_id sconosciuto"):
        fbc.save_action_plan(build_dir, bad)


def test_reinit_from_result(bundle_session: Path) -> None:
    build_dir = fbc.init_components_session(bundle_session)
    # simula una build conclusa
    (build_dir / "final.f3d").write_bytes(b"dummy")
    fbc.save_manifest(build_dir, {"parts": [{"id": "body", "primitive_type": "cylinder"}]})
    fbc.record_evaluation(build_dir, {"iteration": 1, "overall_score": 70,
                                      "actionable_gaps": ["manca smusso"]})

    res = fbc.reinit_from_result(build_dir)
    new_dir = Path(res["build_dir"])
    assert new_dir.exists()
    assert res["iteration"] == 2
    assert res["hints"] == ["manca smusso"]
    assert (new_dir / "seed.f3d").exists()
    # refs whole + componenti copiati
    assert (new_dir / "refs" / "front.png").exists()
    assert (new_dir / "refs" / "components" / "body" / "front.png").exists()
