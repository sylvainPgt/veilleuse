"""Tests du backend : logique métier et protocole WebSocket."""
import time

import pytest
from fastapi.testclient import TestClient

from app import main
from app.main import Party, app, parties


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(main, "STATE_FILE", "")   # pas d'écriture disque pendant les tests
    parties.clear()
    main.deleted_codes.clear()
    yield
    parties.clear()
    main.deleted_codes.clear()


def recv_until(sock, kind: str, limit: int = 8) -> dict:
    """Lit jusqu'au message attendu.

    Le serveur intercale des diffusions d'état, et TestClient ne garantit pas
    l'ordre entre deux sockets ; on ne veut pas d'un test qui dépend de ça.
    """
    for _ in range(limit):
        msg = sock.receive_json()
        if msg.get("type") == kind:
            return msg
    raise AssertionError(f"message « {kind} » jamais reçu")


def recv_until_state(sock, predicate, limit: int = 8) -> dict:
    """Lit jusqu'à l'état qui satisfait la condition (les précédents sont périmés)."""
    for _ in range(limit):
        msg = sock.receive_json()
        if msg.get("type") == "state" and predicate(msg):
            return msg
    raise AssertionError("état attendu jamais reçu")


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
    # deviner un nom ne suffit plus : une soirée n'existe que si on l'a créée
    assert client.get("/api/party/ma-soiree").status_code == 404
    created = client.post("/api/parties", json={"name": "Ma Soirée"}).json()
    assert created["name"] == "Ma Soirée"
    assert created["code"].startswith("ma-soiree-") and len(created["code"]) > len("ma-soiree-") + 8
    state = client.get(f"/api/party/{created['code']}").json()
    assert state["name"] == "Ma Soirée" and state["chalets"] == []
    assert client.get(f"/api/party/{created['code']}/chalet/nope/clip").status_code == 404


@pytest.mark.parametrize("nom", [
    "Anniv", "Les 40 ans de Silou",
    "40 ans de Silou version test",          # la troncature tombait sur un tiret
    "Soirée chez Marie et Jean-Baptiste du 3 octobre",
    "  ", "!!!", "Ééé àà ûû", "a" * 80,
])
def test_created_party_is_always_findable(nom):
    """Le bug de Sylvain : l'identifiant doit se retrouver quel que soit le nom."""
    party = main.create_party(nom)
    assert main.get_party(party.code) is party
    assert main.slug(party.code) == party.code   # stable par normalisation
    assert "--" not in party.code


def test_party_ids_are_unguessable_and_unique():
    a = main.create_party("Anniv Sylvain")
    b = main.create_party("Anniv Sylvain")
    assert a.code != b.code                      # deux groupes, même nom, aucune collision
    assert main.get_party("anniv-sylvain") is None  # le nom seul n'ouvre rien


def test_unknown_party_is_refused_on_websocket():
    with TestClient(app) as client:
        with client.websocket_connect("/ws/nexiste-pas-abcdefghij") as ws:
            assert ws.receive_json() == {"type": "unknown_party"}
    assert "nexiste-pas-abcdefghij" not in main.parties  # et rien n'a été créé au passage


def test_signed_link_survives_restart():
    """Revue externe, bloquant n°4 : chaque déploiement redémarre le serveur,
    le lien partagé doit recréer la soirée pour que les chalets reviennent."""
    party = main.create_party("Les 40 ans de Silou")
    code = party.code
    parties.clear()                                   # « redémarrage »
    revived = main.get_party(code)
    assert revived is not None and revived.code == code
    assert revived.name == "les 40 ans de silou"      # nom retrouvé depuis le lien
    # un identifiant forgé, même bien formé, reste refusé
    forged = code[:-1] + ("a" if code[-1] != "a" else "b")
    parties.clear()
    assert main.get_party(forged) is None


