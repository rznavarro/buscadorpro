"""Prueba mínima con navegador real. Correr con: uv run pytest -m browser"""

import asyncio

import pytest

from app.browser.engine import BrowserEngine

pytestmark = pytest.mark.browser


def test_browser_opens_page_and_closes():
    async def run() -> tuple[str, bool]:
        engine = BrowserEngine()
        async with engine:
            page = await engine.page()
            # Página local: la prueba no depende de internet.
            await page.goto("data:text/html,<title>Vortexia OK</title><h1>hola</h1>")
            title = await page.title()
        return title, engine.is_connected

    title, still_connected = asyncio.run(run())
    assert title == "Vortexia OK"
    assert still_connected is False
