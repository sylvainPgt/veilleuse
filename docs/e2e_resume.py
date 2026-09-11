"""Reprise après rechargement : on ne renvoie jamais quelqu'un à l'accueil.

Recharger la page en pleine soirée renvoyait à l'écran d'accueil, rôle perdu.
L'onglet mémorise désormais son rôle (sessionStorage : survit au rechargement,
pas à la fermeture). Ce script vérifie aussi ce qui NE doit pas reprendre.

Usage : python docs/e2e_resume.py   (lance son propre serveur sur :8797)
"""
import asyncio
import os
import sys

from playwright.async_api import async_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import (MIC_ARGS, MOBILE, create_party, join_as_chalet,  # noqa: E402
                      join_as_receiver, start_server)

PORT = 8797
BASE = f"http://127.0.0.1:{PORT}"


async def main() -> None:
    srv = start_server(PORT, VEILLEUSE_HEARTBEAT_TIMEOUT="45")
    async with async_playwright() as p:
        b = await p.chromium.launch(args=MIC_ARGS)
        try:
            errs: list[str] = []
            ctx = await b.new_context(viewport=MOBILE, permissions=["microphone"], locale="fr-FR")
            code = create_party(PORT, "Reprise")

            # ---- 1. la salle revient sur ses tuiles ----
            sa = await join_as_receiver(ctx, PORT, code, "Marie", errs)
            await sa.reload()
            await sa.wait_for_selector("#view-salle:not(.hidden)", timeout=10000)
            # le son exige un geste : la carte d'activation doit réapparaître
            print("1. salle reprend après rechargement, carte « Activer les alertes » réaffichée :",
                  await sa.is_visible("#salle-armed"))
            assert await sa.is_visible("#salle-armed")

            # ---- 2. le chalet revient en veille, micro relancé sans geste ----
            ch = await join_as_chalet(ctx, PORT, code, "Mésange", "Léo", errs)
            await ch.reload()
            await ch.wait_for_selector("#view-chalet-run:not(.hidden)", timeout=10000)
            await ch.wait_for_function(
                "document.getElementById('run-status').textContent.includes('écoute')", timeout=10000)
            print("2. chalet reprend, micro relancé, statut :", (await ch.inner_text("#run-status")).strip())

            # ---- 3. « Retour » efface la reprise ----
            await sa.click("#view-salle [data-back]")
            await sa.wait_for_selector("#view-home:not(.hidden)")
            await sa.reload()
            await sa.wait_for_selector("#view-home:not(.hidden)", timeout=10000)
            print("3. après « Retour », recharger reste sur l'accueil")

            # ---- 4. un lien vers une AUTRE soirée gagne sur la reprise ----
            autre = create_party(PORT, "Autre")
            await ch.goto(f"{BASE}/#{autre}")
            await ch.reload()
            await ch.wait_for_selector("#view-home:not(.hidden)", timeout=10000)
            rempli = autre in await ch.input_value("#in-code")
            print("4. lien vers une autre soirée → accueil prérempli avec CE lien :", rempli)
            assert rempli

            assert not errs, errs
            print("erreurs JS : aucune")
        finally:
            await b.close()
            srv.terminate(); srv.wait()



asyncio.run(main())
