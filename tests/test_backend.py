"""Tests du backend : logique métier et protocole WebSocket."""
import time

import pytest
from fastapi.testclient import TestClient

from app import main
from app.main import Party, app, parties


@pytest.fixture(autouse=True)
def _clean():
    parties.clear()
    yield
    parties.clear()


def test_slug():
    assert main.slug("Anniv Sylvain !") == "anniv-sylvain"
    assert main.slug("") == "soiree"


def test_alert_lifecycle():
    p = Party("test")
    c = p.register_chalet("mesange", "Mésange", "Léo 4 ans")
    assert c.status() == "offline"
    p.heartbeat(c, 10, 80, 55)
    assert c.status() == "ok"
    p.noise(c, 60)
    assert c.status() == "noise"
    p.raise_alert(c, 90, None)
    assert c.status() == "alert"
    p.raise_alert(c, 95, "data:audio/webm;base64,AAAA")  # alerte pendant une alerte : fusion, pas de doublon
    assert sum(e["kind"] == "alert" for e in p.events) == 1
    assert c.alert["clip"] and c.alert["level"] == 95
    p.ack(c, "Marie")
    assert c.status() == "acked" and c.alert["acked_by"] == "Marie"
    p.resolve(c, "Marie")
    assert c.status() == "ok" and c.alert is None


def test_watchdog_offline_and_escalation(monkeypatch):
    p = Party("test")
    c = p.register_chalet("pinson", "Pinson", "")
    p.heartbeat(c, 5, None, None)
    p.raise_alert(c, 80, None)
    assert not p.watchdog()
    c.last_hb -= main.HEARTBEAT_TIMEOUT + 1
    c.alert["started"] -= main.ESCALATION_DELAY + 1
    assert p.watchdog()
    assert c.online is False and c.alert["escalated"] is True
    assert c.status() == "escalated"
    kinds = [e["kind"] for e in p.events]
    assert "offline" in kinds and "escalated" in kinds


def test_clip_too_big_is_dropped():
    p = Party("test")
    c = p.register_chalet("a", "A", "")
    p.raise_alert(c, 80, "x" * (main.MAX_CLIP_BYTES + 1))
    assert c.alert["clip"] is None
    assert c.to_dict()["alert"]["has_clip"] is False


def test_http_endpoints():
    client = TestClient(app)
    assert client.get("/api/health").json()["ok"] is True
    assert client.get("/").status_code == 200
    state = client.get("/api/party/Ma Soirée").json()
    assert state["code"] == "ma-soiree" and state["chalets"] == []
    assert client.get("/api/party/ma-soiree/chalet/nope/clip").status_code == 404


def test_websocket_flow():
    client = TestClient(app)
    with client.websocket_connect("/ws/fete") as emitter, client.websocket_connect("/ws/fete") as receiver:
        assert emitter.receive_json()["type"] == "state"
        assert receiver.receive_json()["type"] == "state"
        receiver.send_json({"type": "hello", "role": "salle", "name": "Marie"})
        receiver.receive_json(); emitter.receive_json()  # diffusion après hello

        emitter.send_json({"type": "register", "chalet_id": "mesange", "name": "Mésange", "kids": "Léo"})
        assert emitter.receive_json() == {"type": "registered", "chalet_id": "mesange"}
        st = receiver.receive_json(); emitter.receive_json()
        assert st["chalets"][0]["name"] == "Mésange" and "Marie" in st["receivers"]

        emitter.send_json({"type": "hb", "level": 12, "battery": 77, "threshold": 50})
        st = receiver.receive_json(); emitter.receive_json()
        assert st["type"] == "state" and st["chalets"][0]["status"] == "ok"

        emitter.send_json({"type": "hb", "level": 20, "battery": 76, "threshold": 50})  # niveau seul → message léger
        lv = receiver.receive_json(); emitter.receive_json()
        assert lv["type"] == "level" and lv["level"] == 20

        emitter.send_json({"type": "alert", "level": 90})
        st = receiver.receive_json(); emitter.receive_json()
        assert st["chalets"][0]["status"] == "alert"

        receiver.send_json({"type": "ack", "chalet_id": "mesange"})
        st = receiver.receive_json(); emitter.receive_json()
        assert st["chalets"][0]["alert"]["acked_by"] == "Marie"

        receiver.send_json({"type": "resolve", "chalet_id": "mesange"})
        st = receiver.receive_json(); emitter.receive_json()
        assert st["chalets"][0]["status"] == "ok"

        receiver.send_json({"type": "ping"})
        assert receiver.receive_json()["type"] == "pong"
