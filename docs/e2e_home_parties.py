"""Soirées connues dès l'accueil, sans inventaire public des liens privés."""
import asyncio
import os
import tempfile

from playwright.async_api import async_playwright
from _harness import start_server


async def main():
    port = 8798
    with tempfile.TemporaryDirectory() as tmp:
        server = start_server(port, VEILLEUSE_STATE_FILE=os.path.join(tmp, "state.json"))
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                ctx = await browser.new_context()
                page = await ctx.new_page()
                await page.goto(f"http://127.0.0.1:{port}/")
                assert await page.locator("#party-chips .chip").count() == 0
                assert await page.is_visible("button[data-role=chalet]")
                assert await page.is_visible("button[data-role=salle]")

                async def create(name):
                    await page.click("button[data-role=salle]")
                    await page.click("#btn-toggle-create")
                    await page.fill("#in-partyname", name)
                    await page.fill("#in-name", "Testeur")
                    await page.click("#btn-continue")
                    await page.wait_for_selector("#share-card:not(.hidden)")
                    code = (await page.text_content("#share-link")).split("#")[-1]
                    await page.click("#btn-share-go")
                    await page.wait_for_selector("#view-salle:not(.hidden)")
                    await page.click("#view-salle [data-back]")
                    return code

                first = await create("Même nom")
                await page.reload()  # l'onglet revient à l'accueil, pas à une session active
                assert await page.is_visible("#home-parties")
                assert await page.locator("#party-chips .chip").count() == 1, "La soirée doit être visible avant le choix du rôle"
                second = await create("Même nom")
                assert first != second
                await page.reload()
                assert await page.locator("#party-chips .chip").count() == 2, "Deux liens distincts même avec le même nom"
                await page.locator("#party-chips .chip").nth(1).click()
                assert first in await page.input_value("#in-code")
                await page.click("button[data-role=chalet]")
                await page.click("#btn-continue")
                await page.wait_for_selector("#view-chalet-setup:not(.hidden)")
                await page.click("#view-chalet-setup [data-back]")
                await page.reload()
                assert await page.locator("#party-chips .chip").count() == 2
                # Réentrée dans le même lien : pas de doublon, même après rechargement.
                await page.locator("#party-chips .chip").nth(1).click()
                await page.click("button[data-role=salle]")
                await page.click("#btn-continue")
                await page.wait_for_selector("#view-salle:not(.hidden)")
                await page.click("#view-salle [data-back]")
                await page.reload()
                assert await page.locator("#party-chips .chip").count() == 2
                # Une entrée locale obsolète ne doit pas prétendre être une soirée existante.
                await page.evaluate("""() => {
                    const list = JSON.parse(localStorage.getItem('veilleuse.recent'));
                    list.push({code: 'soiree-inexistante-12345', name: 'Supprimée', ts: 1});
                    localStorage.setItem('veilleuse.recent', JSON.stringify(list));
                }""")
                await page.reload()
                await page.wait_for_function("!document.querySelector('#party-chips').textContent.includes('Supprimée')")
                assert await page.locator("#party-chips .chip").count() == 2

                outsider = await browser.new_context()
                other_page = await outsider.new_page()
                await other_page.goto(f"http://127.0.0.1:{port}/")
                assert await other_page.locator("#party-chips .chip").count() == 0, "Pas de liste publique des soirées"
                await outsider.close()
                await page.click("#btn-forget-all")
                assert await page.locator("#party-chips .chip").count() == 0
                await page.reload()
                assert await page.locator("#party-chips .chip").count() == 0
                await ctx.close()
                await browser.close()
            print("OK — accueil vide, création, retour/rechargement, doublons, lien absent, isolation, effacement")
        finally:
            server.terminate()
            server.wait(timeout=10)


if __name__ == "__main__":
    asyncio.run(main())