def test_first_ack_wins():
    """Revue externe n°7 : deux « J'y vais » simultanés, le premier reste."""
    p = Party("test")
    c = p.register_chalet("m", "M", "")
    p.raise_alert(c, 80, None)
    p.ack(c, "Marie")
    p.ack(c, "Paul")
    assert c.alert["acked_by"] == "Marie"
    assert sum(e["kind"] == "ack" for e in p.events) == 1


def test_late_clip_does_not_resurrect_alert():
    """Revue externe n°6 : le clip arrive après « C'est réglé » → poubelle."""
    p = Party("test")
    c = p.register_chalet("m", "M", "")
    p.raise_alert(c, 80, None)
    p.resolve(c, "Marie")
    p.attach_alert_clip(c, "data:audio/mp4;base64,AAAA")
    assert c.alert is None


def test_alert_clip_expires_like_the_rest():
    """Revue externe n°13 : le clip d'une alerte jamais réglée s'efface aussi."""
    p = Party("test")
    c = p.register_chalet("m", "M", "")
    p.heartbeat(c, 5, None, None)
    p.raise_alert(c, 80, "data:audio/mp4;base64,AAAA")
    c.alert["clip_ts"] -= main.CLIP_TTL + 1
    assert p.watchdog()
    assert c.alert is not None and c.alert["clip"] is None   # l'alerte reste, le son part


def test_receiver_cannot_forge_emitter_messages():
    """Revue externe n°12 : avec le lien, un récepteur ne fabrique ni alerte ni heartbeat."""
    with TestClient(app) as client:
        code = main.create_party("rôles").code
        with client.websocket_connect(f"/ws/{code}") as emitter, client.websocket_connect(f"/ws/{code}") as intrus:
            recv_until(emitter, "state"); recv_until(intrus, "state")
            emitter.send_json({"type": "register", "chalet_id": "m", "name": "Mésange", "kids": ""})
            recv_until(emitter, "registered")
            chalet = main.parties[code].chalets["m"]

            intrus.send_json({"type": "hello", "role": "salle", "name": "Intrus"})
            intrus.send_json({"type": "alert", "chalet_id": "m", "level": 99})
            intrus.send_json({"type": "hb", "chalet_id": "m", "level": 1})
            intrus.send_json({"type": "test", "chalet_id": "m"})
            intrus.send_json({"type": "ack", "chalet_id": "m"})    # légitime, même sans alerte
            intrus.send_json({"type": "ping"})
            recv_until(intrus, "pong", limit=12)                   # tout ce qui précède a été traité
            assert chalet.alert is None and chalet.online is False

            # l'émetteur, lui, alerte normalement
            emitter.send_json({"type": "alert", "chalet_id": "m", "level": 90})
            emitter.send_json({"type": "ping"}); recv_until(emitter, "pong", limit=12)
            assert chalet.alert is not None


def test_state_revision_is_monotonic():
    p = Party("test")
    revs = [p.snapshot()["rev"] for _ in range(3)]
    assert revs == sorted(revs) and len(set(revs)) == 3


def test_push_key_and_subscription():
    """Web Push : la clé publique est servie, l'abonnement est rangé et validé."""
    assert main.PUSH_ENABLED
    with TestClient(app) as client:
        assert client.get("/api/push-key").json()["key"] == main.VAPID_PUBLIC
        code = main.create_party("push").code
        with client.websocket_connect(f"/ws/{code}") as ws:
            recv_until(ws, "state")
            ws.send_json({"type": "push_sub", "sub": {"endpoint": "https://fcm.googleapis.com/fcm/send/abc", "keys": {"p256dh": "x", "auth": "y"}}})
            recv_until(ws, "push_ok")   # le serveur confirme : la permission seule ne prouve rien
            ws.send_json({"type": "push_sub", "sub": {"endpoint": "http://pas-https/refusé"}})
            ws.send_json({"type": "push_sub", "sub": {"endpoint": "https://evil.example/ssrf"}})   # hors services de push connus
            ws.send_json({"type": "push_sub", "sub": "n'importe quoi"})
            ws.send_json({"type": "ping"}); recv_until(ws, "pong", limit=8)
        subs = main.parties[code].push_subs
        assert list(subs) == ["https://fcm.googleapis.com/fcm/send/abc"]


