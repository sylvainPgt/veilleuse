"""Veilleuse — babyphone collectif pour les soirées en gîte.

Backend minimal : une "soirée" identifiée par un code, des chalets qui
émettent des heartbeats et des alertes, des récepteurs qui voient tout.
Tout tient en mémoire ; les émetteurs se ré-enregistrent à chaque
reconnexion, donc un redémarrage du serveur n'est pas dramatique.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import unicodedata
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
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

ADMIN_TOKEN = os.getenv("VEILLEUSE_ADMIN_TOKEN", "")                        # vide = page d'admin désactivée

# Signe les identifiants de soirée pour qu'un lien survive à un redémarrage du
# serveur (chaque déploiement en est un). Sans la variable, un secret est tiré au
# démarrage : tout marche, mais les liens meurent avec le processus.
SECRET = os.getenv("VEILLEUSE_SECRET", "") or secrets.token_hex(32)
if not os.getenv("VEILLEUSE_SECRET"):
    log.warning("VEILLEUSE_SECRET absent : les liens de soirée ne survivront pas à un redémarrage")

# --- Web Push -----------------------------------------------------------------
# Les clés VAPID sont dérivées de VEILLEUSE_SECRET : rien de plus à configurer, et
# elles restent stables tant que le secret l'est — condition pour que les
# abonnements des téléphones survivent aux redémarrages.
try:
    from cryptography.hazmat.primitives.asymmetric import ec
    from pywebpush import WebPushException, webpush

    _P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
    _priv_int = int.from_bytes(hashlib.sha256(f"vapid:{SECRET}".encode()).digest(), "big") % (_P256_ORDER - 1) + 1
    _pub = ec.derive_private_key(_priv_int, ec.SECP256R1()).public_key().public_numbers()
    _b64u = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()  # noqa: E731
    VAPID_PUBLIC = _b64u(b"\x04" + _pub.x.to_bytes(32, "big") + _pub.y.to_bytes(32, "big"))
    VAPID_PRIVATE = _b64u(_priv_int.to_bytes(32, "big"))
    PUSH_ENABLED = True
except Exception:  # noqa: BLE001 — sans pywebpush, l'app marche, juste sans push
    PUSH_ENABLED = False
    VAPID_PUBLIC = ""
    webpush = WebPushException = None

VAPID_CLAIMS = {"sub": "mailto:veilleuse@40ansdesilou.fr"}


def _push_one(sub: dict[str, Any], payload: str) -> None:
    webpush(subscription_info=sub, data=payload, ttl=180,
            vapid_private_key=VAPID_PRIVATE, vapid_claims=dict(VAPID_CLAIMS))


async def push_party(party: "Party", title: str, body: str, tag: str) -> None:
    """Pousse une notification à tous les abonnés de la soirée. Les abonnements
    morts (désinscription, app réinstallée) sont élagués au passage."""
    if not PUSH_ENABLED or not party.push_subs:
        return
    payload = json.dumps({"title": title, "body": body, "tag": tag})
    dead = []
    for endpoint, entry in list(party.push_subs.items()):
        try:
            await asyncio.to_thread(_push_one, entry["sub"], payload)
        except WebPushException as exc:
            resp = getattr(exc, "response", None)
            if resp is not None and resp.status_code in (403, 404, 410):
                dead.append(endpoint)
        except Exception:  # noqa: BLE001
            log.exception("push")
    for endpoint in dead:
        party.push_subs.pop(endpoint, None)


async def drain_pushes(party: "Party") -> None:
    while party.push_queue:
        title, body, tag = party.push_queue.pop(0)
        await push_party(party, title, body, tag)


# Garde-fous contre l'épuisement mémoire : la création de soirée est publique.
MAX_PARTIES = int(os.getenv("VEILLEUSE_MAX_PARTIES", "300"))
MAX_CHALETS = int(os.getenv("VEILLEUSE_MAX_CHALETS", "40"))
MAX_SOCKETS = int(os.getenv("VEILLEUSE_MAX_SOCKETS", "150"))               # par soirée

STATIC_DIR = Path(__file__).parent / "static"

# Sans i, l, o, 0, 1 : un identifiant qu'on peut relire à voix haute sans se tromper.
ID_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


SIG_LEN = 8


def _sign(base: str) -> str:
    digest = hmac.new(SECRET.encode(), base.encode(), hashlib.sha256).digest()
    return "".join(ID_ALPHABET[b % len(ID_ALPHABET)] for b in digest[:SIG_LEN])


def new_party_id(name: str) -> str:
    """Nom lisible + suffixe imprévisible + signature. Le suffixe protège la soirée
    (on ne devine pas « anniv-sylvain-k3f9x2qa »), la signature la fait survivre à
    un redémarrage : le serveur reconnaît ses propres liens sans rien stocker.

    L'identifiant doit être stable par slug() : sans le strip("-"), une troncature
    tombant sur un tiret donnait « ...version--abcd », que slug() ramenait ensuite à
    « ...version-abcd » — la soirée devenait alors introuvable.
    """
    # prefix(21) + "-" + hasard(10) + signature(8) = 40, la longueur maximale de slug() :
    # l'identifiant reste ainsi stable par normalisation.
    prefix = slug(name)[:21].strip("-")
    base = f"{prefix}-{''.join(secrets.choice(ID_ALPHABET) for _ in range(10))}"
    return base + _sign(base)


def id_is_signed(code: str) -> bool:
    """Vrai si ce code a été émis par ce serveur (avant ou après redémarrage)."""
    if len(code) <= SIG_LEN:
        return False
    base, sig = code[:-SIG_LEN], code[-SIG_LEN:]
    return hmac.compare_digest(_sign(base), sig)


def name_from_id(code: str) -> str:
    """Retrouve un nom présentable depuis l'identifiant, quand la mémoire est perdue."""
    base = code[:-SIG_LEN]
    prefix = re.sub(r"-[a-z2-9]{10}$", "", base)
    return prefix.replace("-", " ") or "soirée"


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
    def __init__(self, code: str, name: str = ""):
        self.code = code
        self.name = name or code
        self.chalets: dict[str, Chalet] = {}
        self.events: list[dict[str, Any]] = []
        self.sockets: dict[WebSocket, dict[str, Any]] = {}
        self.created = now()
        self.last_activity = now()
        self.rev = 0
        self.push_subs: dict[str, dict[str, Any]] = {}   # endpoint → {sub}
        self.push_queue: list[tuple[str, str, str]] = []  # (titre, corps, tag) à pousser

    # -- événements / état ---------------------------------------------------
    def add_event(self, kind: str, chalet: Chalet | None = None, **extra: Any) -> dict[str, Any]:
        ev = {"ts": now(), "kind": kind, "chalet_id": chalet.id if chalet else None,
              "chalet_name": chalet.name if chalet else None, **extra}
        self.events.append(ev)
        del self.events[:-MAX_EVENTS]
        return ev

    def snapshot(self) -> dict[str, Any]:
        # Deux diffusions peuvent se chevaucher sur une socket lente : la révision
        # permet au client d'ignorer un état plus vieux que le dernier affiché.
        self.rev += 1
        return {
            "type": "state",
            "rev": self.rev,
            "code": self.code,
            "name": self.name,
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
                        "escalated": False, "clip": clip, "clip_ts": now() if clip else None,
                        "level": chalet.level, "reason": reason}
        self.add_event("alert", chalet, level=chalet.level, reason=reason)
        titre = "Test — " + chalet.name if reason == "test" else "Ça sonne — " + chalet.name
        self.push_queue.append((titre, chalet.kids or "", "veilleuse-" + chalet.id))

    def attach_alert_clip(self, chalet: Chalet, clip: str | None) -> None:
        """Le clip arrive quelques secondes après l'alerte. S'il n'y a plus d'alerte
        (déjà réglée), on le jette : il ne doit surtout pas en recréer une."""
        if not chalet.alert or not clip or len(clip) > MAX_CLIP_BYTES:
            return
        chalet.alert["clip"] = clip
        chalet.alert["clip_ts"] = now()

    def ack(self, chalet: Chalet, by: str) -> None:
        if not chalet.alert:
            return
        if chalet.alert.get("acked_by"):
            return  # le premier « J'y vais » gagne : deux personnes qui tapent en même temps ne doivent pas s'écraser
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
                self.push_queue.append(("Chalet muet — " + chalet.name,
                                        "Plus de nouvelles du babyphone. " + (chalet.kids or ""),
                                        "veilleuse-" + chalet.id))
                changed = True
            if chalet.listen_at and t - chalet.listen_at > LISTEN_HOLD:
                chalet.listen_by = chalet.listen_at = None
                changed = True
            # un clip demandé à la volée s'efface tout seul : rien ne doit traîner en mémoire
            if chalet.clip and t - chalet.clip["ts"] > CLIP_TTL:
                chalet.clip = None
                changed = True
            # même règle pour le clip d'une alerte qui traîne sans être réglée
            a = chalet.alert
            if a and a.get("clip") and t - (a.get("clip_ts") or a["started"]) > CLIP_TTL:
                a["clip"] = a["clip_ts"] = None
                changed = True
            a = chalet.alert
            if a and not a["acked_by"] and not a["escalated"] and t - a["started"] > ESCALATION_DELAY:
                a["escalated"] = True
                self.add_event("escalated", chalet)
                self.push_queue.append(("Personne n'a répondu — " + chalet.name,
                                        "L'alerte sonne sans réponse. " + (chalet.kids or ""),
                                        "veilleuse-" + chalet.id))
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


