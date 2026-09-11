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
ACK_REMINDER = float(os.getenv("VEILLEUSE_ACK_REMINDER", "240"))            # s après « J'y vais » sans résolution → rappel
REVIVE_GRACE = float(os.getenv("VEILLEUSE_REVIVE_GRACE", "120"))            # s laissées aux chalets restaurés pour revenir après un redémarrage
STATE_FILE = os.getenv("VEILLEUSE_STATE_FILE", "data/state.json")           # persistance minimale ("" = désactivée)
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


# Seuls les vrais services de push reçoivent des requêtes du serveur : accepter
# n'importe quelle URL https reviendrait à offrir un canon à requêtes (SSRF).
ALLOWED_PUSH_HOSTS = ("fcm.googleapis.com", "push.apple.com",
                      "push.services.mozilla.com", "notify.windows.com")


def push_endpoint_ok(endpoint: str) -> bool:
    try:
        from urllib.parse import urlsplit
        parts = urlsplit(endpoint)
        host = parts.hostname or ""
    except Exception:  # noqa: BLE001
        return False
    return parts.scheme == "https" and any(host == h or host.endswith("." + h) for h in ALLOWED_PUSH_HOSTS)


def _push_one(sub: dict[str, Any], payload: str, tag: str) -> None:
    # Urgency: high — sans elle, Android en économie d'énergie (écran éteint, Doze)
    # retarde ou groupe la livraison. Topic : une nouvelle alerte du même chalet
    # remplace la précédente en attente. timeout : un fournisseur qui ne répond pas
    # ne doit jamais retenir un thread indéfiniment.
    webpush(subscription_info=sub, data=payload, ttl=180, timeout=8,
            vapid_private_key=VAPID_PRIVATE, vapid_claims=dict(VAPID_CLAIMS),
            headers={"Urgency": "high", "Topic": re.sub(r"[^A-Za-z0-9_-]", "", tag)[:32]})


async def _push_entry(party: "Party", endpoint: str, entry: dict[str, Any], payload: str, tag: str) -> bool:
    """True seulement si le service de push a ACCEPTÉ l'envoi : c'est ce qui permet
    de ne pas répondre « essai envoyé » quand rien n'est parti."""
    try:
        await asyncio.wait_for(asyncio.to_thread(_push_one, entry["sub"], payload, tag), timeout=12)
        return True
    except WebPushException as exc:
        resp = getattr(exc, "response", None)
        if resp is not None and resp.status_code in (403, 404, 410):
            party.push_subs.pop(endpoint, None)
    except (TimeoutError, asyncio.TimeoutError):
        log.warning("push trop lent, abandonné pour cet abonné")
    except Exception:  # noqa: BLE001
        log.exception("push")
    return False


_PUSH_CONCURRENCY = asyncio.Semaphore(6)


async def push_party(party: "Party", title: str, body: str, tag: str, only_endpoint: str | None = None,
                     extra: dict[str, Any] | None = None) -> int:
    """Pousse une notification aux abonnés de la soirée, en parallèle borné :
    un destinataire lent ou mort ne retarde pas les autres. Retourne le nombre
    d'envois acceptés par les services de push."""
    if not PUSH_ENABLED or not party.push_subs:
        return 0
    payload = json.dumps({"title": title, "body": body, "tag": tag, "code": party.code, **(extra or {})})

    async def one(endpoint: str, entry: dict[str, Any]) -> bool:
        async with _PUSH_CONCURRENCY:
            return await _push_entry(party, endpoint, entry, payload, tag)

    targets = [(e, s) for e, s in list(party.push_subs.items()) if only_endpoint is None or e == only_endpoint]
    results = await asyncio.gather(*(one(e, s) for e, s in targets))
    return sum(1 for r in results if r)


async def _drain_task(party: "Party") -> None:
    while party.push_queue:
        title, body, tag = party.push_queue.pop(0)
        await push_party(party, title, body, tag)


