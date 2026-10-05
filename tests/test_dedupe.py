"""No repetir leads: huellas (número, web, red), lista de ya contactados y saltos en la búsqueda."""

import asyncio
import re
import time
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient

from app.analyze.collector import WebCollector
from app.extract.html_facts import fold_accents
from app.config import Settings
from app.db.repository import Repository
from app.db.schema import create_db_engine, init_db
from app.dedupe import (
    NUMBER,
    SOCIAL,
    WEB,
    business_fingerprints,
    ensure_fingerprints,
    import_contacted,
    link_fingerprints,
    number_key,
    parse_contacted,
    site_key,
    unmark_duplicate,
)
from app.extract.phones import canonical_number, normalize_phone
from app.extract.whatsapp import choose_whatsapp, extract_whatsapp, normalize_whatsapp_number
from app.leads import discard_lead, duplicates_for_day, leads_for_day
from app.models import Business, WebsiteStatus, WhatsAppConfidence, WhatsAppSource
from app.pipeline import LeadsProgress, is_lead, run_leads_job
from app.sources.base import MapsListing, MapsPlace, MapsSource
from app.web.app import create_app

FILLER = "<p>" + "Servicio a domicilio, atención rápida y garantizada para toda la ciudad. " * 6 + "</p>"


def page(body: str = "") -> httpx.Response:
    html = f"<html><head><title>Negocio</title></head><body>{FILLER}{body}</body></html>"
    return httpx.Response(200, html=html)


@pytest.fixture
def repo() -> Repository:
    engine = create_db_engine(":memory:")
    init_db(engine)
    return Repository(engine)


# --- Números ---------------------------------------------------------------------------


def test_mexican_mobile_with_old_1_is_normalized():
    assert normalize_phone("+52 1 55 1234 5678").e164 == "+525512345678"
    assert normalize_phone("+52 55 1234 5678").e164 == "+525512345678"


def test_mexican_whatsapp_link_keeps_its_format_and_matches_maps_phone():
    # WhatsApp usa 521…: el enlace se conserva tal cual (abre el chat) y no se marca como mal escrito.
    normalized = normalize_whatsapp_number("5215512345678", "MX")
    assert (normalized.number, normalized.note, normalized.broken) == ("5215512345678", None, False)
    candidates = extract_whatsapp('<a href="https://wa.me/5215512345678">WA</a>', source=WhatsAppSource.WEB).candidates
    decision = choose_whatsapp(candidates, "+525512345678")
    assert decision.confidence == WhatsAppConfidence.ALTA  # coincide con el teléfono de Maps


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("5215512345678", "525512345678"),  # México: con y sin el 1
        ("5491123456789", "541123456789"),  # Argentina: WhatsApp (549) y Maps (54)
        ("+56 9 9355 7317", "56993557317"),
    ],
)
def test_same_number_in_different_formats_has_the_same_key(a, b):
    assert number_key(a) == number_key(b) == canonical_number(b)


def test_short_numbers_are_not_fingerprints():
    assert number_key("1234") is None
    assert number_key(None) is None


# --- Webs y enlaces -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "key"),
    [
        ("https://www.negocio.cl/contacto?utm_source=maps", "negocio.cl"),
        ("http://negocio.cl", "negocio.cl"),
        ("negocio.cl/sucursal-rancagua", "negocio.cl"),  # sucursales de la misma web
        ("https://linktr.ee/Negocio", "linktr.ee/negocio"),
        ("https://sites.google.com/view/cerrajeria-xyz/inicio", "sites.google.com/view/cerrajeria-xyz"),
        ("https://usuario.wixsite.com/mi-negocio/contacto", "usuario.wixsite.com/mi-negocio"),
        ("https://booksy.com/es-cl/12345_salon-bella", "booksy.com/es-cl/12345_salon-bella"),
        ("https://www.google.com/url?q=https://negocio.cl/&sa=U", "negocio.cl"),
        ("https://linktr.ee/", None),  # la portada de la plataforma no es de ningún negocio
        ("", None),
    ],
)
def test_site_key(url, key):
    assert site_key(url) == key


