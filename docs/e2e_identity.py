"""Identité des émetteurs : un jeton par soirée, un seul émetteur courant.

Deux pièges fermés ici. Le jeton d'émetteur était rangé globalement : passer de
la soirée A à la B écrasait celui de A, et revenir à A finissait en tuile
dupliquée silencieuse. Et deux sockets pouvaient tenir le même chalet en même
temps, sans que l'ancienne perde ses droits.

Usage : python docs/e2e_identity.py   (lance son propre serveur sur :8796)
"""
import asyncio
import os
import sys

from playwright.async_api import async_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import (MIC_ARGS, MOBILE, create_party, join_as_chalet,  # noqa: E402
                      start_server, state)

PORT = 8796
BASE = f"http://127.0.0.1:{PORT}"


async def main() -> None:
    srv = start_server(PORT, VEILLEUSE_HEARTBEAT_TIMEOUT="45")
    async with async_playwright() as p:
        b = await p.chromium.launch(args=MIC_ARGS)
        try:
            errs: list[str] = []
            ctx = await b.new_context(viewport=MOBILE, permissions=["microphone"], locale="fr-FR")
            code_a = create_party(PORT, "Soirée A")
            code_b = create_party(PORT, "Soirée B")

            # ---- 1. A → B → retour A : pas de tuile dupliquée ----
            pg = await join_as_chalet(ctx, PORT, code_a, "Mésange", "Léo", errs)
            await pg.click("#btn-stop"); await pg.wait_for_selector("#view-home:not(.hidden)")
            pg = await join_as_chalet(ctx, PORT, code_b, "Mésange", "Léo", errs)
            await pg.click("#btn-stop"); await pg.wait_for_selector("#view-home:not(.hidden)")
            pg = await join_as_chalet(ctx, PORT, code_a, "Mésange", "Léo", errs)
            st = await state(pg, PORT, code_a)
            noms = [c["name"] for c in st["chalets"]]
            print("1. A → B → A : chalets dans A =", noms)
            assert len(st["chalets"]) == 1, "le jeton de B a écrasé celui de A"
            # les jetons sont bien distincts et rangés par soirée
            toks = await pg.evaluate(
                f"[localStorage.getItem('veilleuse.ctok:{code_a}'),"
                f" localStorage.getItem('veilleuse.ctok:{code_b}')]")
            assert toks[0] and toks[1] and toks[0] != toks[1], "les jetons ne sont pas cloisonnés"
            print("   jetons distincts par soirée : True")

            # ---- 2. relève : le second onglet prend la main, le premier est démis ----
            pg2 = await join_as_chalet(ctx, PORT, code_a, "Mésange", "Léo", errs)
            await pg.wait_for_function(
                "document.getElementById('run-status').textContent.includes('Repris')", timeout=10000)
            st = await state(pg2, PORT, code_a)
            print("2. second onglet enregistré → premier démis :",
                  (await pg.inner_text("#run-status")).strip(), "| chalets :", len(st["chalets"]))
            assert len(st["chalets"]) == 1

            # ---- 3. « Reprendre ici » rend la main, dans l'autre sens ----
            await pg.click("#btn-retake")
            await pg2.wait_for_function(
                "document.getElementById('run-status').textContent.includes('Repris')", timeout=10000)
            await pg.wait_for_function(
                "document.getElementById('run-status').textContent.includes('écoute')", timeout=10000)
            print("3. « Reprendre ici » → le premier réémet, le second est démis à son tour")

            assert not errs, errs
            print("erreurs JS : aucune")
        finally:
            await b.close()
            srv.terminate(); srv.wait()


asyncio.run(main())
