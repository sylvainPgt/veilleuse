"""Interactions réelles sur les tuiles pendant qu'elles s'actualisent.

Ce script n'utilise QUE des interactions authentiques — clic souris sans `force`,
appui tactile, et une séquence appui / changement d'état / relâchement aux
coordonnées. Jamais `dispatch_event` : un événement synthétique contourne les
contrôles d'actionnabilité et masquerait précisément le défaut à interdire.

Le défaut : `#tiles` était reconstruit en bloc chaque seconde. Le bouton sous le
doigt était détruit entre `pointerdown` et `pointerup`, les deux tombaient sur des
nœuds différents, et le navigateur n'émettait alors aucun clic — « J'y vais »
pouvait rester sans effet. Correctif : mises à jour chirurgicales, et aucune
reconstruction / suppression / réordonnancement pendant qu'un doigt est posé.

Les émetteurs sont pilotés en WebSocket brut (pas de navigateur, pas de micro) :
c'est ce qui rend les changements d'état déterministes à la milliseconde près.
Seul le récepteur est un vrai navigateur, tactile activé.

Usage : python docs/e2e_taps.py   (lance son propre serveur sur :8793)
"""
import asyncio
import os
import sys

from playwright.async_api import async_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import (MOBILE, Emitter, create_party, dismiss_overlay,  # noqa: E402
                      start_server, state)

PORT = 8793
BASE = f"http://127.0.0.1:{PORT}"



