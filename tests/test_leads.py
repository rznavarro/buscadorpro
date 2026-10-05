"""Leads diarios: búsqueda de 20 nuevos, lista del día, Existe / No existe y CSV (sin internet)."""

import asyncio
import re
from datetime import date

import httpx
import pytest

from app.analyze.collector import WebCollector
from app.config import Settings
from app.db.repository import Repository
from app.db.schema import create_db_engine, init_db
from app.extract.phones import is_probable_mobile, region_for_phone
from app.extract.whatsapp import extract_whatsapp
from app.leads import SORTS, confirm_whatsapp, discard_lead, leads_csv, leads_for_day, restore_lead, sort_leads
from app.models import SearchStatus, WebsiteStatus, WhatsAppCheck, WhatsAppConfidence, WhatsAppSource, WhatsAppStatus
from app.pipeline import LeadsProgress, is_lead, run_leads_job
from app.sources.base import MapsBlockedError, MapsListing, MapsPlace, MapsPlaceError, MapsSource

FILLER = "<p>" + "Servicio a domicilio, atención rápida y garantizada para toda la ciudad. " * 6 + "</p>"


def page(body: str) -> str:
    return f"<html><head><title>Negocio</title></head><body>{FILLER}{body}</body></html>"


# --- Celulares y país --------------------------------------------------------------


@pytest.mark.parametrize(
    ("number", "mobile"),
    [
        ("+56993557317", True),  # celular Chile
        ("+56722234567", False),  # fijo Rancagua
        ("+56224859409", False),  # fijo Santiago
        ("+5491123456789", True),  # celular Argentina
        ("+541141234567", False),  # fijo Argentina
        ("+13055551234", True),  # EE. UU.: no distingue, se considera probable
        ("+34612345678", True),  # celular España
        ("+34912345678", False),  # fijo España
        (None, False),
    ],
)
def test_probable_mobile(number, mobile):
    assert is_probable_mobile(number) is mobile


def test_region_for_phone():
    assert region_for_phone("+13055551234") == "US"
    assert region_for_phone("+56993557317") == "CL"
    assert region_for_phone(None, "AR") == "AR"


# --- Búsqueda de leads (fuente de Maps simulada) ---------------------------------------------


class FakeMaps(MapsSource):
    def __init__(self, places: list[MapsPlace | Exception]) -> None:
        self.places = {f"https://www.google.com/maps/place/n{i}": p for i, p in enumerate(places)}
        self.opened: list[str] = []

    async def search(self, query, limit):
        return [MapsListing(position=i + 1, name=f"n{i}", url=url) for i, url in enumerate(self.places)][:limit]

    async def get_details(self, listing):
        self.opened.append(listing.url)
        outcome = self.places[listing.url]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome.model_copy(update={"maps_url": listing.url})


AUTO = object()  # web propia inventada a partir del nombre (todas abren, salvo que la ruta diga otra cosa)


def place(name: str, *, phone: str | None = None, website: str | None | object = AUTO, maps_wa: str | None = None) -> MapsPlace:
    if website is AUTO:
        website = "https://" + re.sub(r"[^a-z0-9]", "", name.lower()) + ".cl/"
    return MapsPlace(
        maps_url="https://www.google.com/maps/place/x",
        name=name,
        phone_raw=phone,
        phone_e164=phone,
        website=website,
        website_kind="web_propia" if website else None,
        whatsapp=extract_whatsapp(f'<a href="https://wa.me/{maps_wa}">WA</a>' if maps_wa else "", source=WhatsAppSource.MAPS),
    )


@pytest.fixture
def repo() -> Repository:
    engine = create_db_engine(":memory:")
    init_db(engine)
    return Repository(engine)