def test_different_salons_on_the_same_booking_platform_are_not_the_same_web():
    assert site_key("https://booksy.com/es-cl/111_salon-a") != site_key("https://booksy.com/es-cl/222_salon-b")


@pytest.mark.parametrize(
    ("url", "prints"),
    [
        ("http://wa.me/56957734621", [(NUMBER, "56957734621")]),
        ("https://api.whatsapp.com/send?phone=5215512345678", [(NUMBER, "525512345678")]),
        ("https://wa.link/abc123", [(WEB, "wa.link/abc123")]),
        ("https://wa.me/message/XYZ", [(WEB, "wa.me/message/xyz")]),
        ("https://www.facebook.com/profile.php?id=61560834118490&mibextid=ZbWKwL",
         [(SOCIAL, "https://www.facebook.com/profile.php?id=61560834118490")]),
        ("https://www.instagram.com/Cerrajeria.XYZ/", [(SOCIAL, "https://www.instagram.com/cerrajeria.xyz")]),
        ("https://linktr.ee/negocio", [(WEB, "linktr.ee/negocio")]),
        ("https://maps.app.goo.gl/abc", []),
        (None, []),
    ],
)
def test_link_fingerprints(url, prints):
    assert link_fingerprints(url) == prints


def test_business_fingerprints_collect_numbers_web_and_socials():
    business = Business(
        google_maps_url="https://www.google.com/maps/place/x",
        business_name="X",
        phone_e164="+56722234567",
        whatsapp_url="https://wa.me/56993557317",
        whatsapp_candidates=[{"number": "56981234567"}, {"number": None}],
        website="https://www.negocio.cl/",
        instagram="https://www.instagram.com/negocio",
        field_sources={"website_maps": "https://linktr.ee/negocio"},
    )
    assert business_fingerprints(business) == [
        (NUMBER, "56722234567"),
        (NUMBER, "56993557317"),
        (NUMBER, "56981234567"),
        (WEB, "negocio.cl"),
        (WEB, "linktr.ee/negocio"),
        (SOCIAL, "https://www.instagram.com/negocio"),
    ]


# --- Lista de ya contactados ------------------------------------------------------------------

PASTED = """inmobiliaria sol\t+1 (954) 555-0147\tcontactado\t1.39 pm\tlunes 30 de agosoto
taller norte\t+56 9 9123 0001\tcontactado\t7:00 pm\tmiercoles 2 de septiembre
estudio luna\t+54 9 11 2345-0002\tcontactado\t3:56 pm\tjueves 3 de septiembre
cerrajeria centro mx\t+52 1 55 1234 0003\tcontactado\t8:07 pm\tdomingo 13 de septiembre
vidrios del valle\t+56 9 8123 0004\trespuesta positiva\t3:42 pm\tlunes 21 de septiembre\tchecked
climas maipu \t+56 2 2345 0005\tcontactado\t8:35 pm\tmiercoles 23 de septiembre

sin estado\t+56 9 8123 0006\t9:10 am\tviernes 25 de septiembre
nombre\tteléfono\testado\thora\tfecha
taller norte\t+56 9 9123 0001\tdemo enviada\t1:00 pm\tjueves 24 de septiembre
pegado sin tabulaciones +56 9 9123 0007 contactado
"""


def test_parse_contacted_reads_every_format():
    rows, invalid = parse_contacted(PASTED)
    by_name = {row.name: row for row in rows}
    assert by_name["inmobiliaria sol"].phone_e164 == "+19545550147"
    assert by_name["estudio luna"].phone_e164 == "+5491123450002"
    assert by_name["cerrajeria centro mx"].phone_e164 == "+525512340003"
    assert by_name["climas maipu"].phone_e164 == "+56223450005"  # fijo
    assert by_name["vidrios del valle"].status == "respuesta positiva"
    assert by_name["vidrios del valle"].note == "checked"
    assert by_name["vidrios del valle"].contacted_on == "3:42 pm, lunes 21 de septiembre"
    assert by_name["sin estado"].status is None  # la columna siguiente era la hora
    assert by_name["pegado sin tabulaciones"].phone_e164 == "+56991230007"
    assert invalid == ["nombre\tteléfono\testado\thora\tfecha"]  # la fila de títulos no tiene número
    assert len(rows) == 9


