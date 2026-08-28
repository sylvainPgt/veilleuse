"""Veilleuse — babyphone collectif pour les soirées en gîte.

Backend minimal : une "soirée" identifiée par un code, des chalets qui
émettent des heartbeats et des alertes, des récepteurs qui voient tout.
Tout tient en mémoire ; les émetteurs se ré-enregistrent à chaque
reconnexion, donc un redémarrage du serveur n'est pas dramatique.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import unicodedata
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

log = logging.getLogger("veilleuse")

# --- Réglages (surchargeables par variables d'environnement) -----------------
HEARTBEAT_TIMEOUT = float(os.getenv("VEILLEUSE_HEARTBEAT_TIMEOUT", "45"))   # s sans nouvelles → chalet muet
ESCALATION_DELAY = float(os.getenv("VEILLEUSE_ESCALATION_DELAY", "90"))     # s sans acquittement → escalade
NOISE_HOLD = float(os.getenv("VEILLEUSE_NOISE_HOLD", "20"))                 # s pendant lesquels un bruit reste affiché
LISTEN_HOLD = float(os.getenv("VEILLEUSE_LISTEN_HOLD", "25"))               # s pendant lesquels « X écoute » reste affiché
LISTEN_SECONDS = float(os.getenv("VEILLEUSE_LISTEN_SECONDS", "10"))         # durée du clip demandé à la volée
CLIP_TTL = float(os.getenv("VEILLEUSE_CLIP_TTL", "120"))                    # s avant qu'un clip s'efface tout seul
PARTY_EMPTY_TTL = float(os.getenv("VEILLEUSE_PARTY_EMPTY_TTL", "900"))      # s avant d'oublier une soirée jamais habitée (faute de frappe)
PARTY_TTL = float(os.getenv("VEILLEUSE_PARTY_TTL", str(24 * 3600)))         # s avant d'oublier une soirée désertée
WATCHDOG_PERIOD = 2.0
MAX_EVENTS = 60
MAX_CLIP_BYTES = 200_000  # clip audio base64, on refuse au-delà pour protéger le réseau faible

STATIC_DIR = Path(__file__).parent / "static"


def now() -> float:
    return time.time()


def slug(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = re.sub(r"[^a-z0-9]+", "-", value.strip().lower())
    return value.strip("-")[:40] or "soiree"


# --- Modèle ------------------------------------------------------------------
class Chalet:
    def __init__(self, chalet_id: str, name: str, kids: str = ""):
        self.id = chalet_id
        self.name = name
        self.kids = kids
        self.level = 0
        self.battery: int | None = None
        self.threshold: int | None = None
        self.last_hb: float | None = None
        self.last_noise: float | None = None
        self.online = False
        self.alert: dict[str, Any] | None = None  # {started, acked_by, acked_at, escalated, clip, level}
        self.clip: dict[str, Any] | None = None   # dernier clip demandé à la volée {data, ts, by}
        self.listen_by: str | None = None         # qui écoute en ce moment
        self.listen_at: float | None = None

    def status(self) -> str:
        if self.alert:
            return "escalated" if self.alert.get("escalated") else ("acked" if self.alert.get("acked_by") else "alert")
        if not self.online:
            return "offline"
        if self.last_noise and now() - self.last_noise < NOISE_HOLD:
            return "noise"
        return "ok"

    def to_dict(self) -> dict[str, Any]:
        alert = None
        if self.alert:
            alert = {k: v for k, v in self.alert.items() if k != "clip"}
            alert["has_clip"] = bool(self.alert.get("clip"))
        return {
            "id": self.id,
            "name": self.name,
            "kids": self.kids,
            "status": self.status(),
            "level": self.level,
            "battery": self.battery,
            "threshold": self.threshold,
            "last_hb": self.last_hb,
            "online": self.online,
            "alert": alert,
            "listen_by": self.listen_by,
            "has_fresh_clip": bool(self.clip),
        }


class Party:
    def __init__(self, code: str):
        self.code = code
        self.chalets: dict[str, Chalet] = {}
        self.events: list[dict[str, Any]] = []
        self.sockets: dict[WebSocket, dict[str, Any]] = {}
        self.created = now()
        self.last_activity = now()

    # -- événements / état ---------------------------------------------------
    def add_event(self, kind: str, chalet: Chalet | None = None, **extra: Any) -> dict[str, Any]:
        ev = {"ts": now(), "kind": kind, "chalet_id": chalet.id if chalet else None,
              "chalet_name": chalet.name if chalet else None, **extra}
        self.events.append(ev)
        del self.events[:-MAX_EVENTS]
        return ev

    def snapshot(self) -> dict[str, Any]:
        return {
            "type": "state",
            "code": self.code,
            "now": now(),
            "config": {"heartbeat_timeout": HEARTBEAT_TIMEOUT, "escalation_delay": ESCALATION_DELAY},
            "chalets": [c.to_dict() for c in self.chalets.values()],
            "receivers": sorted({m.get("name") for m in self.sockets.values() if m.get("role") == "salle" and m.get("name")}),
            "events": self.events[-30:],
        }

    async def broadcast(self, message: dict[str, Any] | None = None) -> None:
        payload = json.dumps(message or self.snapshot())
        dead = []
        for ws in list(self.sockets):
            try:
                await ws.send_text(payload)
            except Exception:  # noqa: BLE001 — socket morte, on nettoie
                dead.append(ws)
        for ws in dead:
            self.sockets.pop(ws, None)

    # -- logique métier ------------------------------------------------------
    def register_chalet(self, chalet_id: str, name: str, kids: str) -> Chalet:
        chalet = self.chalets.get(chalet_id)
        if chalet is None:
            chalet = Chalet(chalet_id, name, kids)
            self.chalets[chalet_id] = chalet
            self.add_event("registered", chalet)
        else:
            chalet.name, chalet.kids = name or chalet.name, kids if kids is not None else chalet.kids
        return chalet

    def heartbeat(self, chalet: Chalet, level: int, battery: int | None, threshold: int | None) -> bool:
        """Retourne True si l'état visible a changé (pour limiter les diffusions)."""
        was_online, old_status = chalet.online, chalet.status()
        chalet.level = max(0, min(100, int(level)))
        chalet.battery = battery
        chalet.threshold = threshold
        chalet.last_hb = now()
        chalet.online = True
        if not was_online:
            self.add_event("online", chalet)
        return not was_online or old_status != chalet.status()

    def noise(self, chalet: Chalet, level: int) -> None:
        chalet.last_noise = now()
        chalet.level = max(0, min(100, int(level)))

    def raise_alert(self, chalet: Chalet, level: int, clip: str | None, reason: str = "noise") -> None:
        chalet.last_noise = now()
        chalet.level = max(0, min(100, int(level)))
        if clip and len(clip) > MAX_CLIP_BYTES:
            clip = None
        if chalet.alert:
            # Alerte déjà en cours : on rafraîchit le clip/niveau, on ne repart pas de zéro
            chalet.alert["level"] = max(chalet.alert.get("level", 0), chalet.level)
            if clip:
                chalet.alert["clip"] = clip
            chalet.alert["last_noise"] = now()
            return
        chalet.alert = {"started": now(), "last_noise": now(), "acked_by": None, "acked_at": None,
                        "escalated": False, "clip": clip, "level": chalet.level, "reason": reason}
        self.add_event("alert", chalet, level=chalet.level, reason=reason)

    def ack(self, chalet: Chalet, by: str) -> None:
        if not chalet.alert:
            return
        chalet.alert["acked_by"] = by or "quelqu'un"
        chalet.alert["acked_at"] = now()
        chalet.alert["escalated"] = False
        self.add_event("ack", chalet, by=chalet.alert["acked_by"])

    def resolve(self, chalet: Chalet, by: str) -> None:
        chalet.clip = None  # on ne garde pas d'audio, même sans alerte en cours
        if not chalet.alert:
            return
        chalet.alert = None
        chalet.last_noise = None
        self.add_event("resolved", chalet, by=by or "quelqu'un")

    def emitter_socket(self, chalet_id: str) -> WebSocket | None:
        """La socket du téléphone posé dans ce chalet, s'il est connecté."""
        for ws, meta in self.sockets.items():
            if meta.get("role") == "chalet" and meta.get("chalet_id") == chalet_id:
                return ws
        return None

    def watchdog(self) -> bool:
        """Vérifie muets et escalades. Retourne True si quelque chose a changé."""
        changed = False
        t = now()
        for chalet in self.chalets.values():
            if chalet.online and chalet.last_hb and t - chalet.last_hb > HEARTBEAT_TIMEOUT:
                chalet.online = False
                self.add_event("offline", chalet)
                changed = True
            if chalet.listen_at and t - chalet.listen_at > LISTEN_HOLD:
                chalet.listen_by = chalet.listen_at = None
                changed = True
            # un clip demandé à la volée s'efface tout seul : rien ne doit traîner en mémoire
            if chalet.clip and t - chalet.clip["ts"] > CLIP_TTL:
                chalet.clip = None
                changed = True
            a = chalet.alert
            if a and not a["acked_by"] and not a["escalated"] and t - a["started"] > ESCALATION_DELAY:
                a["escalated"] = True
                self.add_event("escalated", chalet)
                changed = True
        return changed