def run_job(repo, maps, target, routes=None, progress=None):
    progress = progress or LeadsProgress(query="peluquerías en Miami", target=target)

    def handler(request):
        # Toda web abre, salvo las que la prueba define distinto en `routes`.
        default = httpx.Response(200, text=page(""), headers={"content-type": "text/html"})
        return (routes or {}).get(str(request.url), default)

    async def go():
        settings = Settings(_env_file=None, web_max_internal_pages=1)
        async with WebCollector(settings, transport=httpx.MockTransport(handler), use_browser=False) as collector:
            return await run_leads_job(maps, collector, repo, progress)

    return asyncio.run(go())


def test_collects_new_leads_until_target(repo):
    maps = FakeMaps(
        [
            place("Celular", phone="+56993557317"),
            place("Fijo", phone="+56722234567"),  # no es lead: sin WhatsApp ni celular
            place("Sin web", phone="+56954321098", website=None),  # no es lead: solo interesan negocios con web
            place("WhatsApp en Maps", phone="+56722234567", maps_wa="56987654321"),
            place("Otro celular", phone="+56981234567"),
        ]
    )
    result = run_job(repo, maps, target=2)
    assert result.status == SearchStatus.TERMINADA
    assert (result.leads, result.reviewed, result.no_website) == (2, 4, 1)
    assert len(maps.opened) == 4  # al llegar a la meta no se abren más fichas
    assert result.message.startswith("Listo: 2 leads nuevos.")
    assert "1 sin web o con la web caída (saltados)" in result.message
    assert repo.get_search(result.search_id).leads_found == 2


def test_only_businesses_with_a_working_website_are_leads(repo):
    routes = {"https://caida.cl/": httpx.Response(500, text="error", headers={"content-type": "text/html"})}
    maps = FakeMaps(
        [
            place("Sin web", phone="+56993557317", website=None),
            place("Caida", phone="+56981234567", website="https://caida.cl/"),
            place("Con web", phone="+56954321098"),
        ]
    )
    result = run_job(repo, maps, 10, routes)
    assert (result.leads, result.no_website) == (1, 2)
    assert any("Sin web propia, se salta: Sin web" in line for line in result.log)
    assert any("Su web no funciona, se salta: Caida (La web responde con error 500" in line for line in result.log)
    assert [lead.name for lead in leads_for_day(repo, date.today())] == ["Con web"]
    sin_web = repo.find_business(google_maps_url="https://www.google.com/maps/place/n0")
    assert sin_web.website_status == WebsiteStatus.SIN_WEB  # guardado: no se vuelve a abrir otro día
    assert is_lead(sin_web) is False


class CardMaps(FakeMaps):
    """Como FakeMaps, pero cada resultado trae lo que muestra su tarjeta en la lista de Maps."""

    def __init__(self, places, cards):
        super().__init__(places)
        self.cards = cards

    async def search(self, query, limit):
        listings = await super().search(query, limit)
        return [listing.model_copy(update={"card_seen": True, "website": card}) for listing, card in zip(listings, self.cards)]


def test_cards_without_an_own_website_are_skipped_without_opening(repo):
    maps = CardMaps(
        [place("A", phone="+56993557317"), place("B", phone="+56981234567"), place("C", phone="+56954321098"),
         place("D", phone="+56991234567", website="https://linktr.ee/d")],
        [None, "https://www.facebook.com/negocio", "https://c.cl/", "https://linktr.ee/d"],
    )
    result = run_job(repo, maps, 10)
    # A (sin botón "Sitio web") y B (su "sitio" es Facebook) no se abren; un Linktree sí.
    assert maps.opened == ["https://www.google.com/maps/place/n2", "https://www.google.com/maps/place/n3"]
    assert result.no_website == 3  # A, B y D (su Linktree no lleva a una web propia)
    assert result.leads == 1


def test_already_reviewed_businesses_are_skipped_without_opening(repo):
    run_job(repo, FakeMaps([place("A", phone="+56993557317"), place("B", phone="+56981234567")]), target=2)
    again = FakeMaps([place("A", phone="+56993557317"), place("B", phone="+56981234567"), place("C", phone="+56954321098")])
    result = run_job(repo, again, target=5)
    assert result.skipped == 2
    assert again.opened == ["https://www.google.com/maps/place/n2"]
    assert result.leads == 1
    assert "no tenía más negocios nuevos: 1 de 5" in result.message