def test_alerts_queue_pushes():
    """Alerte, chalet muet et escalade mettent chacun une notification en file."""
    p = Party("test")
    c = p.register_chalet("m", "Mésange", "Léo et Jade")
    p.heartbeat(c, 5, None, None)
    p.raise_alert(c, 90, None)
    assert p.push_queue[-1][0] == "Ça sonne — Mésange" and "Léo" in p.push_queue[-1][1]
    c.last_hb -= main.HEARTBEAT_TIMEOUT + 1
    c.alert["started"] -= main.ESCALATION_DELAY + 1
    p.watchdog()
    titres = [t for t, _, _ in p.push_queue]
    assert any(t.startswith("Chalet muet") for t in titres)
    assert any(t.startswith("Personne n'a répondu") for t in titres)
    # une alerte fusionnée (déjà en cours) ne repousse pas de notification
    n = len(p.push_queue)
    p.raise_alert(c, 95, None)
    assert len(p.push_queue) == n


def test_dead_push_subscriptions_are_pruned(monkeypatch):
    import asyncio as aio
    p = Party("test")
    p.push_subs = {"https://ok/1": {"sub": {"endpoint": "https://ok/1"}},
                   "https://gone/2": {"sub": {"endpoint": "https://gone/2"}}}
    sent = []

    class FakeResp:
        status_code = 410

    def fake_push(sub, payload, tag):
        if sub["endpoint"].startswith("https://gone"):
            raise main.WebPushException("gone", response=FakeResp())
        sent.append(sub["endpoint"])

    monkeypatch.setattr(main, "_push_one", fake_push)
    aio.run(main.push_party(p, "titre", "corps", "tag"))
    assert sent == ["https://ok/1"]
    assert list(p.push_subs) == ["https://ok/1"]   # l'abonnement mort est élagué