parties: dict[str, Party] = {}


def cleanup_parties() -> bool:
    """Oublie les soirées mortes : sans personne de connecté, une soirée jamais
    habitée (faute de frappe) part vite, une soirée finie part au bout d'un jour.
    C'est la seule « suppression » — pas de compte, donc pas de bouton."""
    t = now()
    changed = False
    for code, party in list(parties.items()):
        if party.sockets:
            continue
        idle = t - party.last_activity
        if (not party.chalets and idle > PARTY_EMPTY_TTL) or idle > PARTY_TTL:
            del parties[code]
            log.info("soirée %s expirée", code)
            changed = True
    return changed


def get_party(code: str) -> Party:
    code = slug(code)
    if code not in parties:
        parties[code] = Party(code)
        log.info("nouvelle soirée %s", code)
    return parties[code]


# --- Application -------------------------------------------------------------
async def watchdog_loop() -> None:
    while True:
        await asyncio.sleep(WATCHDOG_PERIOD)
        try:
            cleanup_parties()
        except Exception:  # noqa: BLE001
            log.exception("cleanup")
        for party in list(parties.values()):
            try:
                if party.watchdog():
                    await party.broadcast()
            except Exception:  # noqa: BLE001
                log.exception("watchdog")


@asynccontextmanager
async def lifespan(_: FastAPI):
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    task = asyncio.create_task(watchdog_loop())
    yield
    task.cancel()


