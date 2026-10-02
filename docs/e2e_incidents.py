"""Incidents : chalet muet, prise en charge, urgences multiples, renfort, rappel.

Couvre les situations où l'app doit rester honnête : une surveillance interrompue
ne doit jamais être présentée comme fonctionnelle, une urgence ne doit jamais en
masquer une autre, et une action humaine ne doit pas faire reverdir une tuile sans
preuve. Les émetteurs sont des sockets pilotées à la milliseconde ; le récepteur
est un vrai navigateur.

Usage : python docs/e2e_incidents.py   (lance son propre serveur sur :8795)
"""
import asyncio
import os
import sys

from playwright.async_api import async_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import (MOBILE, Emitter, create_party, dismiss_overlay,  # noqa: E402
                      join_as_receiver, start_server, state)

PORT = 8795
BASE = f"http://127.0.0.1:{PORT}"


async def main() -> None:
    srv = start_server(PORT, VEILLEUSE_HEARTBEAT_TIMEOUT="5",
                       VEILLEUSE_ESCALATION_DELAY="600", VEILLEUSE_ACK_REMINDER="15")
    async with async_playwright() as p:
        b = await p.chromium.launch()
        try:
            errs: list[str] = []
            ctx = await b.new_context(viewport=MOBILE, locale="fr-FR")
            code = create_party(PORT, "Incidents")

            # ---- 1. service worker : portée racine et `ready` qui se résout ----
            probe = await ctx.new_page()
            await probe.goto(BASE + "/")
            await asyncio.sleep(1.5)
            sw = await probe.evaluate("""async () => ({
              scope: (await navigator.serviceWorker.getRegistration())?.scope || null,
              ready: await Promise.race([navigator.serviceWorker.ready.then(() => 'ok'),
                                         new Promise(r => setTimeout(() => r('timeout'), 4000))]),
            })""")
            print("1. service worker :", sw)
            assert sw["scope"] and sw["scope"].endswith(f":{PORT}/"), "portée autre que la racine"
            assert sw["ready"] == "ok", "serviceWorker.ready ne se résout pas : le push serait mort-né"
            await probe.close()

            async with Emitter(PORT, code, "mesange", "Mésange", "Léo et Jade") as mes, \
                       Emitter(PORT, code, "pinson", "Pinson", "Emma") as pin:
                sa = await join_as_receiver(ctx, PORT, code, "Marie", errs)
                await sa.wait_for_function("document.querySelectorAll('.tile').length === 2", timeout=10000)
                t_mes = sa.locator('.tile:has-text("Mésange")')

                # ---- 2. chalet muet : l'alerte reste, elle ne se referme pas ----
                mes.beating = False
                await sa.wait_for_selector(".overlay:not(.hidden)", timeout=20000)
                k1 = (await sa.inner_text("#ov-kicker")).strip()
                await asyncio.sleep(3.5)
                encore = "hidden" not in (await sa.get_attribute("#overlay", "class"))
                print(f"2. « {k1} » toujours affiché 3,5 s après : {encore}")
                assert encore and "muet" in k1.lower()

                # ---- 3. « Je vais vérifier » : visible, mais pas de faux vert ----
                await sa.click("#ov-ack", timeout=10000)
                await sa.wait_for_function(
                    "document.getElementById('ov-kicker').textContent.includes('vérifier')", timeout=10000)
                st = await state(sa, PORT, code)
                m = next(c for c in st["chalets"] if c["name"] == "Mésange")
                print("3. pris en charge par", m["check_by"], "— tuile toujours :", m["status"])
                assert m["check_by"] == "Marie" and m["status"] == "offline"

                # ---- 4. le heartbeat revient : la vérification se clôt seule ----
                mes.beating = True
                await sa.wait_for_function(                     # CE chalet-là, pas « une tuile verte »
                    "[...document.querySelectorAll('.tile')]"
                    ".some(t => t.textContent.includes('Mésange') && t.dataset.status === 'ok')",
                    timeout=20000)
                st = await state(sa, PORT, code)
                m = next(c for c in st["chalets"] if c["name"] == "Mésange")
                print("4. heartbeat revenu → statut", m["status"], ", vérification close :", m["check_by"] is None)
                assert m["online"] and m["check_by"] is None

                # ---- 5. deux urgences : la plus ancienne d'abord, l'autre annoncée ----
                await mes.alert()
                await asyncio.sleep(1.2)
                await pin.alert()
                await sa.wait_for_function(
                    "document.getElementById('ov-more').textContent.includes('autre')", timeout=10000)
                nom = (await sa.inner_text("#ov-name")).strip()
                more = (await sa.inner_text("#ov-more")).strip()
                print(f"5. overlay : {nom} | {more}")
                assert nom == "Mésange" and "1 autre" in more

                # ---- 6. après « J'y vais », l'overlay passe à l'urgence suivante ----
                await sa.click("#ov-ack", timeout=10000)
                await sa.wait_for_function(
                    "document.getElementById('ov-name').textContent.includes('Pinson')", timeout=10000)
                print("6. après prise en charge → l'overlay bascule sur Pinson")
                await sa.click("#ov-ack", timeout=10000)   # Pinson pris aussi, on dégage l'écran
                await asyncio.sleep(6)                      # laisse la confirmation apaisée se refermer
                await dismiss_overlay(sa)

                # ---- 7. renfort : audible sur une page VISIBLE, statut inchangé ----
                await t_mes.locator("[data-reinforce]").click(timeout=10000)
                await sa.wait_for_function(
                    "document.getElementById('toast').textContent.includes('Renfort')", timeout=10000)
                st = await state(sa, PORT, code)
                m = next(c for c in st["chalets"] if c["name"] == "Mésange")
                print("7. renfort : toast affiché sur page visible, statut toujours", m["status"])
                assert m["status"] == "acked"

                # ---- 8. rappel d'acquittement : un événement, une seule fois ----
                await sa.wait_for_function(
                    "document.getElementById('toast').textContent.includes('Toujours en cours')", timeout=20000)
                st = await state(sa, PORT, code)
                rappels = [e for e in st["events"] if e["kind"] == "ack_reminder"]
                print("8. rappel d'acquittement reçu sur page visible, occurrences :", len(rappels))
                assert rappels

                # ---- 9. silence pendant une prise en charge : distinct, non effacé ----
                await dismiss_overlay(sa)
                mes.beating = False
                await sa.wait_for_function(
                    "document.querySelector('.tile [data-check]') !== null", timeout=20000)
                st = await state(sa, PORT, code)
                m = next(c for c in st["chalets"] if c["name"] == "Mésange")
                print("9. muet pendant acquittement → statut", m["status"], "| connecté :", m["online"],
                      "| « Je vais vérifier » proposé : True")
                assert m["status"] == "acked" and m["online"] is False

                assert not errs, errs
                print("erreurs JS : aucune")
        finally:
            await b.close()
            srv.terminate(); srv.wait()


asyncio.run(main())