def schedule_pushes(party: "Party") -> None:
    """Détache l'envoi des push du chemin critique : ni le watchdog ni le
    traitement des messages n'attendent un fournisseur de notifications."""
    if not party.push_queue:
        return
    if party.push_task is not None and not party.push_task.done():
        return
    party.push_task = asyncio.create_task(_drain_task(party))


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
        self.alert: dict[str, Any] | None = None  # {id, started, acked_by, acked_at, escalated, clip, level}
        self.clip: dict[str, Any] | None = None   # dernier clip demandé à la volée {data, ts, by}
        self.listen_by: str | None = None         # qui écoute en ce moment
        self.listen_at: float | None = None
        # Jeton d'émetteur : seule la socket qui le présente peut reprendre ce chalet.
        # Sans lui, n'importe qui ayant le lien pouvait s'enregistrer sur un chalet
        # existant et parler à sa place.
        self.token = "".join(secrets.choice(ID_ALPHABET) for _ in range(12))
        # « Chalet muet » pris en charge : X va vérifier — sans pour autant reverdir
        # la tuile, seule la reprise du heartbeat prouve que la surveillance est revenue.
        self.check_by: str | None = None
        self.check_at: float | None = None
        # Chalet restauré après redémarrage : délai de grâce pour revenir, après quoi
        # son absence est poussée — sinon un chalet qui ne revient jamais restait
        # muet en silence (le push « muet » n'existait que sur une transition).
        self.revive_deadline: float | None = None

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
            "check_by": self.check_by,
            "check_at": self.check_at,
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
        self.push_task: asyncio.Task | None = None        # vidage en cours, détaché du chemin critique

    # -- événements / état ---------------------------------------------------
    def add_event(self, kind: str, chalet: Chalet | None = None, **extra: Any) -> dict[str, Any]:
        ev = {"ts": now(), "kind": kind, "chalet_id": chalet.id if chalet else None,
              "chalet_name": chalet.name if chalet else None, **extra}
        self.events.append(ev)
        del self.events[:-MAX_EVENTS]
        mark_dirty()   # tout événement vaut d'être persisté pour la reprise
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
        # Envois en parallèle, chacun borné à 3 s : un téléphone en 3G moribonde ne
        # doit pas retenir l'état des autres. Passé le délai, la socket est réputée
        # morte — le client, lui, se reconnecte tout seul.
        payload = json.dumps(message or self.snapshot())

        async def send(ws: WebSocket) -> WebSocket | None:
            try:
                await asyncio.wait_for(ws.send_text(payload), timeout=3.0)
                return None
            except Exception:  # noqa: BLE001 — morte ou trop lente, on nettoie
                return ws

        for dead in await asyncio.gather(*(send(ws) for ws in list(self.sockets))):
            if dead is not None:
                self.sockets.pop(dead, None)
                # la fermeture aussi est bornée : une socket zombie dont close()
                # ne rend jamais la main suspendait broadcast(), donc le watchdog
                try:
                    await asyncio.wait_for(dead.close(), timeout=1.0)
                except Exception:  # noqa: BLE001
                    pass

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
        chalet.revive_deadline = None   # revenu : plus rien à surveiller côté reprise
        if not was_online:
            chalet.check_by = chalet.check_at = None   # la surveillance a repris : la vérification est close
            self.add_event("online", chalet)
        return not was_online or old_status != chalet.status()

    def noise(self, chalet: Chalet, level: int) -> None:
        chalet.last_noise = now()
        chalet.level = max(0, min(100, int(level)))

    def raise_alert(self, chalet: Chalet, level: int, clip: str | None, reason: str = "noise",
                    aid_hint: str | None = None) -> None:
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
        # Chaque occurrence d'alerte porte son identifiant : une action (« J'y vais »,
        # « C'est réglé », clip) restée en attente ne peut pas toucher la suivante.
        # L'émetteur peut fournir le sien (aid_hint) : le clip enregistré pendant les
        # secondes suivantes est ainsi corrélé DÈS le déclenchement, pas après coup.
        hint = aid_hint if aid_hint and re.fullmatch(r"[a-z0-9]{4,12}", str(aid_hint)) else None
        chalet.alert = {"id": hint or "".join(secrets.choice(ID_ALPHABET) for _ in range(8)),
                        "started": now(), "last_noise": now(), "acked_by": None, "acked_at": None,
                        "escalated": False, "reminded": False, "clip": clip, "clip_ts": now() if clip else None,
                        "level": chalet.level, "reason": reason}
        self.add_event("alert", chalet, level=chalet.level, reason=reason)
        titre = "Test — " + chalet.name if reason == "test" else "Ça sonne — " + chalet.name
        self.push_queue.append((titre, chalet.kids or "", "veilleuse-" + chalet.id))

    @staticmethod
    def aid_matches(chalet: Chalet, aid: str | None) -> bool:
        """Une action vise une occurrence d'alerte précise, obligatoirement : la
        tolérance « sans aid = l'alerte en cours » maintenait exactement le défaut
        (une action périmée retombait sur la suivante). Un client trop vieux pour
        fournir l'aid reçoit action_stale et doit recharger la page."""
        return chalet.alert is not None and aid is not None and aid == chalet.alert.get("id")

    def attach_alert_clip(self, chalet: Chalet, clip: str | None, aid: str | None = None) -> None:
        """Le clip arrive quelques secondes après l'alerte. S'il n'y a plus d'alerte,
        si une AUTRE a pris sa place, ou s'il n'est pas corrélé, on le jette."""
        if not self.aid_matches(chalet, aid) or not clip or len(clip) > MAX_CLIP_BYTES:
            return
        chalet.alert["clip"] = clip
        chalet.alert["clip_ts"] = now()

    def ack(self, chalet: Chalet, by: str, aid: str | None = None) -> bool:
        if not self.aid_matches(chalet, aid):
            return False
        if chalet.alert.get("acked_by"):
            return True  # le premier « J'y vais » gagne ; le second n'écrase pas mais n'est pas « périmé »
        chalet.alert["acked_by"] = by or "quelqu'un"
        chalet.alert["acked_at"] = now()
        chalet.alert["escalated"] = False
        self.add_event("ack", chalet, by=chalet.alert["acked_by"])
        return True

    def resolve(self, chalet: Chalet, by: str, aid: str | None = None) -> bool:
        if not self.aid_matches(chalet, aid):
            return False  # aucun effet secondaire pour une action refusée (le clip inclus)
        chalet.clip = None  # on ne garde pas d'audio une fois l'alerte réglée
        chalet.alert = None
        chalet.last_noise = None
        self.add_event("resolved", chalet, by=by or "quelqu'un")
        return True

    def release(self, chalet: Chalet, by: str, aid: str | None = None) -> bool:
        """« Je ne peux plus y aller » : rend l'alerte à tous, chrono d'escalade relancé."""
        if not self.aid_matches(chalet, aid) or not chalet.alert.get("acked_by"):
            return False
        who = chalet.alert["acked_by"]
        chalet.alert["acked_by"] = chalet.alert["acked_at"] = None
        chalet.alert["escalated"] = False
        chalet.alert["reminded"] = False
        chalet.alert["started"] = now()
        self.add_event("released", chalet, by=by or who)
        self.push_queue.append(("De nouveau sans réponse — " + chalet.name,
                                f"{who} ne peut plus y aller. " + (chalet.kids or ""),
                                "veilleuse-" + chalet.id))
        return True

    def reinforce(self, chalet: Chalet, by: str, aid: str | None = None) -> bool:
        """« Demander du renfort » : re-sonne tout le monde sans lâcher la prise en charge."""
        if not self.aid_matches(chalet, aid):
            return False
        who = by or chalet.alert.get("acked_by") or "quelqu'un"
        self.add_event("reinforce", chalet, by=who)
        self.push_queue.append(("Renfort demandé — " + chalet.name,
                                f"{who} demande de l'aide. " + (chalet.kids or ""),
                                "veilleuse-" + chalet.id))
        return True

    def check(self, chalet: Chalet, by: str) -> bool:
        """« Je vais vérifier » un chalet muet. La tuile reste muette tant que le
        heartbeat n'est pas revenu : une intention n'est pas une preuve."""
        if chalet.online:
            return False
        chalet.check_by = (by or "quelqu'un")[:40]
        chalet.check_at = now()
        self.add_event("check", chalet, by=chalet.check_by)
        return True

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
            # chalet attendu depuis le redémarrage, jamais revenu : on prévient (une fois)
            if chalet.revive_deadline and t > chalet.revive_deadline:
                chalet.revive_deadline = None
                if not chalet.online:
                    self.add_event("offline", chalet)
                    self.push_queue.append(("Chalet muet — " + chalet.name,
                                            "Le babyphone n'est pas revenu après le redémarrage du serveur. "
                                            + (chalet.kids or ""),
                                            "veilleuse-" + chalet.id))
                    changed = True
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
            # « J'y vais » puis plus rien : au bout d'un moment, on rappelle que
            # l'alerte attend toujours sa résolution — un oubli ne doit pas
            # laisser un chalet en suspens toute la nuit.
            if a and a.get("acked_by") and not a.get("reminded") and a.get("acked_at") \
                    and t - a["acked_at"] > ACK_REMINDER:
                a["reminded"] = True
                self.add_event("ack_reminder", chalet, by=a["acked_by"])
                self.push_queue.append(("Toujours en cours — " + chalet.name,
                                        f"{a['acked_by']} y est allé il y a {int((t - a['acked_at']) / 60)} min "
                                        "sans « C'est réglé ». Tout va bien ?",
                                        "veilleuse-" + chalet.id))
                changed = True
        del self.push_queue[:-30]   # borné : si personne n'écoute, inutile d'accumuler
        return changed