def test_foreign_business_whatsapp_uses_its_country(repo):
    routes = {
        "https://salon.com/": httpx.Response(
            200, text=page('<a href="https://wa.me/3055551234">WA</a>'), headers={"content-type": "text/html"}
        )
    }
    run_job(repo, FakeMaps([place("Salon Miami", phone="+13055551234", website="https://salon.com/")]), 1, routes)
    business = repo.find_business(google_maps_url="https://www.google.com/maps/place/n0")
    assert business.whatsapp_url == "https://wa.me/13055551234"  # +1, no +56
    assert business.whatsapp_confidence == WhatsAppConfidence.ALTA  # coincide con su teléfono


def test_stop_button_stops_the_search(repo):
    progress = LeadsProgress(query="x", target=10, stop_requested=True)
    result = run_job(repo, FakeMaps([place("A", phone="+56993557317")]), 10, progress=progress)
    assert result.status == SearchStatus.DETENIDA
    assert repo.get_search(result.search_id).status == SearchStatus.DETENIDA


def test_block_keeps_leads_found_before(repo):
    maps = FakeMaps([place("A", phone="+56993557317"), MapsBlockedError("captcha"), place("C", phone="+56981234567")])
    result = run_job(repo, maps, 10)
    assert result.status == SearchStatus.BLOQUEADO
    assert result.leads == 1
    assert repo.get_search(result.search_id).status == SearchStatus.BLOQUEADO


def test_closed_browser_window_is_explained_in_plain_words(repo):
    class ClosedWindow(FakeMaps):
        async def get_details(self, listing):
            raise RuntimeError("Page.goto: Target page, context or browser has been closed")

    result = run_job(repo, ClosedWindow([place("A", phone="+56993557317")]), 5)
    assert result.status == SearchStatus.FALLIDA
    assert result.error.startswith("Se cerró la ventana del navegador durante la búsqueda")


def test_a_failing_place_does_not_stop_the_search(repo):
    maps = FakeMaps([MapsPlaceError("La ficha no cargó"), place("B", phone="+56993557317")])
    result = run_job(repo, maps, 5)
    assert result.status == SearchStatus.TERMINADA
    assert result.leads == 1


# --- Lista del día y acciones ----------------------------------------------------------------


@pytest.fixture
def day_with_leads(repo):
    maps = FakeMaps(
        [
            place("Celular", phone="+56993557317"),
            place("Verificado", phone="+56722234567", maps_wa="56987654321"),
            place("Confirmado alta", phone="+56981234567", maps_wa="56981234567"),
            place("Fijo", phone="+56722234568"),
            place("Sin web", phone="+56954321098", website=None),
        ]
    )
    run_job(repo, maps, 10)
    return repo


def test_day_list_shows_only_leads_sorted_by_whatsapp(day_with_leads):
    leads = leads_for_day(day_with_leads, date.today())
    assert [lead.name for lead in leads] == ["Confirmado alta", "Verificado", "Celular"]
    assert [lead.whatsapp_state for lead in leads] == ["verificado", "verificado", "por_probar"]
    by_name = {lead.name: lead for lead in leads}
    assert by_name["Celular"].whatsapp_url == "https://wa.me/56993557317"  # enlace solo para probar
    assert by_name["Celular"].whatsapp_number == "+56 9 9355 7317"
    assert leads_for_day(day_with_leads, date(2020, 1, 1)) == []


def test_probing_a_mobile_does_not_mark_it_verified(day_with_leads):
    business = day_with_leads.find_business(google_maps_url="https://www.google.com/maps/place/n0")
    assert business.whatsapp_status == WhatsAppStatus.NO_CONFIRMADO
    assert business.whatsapp_url is None