def test_import_contacted_does_not_duplicate_and_keeps_the_latest_status(repo):
    first = import_contacted(repo, PASTED)
    assert (first.added, first.updated) == (8, 1)  # "taller norte" aparece dos veces
    repeated = next(c for c in repo.list_contacted() if c.name == "taller norte")
    assert repeated.status == "demo enviada"
    again = import_contacted(repo, PASTED)
    assert (again.added, again.updated) == (0, 9)
    assert repo.count_contacted() == 8


def test_import_hides_saved_leads_that_were_already_contacted(repo):
    lead, _ = repo.upsert_business(
        Business(google_maps_url="https://www.google.com/maps/place/v", business_name="Vidriería", phone_e164="+56981230004")
    )
    result = import_contacted(repo, PASTED)
    assert [b.business_name for b in result.flagged] == ["Vidriería"]
    hidden = repo.get_business(lead.id)
    assert not is_lead(hidden)
    assert "vidrios del valle" in hidden.duplicate_reason
    assert "respuesta positiva" in hidden.duplicate_reason


# --- En la búsqueda de leads --------------------------------------------------------------------


class CardMaps(MapsSource):
    """Maps simulado: cada resultado trae lo que muestra su tarjeta (web y teléfono) y su ficha."""

    def __init__(self, results: list[tuple[MapsPlace, str | None, str | None]], prefix: str = "p") -> None:
        self.results = {f"https://www.google.com/maps/place/{prefix}{i}": r for i, r in enumerate(results)}
        self.opened: list[str] = []

    async def search(self, query, limit):
        return [
            MapsListing(position=i + 1, name=place.name, url=url, website=website, phone_e164=phone)
            for i, (url, (place, website, phone)) in enumerate(self.results.items())
        ][:limit]

    async def get_details(self, listing):
        self.opened.append(listing.url)
        return self.results[listing.url][0].model_copy(update={"maps_url": listing.url})


AUTO = object()  # web propia inventada a partir del nombre: solo los negocios con web pueden ser leads


def place(name: str, *, phone: str | None = None, website: str | None | object = AUTO) -> MapsPlace:
    if website is AUTO:
        website = "https://" + re.sub(r"[^a-z0-9]", "", fold_accents(name).lower()) + ".cl/"
    return MapsPlace(
        maps_url="https://www.google.com/maps/place/x",
        name=name,
        phone_raw=phone,
        phone_e164=phone,
        website=website,
        website_kind="web_propia" if website else None,
        whatsapp=extract_whatsapp("", source=WhatsAppSource.MAPS),
    )


def run_job(repo, maps, target=10, routes=None):
    progress = LeadsProgress(query="cerrajeros en Rancagua", target=target)

    def handler(request):
        return (routes or {}).get(str(request.url), page())  # toda web abre, salvo que la prueba diga otra cosa

    async def go():
        settings = Settings(_env_file=None, web_max_internal_pages=1)
        async with WebCollector(settings, transport=httpx.MockTransport(handler), use_browser=False) as collector:
            return await run_leads_job(maps, collector, repo, progress, review_webs=False)

    return asyncio.run(go())


def test_contacted_number_on_the_card_is_skipped_without_opening(repo):
    import_contacted(repo, PASTED)
    maps = CardMaps([(place("Taller Norte", phone="+56991230001"), None, "+56991230001"), (place("Nuevo", phone="+56981234567"), None, None)])
    result = run_job(repo, maps)
    assert maps.opened == ["https://www.google.com/maps/place/p1"]  # el contactado ni se abre
    assert (result.leads, result.duplicates) == (1, 1)
    assert any("Repetido, se salta: Taller Norte" in line and "ya contactaste" in line for line in result.log)