parties: dict[str, Party] = {}

# --- Persistance minimale ------------------------------------------------------
# Évolution assumée du principe « tout en mémoire » (décision Sylvain, sept. 2026) :
# un redémarrage recréait les soirées VIDES — chalets attendus disparus du tableau,
# alertes en cours perdues, abonnements push envolés pour les apps fermées. On
# écrit donc le strict nécessaire à la reprise dans un petit JSON local, réécrit
# au fil de l'eau et relu au démarrage. JAMAIS les clips audio. Le fichier vit sur
# le serveur de Sylvain, expire avec les soirées, et s'efface avec elles.
_state_dirty = False
_persist_status = "off" if not STATE_FILE else "ok"   # exposé sur /api/health : une
_persist_warned = False                                # persistance en panne ne doit pas être silencieuse


def mark_dirty() -> None:
    global _state_dirty
    _state_dirty = True


def save_state(force: bool = False) -> bool:
    global _state_dirty
    if not STATE_FILE or (not _state_dirty and not force):
        return False
    data = {
        "v": 1,
        "saved_at": now(),
        "deleted_codes": sorted(deleted_codes),
        "parties": [{
            "code": p.code, "name": p.name, "created": p.created, "last_activity": p.last_activity,
            "push_subs": p.push_subs,
            "chalets": [{
                "id": c.id, "name": c.name, "kids": c.kids, "threshold": c.threshold,
                "last_hb": c.last_hb, "token": c.token,
                # l'alerte survit, pas son audio
                "alert": {k: v for k, v in c.alert.items() if k not in ("clip",)} | {"clip_ts": None}
                         if c.alert else None,
            } for c in p.chalets.values()],
        } for p in parties.values()],
    }
    try:
        path = Path(STATE_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False))
        tmp.replace(path)   # écriture atomique : jamais de fichier à moitié écrit
        _state_dirty = False
        global _persist_status, _persist_warned
        _persist_status, _persist_warned = "ok", False
        return True
    except OSError:
        _mark_persist_error()
        return False


