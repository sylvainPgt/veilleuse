import asyncio, json
from playwright.async_api import async_playwright

M = {"width": 390, "height": 844}
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", "--autoplay-policy=no-user-gesture-required"])
        ctx = await b.new_context(viewport=M, device_scale_factor=2, permissions=["microphone"], locale="fr-FR")
        errors = []
        # --- récepteur
        salle = await ctx.new_page(); salle.on("pageerror", lambda e: errors.append(("salle", str(e))))
        await salle.goto("http://localhost:8000/")
        await salle.screenshot(path="docs/01-accueil.png")
        await salle.fill("#in-code", "anniv sylvain"); await salle.fill("#in-name", "Marie")
        await salle.click("button[data-role=salle]")
        await salle.wait_for_selector("#view-salle:not(.hidden)")
        await salle.click("#btn-arm")
        await asyncio.sleep(0.5)
        await salle.screenshot(path="docs/02-salle-vide.png")
        # --- émetteur
        ch = await ctx.new_page(); ch.on("pageerror", lambda e: errors.append(("chalet", str(e))))
        await ch.goto("http://localhost:8000/")
        await ch.fill("#in-code", "anniv sylvain"); await ch.fill("#in-name", "Sylvain")
        await ch.click("button[data-role=chalet]")
        await ch.wait_for_selector("#view-chalet-setup:not(.hidden)")
        await ch.fill("#in-chalet", "Mésange"); await ch.fill("#in-kids", "Léo 4 ans, Jade 2 ans")
        await ch.click("#btn-mic"); await asyncio.sleep(1)
        await ch.screenshot(path="docs/03-chalet-reglage.png")
        await ch.click("#form-chalet button[type=submit]")
        await ch.wait_for_selector("#view-chalet-run:not(.hidden)")
        await asyncio.sleep(1.5)
        await ch.screenshot(path="docs/04-chalet-veille.png")
        # second chalet via API-less path: another emitter page
        ch2 = await ctx.new_page(); ch2.on("pageerror", lambda e: errors.append(("chalet2", str(e))))
        await ch2.goto("http://localhost:8000/")
        await ch2.fill("#in-code", "anniv sylvain"); await ch2.fill("#in-name", "Paul")
        await ch2.click("button[data-role=chalet]")
        await ch2.fill("#in-chalet", "Pinson"); await ch2.fill("#in-kids", "Emma 6 ans")
        await ch2.click("#form-chalet button[type=submit]")
        await ch2.wait_for_selector("#view-chalet-run:not(.hidden)"); await asyncio.sleep(1)
        # choose own chalet on receiver, then trigger test alert
        await salle.select_option("#sel-own", "mesange")
        await salle.screenshot(path="docs/05-salle-ok.png")
        await ch.click("#btn-test"); await asyncio.sleep(1)
        await salle.screenshot(path="docs/06-salle-alerte.png")
        await ch.screenshot(path="docs/07-chalet-alerte.png")
        await salle.click("#ov-ack"); await asyncio.sleep(0.8)
        await salle.screenshot(path="docs/08-salle-jyvais.png")
        # sono screen
        sono = await ctx.new_page(); await sono.set_viewport_size({"width": 1280, "height": 720})
        sono.on("pageerror", lambda e: errors.append(("sono", str(e))))
        await sono.goto("http://localhost:8000/?mode=sono&code=anniv-sylvain"); await asyncio.sleep(1)
        await sono.click("#btn-arm"); await asyncio.sleep(0.3)
        await sono.screenshot(path="docs/09-sono.png")
        await salle.click("[data-resolve=mesange]"); await asyncio.sleep(0.8)
        await ch2.click("#btn-test"); await asyncio.sleep(1)
        await sono.screenshot(path="docs/10-sono-alerte.png")
        # offline simulation: close emitter 2 page → after timeout it goes muet (skip waiting 45s; check state via API)
        st = json.loads(await salle.evaluate("fetch('/api/party/anniv-sylvain').then(r=>r.json()).then(JSON.stringify)"))
        print({c["name"]: c["status"] for c in st["chalets"]})
        print("errors:", errors)
        await b.close()
asyncio.run(main())