def get_party(code: str) -> Party | None:
    """Une soirée n'existe que si quelqu'un l'a créée : deviner un nom n'ouvre rien.
    Exception voulue : un identifiant signé par ce serveur est recréé vide s'il
    manque — c'est ce qui fait survivre les liens à un redémarrage (chaque
    déploiement en est un), les chalets se ré-enregistrant ensuite tout seuls.

    On tente la clé telle quelle avant de la normaliser : un identifiant valide ne
    doit jamais dépendre des transformations de slug()."""
    party = parties.get(code) or parties.get(slug(code))
    if party is None and id_is_signed(code) and len(parties) < MAX_PARTIES:
        party = Party(code, name_from_id(code))
        parties[code] = party
        log.info("soirée %s recréée depuis son lien signé", code)
    return party


def create_party(name: str) -> Party | None:
    if len(parties) >= MAX_PARTIES:
        return None
    party = Party(new_party_id(name), (name or "soirée").strip()[:60])
    parties[party.code] = party
    log.info("nouvelle soirée %s", party.code)
    return party


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
                await drain_pushes(party)
            except Exception:  # noqa: BLE001
                log.exception("watchdog")


@asynccontextmanager
async def lifespan(_: FastAPI):
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    task = asyncio.create_task(watchdog_loop())
    yield
    task.cancel()