def test_contacted_number_in_the_listing_is_skipped_after_opening(repo):
    import_contacted(repo, PASTED)
    # La tarjeta no mostró el teléfono; la ficha sí (con el formato antiguo de México).
    maps = CardMaps([(place("Cerrajería 24 h", phone="+525512340003", website="https://cerrajeria24.mx/"), None, None)])
    result = run_job(repo, maps)
    assert (result.leads, result.duplicates) == (0, 1)
    business = repo.find_business(google_maps_url="https://www.google.com/maps/place/p0")
    assert business.website_status is None  # no se gastó tiempo revisando su web
    assert "cerrajeria centro mx" in business.duplicate_reason
    hidden = duplicates_for_day(repo, date.today())
    assert [d.name for d in hidden] == ["Cerrajería 24 h"]
    assert leads_for_day(repo, date.today()) == []


def test_same_website_is_not_reviewed_twice_in_one_search(repo):
    routes = {"https://cadena.cl/": page()}
    maps = CardMaps(
        [
            (place("Cadena Centro", phone="+56993557317", website="https://cadena.cl/"), None, None),
            (place("Cadena Norte", phone="+56981234567", website="https://www.cadena.cl/norte"), None, None),
        ]
    )
    result = run_job(repo, maps, routes=routes)
    assert (result.leads, result.duplicates) == (1, 1)
    north = repo.find_business(google_maps_url="https://www.google.com/maps/place/p1")
    assert north.duplicate_reason == "Tiene la misma web (cadena.cl) que «Cadena Centro», que ya apareció en tus leads."


def test_known_website_on_the_card_is_skipped_without_opening(repo):
    routes = {"https://cadena.cl/": page()}
    run_job(repo, CardMaps([(place("Cadena Centro", phone="+56993557317", website="https://cadena.cl/"), None, None)]), routes=routes)
    maps = CardMaps([(place("Cadena Sur", phone="+56981234567"), "https://cadena.cl/sur", "+56981234567")], prefix="q")
    result = run_job(repo, maps)
    assert maps.opened == []
    assert result.duplicates == 1


def test_whatsapp_found_on_the_web_matching_a_previous_lead_is_a_duplicate(repo):
    run_job(repo, CardMaps([(place("Cerrajero Juan", phone="+56993557317"), None, None)]))
    # Otra ficha con teléfono fijo y web propia… cuyo WhatsApp es el del lead anterior.
    routes = {"https://otra-ficha.cl/": page('<a href="https://wa.me/56993557317">Escríbenos</a>')}
    maps = CardMaps([(place("Cerrajería JP", phone="+56722234567", website="https://otra-ficha.cl/"), None, None)], prefix="q")
    result = run_job(repo, maps, routes=routes)
    assert (result.leads, result.duplicates) == (0, 1)
    business = repo.find_business(google_maps_url="https://www.google.com/maps/place/q0")
    assert "«Cerrajero Juan»" in business.duplicate_reason
    assert "+56 9 9355 7317" in business.duplicate_reason


def test_number_of_a_discarded_lead_never_comes_back(repo):
    run_job(repo, CardMaps([(place("No existe", phone="+56993557317"), None, None)]))
    first = repo.find_business(google_maps_url="https://www.google.com/maps/place/p0")
    discard_lead(repo, first.id)
    result = run_job(repo, CardMaps([(place("Misma persona, otra ficha", phone="+56993557317"), None, None)], prefix="q"))
    assert (result.leads, result.duplicates) == (0, 1)
    assert any("«No existe» (lo descartaste)" in line for line in result.log)