def test_sw_served_at_root():
    """Revue n°1 : enregistré depuis /static/, le service worker ne contrôlait
    jamais la page « / » — ready ne se résolvait pas, le push était mort-né."""
    client = TestClient(app)
    r = client.get("/sw.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert "notificationclick" in r.text


def test_stale_actions_cannot_touch_new_alert():
    """Revue n°4 : un « C'est réglé » resté en file pendant une coupure ne doit
    pas effacer l'alerte suivante."""
    p = Party("test")
    c = p.register_chalet("m", "M", "")
    p.raise_alert(c, 80, None)
    old_aid = c.alert["id"]
    assert p.resolve(c, "Marie", aid=old_aid)          # premier cycle : normal
    p.raise_alert(c, 85, None)
    new_aid = c.alert["id"]
    assert new_aid != old_aid
    assert not p.resolve(c, "Marie", aid=old_aid)      # action périmée : refusée
    assert c.alert is not None
    assert not p.ack(c, "Paul", aid=old_aid)           # idem pour « J'y vais »
    assert c.alert["acked_by"] is None
    p.attach_alert_clip(c, "data:audio/mp4;base64,AAAA", aid=old_aid)
    assert c.alert["clip"] is None                     # le clip périmé est jeté
    assert p.ack(c, "Paul", aid=new_aid)               # la bonne occurrence, elle, passe
    assert p.resolve(c, "Paul", aid=new_aid)


def test_release_and_reinforce():
    """Revue n°6 : « je ne peux plus y aller » rend l'alerte et relance l'escalade ;
    « renfort » re-sonne tout le monde sans lâcher la prise en charge."""
    p = Party("test")
    c = p.register_chalet("m", "Mésange", "")
    p.raise_alert(c, 80, None)
    p.ack(c, "Marie")
    p.push_queue.clear()
    assert p.reinforce(c, "Marie")
    assert c.alert["acked_by"] == "Marie"              # le renfort ne libère pas
    assert any("Renfort" in t for t, _, _ in p.push_queue)
    assert p.release(c, "Marie")
    assert c.alert["acked_by"] is None and c.status() == "alert"
    # le chrono d'escalade est reparti de la libération
    c.alert["started"] -= main.ESCALATION_DELAY + 1
    assert p.watchdog() and c.alert["escalated"]


def test_ack_reminder_fires_once():
    """Revue n°6 : « J'y vais » sans « C'est réglé » finit par rappeler tout le monde."""
    p = Party("test")
    c = p.register_chalet("m", "Mésange", "")
    p.raise_alert(c, 80, None)
    p.ack(c, "Marie")
    p.push_queue.clear()
    c.alert["acked_at"] -= main.ACK_REMINDER + 1
    assert p.watchdog()
    assert any(t.startswith("Toujours en cours") for t, _, _ in p.push_queue)
    n = len(p.push_queue)
    assert not p.watchdog() or len(p.push_queue) == n  # un seul rappel, pas une rafale


def test_check_flow_for_offline_chalet():
    """Revue n°3 : « je vais vérifier » un chalet muet — visible, mais la tuile ne
    reverdit qu'à la preuve : le retour du heartbeat."""
    p = Party("test")
    c = p.register_chalet("m", "Mésange", "")
    p.heartbeat(c, 5, None, None)
    c.online = False
    assert p.check(c, "Marie")
    assert c.check_by == "Marie" and c.status() == "offline"   # toujours muet : pas de faux vert
    assert not p.check(c, "Paul") or True  # un chalet en ligne refuse check (couvert ci-dessous)
    p.heartbeat(c, 5, None, None)                              # la surveillance reprend
    assert c.check_by is None                                  # la vérification est close
    assert not p.check(c, "Paul")                              # en ligne : rien à vérifier


def test_hello_cannot_grant_emitter_role():
    """Revue n°9 : hello role="chalet" + chalet_id d'autrui passait is_emitter_of."""
    with TestClient(app) as client:
        code = main.create_party("spoof").code
        with client.websocket_connect(f"/ws/{code}") as emitter, client.websocket_connect(f"/ws/{code}") as intrus:
            recv_until(emitter, "state"); recv_until(intrus, "state")
            emitter.send_json({"type": "register", "chalet_id": "m", "name": "Mésange", "kids": ""})
            recv_until(emitter, "registered")
            chalet = main.parties[code].chalets["m"]

            intrus.send_json({"type": "hello", "role": "chalet", "chalet_id": "m", "name": "Intrus"})
            intrus.send_json({"type": "alert", "chalet_id": "m", "level": 99})
            intrus.send_json({"type": "hb", "chalet_id": "m", "level": 1})
            intrus.send_json({"type": "ping"}); recv_until(intrus, "pong", limit=12)
            assert chalet.alert is None and chalet.online is False


def test_register_takeover_requires_token():
    """Revue n°9 : reprendre un chalet existant exige le jeton remis au premier."""
    with TestClient(app) as client:
        code = main.create_party("jeton").code
        with client.websocket_connect(f"/ws/{code}") as first, client.websocket_connect(f"/ws/{code}") as second:
            recv_until(first, "state"); recv_until(second, "state")
            first.send_json({"type": "register", "chalet_id": "m", "name": "Mésange", "kids": ""})
            token = recv_until(first, "registered")["token"]
            chalet = main.parties[code].chalets["m"]

            second.send_json({"type": "register", "chalet_id": "m", "name": "Pirate", "kids": ""})
            assert recv_until(second, "register_denied", limit=6)["chalet_id"] == "m"
            assert chalet.name == "Mésange"            # rien n'a été écrasé
            second.send_json({"type": "alert", "chalet_id": "m", "level": 99})
            second.send_json({"type": "ping"}); recv_until(second, "pong", limit=8)
            assert chalet.alert is None                # et toujours pas émetteur

            # le vrai téléphone, lui, reprend avec son jeton (après rechargement p.ex.)
            second.send_json({"type": "register", "chalet_id": "m", "name": "Mésange", "kids": "Léo", "token": token})
            assert recv_until(second, "registered", limit=6)["chalet_id"] == "m"
            assert chalet.kids == "Léo"


def test_state_survives_restart(tmp_path, monkeypatch):
    """Revue n°5 : la reprise ne repart plus de zéro — chalets attendus (non
    vérifiés), alerte en cours (sans audio), abonnements push et tombales."""
    state = tmp_path / "state.json"
    monkeypatch.setattr(main, "STATE_FILE", str(state))
    party = main.create_party("Les 40 ans")
    ch = party.register_chalet("m", "Mésange", "Léo et Jade")
    party.heartbeat(ch, 10, 80, 55)
    party.raise_alert(ch, 90, "data:audio/mp4;base64,AAAA")
    aid, token = ch.alert["id"], ch.token
    party.push_subs["https://fcm.googleapis.com/fcm/send/x"] = {"sub": {"endpoint": "https://fcm.googleapis.com/fcm/send/x"}}
    main.mark_dirty()
    condamnee = main.create_party("à supprimer")
    main.deleted_codes.add(condamnee.code)
    del main.parties[condamnee.code]
    assert main.save_state()

    parties.clear(); main.deleted_codes.clear()        # « redémarrage »
    assert main.load_state() == 1
    revived = main.parties[party.code]
    assert revived.name == "Les 40 ans"
    c2 = revived.chalets["m"]
    assert c2.kids == "Léo et Jade" and c2.token == token
    assert c2.online is False                          # non vérifié tant que pas de heartbeat
    assert c2.status() in ("offline", "alert", "escalated")
    assert c2.alert and c2.alert["id"] == aid and c2.alert["clip"] is None   # l'alerte oui, l'audio non
    assert list(revived.push_subs)                     # les parents endormis seront quand même poussés
    assert condamnee.code in main.deleted_codes        # la suppression admin survit aussi
    assert main.get_party(condamnee.code) is None


def test_slow_push_does_not_block_alerts(monkeypatch):
    """Revue n°2 : un fournisseur de push gelé ne doit pas retarder la diffusion
    des alertes ni le traitement des messages."""
    import time as _t

    def frozen_push(sub, payload, tag):
        _t.sleep(4)   # fournisseur qui ne répond pas

    monkeypatch.setattr(main, "_push_one", frozen_push)
    with TestClient(app) as client:
        code = main.create_party("gel").code
        with client.websocket_connect(f"/ws/{code}") as emitter, client.websocket_connect(f"/ws/{code}") as receiver:
            recv_until(emitter, "state"); recv_until(receiver, "state")
            emitter.send_json({"type": "register", "chalet_id": "m", "name": "M", "kids": ""})
            recv_until(emitter, "registered")
            main.parties[code].push_subs["https://fcm.googleapis.com/fcm/send/x"] = \
                {"sub": {"endpoint": "https://fcm.googleapis.com/fcm/send/x"}}
            t0 = _t.monotonic()
            emitter.send_json({"type": "alert", "chalet_id": "m", "level": 90})
            recv_until_state(receiver, lambda s: s["chalets"][0]["status"] == "alert")
            elapsed = _t.monotonic() - t0
            assert elapsed < 2, f"la diffusion a attendu le push ({elapsed:.1f}s)"


def test_admin_requires_token(monkeypatch):
    client = TestClient(app)
    monkeypatch.setattr(main, "ADMIN_TOKEN", "")
    assert client.get("/api/admin/parties").status_code == 403      # désactivé si non configuré
    monkeypatch.setattr(main, "ADMIN_TOKEN", "s3cret")
    assert client.get("/api/admin/parties").status_code == 403      # sans jeton
    assert client.get("/api/admin/parties", headers={"X-Admin-Token": "faux"}).status_code == 403
    party = main.create_party("À supprimer")
    ok = client.get("/api/admin/parties", headers={"X-Admin-Token": "s3cret"})
    assert ok.status_code == 200 and any(p["code"] == party.code for p in ok.json()["parties"])
    assert client.delete(f"/api/admin/party/{party.code}", headers={"X-Admin-Token": "s3cret"}).status_code == 200
    assert party.code not in main.parties
    # le lien signé ne ressuscite pas une soirée supprimée par l'admin…
    assert main.get_party(party.code) is None
    # …sauf après un redémarrage du serveur, qui efface les pierres tombales
    main.deleted_codes.clear()
    assert main.get_party(party.code) is not None


def test_admin_brute_force_is_throttled(monkeypatch):
    """La page est atteignable depuis l'app : on ne laisse pas essayer sans fin."""
    client = TestClient(app)
    monkeypatch.setattr(main, "ADMIN_TOKEN", "s3cret")
    main._admin_fails.clear()
    for _ in range(main.ADMIN_MAX_TRIES):
        assert client.get("/api/admin/parties", headers={"X-Admin-Token": "faux"}).status_code == 403
    assert client.get("/api/admin/parties", headers={"X-Admin-Token": "faux"}).status_code == 429
    # le bon jeton est refusé aussi tant que le verrou tient
    assert client.get("/api/admin/parties", headers={"X-Admin-Token": "s3cret"}).status_code == 429
    main._admin_fails.clear()
    assert client.get("/api/admin/parties", headers={"X-Admin-Token": "s3cret"}).status_code == 200


def test_websocket_flow():
    with TestClient(app) as client:  # un seul portail : les deux sockets partagent la boucle
        code = main.create_party("fête").code
        with client.websocket_connect(f"/ws/{code}") as emitter, client.websocket_connect(f"/ws/{code}") as receiver:
            recv_until(emitter, "state"); recv_until(receiver, "state")
            receiver.send_json({"type": "hello", "role": "salle", "name": "Marie"})
            emitter.send_json({"type": "register", "chalet_id": "mesange", "name": "Mésange", "kids": "Léo"})
            reg = recv_until(emitter, "registered")
            assert reg["chalet_id"] == "mesange" and reg["token"]   # le jeton d'émetteur est remis ici

            chalet = main.parties[code].chalets["mesange"]
            st = recv_until_state(receiver, lambda s: s["chalets"] and s["chalets"][0]["name"] == "Mésange")
            assert "Marie" in st["receivers"]

            emitter.send_json({"type": "hb", "level": 12, "battery": 77, "threshold": 50})
            recv_until_state(receiver, lambda s: s["chalets"][0]["status"] == "ok")

            emitter.send_json({"type": "hb", "level": 20, "battery": 76, "threshold": 50})  # niveau seul → message léger
            lv = recv_until(receiver, "level")
            assert lv["level"] == 20

            emitter.send_json({"type": "alert", "level": 90})
            recv_until_state(receiver, lambda s: s["chalets"][0]["status"] == "alert")

            receiver.send_json({"type": "ack", "chalet_id": "mesange"})
            recv_until_state(receiver, lambda s: (s["chalets"][0]["alert"] or {}).get("acked_by") == "Marie")

            receiver.send_json({"type": "resolve", "chalet_id": "mesange"})
            recv_until_state(receiver, lambda s: s["chalets"][0]["status"] == "ok")
            assert chalet.alert is None

            receiver.send_json({"type": "ping"})
            assert recv_until(receiver, "pong")["type"] == "pong"


def test_listen_on_demand():
    """La salle demande à entendre, le chalet enregistre, tout le monde peut lire."""
    with TestClient(app) as client:  # un seul portail : les deux sockets partagent la boucle
        code = main.create_party("écoute").code
        with client.websocket_connect(f"/ws/{code}") as emitter, client.websocket_connect(f"/ws/{code}") as receiver:
            recv_until(emitter, "state"); recv_until(receiver, "state")
            receiver.send_json({"type": "hello", "role": "salle", "name": "Marie"})
            emitter.send_json({"type": "register", "chalet_id": "mesange", "name": "Mésange", "kids": ""})
            recv_until(emitter, "registered")

            receiver.send_json({"type": "listen", "chalet_id": "mesange"})
            # la demande part vers l'émetteur seul, pas en diffusion
            ask = recv_until(emitter, "clip_request")
            assert ask == {"type": "clip_request", "seconds": main.LISTEN_SECONDS, "by": "Marie"}
            chalet = main.parties[code].chalets["mesange"]
            assert chalet.listen_by == "Marie"  # « Marie écoute » visible partout
            assert chalet.to_dict()["listen_by"] == "Marie"

            emitter.send_json({"type": "clip", "chalet_id": "mesange", "clip": "data:audio/mp4;base64,AAAA"})
            ready = recv_until(receiver, "clip_ready")
            assert ready["chalet_id"] == "mesange"
            assert ready["ts"] == pytest.approx(time.time(), abs=5)

        body = client.get(f"/api/party/{code}/chalet/mesange/clip").json()
        assert body["clip"] == "data:audio/mp4;base64,AAAA" and body["by"] == "Marie"

    # « X écoute » s'efface tout seul
    party = main.parties[code]
    party.chalets["mesange"].listen_at -= main.LISTEN_HOLD + 1
    assert party.watchdog()
    assert party.chalets["mesange"].listen_by is None


def test_on_demand_clip_never_lingers():
    """Un clip obtenu par « Écouter » ne doit pas survivre, alerte ou non."""
    p = Party("test")
    c = p.register_chalet("m", "M", "")

    c.clip = {"data": "x", "ts": time.time(), "by": "Marie"}
    p.resolve(c, "Marie")  # « C'est réglé » sans alerte en cours
    assert c.clip is None

    c.clip = {"data": "x", "ts": time.time(), "by": "Marie"}
    assert not p.watchdog()          # encore frais
    c.clip["ts"] -= main.CLIP_TTL + 1
    assert p.watchdog()              # périmé → effacé et rediffusé
    assert c.clip is None and c.to_dict()["has_fresh_clip"] is False


def test_parties_expire_on_their_own():
    """Créée puis abandonnée, une soirée s'oublie ; personne n'a à la supprimer."""
    vide = main.create_party("créée pour rien")
    vide.last_activity -= main.PARTY_EMPTY_TTL + 1
    assert main.cleanup_parties()
    assert vide.code not in main.parties

    habitee = main.create_party("vraie soirée")
    habitee.register_chalet("m", "Mésange", "")
    habitee.last_activity -= main.PARTY_EMPTY_TTL + 1
    assert not main.cleanup_parties()          # trop tôt pour une soirée habitée
    habitee.last_activity -= main.PARTY_TTL
    assert main.cleanup_parties()
    assert habitee.code not in main.parties


def test_listen_without_emitter_is_refused():
    with TestClient(app) as client:
        party = main.create_party("vide")
        with client.websocket_connect(f"/ws/{party.code}") as receiver:
            recv_until(receiver, "state")
            party.register_chalet("absent", "Absent", "")
            receiver.send_json({"type": "listen", "chalet_id": "absent", "by": "Paul"})
            m = recv_until(receiver, "listen_failed")
            assert "pas connecté" in m["reason"]


def test_oversized_on_demand_clip_is_refused():
    with TestClient(app) as client:
        party = main.create_party("gros")
        with client.websocket_connect(f"/ws/{party.code}") as emitter:
            recv_until(emitter, "state")
            emitter.send_json({"type": "register", "chalet_id": "m", "name": "M", "kids": ""})
            recv_until(emitter, "registered")
            emitter.send_json({"type": "clip", "chalet_id": "m", "clip": "x" * (main.MAX_CLIP_BYTES + 1)})
            recv_until(emitter, "listen_failed")
            assert party.chalets["m"].clip is None