def _mark_persist_error() -> None:
    global _persist_status, _persist_warned
    _persist_status = "error"
    if not _persist_warned:   # une fois, pas toutes les deux secondes
        _persist_warned = True
        log.exception("persistance impossible (%s) — l'app continue en mémoire seule, "
                      "voir le champ « persistence » de /api/health", STATE_FILE)


def load_state() -> int:
    """Recharge les soirées au démarrage. Les chalets reviennent « non vérifiés » :
    online=False tant qu'un vrai heartbeat n'a pas prouvé que la surveillance a repris."""
    if not STATE_FILE:
        return 0
    try:
        data = json.loads(Path(STATE_FILE).read_text())
    except FileNotFoundError:
        return 0
    except (OSError, ValueError):
        log.exception("état persisté illisible, on repart de zéro")
        return 0
    deleted_codes.update(data.get("deleted_codes") or [])
    n = 0
    for pd in data.get("parties") or []:
        code = str(pd.get("code") or "")
        if not code or code in deleted_codes or not id_is_signed(code):
            continue   # un fichier trafiqué ne fait pas naître de soirée non signée
        party = Party(code, str(pd.get("name") or "")[:60])
        party.created = float(pd.get("created") or now())
        party.last_activity = float(pd.get("last_activity") or now())
        party.push_subs = {e: s for e, s in (pd.get("push_subs") or {}).items()
                           if push_endpoint_ok(e)}
        for cd in (pd.get("chalets") or [])[:MAX_CHALETS]:
            c = Chalet(str(cd.get("id") or "")[:40], str(cd.get("name") or "Chalet")[:40],
                       str(cd.get("kids") or "")[:80])
            if not c.id:
                continue
            c.threshold = cd.get("threshold")
            c.last_hb = cd.get("last_hb")
            c.token = str(cd.get("token") or c.token)
            alert = cd.get("alert")
            if isinstance(alert, dict) and alert.get("started"):
                c.alert = {**alert, "clip": None, "clip_ts": None}
            c.online = False   # non vérifié jusqu'au prochain heartbeat
            c.revive_deadline = now() + REVIVE_GRACE   # passé ce délai sans retour → « chalet muet » poussé
            party.chalets[c.id] = c
        parties[party.code] = party
        n += 1
    if n:
        log.info("%d soirée(s) rechargée(s) depuis %s", n, STATE_FILE)
    return n


