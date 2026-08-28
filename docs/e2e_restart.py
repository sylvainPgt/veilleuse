"""Non-régression : la soirée survit à un redémarrage du serveur, pages ouvertes.

Scénario complet, sans jamais recharger les navigateurs :
création → chalet + salle connectés → plusieurs états (la révision monte) →
arrêt réel d'Uvicorn → redémarrage avec le même VEILLEUSE_SECRET → reconnexion
automatique → soirée recréée depuis le lien signé → chalet ré-enregistré →
une nouvelle alerte doit atteindre la salle.

La dernière assertion est celle qui compte : avant le correctif de lastRev,
le serveur repartait à rev=1, le client restait au rev d'avant redémarrage,
et tous les nouveaux états — alertes comprises — étaient ignorés en silence.

Usage : python docs/e2e_restart.py   (lance son propre serveur sur :8791)
"""
import asyncio
import os
import subprocess
import time

from playwright.async_api import async_playwright

PORT = 8791
BASE = f"http://127.0.0.1:{PORT}"
ENV = {**os.environ, "VEILLEUSE_SECRET": "secret-stable-pour-le-test"}
M = {"width": 390, "height": 844}


def start_server() -> subprocess.Popen:
    proc = subprocess.Popen(
        ["python3", "-m", "uvicorn", "app.main:app", "--port", str(PORT)],
        cwd=os.path.join(os.path.dirname(__file__), ".."),
        env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 15
    import urllib.request
    while time.time() < deadline:
        try:
            urllib.request.urlopen(BASE + "/api/health", timeout=1)
            return proc
        except OSError:
            time.sleep(0.3)
    raise RuntimeError("le serveur n'a pas démarré")


async def main() -> None:
    srv = start_server()
    async with async_playwright() as p:
        b = await p.chromium.launch(args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"])
        try:
            ctx_ch = await b.new_context(viewport=M, permissions=["microphone"], locale="fr-FR")
            ctx_sa = await b.new_context(viewport=M, locale="fr-FR")
            errs: list[str] = []

            r = await ctx_ch.request.post(BASE + "/api/parties", data={"name": "test redémarrage"})
            code = (await r.json())["code"]
            link = f"{BASE}/#{code}"
            print("1. soirée créée :", code)

            ch = await ctx_ch.new_page(); ch.on("pageerror", lambda e: errs.append(f"chalet: {e}"))
            await ch.goto(link)
            await ch.click("button[data-role=chalet]"); await ch.click("#btn-continue")
            await ch.click("#btn-mic"); await asyncio.sleep(0.6)
            await ch.fill("#in-chalet", "Mésange"); await ch.fill("#in-kids", "Léo")
            await ch.click("#form-chalet button[type=submit]")
            await ch.wait_for_selector("#view-chalet-run:not(.hidden)")

            sa = await ctx_sa.new_page(); sa.on("pageerror", lambda e: errs.append(f"salle: {e}"))
            await sa.goto(link)
            await sa.click("button[data-role=salle]"); await sa.fill("#in-name", "Marie")
            await sa.click("#btn-continue")
            await sa.wait_for_selector(".tile", timeout=10000)
            print("2. chalet et salle connectés, tuile visible")

            # fait monter la révision : alerte de test puis fausse alerte, deux fois
            for _ in range(2):
                await ch.click("#btn-test")
                await sa.wait_for_selector('.tile[data-status=alert]', timeout=8000)
                await ch.click("#btn-cancel")
                await sa.wait_for_selector('.tile:not([data-status=alert])', timeout=8000)
            print("3. plusieurs états échangés, révision montée")

            srv.terminate(); srv.wait()
            print("4. serveur arrêté (pages toujours ouvertes)")
            await sa.wait_for_selector("#conn-lost:not(.hidden)", timeout=25000)
            print("5. la salle affiche la perte de connexion")

            srv = start_server()
            print("6. serveur redémarré, même secret")

            # reconnexion automatique (backoff ≤ 15 s), soirée recréée, chalet ré-enregistré
            await sa.wait_for_selector("#conn-lost.hidden", state="attached", timeout=40000)
            await sa.wait_for_selector('.tile[data-status=ok]', timeout=40000)
            print("7. reconnexion automatique : tuile revenue au vert, sans recharger")

            # l'assertion qui compte : une nouvelle alerte doit passer
            await ch.click("#btn-test")
            await sa.wait_for_selector('.tile[data-status=alert]', timeout=10000)
            print("8. une alerte post-redémarrage atteint la salle : OK")

            assert not errs, errs
            print("erreurs JS : aucune")
        finally:
            await b.close()
            srv.terminate(); srv.wait()


asyncio.run(main())