app = FastAPI(title="Veilleuse", version="0.1.0", lifespan=lifespan)


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "parties": len(parties), "version": app.version}


@app.get("/api/parties")
async def parties_list() -> dict[str, Any]:
    """Les soirées vivantes, pour l'accueil : on rejoint l'existante au lieu d'en
    créer une par faute de frappe. Les coquilles vides ne sont pas montrées."""
    return {"parties": [
        {"code": p.code, "chalets": len(p.chalets),
         "receivers": sum(1 for m in p.sockets.values() if m.get("role") == "salle")}
        for p in parties.values() if p.chalets or p.sockets
    ]}


@app.get("/api/party/{code}")
async def party_state(code: str) -> dict[str, Any]:
    return get_party(code).snapshot()


@app.get("/api/party/{code}/chalet/{chalet_id}/clip")
async def chalet_clip(code: str, chalet_id: str, kind: str = "fresh"):
    """kind=fresh : le dernier clip demandé à la volée. kind=alert : celui de l'alerte."""
    chalet = get_party(code).chalets.get(chalet_id)
    if not chalet:
        return JSONResponse({"error": "no clip"}, status_code=404)
    if kind == "fresh" and chalet.clip:
        return {"clip": chalet.clip["data"], "ts": chalet.clip["ts"], "by": chalet.clip.get("by")}
    if chalet.alert and chalet.alert.get("clip"):
        return {"clip": chalet.alert["clip"], "ts": chalet.alert["started"]}
    return JSONResponse({"error": "no clip"}, status_code=404)