# Pierres tombales : une soirée supprimée par l'admin ne doit pas être ressuscitée
# par son lien signé. Persistées avec l'état : la suppression survit désormais
# aussi aux redémarrages tant que le fichier d'état existe.
deleted_codes: set[str] = set()


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
            mark_dirty()
            log.info("soirée %s… expirée", code[:10])
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
    if code in deleted_codes or slug(code) in deleted_codes:
        return None
    if party is None and id_is_signed(code) and len(parties) < MAX_PARTIES:
        party = Party(code, name_from_id(code))
        parties[code] = party
        mark_dirty()
        # jamais l'identifiant complet dans les journaux : c'est la clé de la soirée
        log.info("soirée %s… recréée depuis son lien signé", code[:10])
    return party


def create_party(name: str) -> Party | None:
    if len(parties) >= MAX_PARTIES:
        return None
    party = Party(new_party_id(name), (name or "soirée").strip()[:60])
    parties[party.code] = party
    mark_dirty()
    log.info("nouvelle soirée %s…", party.code[:10])
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
                schedule_pushes(party)   # détaché : un fournisseur de push lent ne gèle pas la surveillance
            except Exception:  # noqa: BLE001
                log.exception("watchdog")
        try:
            save_state()   # au plus une écriture par cycle, seulement si quelque chose a changé
        except Exception:  # noqa: BLE001
            log.exception("save_state")