app = FastAPI(title="Veilleuse", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    if request.url.path.startswith("/api/"):
        # jamais de cache : un clip audio ou un état ne doit pas survivre dans un navigateur
        resp.headers["Cache-Control"] = "no-store, private"
    return resp


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "parties": len(parties), "version": app.version}


@app.post("/api/parties")
async def party_create(body: dict[str, Any]) -> dict[str, Any]:
    """Créer reste ouvert à tous : chacun fait sa soirée et partage son lien.
    C'est rejoindre qui demande de connaître l'identifiant complet."""
    party = create_party(str(body.get("name") or "")[:60])
    if party is None:
        return JSONResponse({"error": "server full"}, status_code=503)
    return {"code": party.code, "name": party.name}


@app.get("/api/push-key")
async def push_key():
    if not PUSH_ENABLED:
        return JSONResponse({"error": "push disabled"}, status_code=404)
    return {"key": VAPID_PUBLIC}


@app.get("/api/party/{code}")
async def party_state(code: str):
    party = get_party(code)
    if party is None:
        return JSONResponse({"error": "unknown party"}, status_code=404)
    return party.snapshot()


@app.get("/api/party/{code}/chalet/{chalet_id}/clip")
async def chalet_clip(code: str, chalet_id: str, kind: str = "fresh"):
    """kind=fresh : le dernier clip demandé à la volée. kind=alert : celui de l'alerte."""
    party = get_party(code)
    chalet = party.chalets.get(chalet_id) if party else None
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
    if party is None:  # identifiant inconnu : on ne crée rien, on renvoie poliment
        await ws.send_text(json.dumps({"type": "unknown_party"}))
        await ws.close()
        return
    if len(party.sockets) >= MAX_SOCKETS:
        await ws.close()
        return
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
            await drain_pushes(party)
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
        wanted = slug(msg.get("chalet_id") or msg.get("name", ""))
        if wanted not in party.chalets and len(party.chalets) >= MAX_CHALETS:
            return False
        chalet = party.register_chalet(wanted, (msg.get("name") or "Chalet")[:40], (msg.get("kids") or "")[:80])
        meta["role"], meta["chalet_id"] = "chalet", chalet.id
        await ws.send_text(json.dumps({"type": "registered", "chalet_id": chalet.id}))
        return True

    chalet = party.chalets.get(msg.get("chalet_id") or meta.get("chalet_id") or "")

    # Émettre pour un chalet est réservé à la socket qui s'y est enregistrée : avec
    # le lien, un récepteur pouvait sinon fabriquer alertes, heartbeats et clips.
    # « J'y vais », « C'est réglé » et « Écouter » restent ouverts à tous : c'est le principe.
    is_emitter_of = meta.get("role") == "chalet" and chalet is not None and meta.get("chalet_id") == chalet.id
    if kind in ("hb", "noise", "alert", "alert_clip", "clip", "test") and not is_emitter_of:
        return False

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

    if kind == "alert_clip" and chalet:  # le clip arrive après coup : jamais une nouvelle alerte
        party.attach_alert_clip(chalet, msg.get("clip"))
        return chalet.alert is not None

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

    if kind == "push_sub":  # récepteur : « voici où me pousser les notifications »
        sub = msg.get("sub")
        if not (PUSH_ENABLED and isinstance(sub, dict)):
            return False
        endpoint = str(sub.get("endpoint") or "")
        if (endpoint.startswith("https://")
                and len(json.dumps(sub)) < 2000 and len(party.push_subs) < MAX_SOCKETS):
            party.push_subs[endpoint] = {"sub": sub}
        return False

    if kind == "ping":
        await ws.send_text(json.dumps({"type": "pong", "ts": now()}))
        return False

    return False