def test_confirm_exists_marks_whatsapp_verified_by_joaquin(day_with_leads):
    business = day_with_leads.find_business(google_maps_url="https://www.google.com/maps/place/n0")
    confirmed = confirm_whatsapp(day_with_leads, business.id)
    assert confirmed.whatsapp_status == WhatsAppStatus.VERIFICADO
    assert confirmed.whatsapp_url == "https://wa.me/56993557317"
    assert confirmed.whatsapp_source == WhatsAppSource.MANUAL
    assert confirmed.whatsapp_check == WhatsAppCheck.EXISTE
    assert confirmed.whatsapp_confidence == WhatsAppConfidence.ALTA
    first = leads_for_day(day_with_leads, date.today())[0]
    assert (first.name, first.whatsapp_state) == ("Celular", "confirmado")


def test_not_exists_discards_and_undo_restores(day_with_leads):
    business = day_with_leads.find_business(google_maps_url="https://www.google.com/maps/place/n0")
    discarded = discard_lead(day_with_leads, business.id)
    assert discarded.discarded_at is not None
    assert discarded.whatsapp_check == WhatsAppCheck.NO_EXISTE
    assert is_lead(discarded) is False
    assert "Celular" not in [lead.name for lead in leads_for_day(day_with_leads, date.today())]

    restored = restore_lead(day_with_leads, business.id)
    assert restored.discarded_at is None and restored.whatsapp_check is None
    assert "Celular" in [lead.name for lead in leads_for_day(day_with_leads, date.today())]


def test_discarded_lead_never_comes_back(day_with_leads):
    business = day_with_leads.find_business(google_maps_url="https://www.google.com/maps/place/n0")
    discard_lead(day_with_leads, business.id)
    again = FakeMaps([place("Celular", phone="+56993557317")])
    result = run_job(day_with_leads, again, 5)
    assert again.opened == []
    assert result.skipped == 1


def test_undo_after_confirm_returns_to_probe(day_with_leads):
    business = day_with_leads.find_business(google_maps_url="https://www.google.com/maps/place/n0")
    confirm_whatsapp(day_with_leads, business.id)
    restored = restore_lead(day_with_leads, business.id)
    assert restored.whatsapp_status == WhatsAppStatus.NO_CONFIRMADO
    assert restored.whatsapp_url is None


def test_sort_by_name_and_unknown_order_falls_back_to_whatsapp(day_with_leads):
    leads = leads_for_day(day_with_leads, date.today())
    assert [lead.name for lead in sort_leads(leads, "nombre")] == ["Celular", "Confirmado alta", "Verificado"]
    assert "web" not in SORTS  # ya no hay leads sin web que ordenar primero
    assert [lead.name for lead in sort_leads(leads, "web")] == [lead.name for lead in sort_leads(leads, "whatsapp")]


def test_csv_export_opens_in_spanish_excel(day_with_leads):
    content = leads_csv(leads_for_day(day_with_leads, date.today()))
    assert content.startswith("﻿")  # BOM: Excel respeta las tildes
    header, first, *_ = content.lstrip("﻿").splitlines()
    assert header.startswith("Negocio;Rubro;Ciudad;WhatsApp;Número;Estado WhatsApp;Confianza")
    assert first.startswith("Confirmado alta;")
    assert "Verificado (publicado por el negocio)" in first


def test_day_counter_counts_only_current_leads(day_with_leads):
    from app.leads import days_with_searches

    (day, total, queries), *_ = days_with_searches(day_with_leads)
    assert total == 3  # Celular, Verificado y Confirmado alta: "Fijo" y "Sin web" no son leads
    business = day_with_leads.find_business(google_maps_url="https://www.google.com/maps/place/n0")
    discard_lead(day_with_leads, business.id)
    assert days_with_searches(day_with_leads)[0][1] == 2  # el descartado ya no cuenta