@asynccontextmanager
async def lifespan(_: FastAPI):
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    load_state()
    task = asyncio.create_task(watchdog_loop())
    yield
    task.cancel()
    save_state(force=True)   # dernier instantané avant de mourir : la reprise part de là


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
    return {"ok": True, "parties": len(parties), "version": app.version,
            "persistence": _persist_status if STATE_FILE else "off"}   # « error » = à corriger avant la fête


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
            schedule_pushes(party)   # détaché : jamais dans le chemin des messages
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
    if kind == "hello":  # récepteur : {role: "salle", name}
        # hello ne confère JAMAIS les droits d'un émetteur : déclarer role="chalet"
        # avec le chalet_id d'un autre suffisait à passer le contrôle is_emitter_of.
        # Seul register — protégé par le jeton — donne ce rôle.
        meta["role"] = "salle"
        meta["name"] = (msg.get("name") or "")[:40]
        return True

    if kind == "register":  # émetteur : {chalet_id, name, kids, token?}
        wanted = slug(msg.get("chalet_id") or msg.get("name", ""))
        existing = party.chalets.get(wanted)
        if existing is None and len(party.chalets) >= MAX_CHALETS:
            return False
        # Reprendre un chalet existant exige son jeton, remis à la première
        # inscription : sans lui, quiconque a le lien pouvait parler à la place
        # d'un babyphone (les identifiants sont visibles dans l'état diffusé).
        if existing is not None and not hmac.compare_digest(str(msg.get("token") or ""), existing.token):
            await ws.send_text(json.dumps({"type": "register_denied", "chalet_id": wanted}))
            return False
        chalet = party.register_chalet(wanted, (msg.get("name") or "Chalet")[:40], (msg.get("kids") or "")[:80])
        # Un seul émetteur courant par chalet : la dernière inscription (jeton en
        # main) gagne, l'ancienne connexion est démise et prévenue — deux sockets
        # simultanées pour un même chalet n'existent plus.
        prev = party.emitter_socket(chalet.id)
        if prev is not None and prev is not ws:
            prev_meta = party.sockets.get(prev)
            if prev_meta:
                prev_meta["role"] = prev_meta["chalet_id"] = None
            try:
                await asyncio.wait_for(prev.send_text(json.dumps(
                    {"type": "superseded", "chalet_id": chalet.id})), timeout=1.0)
            except Exception:  # noqa: BLE001
                pass
        meta["role"], meta["chalet_id"] = "chalet", chalet.id
        await ws.send_text(json.dumps({"type": "registered", "chalet_id": chalet.id, "token": chalet.token}))
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
        party.raise_alert(chalet, msg.get("level", 0), msg.get("clip"), msg.get("reason", "noise"),
                          aid_hint=msg.get("aid"))
        return True

    if kind == "alert_clip" and chalet:  # le clip arrive après coup : jamais une nouvelle alerte
        party.attach_alert_clip(chalet, msg.get("clip"), aid=msg.get("aid"))
        return chalet.alert is not None

    # Les actions humaines visent une occurrence d'alerte précise (aid) : une action
    # restée en attente pendant une coupure ne doit pas toucher l'alerte suivante.
    if kind == "ack" and chalet:
        if not party.ack(chalet, msg.get("by") or meta.get("name") or "", aid=msg.get("aid")):
            await ws.send_text(json.dumps({"type": "action_stale", "chalet_id": chalet.id, "action": "ack"}))
            return False
        return True

    if kind == "resolve" and chalet:
        if not party.resolve(chalet, msg.get("by") or meta.get("name") or "", aid=msg.get("aid")):
            await ws.send_text(json.dumps({"type": "action_stale", "chalet_id": chalet.id, "action": "resolve"}))
            return False
        return True

    if kind == "release" and chalet:  # « je ne peux plus y aller »
        return party.release(chalet, msg.get("by") or meta.get("name") or "", aid=msg.get("aid"))

    if kind == "reinforce" and chalet:  # « demandez du renfort »
        return party.reinforce(chalet, msg.get("by") or meta.get("name") or "", aid=msg.get("aid"))

    if kind == "check" and chalet:  # « je vais vérifier » un chalet muet
        return party.check(chalet, msg.get("by") or meta.get("name") or "")

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
        try:
            # borné : un émetteur gelé ne doit pas suspendre la boucle du demandeur
            await asyncio.wait_for(emitter.send_text(json.dumps(
                {"type": "clip_request", "seconds": LISTEN_SECONDS, "by": who})), timeout=3.0)
        except Exception:  # noqa: BLE001
            await ws.send_text(json.dumps({"type": "listen_failed", "chalet_id": chalet.id,
                                           "reason": "Le chalet ne répond pas."}))
            return True
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
        if (push_endpoint_ok(endpoint)
                and len(json.dumps(sub)) < 2000 and len(party.push_subs) < MAX_SOCKETS):
            party.push_subs[endpoint] = {"sub": sub}
            mark_dirty()
            # confirmation explicite : le téléphone sait que le serveur le poussera —
            # une permission accordée n'a jamais suffi à le prouver.
            await ws.send_text(json.dumps({"type": "push_ok"}))
        return False

    if kind == "push_test":  # « envoie-MOI une notification d'essai, que je la voie arriver »
        endpoint = str(msg.get("endpoint") or "")
        sent = 0
        if PUSH_ENABLED and endpoint in party.push_subs:
            sent = await push_party(party, "Notification d'essai — Veilleuse",
                                    "Tout est en place : les alertes vous parviendront ainsi. 🎉",
                                    "veilleuse-essai", only_endpoint=endpoint, extra={"test": True})
        # « envoyé » seulement si le service de push a accepté — un échec absorbé
        # en silence était présenté comme un succès.
        await ws.send_text(json.dumps({"type": "push_test_sent" if sent else "push_test_failed"}))
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


