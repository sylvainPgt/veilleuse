"""Briques communes aux scénarios navigateur de `docs/`.

Chaque script lance SON serveur sur son propre port, avec ses propres délais :
aucun n'a besoin qu'on ait démarré quoi que ce soit à la main, et deux scripts
peuvent tourner en parallèle sans se marcher dessus.
"""
import asyncio
import json
import os
import subprocess
import time
import urllib.request

from websockets.asyncio.client import connect

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
MOBILE = {"width": 390, "height": 844}


def start_server(port: int, **env: str) -> subprocess.Popen:
    """Démarre uvicorn et attend qu'il réponde. `env` surcharge les réglages —
    les scénarios ont besoin de délais courts (heartbeat, escalade, rappel)."""
    full = {**os.environ, "VEILLEUSE_STATE_FILE": "", **env}
    proc = subprocess.Popen(
        ["python3", "-m", "uvicorn", "app.main:app", "--port", str(port)],
        cwd=ROOT, env=full, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1)
            return proc
        except OSError:
            time.sleep(0.3)
    proc.terminate()
    raise RuntimeError(f"le serveur n'a pas démarré sur :{port}")


def create_party(port: int, name: str) -> str:
    """Crée une soirée et renvoie son identifiant signé."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/parties",
                                 data=json.dumps({"name": name}).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())["code"]


async def state(page, port: int, code: str) -> dict:
    """L'état vu par le serveur, lu depuis la page (même origine)."""
    return json.loads(await page.evaluate(
        f"fetch('/api/party/{code}').then(r=>r.json()).then(JSON.stringify)"))


async def dismiss_overlay(page) -> None:
    """L'alerte plein écran recouvre les tuiles : on la masque d'un vrai clic."""
    if "hidden" not in (await page.get_attribute("#overlay", "class")):
        await page.click("#ov-dismiss", timeout=5000)
        await page.wait_for_selector("#overlay.hidden", state="attached", timeout=5000)


class Emitter:
    """Un téléphone de chalet réduit à sa socket : on décide exactement quand il
    bat, quand il se tait et quand il alerte. Plus déterministe qu'un navigateur
    avec micro simulé — le parcours émetteur complet, lui, est couvert par
    `e2e.py` et `e2e_resume.py`."""

    def __init__(self, port: int, code: str, cid: str, name: str, kids: str = ""):
        self.port, self.code, self.cid, self.name, self.kids = port, code, cid, name, kids
        self.beating = True
        self.battery = 80
        self.aid: str | None = None
        self.token: str | None = None

    async def __aenter__(self):
        self.ws = await connect(f"ws://127.0.0.1:{self.port}/ws/{self.code}")
        await self.ws.recv()                       # état initial
        await self.send({"type": "register", "chalet_id": self.cid,
                         "name": self.name, "kids": self.kids})
        self.beater = asyncio.create_task(self._beat_loop())
        self.reader = asyncio.create_task(self._read_loop())
        await asyncio.sleep(0.6)
        return self

    async def __aexit__(self, *_):
        self.beater.cancel(); self.reader.cancel()
        await self.ws.close()

    async def send(self, msg: dict) -> None:
        await self.ws.send(json.dumps(msg))

    async def beat(self) -> None:
        await self.send({"type": "hb", "chalet_id": self.cid, "level": 5,
                         "battery": self.battery, "threshold": 55})

    async def alert(self, reason: str = "noise") -> str:
        self.aid = f"a{int(time.time() * 1000) % 10 ** 7}"
        await self.send({"type": "alert", "chalet_id": self.cid, "level": 90,
                         "reason": reason, "aid": self.aid})
        return self.aid

    async def _beat_loop(self) -> None:
        while True:
            try:
                if self.beating:
                    await self.beat()
                await asyncio.sleep(2)
            except (asyncio.CancelledError, Exception):
                return

    async def _read_loop(self) -> None:
        """Répond aux demandes d'écoute : sans clip en retour, le récepteur
        resterait bloqué 30 s sur « une écoute est déjà en cours »."""
        while True:
            try:
                msg = json.loads(await self.ws.recv())
                if msg.get("type") == "registered":
                    self.token = msg.get("token")
                elif msg.get("type") == "clip_request":
                    await self.send({"type": "clip", "chalet_id": self.cid,
                                     "clip": "data:audio/mp4;base64,AAAA"})
            except (asyncio.CancelledError, Exception):
                return


async def join_as_receiver(ctx, port: int, code: str, name: str, errs: list) -> object:
    """Ouvre un récepteur (salle), alertes activées."""
    pg = await ctx.new_page()
    pg.on("pageerror", lambda e: errs.append(f"{name}: {e}"))
    await pg.goto(f"http://127.0.0.1:{port}/#{code}")
    await pg.click("button[data-role=salle]")
    await pg.fill("#in-name", name)
    await pg.click("#btn-continue")
    await pg.wait_for_selector("#view-salle:not(.hidden)", timeout=10000)
    await pg.click("#btn-arm")
    return pg


async def join_as_chalet(ctx, port: int, code: str, nom: str, kids: str, errs: list) -> object:
    """Ouvre un émetteur dans un vrai navigateur (micro simulé par Chromium)."""
    pg = await ctx.new_page()
    pg.on("pageerror", lambda e: errs.append(f"{nom}: {e}"))
    await pg.goto(f"http://127.0.0.1:{port}/#{code}")
    await pg.click("button[data-role=chalet]")
    await pg.click("#btn-continue")
    await pg.wait_for_selector("#view-chalet-setup:not(.hidden)", timeout=10000)
    if await pg.is_visible("#btn-mic"):
        await pg.click("#btn-mic")
        await asyncio.sleep(0.5)
    if not await pg.input_value("#in-chalet"):
        await pg.fill("#in-chalet", nom)
        await pg.fill("#in-kids", kids)
    await pg.click("#form-chalet button[type=submit]")
    await pg.wait_for_function(
        "document.getElementById('run-status').textContent.includes('écoute')", timeout=15000)
    return pg


MIC_ARGS = ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"]