async def main() -> None:
    srv = start_server(PORT, VEILLEUSE_HEARTBEAT_TIMEOUT="6",
                       VEILLEUSE_ESCALATION_DELAY="600", VEILLEUSE_ACK_REMINDER="600")
    async with async_playwright() as p:
        b = await p.chromium.launch()
        try:
            errs: list[str] = []
            # tactile activé : on veut pouvoir taper, pas seulement cliquer
            ctx = await b.new_context(viewport=MOBILE, locale="fr-FR", has_touch=True)
            code = create_party(PORT, "Appuis")

            async with Emitter(PORT, code, "alpha", "Alpha", "Léo et Jade") as alpha, \
                       Emitter(PORT, code, "zebre", "Zèbre", "Emma") as zebre:
                sa = await ctx.new_page()
                sa.on("pageerror", lambda e: errs.append(f"salle: {e}"))
                await sa.goto(f"{BASE}/#{code}")
                await sa.click("button[data-role=salle]")
                await sa.fill("#in-name", "Marie")
                await sa.click("#btn-continue")
                await sa.wait_for_function("document.querySelectorAll('.tile').length === 2", timeout=10000)
                await sa.click("#btn-arm")
                t_alpha = sa.locator('.tile:has-text("Alpha")')
                t_zebre = sa.locator('.tile:has-text("Zèbre")')

                # ---- 1. « J'y vais » depuis l'alerte plein écran, clic réel ----
                await alpha.alert()
                await sa.wait_for_selector(".overlay:not(.hidden)", timeout=10000)
                avant = await sa.inner_text("#ov-since")
                await asyncio.sleep(1.3)                       # les durées défilent pendant qu'on vise
                assert avant != await sa.inner_text("#ov-since"), "rien ne s'actualise : le test ne prouverait rien"
                await sa.click("#ov-ack", timeout=10000)       # clic réel, sans force
                await sa.wait_for_function(
                    "document.querySelector('.tile[data-status=acked]') !== null", timeout=10000)
                st = await state(sa, PORT, code)
                assert next(c for c in st["chalets"] if c["name"] == "Alpha")["alert"]["acked_by"] == "Marie"
                print("1. « J'y vais » (clic réel, overlay, durées qui défilent) → pris par Marie")

                # ---- 2. « Renfort » sur la tuile, clic réel ----
                await dismiss_overlay(sa)
                before = await t_alpha.locator(".meta .line").text_content()
                await asyncio.sleep(1.3)
                assert before != await t_alpha.locator(".meta .line").text_content(), "les tuiles ne s'actualisent pas"
                await t_alpha.locator("[data-reinforce]").click(timeout=10000)
                await sa.wait_for_function(
                    "document.getElementById('toast').textContent.includes('Renfort')", timeout=10000)
                st = await state(sa, PORT, code)
                assert any(e["kind"] == "reinforce" for e in st["events"])
                print("2. « Renfort » (clic réel sur tuile en cours d'actualisation) → journal, statut :",
                      next(c for c in st["chalets"] if c["name"] == "Alpha")["status"])

                # ---- 3. « C'est réglé » sur la tuile, clic réel ----
                await dismiss_overlay(sa)
                await t_alpha.locator("[data-resolve]").click(timeout=10000)
                await sa.wait_for_function(
                    "document.querySelector('.tile[data-status=acked]') === null", timeout=10000)
                st = await state(sa, PORT, code)
                assert next(c for c in st["chalets"] if c["name"] == "Alpha")["alert"] is None
                print("3. « C'est réglé » (clic réel) → alerte close")

                # ---- 4. les mêmes gestes au TOUCHER ----
                await alpha.alert()
                await sa.wait_for_selector(".overlay:not(.hidden)", timeout=10000)
                await dismiss_overlay(sa)
                ack = t_alpha.locator("[data-ack]")
                await ack.wait_for(timeout=10000)
                await asyncio.sleep(1.3)
                await ack.tap(timeout=10000)                   # appui tactile réel
                await sa.wait_for_function(
                    "document.querySelector('.tile[data-status=acked]') !== null", timeout=10000)
                await dismiss_overlay(sa)
                await t_alpha.locator("[data-resolve]").tap(timeout=10000)
                await sa.wait_for_function(
                    "document.querySelector('.tile[data-status=acked]') === null", timeout=10000)
                print("4. « J'y vais » puis « C'est réglé » au toucher → pris puis clos")

                # ---- 5. « Je vais vérifier » sur un chalet devenu muet ----
                zebre.beating = False                          # le babyphone se tait
                check = t_zebre.locator("[data-check]")
                await check.wait_for(timeout=20000)
                await dismiss_overlay(sa)
                await asyncio.sleep(1.3)
                await check.click(timeout=10000)
                await sa.wait_for_function(
                    "Array.from(document.querySelectorAll('.tile .meta .line'))"
                    ".some(e => e.textContent.includes('va vérifier'))", timeout=10000)
                st = await state(sa, PORT, code)
                z = next(c for c in st["chalets"] if c["name"] == "Zèbre")
                assert z["check_by"] == "Marie" and z["status"] == "offline"
                print("5. « Je vais vérifier » (clic réel) → pris par Marie, tuile toujours :", z["status"])

                # ---- 6. l'ordre change ENTRE l'appui et le relâchement ----
                # Zèbre est muet et pris en charge (priorité 1) donc placé avant
                # Alpha qui va bien (0). On appuie sur « Écouter » d'Alpha — en
                # seconde position — puis Zèbre redonne signe de vie : l'ordre
                # s'inverse. Le relâchement doit malgré tout agir sur Alpha.
                await dismiss_overlay(sa)
                ordre_avant = await sa.evaluate(
                    "[...document.querySelectorAll('.tile .name')].map(e => e.textContent.trim())")
                btn = t_alpha.locator("[data-listen]")
                box = await btn.bounding_box()
                node_before = await btn.evaluate_handle("e => e")
                await sa.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                await sa.mouse.down()
                zebre.beating = True                           # Zèbre revient → l'ordre doit changer
                await sa.wait_for_function(
                    "async () => (await (await fetch('/api/party/" + code + "')).json())"
                    ".chalets.some(c => c.name === 'Zèbre' && c.status === 'ok')",
                    timeout=15000)                            # preuve que le serveur a bien basculé
                ordre_pendant = await sa.evaluate(
                    "[...document.querySelectorAll('.tile .name')].map(e => e.textContent.trim())")
                same_node = await sa.evaluate("(a) => a === document.querySelector('.tile [data-listen]')",
                                              node_before)
                await sa.mouse.up()
                await sa.wait_for_function(
                    "before => JSON.stringify([...document.querySelectorAll('.tile .name')]"
                    ".map(e => e.textContent.trim())) !== JSON.stringify(before)",
                    arg=ordre_avant, timeout=15000)
                ordre_apres = await sa.evaluate(
                    "[...document.querySelectorAll('.tile .name')].map(e => e.textContent.trim())")
                st = await state(sa, PORT, code)
                ecoute = [c["name"] for c in st["chalets"] if c["listen_by"]]
                print(f"6. ordre {ordre_avant} → figé {ordre_pendant} pendant l'appui → {ordre_apres} après")
                print("   écoute déclenchée sur :", ecoute, "| le nœud pressé a survécu :", same_node)
                assert ordre_pendant == ordre_avant, "les tuiles ont bougé sous le doigt"
                assert ordre_apres != ordre_avant, "l'ordre n'a jamais changé : le test ne prouverait rien"
                assert ecoute == ["Alpha"], f"l'appui a fui vers un autre chalet : {ecoute}"

                # ---- 7. la tuile pressée est RECONSTRUITE pendant l'appui ----
                # Le cas qui perdait les appuis : pendant que le doigt est posé,
                # l'état de CE chalet change (ici sa batterie) et sa tuile doit
                # être redessinée. Si on la reconstruit sous le doigt, appui et
                # relâchement tombent sur deux nœuds et le clic n'existe jamais.
                await sa.wait_for_function(
                    "document.querySelector('.tile [data-listen]').textContent.includes('Écouter')",
                    timeout=20000)                             # l'écoute précédente est retombée
                st = await state(sa, PORT, code)
                listens_avant = sum(1 for e in st["events"] if e["kind"] == "listen")
                btn = t_alpha.locator("[data-listen]")
                box = await btn.bounding_box()
                node_before = await btn.evaluate_handle("e => e")
                await sa.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                await sa.mouse.down()
                alpha.battery = 41                             # change la structure de SA tuile
                await alpha.beat()
                await asyncio.sleep(2.0)
                same_node = await sa.evaluate(
                    "(a) => a === document.querySelector('.tile [data-listen]')", node_before)
                await sa.mouse.up()
                await asyncio.sleep(1.2)
                st = await state(sa, PORT, code)
                listens_apres = sum(1 for e in st["events"] if e["kind"] == "listen")
                batt = next(c for c in st["chalets"] if c["name"] == "Alpha")["battery"]
                print(f"7. tuile reconstruite pendant l'appui : nœud conservé {same_node}, "
                      f"écoutes {listens_avant} → {listens_apres}, batterie rattrapée : {batt} %")
                assert same_node, "la tuile a été reconstruite sous le doigt"
                assert listens_apres == listens_avant + 1, "l'appui s'est perdu"
                assert batt == 41, "la mise à jour différée n'a jamais été rattrapée"

                assert not errs, errs
                print("erreurs JS : aucune")
        finally:
            await b.close()
            srv.terminate()
            srv.wait()


asyncio.run(main())
