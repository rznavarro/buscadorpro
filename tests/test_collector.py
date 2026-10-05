"""Recolector web con webs simuladas (httpx.MockTransport): sin internet ni navegador."""

import asyncio
from collections.abc import Callable

import httpx
import pytest

from app.analyze.collector import SSL_REASON, RenderResult, WebCollector
from app.config import Settings
from app.models import WebsiteStatus, WhatsAppPlacement, WhatsAppSource

PAGE = "<html><head><title>{title}</title></head><body>{body}</body></html>"
FILLER = "<p>" + "Cerrajería a domicilio en Rancagua, apertura de puertas y cambio de chapas. " * 6 + "</p>"


def page(body: str, title: str = "Negocio") -> str:
    return PAGE.format(title=title, body=FILLER + body)


def site(routes: dict[str, httpx.Response | Callable[[httpx.Request], httpx.Response]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        route = routes.get(url)
        if route is None:
            return httpx.Response(404, text="No encontrado")
        return route(request) if callable(route) else route

    return httpx.MockTransport(handler)


def html(text: str, status: int = 200, **headers: str) -> httpx.Response:
    return httpx.Response(status, text=text, headers={"content-type": "text/html; charset=utf-8", **headers})


def run_verify(transport, url, renderer=None, **settings):
    async def go():
        options = {"_env_file": None, "web_max_internal_pages": 3, **settings}
        async with WebCollector(Settings(**options), transport=transport, renderer=renderer, use_browser=False) as c:
            return await c.verify(url)

    return asyncio.run(go())


def test_ok_site_with_whatsapp_on_contact_page():
    transport = site(
        {
            "http://negocio.cl/": httpx.Response(301, headers={"location": "https://negocio.cl/"}),
            "https://negocio.cl/": html(page('<nav><a href="/contacto/">Contacto</a><a href="/servicios/">Servicios</a></nav>')),
            "https://negocio.cl/contacto/": html(page('<a href="https://wa.me/56993557317">WhatsApp</a>')),
            "https://negocio.cl/servicios/": html(page("<p>Servicios</p>")),
        }
    )
    result = run_verify(transport, "http://negocio.cl/")
    assert result.status == WebsiteStatus.OK
    assert result.final_url == "https://negocio.cl/"
    assert result.https is True
    assert result.redirects == ["http://negocio.cl/"]
    assert result.pages_visited == ["https://negocio.cl/contacto/", "https://negocio.cl/servicios/"]
    [candidate] = result.whatsapp_candidates
    assert (candidate.number, candidate.page_url) == ("56993557317", "https://negocio.cl/contacto/")


def test_domain_without_dns_is_down():
    def fail(request):
        raise httpx.ConnectError("[Errno 11001] getaddrinfo failed", request=request)

    result = run_verify(site({"https://noexiste.cl/": fail}), "https://noexiste.cl/")
    assert result.status == WebsiteStatus.CAIDO
    assert result.reason == "El dominio no existe o no tiene la web configurada (sin DNS)"


def test_http_404_is_down():
    result = run_verify(site({"https://negocio.cl/": html("No encontrado", 404)}), "https://negocio.cl/")
    assert result.status == WebsiteStatus.CAIDO
    assert "404" in result.reason


def test_invalid_certificate_is_down_but_whatsapp_is_still_read():
    calls = {"n": 0}

    def ssl_then_content(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate has expired", request=request)
        return html(page('<a href="https://wa.me/56987654321">WA</a>'))

    result = run_verify(site({"https://negocio.cl/": ssl_then_content}), "https://negocio.cl/")
    assert result.status == WebsiteStatus.CAIDO
    assert result.reason == SSL_REASON
    assert [c.number for c in result.whatsapp_candidates] == ["56987654321"]


def test_parked_domain_is_down():
    parked = PAGE.format(title="negocio.cl", body="<h1>Este dominio está a la venta</h1>")
    result = run_verify(site({"https://negocio.cl/": html(parked)}), "https://negocio.cl/")
    assert result.status == WebsiteStatus.CAIDO
    assert result.reason == "El dominio está a la venta"


def test_bot_protection_is_blocked_when_browser_also_gets_the_wall():
    wall = PAGE.format(title="Just a moment...", body="Checking your browser")

    async def renderer(url):
        return RenderResult(final_url=url, status_code=403, html=wall)

    result = run_verify(site({"https://negocio.cl/": html(wall, 403)}), "https://negocio.cl/", renderer)
    assert result.status == WebsiteStatus.BLOQUEADO


def test_bot_protection_passes_in_real_browser():
    async def renderer(url):
        return RenderResult(final_url=url, status_code=200, html=page('<a href="https://wa.me/56987654321">WA</a>'))

    result = run_verify(site({"https://negocio.cl/": html("Forbidden", 403)}), "https://negocio.cl/", renderer)
    assert result.status == WebsiteStatus.OK
    assert result.rendered is True
    assert [c.number for c in result.whatsapp_candidates] == ["56987654321"]


def test_javascript_site_is_rendered_and_floating_whatsapp_found():
    spa = '<html><head><title>App</title><script>window.cfg = {"phone": "x"}</script></head><body><div id="root"></div></body></html>'

    async def renderer(url):
        rendered = page('<div class="whatsapp-float"><a href="https://wa.me/56993557317">WA</a></div>')
        return RenderResult(final_url=url, status_code=200, html=rendered)

    result = run_verify(site({"https://app.manus.space/": html(spa)}), "https://app.manus.space/", renderer)
    assert result.rendered is True
    assert result.whatsapp_floating is True
    assert [(c.number, c.placement) for c in result.whatsapp_candidates] == [("56993557317", WhatsAppPlacement.FLOTANTE)]


def test_redirect_to_facebook_means_only_socials():
    transport = site(
        {
            "https://negocio.cl/": httpx.Response(302, headers={"location": "https://www.facebook.com/negocio"}),
            "https://www.facebook.com/negocio": html(page("Facebook")),
        }
    )
    result = run_verify(transport, "https://negocio.cl/")
    assert result.status == WebsiteStatus.SOLO_REDES
    assert result.socials == {"facebook": ["https://www.facebook.com/negocio"]}


def test_linktree_with_own_website_verifies_the_website_too():
    linktree = page(
        '<a href="https://wa.me/56987654321">WhatsApp</a>'
        '<a href="https://instagram.com/negocio">IG</a>'
        '<a href="https://negocio.cl/">Web</a>'
        '<a href="https://linktr.ee/privacy">Privacidad</a>'
    )
    transport = site(
        {
            "https://linktr.ee/negocio": html(linktree),
            "https://negocio.cl/": html(page('<a href="https://wa.me/56987654321">WA</a>')),
        }
    )
    result = run_verify(transport, "https://linktr.ee/negocio")
    assert result.status == WebsiteStatus.OK
    assert result.final_url == "https://negocio.cl/"
    assert result.website_from_linktree == "https://negocio.cl/"
    assert result.linktree_urls == ["https://linktr.ee/negocio"]
    assert {(c.number, c.source) for c in result.whatsapp_candidates} == {
        ("56987654321", WhatsAppSource.WEB),
        ("56987654321", WhatsAppSource.LINKTREE),
    }
    assert result.socials["instagram"] == ["https://www.instagram.com/negocio"]


def test_linktree_without_website_is_only_socials():
    transport = site({"https://linktr.ee/negocio": html(page('<a href="https://wa.me/56987654321">WhatsApp</a>'))})
    result = run_verify(transport, "https://linktr.ee/negocio")
    assert result.status == WebsiteStatus.SOLO_REDES
    assert [(c.number, c.source) for c in result.whatsapp_candidates] == [("56987654321", WhatsAppSource.LINKTREE)]


def test_linktree_linked_from_website_is_read():
    transport = site(
        {
            "https://negocio.cl/": html(page('<a href="https://linktr.ee/negocio">Nuestros enlaces</a>')),
            "https://linktr.ee/negocio": html(page('<a href="https://wa.me/56987654321">WhatsApp</a>')),
        }
    )
    result = run_verify(transport, "https://negocio.cl/")
    assert result.linktree_urls == ["https://linktr.ee/negocio"]
    assert [(c.number, c.source) for c in result.whatsapp_candidates] == [("56987654321", WhatsAppSource.LINKTREE)]


def test_shortlinks_on_the_website_are_followed():
    transport = site(
        {
            "https://negocio.cl/": html(page('<a href="https://wa.link/abc">WhatsApp</a><a href="https://wa.link/roto">2</a>')),
            "https://wa.link/abc": httpx.Response(301, headers={"location": "https://api.whatsapp.com/send?phone=56987654321"}),
            "https://wa.link/roto": httpx.Response(404),
        }
    )
    result = run_verify(transport, "https://negocio.cl/")
    assert [c.number for c in result.whatsapp_candidates] == ["56987654321"]
    assert result.whatsapp_broken == ["https://wa.link/roto"]


def test_latin1_pages_keep_their_accents():
    body = PAGE.format(title="Peñalolén", body=FILLER).replace("<head>", '<head><meta charset="iso-8859-1">')
    response = httpx.Response(200, content=body.encode("latin-1"), headers={"content-type": "text/html"})
    result = run_verify(site({"https://negocio.cl/": response}), "https://negocio.cl/")
    assert result.facts.title == "Peñalolén"


def test_social_or_whatsapp_links_are_not_fetched():
    def boom(request):
        raise AssertionError("no debería descargarse")

    transport = httpx.MockTransport(boom)
    assert run_verify(transport, "https://www.instagram.com/negocio/").status == WebsiteStatus.SOLO_REDES
    assert run_verify(transport, "https://wa.me/56987654321").status == WebsiteStatus.SIN_WEB


def test_check_links_reports_only_broken_ones():
    transport = site(
        {
            "https://negocio.cl/ok": httpx.Response(200),
            "https://negocio.cl/roto": httpx.Response(404),
            "https://negocio.cl/sin-head": lambda r: httpx.Response(405 if r.method == "HEAD" else 200),
        }
    )

    async def go():
        async with WebCollector(Settings(_env_file=None), transport=transport, use_browser=False) as c:
            return await c.check_links(
                ["https://negocio.cl/ok", "https://negocio.cl/roto", "https://negocio.cl/sin-head"]
            )

    broken = asyncio.run(go())
    assert [(b.url, b.reason) for b in broken] == [
        ("https://negocio.cl/roto", "La web responde con error 404 (página no encontrada)")
    ]


@pytest.mark.parametrize("status", [500, 503])
def test_server_errors_are_down(status):
    result = run_verify(site({"https://negocio.cl/": html("error", status)}), "https://negocio.cl/")
    assert result.status == WebsiteStatus.CAIDO
    assert str(status) in result.reason


def test_parallel_reviews_open_the_browser_only_once(monkeypatch):
    # Error real (2026-10-04): 3 webs revisadas en paralelo, 2 fallaban con "El navegador no está iniciado".
    started: list[object] = []

    class SlowEngine:
        def __init__(self, settings, headless=True):
            self.ready = False

        async def start(self):
            await asyncio.sleep(0.05)  # arrancar Chromium toma tiempo
            self.ready = True
            started.append(self)

        async def close(self):
            pass

    monkeypatch.setattr("app.analyze.collector.BrowserEngine", SlowEngine)

    async def go():
        async with WebCollector(Settings(_env_file=None), transport=site({})) as collector:
            return await asyncio.gather(*(collector._browser() for _ in range(3)))

    engines = asyncio.run(go())
    assert len(started) == 1
    assert all(engine is started[0] and engine.ready for engine in engines)


def test_private_google_site_that_asks_to_log_in_is_not_a_working_web():
    # Caso real (2026-10-04): un Google Sites privado redirige al inicio de sesión de Google.
    login = "https://accounts.google.com/v3/signin/identifier?continue=https%3A%2F%2Fsites.google.com%2Fview%2Fcerrajero"
    transport = site(
        {
            "https://sites.google.com/view/cerrajero": httpx.Response(302, headers={"Location": login}),
            login: httpx.Response(200, html=page("<h1>Iniciar sesión</h1><p>Usa tu cuenta de Google</p>", title="Acceder: Cuentas de Google")),
        }
    )

    async def go():
        async with WebCollector(Settings(_env_file=None), transport=transport, use_browser=False) as collector:
            return await collector.verify("https://sites.google.com/view/cerrajero")

    result = asyncio.run(go())
    assert result.status == WebsiteStatus.CAIDO
    assert result.reason == "La web no es pública: pide iniciar sesión en Google, así que los clientes no pueden verla"
    assert result.whatsapp_candidates == []  # no se lee nada de la página de Google
