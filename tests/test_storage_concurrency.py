"""Test che `storage.update_metadata` sia thread-safe e atomico."""
import json
import threading
from pathlib import Path

import pytest

from src import storage


@pytest.fixture
def session_dir(tmp_path: Path, monkeypatch) -> Path:
    """Crea una session dir reale via storage.create_session, in tmp_path."""
    monkeypatch.setattr(storage.config, "OUTPUTS_ROOT", tmp_path)
    return storage.create_session("test session")


def test_concurrent_append_iterations(session_dir: Path) -> None:
    """20 thread che ognuno appende un'iterazione → la lista finale deve
    contenere tutte e 20 le voci e il JSON deve essere ben formato."""
    N = 20

    def worker(i: int) -> None:
        storage.update_metadata(
            session_dir,
            append_iteration={
                "version": i,
                "prompt": f"prompt-{i}",
                "image_task_id": f"task-{i}",
                "image_path": f"images/{i:02d}.png",
                "front_path": f"images/{i:02d}_front.png",
                "back_path": f"images/{i:02d}_back.png",
                "credits": 1,
            },
        )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    data = json.loads((session_dir / "metadata.json").read_text())
    versions = sorted(it["version"] for it in data["iterations"])
    assert versions == list(range(N)), "non tutte le iterazioni sono state appese"
    assert len(data["iterations"]) == N


def test_concurrent_add_credits(session_dir: Path) -> None:
    """50 thread che aggiungono ognuno 1 credit → totale deve essere 50."""
    N = 50

    def worker() -> None:
        storage.update_metadata(session_dir, add_credits={"text_to_image": 1})

    threads = [threading.Thread(target=worker) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    data = json.loads((session_dir / "metadata.json").read_text())
    assert data["credits"]["text_to_image"] == N


def test_atomic_write_no_partial_file(session_dir: Path, tmp_path: Path) -> None:
    """Dopo molte scritture concorrenti il file deve sempre essere JSON valido
    (mai vuoto, mai troncato). Test indiretto: nessuna eccezione di parsing."""
    N = 100

    def worker(i: int) -> None:
        for _ in range(5):
            storage.update_metadata(session_dir, last_touch=f"thread-{i}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Se in un qualche momento il file era stato visto parzialmente scritto,
    # qualche reader avrebbe fallito. Verifichiamo che il finale sia valido.
    data = storage.load_metadata(session_dir)
    assert "last_touch" in data
    assert data["last_touch"].startswith("thread-")
