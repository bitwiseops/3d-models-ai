"""Smoke test della web UI con FastAPI TestClient.

Non chiama Meshy reale: mocka `workflow.prepare_session` e
`workflow.generate_first_iteration` per stare offline.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from src import config, storage
from src.web import app as web_app


@pytest.fixture
def tmp_outputs(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(config, "OUTPUTS_ROOT", tmp_path)
    return tmp_path


@pytest.fixture
def client(tmp_outputs: Path):
    """TestClient con lifespan attivo (necessario perché JobManager è creato
    nel lifespan event). Yield + close per shutdown executor pulito."""
    app = web_app.create_app()
    with TestClient(app) as c:
        yield c


def _dummy_image(path: Path, w: int = 800, h: int = 450, color=(200, 100, 50)) -> Path:
    """Crea un PNG fittizio per i test."""
    img = Image.new("RGB", (w, h), color)
    img.save(path, format="PNG")
    return path


def test_index_empty(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "Nessuna sessione" in r.text


def test_healthz(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_create_session_routes(client: TestClient, tmp_outputs: Path, monkeypatch) -> None:
    """POST /sessions deve creare la session dir (sincrono) e ritornare 303
    al detail. La parte slow (image gen) è enqueueata ma non testata qui."""

    # Mocka generate_first_iteration per non chiamare Claude/Meshy
    def fake_gen(session, idea, **kwargs):
        path = session / "images" / "01.png"
        _dummy_image(path)
        # Simula update metadata come farebbe il flow reale
        from src import storage as st
        st.update_metadata(
            session,
            append_iteration={
                "version": 1,
                "prompt": "dummy",
                "image_task_id": "fake",
                "image_path": "images/01.png",
                "front_path": "images/01.png",
                "back_path": "images/01.png",
                "credits": 0,
            },
        )
        return path

    monkeypatch.setattr("src.workflow.generate_first_iteration", fake_gen)

    r = client.post(
        "/sessions", data={"idea": "smoke test"}, follow_redirects=False
    )
    assert r.status_code == 303
    location = r.headers["location"]
    assert location.startswith("/sessions/")
    session_id = location.split("/")[-1]

    # Verifica che la session dir esista subito (prepare_session è sincrono)
    session_dir = tmp_outputs / session_id
    assert session_dir.exists()
    assert (session_dir / "metadata.json").exists()

    # Aspetta che il background job finisca (poll basic con TestClient)
    import time
    deadline = time.time() + 5
    while time.time() < deadline:
        meta = storage.load_metadata(session_dir)
        if meta.get("iterations"):
            break
        time.sleep(0.05)

    meta = storage.load_metadata(session_dir)
    assert meta["iterations"], "il job background non ha appeso l'iterazione"

    # Verifica detail page
    r = client.get(f"/sessions/{session_id}")
    assert r.status_code == 200
    assert "smoke test" in r.text


def test_session_list_after_create(client: TestClient, tmp_outputs: Path) -> None:
    """Anche senza job in background, una sessione creata via storage.create_session
    deve apparire nella lista."""
    session = storage.create_session("manual session")
    r = client.get("/")
    assert r.status_code == 200
    assert "manual session" in r.text
    assert session.name in r.text


def test_file_serving(client: TestClient, tmp_outputs: Path) -> None:
    """File serving + path traversal protection."""
    session = storage.create_session("file test")
    img_path = session / "images" / "01.png"
    _dummy_image(img_path)

    # File valido
    r = client.get(f"/sessions/{session.name}/files/images/01.png")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/")

    # Non esistente → 404
    r = client.get(f"/sessions/{session.name}/files/nope.png")
    assert r.status_code == 404

    # Path traversal: httpx normalizza `..` lato client, quindi testiamo
    # encoding URL-encoded dei separatori. Anche se passa, la check con
    # `Path(path).parts` deve fermarlo.
    r = client.get(
        f"/sessions/{session.name}/files/%2e%2e%2fetc%2fpasswd"
    )
    # Accettiamo sia 400 (check fa il suo lavoro) sia 404 (file non trovato).
    # In ogni caso non deve essere 200 con contenuto di /etc/passwd.
    assert r.status_code in (400, 404)


def test_detail_renders_with_active_job(
    client: TestClient, tmp_outputs: Path
) -> None:
    """Regression: session_detail.html → _iteration_list.html → _job_progress.html
    deve renderizzare correttamente quando esiste un active_job (l'include va
    chiamato con `with job=active_job`, altrimenti 'job' is undefined)."""
    # Crea una sessione manualmente
    session = storage.create_session("detail con job")

    # Lancia un job lento per averlo "attivo" durante il render
    import time
    from src.web.routes import sessions as sessions_routes

    # Wrap directly: submit a sleep job to the JobManager via the app
    # Recuperiamo il job_manager dall'app state
    jm = client.app.state.job_manager
    jm.submit(session.name, "image", lambda: time.sleep(0.5) or "done")

    # Detail page deve renderizzare senza errori
    r = client.get(f"/sessions/{session.name}")
    assert r.status_code == 200, r.text[:500]
    assert "detail con job" in r.text


def test_feedback_requires_iteration(client: TestClient, tmp_outputs: Path) -> None:
    session = storage.create_session("nope")
    # Nessuna iterazione → feedback chiama workflow.continue_with_feedback
    # che fallisce. Il job viene comunque accettato (status=running) ma fallirà.
    r = client.post(
        f"/sessions/{session.name}/feedback", data={"feedback": "more details"}
    )
    # 200 perché la sottomissione del job non controlla i prerequisiti business
    # (è il job stesso che fallirà). Verifichiamo solo che non crashi.
    assert r.status_code == 200