def test_leads_saved_before_this_change_also_count(repo):
    # Lead guardado antes de que existieran las huellas.
    repo.upsert_business(
        Business(google_maps_url="https://www.google.com/maps/place/viejo", business_name="Viejo", phone_e164="+56993557317",
                 website="https://viejo.cl/", website_status=WebsiteStatus.OK)
    )
    assert repo.fingerprinted_business_ids() == set()
    result = run_job(repo, CardMaps([(place("Nuevo con el mismo número", phone="+56993557317"), None, "+56993557317")]))
    assert result.duplicates == 1
    assert ensure_fingerprints(repo) == 0  # ya quedaron guardadas al empezar la búsqueda


def test_not_a_duplicate_puts_it_back_in_the_list(repo):
    routes = {"https://cadena.cl/": page()}
    maps = CardMaps(
        [
            (place("Cadena Centro", phone="+56993557317", website="https://cadena.cl/"), None, None),
            (place("Cadena Norte", phone="+56981234567", website="https://cadena.cl/"), None, None),
        ]
    )
    run_job(repo, maps, routes=routes)
    north = repo.find_business(google_maps_url="https://www.google.com/maps/place/p1")
    restored = unmark_duplicate(repo, north.id)
    assert restored.duplicate_reason is None
    assert north.id in repo.fingerprinted_business_ids()
    # Se marcó repetido antes de revisar su web: entra a la lista cuando se confirma que su web abre.
    assert restored.website_status is None and not is_lead(restored)
    repo.update_business(north.id, website_status=WebsiteStatus.OK)
    assert {lead.name for lead in leads_for_day(repo, date.today())} == {"Cadena Centro", "Cadena Norte"}


# --- Página -------------------------------------------------------------------------------------


async def web_opens(business_id, repo):
    """Revisión simulada (Re-analizar / No es repetido): la web abre."""
    repo.update_business(business_id, website_status=WebsiteStatus.OK)


@pytest.fixture
def client(repo, tmp_path):
    async def no_runner(progress, repo):
        return progress

    app = create_app(Settings(_env_file=None, data_dir=tmp_path), repo=repo, runner=no_runner, reviewer=web_opens)
    with TestClient(app) as test_client:
        yield test_client


def test_contacted_page_imports_the_pasted_list(client, repo):
    empty = client.get("/contactados")
    assert empty.status_code == 200
    assert "Ya contactados (0)" in empty.text

    response = client.post("/contactados", data={"lista": PASTED})
    assert response.status_code == 200
    assert "8 nuevos" in response.text
    assert "1 líneas no tenían un número válido" in response.text
    assert "nombre\tteléfono" in response.text  # la línea inválida queda en el cuadro para corregirla
    assert "Ya contactados (8)" in response.text
    assert "+52 55 1234 0003" in response.text  # número legible
    assert repo.count_contacted() == 8


def test_hidden_duplicates_are_listed_and_can_be_restored(client, repo):
    routes = {"https://cadena.cl/": page()}
    maps = CardMaps(
        [
            (place("Cadena Centro", phone="+56993557317", website="https://cadena.cl/"), None, None),
            (place("Cadena Norte", phone="+56981234567", website="https://cadena.cl/"), None, None),
        ]
    )
    run_job(repo, maps, routes=routes)
    home = client.get("/")
    assert "1 repetidos ocultos" in home.text
    assert "misma web (cadena.cl)" in home.text
    north = repo.find_business(google_maps_url="https://www.google.com/maps/place/p1")
    response = client.post(f"/negocio/{north.id}/no-repetido")
    assert response.status_code == 204
    # Su web no se había revisado: se revisa en segundo plano y la página se recarga para mostrar el avance.
    assert response.headers["HX-Refresh"] == "true"
    reviews = client.app.state.reviews
    for _ in range(100):
        if not reviews.running:
            break
        time.sleep(0.02)
    refreshed = client.get(f"/leads?dia={date.today().isoformat()}")
    assert "repetidos ocultos" not in refreshed.text
    assert "Cadena Norte" in refreshed.text