# --- Administration ----------------------------------------------------------
ADMIN_MAX_TRIES = 8               # essais ratés tolérés…
ADMIN_WINDOW = 300.0              # …sur cinq minutes glissantes
_admin_fails: dict[str, list[float]] = {}


def admin_blocked(ip: str) -> bool:
    """La page est désormais accessible depuis l'app : sans ce frein, on pourrait
    essayer des milliers de jetons à la suite."""
    t = now()
    tries = [ts for ts in _admin_fails.get(ip, []) if t - ts < ADMIN_WINDOW]
    _admin_fails[ip] = tries
    return len(tries) >= ADMIN_MAX_TRIES


def admin_guard(request: Request) -> JSONResponse | None:
    """None si l'accès est accordé, sinon la réponse à renvoyer."""
    ip = request.client.host if request.client else "?"
    if admin_blocked(ip):
        return JSONResponse({"error": "too many attempts"}, status_code=429)
    token = request.headers.get("x-admin-token", "")
    if ADMIN_TOKEN and secrets.compare_digest(token, ADMIN_TOKEN):
        _admin_fails.pop(ip, None)
        return None
    _admin_fails.setdefault(ip, []).append(now())
    log.warning("essai d'administration refusé depuis %s", ip)
    return JSONResponse({"error": "forbidden"}, status_code=403)


@app.get("/api/admin/parties")
async def admin_parties(request: Request):
    if (refus := admin_guard(request)) is not None:
        return refus
    t = now()
    return {"parties": sorted((
        {"code": p.code, "name": p.name, "chalets": len(p.chalets),
         "sockets": len(p.sockets), "idle": round(t - p.last_activity), "created": p.created}
        for p in parties.values()), key=lambda d: d["idle"])}


@app.delete("/api/admin/party/{code}")
async def admin_delete(request: Request, code: str):
    if (refus := admin_guard(request)) is not None:
        return refus
    party = parties.pop(slug(code), None)
    if party is None:
        return JSONResponse({"error": "unknown party"}, status_code=404)
    for ws in list(party.sockets):       # on ferme aussi les connexions en cours
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass
    log.info("soirée %s supprimée par l'admin", code)
    return {"deleted": party.code}


# --- Fichiers statiques (la webapp) ------------------------------------------
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/admin")
async def admin_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "admin.html")


@app.get("/aide")
async def aide() -> FileResponse:
    return FileResponse(STATIC_DIR / "aide.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
