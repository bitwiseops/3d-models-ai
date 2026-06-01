"""Test del bootstrap transcript di fusion_build (puro file-IO, niente Fusion).

Copre i soli pezzi nuovi della variante `fusion-build-from-transcript`:
init da transcript, chunking deterministico, persistenza dell'action-log e la
compatibilità di save_manifest con il campo extra `expected_summary`.
"""
import json
from pathlib import Path

import pytest

from src import fusion_build


@pytest.fixture(autouse=True)
def _redirect_outputs(tmp_path: Path, monkeypatch) -> Path:
    """Reindirizza la root di output in tmp_path (FUSION_OUTPUTS_ROOT è calcolata
    all'import, quindi va patchata direttamente sul modulo)."""
    root = tmp_path / "fusion"
    monkeypatch.setattr(fusion_build, "FUSION_OUTPUTS_ROOT", root)
    return root


def _write_transcript(tmp_path: Path, text: str) -> Path:
    f = tmp_path / "transcript.txt"
    f.write_text(text, encoding="utf-8")
    return f


def test_init_transcript_session(tmp_path: Path) -> None:
    f = _write_transcript(tmp_path, "Crea uno sketch sul piano top, rettangolo 50 per 30, estrudi 10 mm.")
    build_dir = fusion_build.init_transcript_session(f, title="Porta penne")

    assert (build_dir / "transcript" / "source.txt").exists()
    assert not (build_dir / "refs").exists(), "il percorso transcript NON deve creare refs/"
    assert (build_dir / "steps").is_dir()

    meta = json.loads((build_dir / "metadata.json").read_text(encoding="utf-8"))
    assert meta["kind"] == "fusion_build_transcript"
    assert meta["status"] == "preprocessing"
    assert meta["views"] == {}
    assert meta["transcript_path"] == "transcript/source.txt"
    assert "porta-penne" in build_dir.name


def test_chunk_transcript_single_pass(tmp_path: Path) -> None:
    f = _write_transcript(tmp_path, "uno due tre quattro cinque")
    build_dir = fusion_build.init_transcript_session(f)
    out = fusion_build.chunk_transcript(build_dir, words=1500, overlap=150)

    assert out["single_pass"] is True
    assert out["count"] == 1
    assert Path(out["chunks"][0]).read_text(encoding="utf-8") == "uno due tre quattro cinque"


def test_chunk_transcript_windows_with_overlap(tmp_path: Path) -> None:
    words = [f"w{i}" for i in range(250)]
    f = _write_transcript(tmp_path, " ".join(words))
    build_dir = fusion_build.init_transcript_session(f)
    out = fusion_build.chunk_transcript(build_dir, words=100, overlap=20)

    assert out["single_pass"] is False
    assert out["count"] >= 3
    chunks = [Path(p).read_text(encoding="utf-8").split() for p in out["chunks"]]
    # ordine preservato: il primo token del primo chunk è w0
    assert chunks[0][0] == "w0"
    # overlap: la coda del chunk i ricompare in testa al chunk i+1
    assert chunks[0][-20:] == chunks[1][:20]
    # copertura completa: l'ultimo token compare nell'ultimo chunk
    assert chunks[-1][-1] == "w249"


def test_chunk_transcript_rejects_bad_overlap(tmp_path: Path) -> None:
    f = _write_transcript(tmp_path, "a b c")
    build_dir = fusion_build.init_transcript_session(f)
    with pytest.raises(ValueError):
        fusion_build.chunk_transcript(build_dir, words=100, overlap=100)


def test_save_action_log_transitions_to_parsing(tmp_path: Path) -> None:
    f = _write_transcript(tmp_path, "sketch ... estrudi ...")
    build_dir = fusion_build.init_transcript_session(f)
    log = {
        "actions": [
            {"n": 1, "op": "sketch", "target": "piano top"},
            {"n": 2, "op": "extrude", "params": {"depth_mm": 10}},
        ],
        "open_questions": ["smusso non quotato"],
    }
    out = fusion_build.save_action_log(build_dir, log)

    assert out["n_actions"] == 2
    assert out["open_questions"] == ["smusso non quotato"]
    assert (build_dir / "transcript" / "action_log.json").exists()
    meta = json.loads((build_dir / "metadata.json").read_text(encoding="utf-8"))
    assert meta["status"] == "parsing"


def test_save_action_log_rejects_invalid(tmp_path: Path) -> None:
    f = _write_transcript(tmp_path, "x")
    build_dir = fusion_build.init_transcript_session(f)
    with pytest.raises(ValueError):
        fusion_build.save_action_log(build_dir, {"actions": []})
    with pytest.raises(ValueError):
        fusion_build.save_action_log(build_dir, {"actions": [{"target": "manca op"}]})


def test_save_manifest_accepts_expected_summary(tmp_path: Path) -> None:
    """Il manifest della variante transcript porta expected_summary: save_manifest
    (riusato invariato) deve lasciar passare il campo extra e portare a 'planning'."""
    f = _write_transcript(tmp_path, "sketch ...")
    build_dir = fusion_build.init_transcript_session(f)
    fusion_build.save_action_log(build_dir, {"actions": [{"n": 1, "op": "sketch"}]})

    manifest = {
        "parts": [{"id": "base", "primitive_type": "cube", "parameters": {"size_mm": [50, 30, 10]}}],
        "expected_summary": "Una base rettangolare 50x30x10 mm.",
        "open_questions": [],
    }
    out = fusion_build.save_manifest(build_dir, manifest)

    assert out["part_ids"] == ["base"]
    saved = json.loads((build_dir / "manifest.json").read_text(encoding="utf-8"))
    assert saved["expected_summary"] == "Una base rettangolare 50x30x10 mm."
    meta = json.loads((build_dir / "metadata.json").read_text(encoding="utf-8"))
    assert meta["status"] == "planning"