@app.get("/api/admin/status")
async def admin_status(request: Request):
    """Ce que `persistence: "ok"` ne dit PAS : que le fichier est sur un volume
    durable. « ok » signifie seulement que la dernière écriture a réussi — sur un
    conteneur sans volume monté, elle réussit aussi, et tout disparaît au
    redéploiement. On expose donc de quoi trancher : le chemin, sa taille, sa
    fraîcheur, et surtout s'il vit sur un système de fichiers distinct de « / »
    (un volume monté, par opposition à la couche éphémère du conteneur)."""
    if (refus := admin_guard(request)) is not None:
        return refus
    info: dict[str, Any] = {"status": _persist_status if STATE_FILE else "off", "path": STATE_FILE or None}
    if STATE_FILE:
        path = Path(STATE_FILE)
        try:
            st = path.stat()
            info |= {"exists": True, "bytes": st.st_size, "age_s": round(now() - st.st_mtime)}
        except OSError:
            info |= {"exists": False, "bytes": 0, "age_s": None}
        try:
            # un volume monté a son propre st_dev ; la couche du conteneur partage celui de « / »
            info["separate_volume"] = os.stat(path.parent).st_dev != os.stat("/").st_dev
        except OSError:
            info["separate_volume"] = None
        info["note"] = ("Volume distinct détecté : l'état devrait survivre à un redéploiement."
                        if info.get("separate_volume") else
                        "ATTENTION : même système de fichiers que « / » — probablement la couche "
                        "éphémère du conteneur. Montez un volume durable sur ce chemin, sinon "
                        "l'état sera perdu au prochain redéploiement.")
    return {"persistence": info, "parties": len(parties),
            "secret_from_env": bool(os.getenv("VEILLEUSE_SECRET")),
            "push": PUSH_ENABLED}


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
    party = parties.pop(slug(code), None) or parties.pop(code, None)
    if party is None:
        return JSONResponse({"error": "unknown party"}, status_code=404)
    deleted_codes.add(party.code)
    if len(deleted_codes) > 500:   # borné, comme tout le reste de la mémoire
        deleted_codes.pop()
    mark_dirty()
    for ws in list(party.sockets):       # on ferme aussi les connexions en cours
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass
    log.info("soirée %s… supprimée par l'admin", party.code[:10])
    return {"deleted": party.code}


# --- Fichiers statiques (la webapp) ------------------------------------------
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/sw.js")
async def service_worker() -> FileResponse:
    """Servi à la racine : un service worker enregistré depuis /static/ n'aurait
    pour portée que /static/ — la page « / » ne serait jamais contrôlée, ready ne
    se résoudrait jamais, et le push resterait lettre morte. C'était le cas."""
    return FileResponse(STATIC_DIR / "sw.js", media_type="text/javascript")


@app.get("/admin")
async def admin_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "admin.html")


@app.get("/aide")
async def aide() -> FileResponse:
    return FileResponse(STATIC_DIR / "aide.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
