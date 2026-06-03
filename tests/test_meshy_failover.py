"""Test del failover chiave Meshy (principale → fallback su crediti esauriti),
offline: niente rete, lo `starter` è simulato.
"""
import types

import pytest

from src import config, meshy_client


@pytest.fixture
def two_keys(monkeypatch):
    monkeypatch.setattr(config, "MESHY_API_KEY", "msy_primary")
    monkeypatch.setattr(config, "MESHY_API_KEY_FALLBACK", "msy_fallback")
    return ["msy_primary", "msy_fallback"]


def test_resolve_keys_order(two_keys):
    assert meshy_client.resolve_keys(None) == ["msy_primary", "msy_fallback"]
    # api_key == principale → comunque l'intera catena (principale + fallback)
    assert meshy_client.resolve_keys("msy_primary") == ["msy_primary", "msy_fallback"]


def test_resolve_keys_explicit_override(two_keys):
    # una key esplicita estranea (es. test key) viene usata da sola
    assert meshy_client.resolve_keys("msy_dummy_test") == ["msy_dummy_test"]


def test_failover_uses_fallback_when_primary_out(two_keys):
    seen = []

    def starter(key):
        seen.append(key)
        if key == "msy_primary":
            raise meshy_client.MeshyInsufficientCredits("primary out")
        return "task-123"

    task_id, used = meshy_client.create_with_failover(starter, api_keys=two_keys)
    assert task_id == "task-123"
    assert used == "msy_fallback"
    assert seen == ["msy_primary", "msy_fallback"]


def test_failover_first_key_ok_no_switch(two_keys):
    def starter(key):
        return "task-ok"

    task_id, used = meshy_client.create_with_failover(starter, api_keys=two_keys)
    assert (task_id, used) == ("task-ok", "msy_primary")


def test_failover_all_out_raises(two_keys):
    def starter(key):
        raise meshy_client.MeshyInsufficientCredits(f"{key} out")

    with pytest.raises(meshy_client.MeshyInsufficientCredits):
        meshy_client.create_with_failover(starter, api_keys=two_keys)


def test_failover_non_credit_error_not_retried(two_keys):
    seen = []

    def starter(key):
        seen.append(key)
        raise meshy_client.MeshyError("400 bad request")

    with pytest.raises(meshy_client.MeshyError):
        meshy_client.create_with_failover(starter, api_keys=two_keys)
    # un errore non-crediti NON deve far tentare la chiave successiva
    assert seen == ["msy_primary"]


def _fake_resp(status_code: int, text: str):
    return types.SimpleNamespace(status_code=status_code, text=text, ok=200 <= status_code < 300)


def test_raise_for_post_classifies_credit_errors():
    with pytest.raises(meshy_client.MeshyInsufficientCredits):
        meshy_client._raise_for_post(_fake_resp(402, "Payment Required"), "x")
    with pytest.raises(meshy_client.MeshyInsufficientCredits):
        meshy_client._raise_for_post(_fake_resp(400, "insufficient credits"), "x")
    # un 400 generico resta MeshyError (non scatena failover)
    with pytest.raises(meshy_client.MeshyError) as ei:
        meshy_client._raise_for_post(_fake_resp(400, "invalid prompt"), "x")
    assert not isinstance(ei.value, meshy_client.MeshyInsufficientCredits)