@app.websocket("/ws/{code}")
async def websocket_endpoint(ws: WebSocket, code: str) -> None:
    await ws.accept()
    party = get_party(code)
    meta: dict[str, Any] = {"role": None, "name": None, "chalet_id": None}
    party.sockets[ws] = meta
    await ws.send_text(json.dumps(party.snapshot()))
    try:
        while True:
            raw = await ws.receive_text()
            party.last_activity = now()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if await handle_message(party, ws, meta, msg):
                await party.broadcast()
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        log.exception("websocket")
    finally:
        party.sockets.pop(ws, None)
        await party.broadcast()


async def handle_message(party: Party, ws: WebSocket, meta: dict[str, Any], msg: dict[str, Any]) -> bool:
    """Traite un message client. Retourne True si l'état doit être rediffusé."""
    kind = msg.get("type")
    if kind == "hello":  # {role, name, chalet_id}
        meta["role"] = msg.get("role")
        meta["name"] = (msg.get("name") or "")[:40]
        meta["chalet_id"] = msg.get("chalet_id")
        return True

    if kind == "register":  # émetteur : {chalet_id, name, kids}
        chalet = party.register_chalet(slug(msg.get("chalet_id") or msg.get("name", "")),
                                       (msg.get("name") or "Chalet")[:40], (msg.get("kids") or "")[:80])
        meta["role"], meta["chalet_id"] = "chalet", chalet.id
        await ws.send_text(json.dumps({"type": "registered", "chalet_id": chalet.id}))
        return True

    chalet = party.chalets.get(msg.get("chalet_id") or meta.get("chalet_id") or "")

    if kind == "hb" and chalet:
        changed = party.heartbeat(chalet, msg.get("level", 0), msg.get("battery"), msg.get("threshold"))
        if not changed:  # niveau seulement : diffusion légère sans tout le snapshot
            await party.broadcast({"type": "level", "chalet_id": chalet.id, "level": chalet.level,
                                   "battery": chalet.battery, "ts": now()})
        return changed

    if kind == "noise" and chalet:
        party.noise(chalet, msg.get("level", 0))
        return True

    if kind == "alert" and chalet:
        party.raise_alert(chalet, msg.get("level", 0), msg.get("clip"), msg.get("reason", "noise"))
        return True

    if kind == "ack" and chalet:
        party.ack(chalet, msg.get("by") or meta.get("name") or "")
        return True

    if kind == "resolve" and chalet:
        party.resolve(chalet, msg.get("by") or meta.get("name") or "")
        return True

    if kind == "test" and chalet:  # test manuel depuis l'émetteur : alerte de vérification
        party.raise_alert(chalet, 100, None, reason="test")
        return True

    if kind == "listen" and chalet:  # récepteur : « fais-moi entendre ce qui se passe maintenant »
        emitter = party.emitter_socket(chalet.id)
        who = (msg.get("by") or meta.get("name") or "")[:40] or "quelqu'un"
        if emitter is None:
            await ws.send_text(json.dumps({"type": "listen_failed", "chalet_id": chalet.id,
                                           "reason": "Ce chalet n'est pas connecté."}))
            return False
        chalet.listen_by, chalet.listen_at = who, now()
        party.add_event("listen", chalet, by=who)
        await emitter.send_text(json.dumps({"type": "clip_request", "seconds": LISTEN_SECONDS, "by": who}))
        return True

    if kind == "clip" and chalet:  # émetteur : voici l'enregistrement demandé
        clip = msg.get("clip")
        if clip and len(clip) <= MAX_CLIP_BYTES:
            chalet.clip = {"data": clip, "ts": now(), "by": chalet.listen_by}
            await party.broadcast({"type": "clip_ready", "chalet_id": chalet.id, "ts": now()})
        else:
            await party.broadcast({"type": "listen_failed", "chalet_id": chalet.id,
                                   "reason": "Le réseau n'a pas laissé passer l'enregistrement."})
        return False

    if kind == "ping":
        await ws.send_text(json.dumps({"type": "pong", "ts": now()}))
        return False

    return False


# --- Fichiers statiques (la webapp) ------------------------------------------
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/aide")
async def aide() -> FileResponse:
    return FileResponse(STATIC_DIR / "aide.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
