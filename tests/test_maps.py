import asyncio
from pathlib import Path

import pytest
from selectolax.lexbor import LexborHTMLParser

from app.config import get_settings
from app.db.repository import Repository
from app.db.schema import create_db_engine, init_db
from app.extract.html_facts import load_communes
from app.extract.links import LinkKind
from app.models import SearchStatus, WebsiteStatus, WhatsAppSource, WhatsAppStatus
from app.pipeline import run_maps_step
from app.sources.base import MapsBlockedError, MapsListing, MapsPlace, MapsPlaceError, MapsSource
from app.sources.maps_parser import (
    detect_block,
    feed_reached_end,
    parse_about,
    parse_address,
    parse_feed,
    parse_hours,
    parse_place,
)

FIXTURES = Path(__file__).parent / "fixtures" / "maps"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def communes():
    return load_communes(get_settings().communes_file)


@pytest.fixture(scope="module")
def feed():
    return parse_feed(fixture("feed.html"))


# --- Lista de resultados (HTML real) ------------------------------------------------------


def test_feed_lists_all_results_in_order(feed):
    assert len(feed) == 12
    assert feed[0][0] == "Multiservice - Jumbo Rancagua | Cerrajero | Cerrajería a domicilio | Duplicado de llaves"
    assert feed[1][0] == "CGC Fenix Cerrajeria"
    assert all("/maps/place/" in card.url for card in feed)


def test_feed_cards_bring_website_and_phone(feed):
    # Lo que muestra cada tarjeta, sin abrir la ficha (sirve para saltar repetidos).
    by_name = {card.name: card for card in feed}
    multiservice = feed[0]
    assert multiservice.website == "https://cerrajeriamultiservice.cl/"
    assert multiservice.phone_e164 == "+56224859409"  # "(2) 2485 9409"
    assert by_name["CGC Fenix Cerrajeria"].phone_e164 == "+56993557317"
    assert by_name["JT Keys"].website == "http://wa.me/56957734621"
    assert by_name["Cerrajero Sanchez"].phone_e164 is None  # la tarjeta no muestra teléfono
    assert by_name["Cerrajero Sánchez"].website is None
    # Dos fichas distintas con el mismo número: la deduplicación las reconocerá.
    assert by_name["Cerrajerias Rancaguas"].phone_e164 == by_name["CerrajeriasBM"].phone_e164 == "+56957787796"


def test_feed_end_of_list_is_detected():
    assert feed_reached_end(fixture("feed.html")) is True
    assert feed_reached_end('<div role="feed"><a href="https://www.google.com/maps/place/A">A</a></div>') is False


def test_feed_does_not_repeat_the_same_place():
    html = (
        '<div role="feed">'
        '<a aria-label="A" href="https://www.google.com/maps/place/A/data=!4m7!3m6!1s0x1:0x2!19sChIJaaaaaaaaaaaa">A</a>'
        '<a aria-label="A (anuncio)" href="https://www.google.com/maps/place/A/data=!4m7!3m6!1s0x1:0x2!19sChIJaaaaaaaaaaaa?x=1">A</a>'
        '<a aria-label="B" href="https://www.google.com/maps/place/B/data=!4m7!3m6!1s0x3:0x4">B</a>'
        "</div>"
    )
    assert [card.name for card in parse_feed(html)] == ["A", "B"]


# --- Ficha (HTML real) -----------------------------------------------------------------------


def test_place_with_hours_and_website(feed, communes):
    place = parse_place(fixture("place_horario.html"), url=feed[0][1], communes=communes)
    assert place.name == "Multiservice - Jumbo Rancagua | Cerrajero | Cerrajería a domicilio | Duplicado de llaves"
    assert place.category == "Cerrajería"
    assert (place.rating, place.review_count) == (3.7, 6)
    assert place.address == "Avenida Presidente Frei 750, Loc. 1035, 2820785 Rancagua, O'Higgins"
    assert (place.city, place.commune) == ("Rancagua", "Rancagua")
    assert (place.phone_raw, place.phone_e164) == ("(2) 2485 9409", "+56224859409")
    assert place.website == "https://cerrajeriamultiservice.cl/"
    assert place.website_kind == LinkKind.WEB_PROPIA
    assert list(place.opening_hours) == ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
    assert place.opening_hours["lunes"] == "De 8 a. m. a 8:30 p. m."
    assert place.place_key == "place_id:ChIJsxKenW1DY5YR6SOAf3cp158"
    assert "hl=" not in place.maps_url and "g_ep=" not in place.maps_url


def test_place_open_24_hours_without_reviews_leaking(feed, communes):
    place = parse_place(fixture("place_24h.html"), url=feed[1][1], communes=communes)
    assert place.name == "CGC Fenix Cerrajeria"
    assert place.category == "Cerrajero"
    # Más abajo, "Otras personas también buscan" muestra 4,6 y 9 reseñas de otro negocio: no se mezcla.
    assert (place.rating, place.review_count) == (5.0, 1)
    assert place.phone_e164 == "+56993557317"
    assert set(place.opening_hours.values()) == {"Abierto las 24 horas"}
    assert place.links == ["https://cerrajeria-2u6qw7vf.manus.space/", "tel:+56993557317"]


def test_place_without_whatsapp_is_not_confirmed_when_it_has_phone(feed, communes):
    business = parse_place(fixture("place_24h.html"), url=feed[1][1], communes=communes).to_business()
    assert business.whatsapp_status == WhatsAppStatus.NO_CONFIRMADO
    assert business.whatsapp_url is None
    assert business.website_status is None  # web propia: se verifica en la Fase 3
    assert business.field_sources["phone_e164"] == "maps"


def test_non_place_page_is_rejected():
    with pytest.raises(ValueError):
        parse_place("<html><body><p>Nada</p></body></html>", url="https://www.google.com/maps")


# --- Ficha sintética: casos que no salieron en la búsqueda real ------------------------------


def _panel(authority: str | None = None, extra_info: str = "", outside: str = "") -> str:
    website = f'<a data-item-id="authority" href="{authority}">Sitio</a>' if authority else ""
    return (
        '<div role="main"><div><div><h1>Gasfiter Rancagua</h1>'
        '<span role="img" aria-label="4,8 estrellas "></span><span role="img" aria-label="1.234 reseñas">(1.234)</span>'
        '<button jsaction="pane.x.category">Gasfíter</button></div></div>'
        '<div role="region" aria-label="Información de Gasfiter Rancagua">'
        '<button data-item-id="address" aria-label="Dirección: Calle 1 123, 2820000 Rancagua, O\'Higgins "></button>'
        '<button data-item-id="phone:tel:+56987654321" aria-label="Teléfono: 9 8765 4321 "></button>'
        f"{website}{extra_info}</div>"
        f"{outside}</div>"
    )


def test_whatsapp_as_maps_website(communes):
    place = parse_place(_panel("https://wa.me/56987654321"), url="https://www.google.com/maps/place/G", communes=communes)
    assert place.review_count == 1234
    assert place.website is None and place.website_kind == LinkKind.WHATSAPP
    business = place.to_business()
    assert business.whatsapp_status == WhatsAppStatus.VERIFICADO
    assert business.whatsapp_url == "https://wa.me/56987654321"
    assert business.whatsapp_source == WhatsAppSource.MAPS
    assert business.whatsapp_evidence == "https://wa.me/56987654321"
    assert business.website_status == WebsiteStatus.SIN_WEB


def test_instagram_as_maps_website_means_only_socials():
    place = parse_place(_panel("/url?q=https://www.instagram.com/gasfiterrancagua/&sa=U"), url="https://www.google.com/maps/place/G")
    business = place.to_business()
    assert business.website is None
    assert business.instagram == "https://www.instagram.com/gasfiterrancagua"
    assert business.website_status == WebsiteStatus.SOLO_REDES
    assert business.field_sources["website_maps"] == "/url?q=https://www.instagram.com/gasfiterrancagua/&sa=U"


def test_linktree_is_kept_to_open_later():
    business = parse_place(_panel("https://linktr.ee/gasfiter"), url="https://www.google.com/maps/place/G").to_business()
    assert business.website == "https://linktr.ee/gasfiter"
    assert business.website_status == WebsiteStatus.SOLO_REDES


def test_google_redirect_in_website_is_unwrapped():
    place = parse_place(_panel("/url?q=https://www.gasfiter.cl/&opi=1"), url="https://www.google.com/maps/place/G")
    assert place.website == "https://www.gasfiter.cl/"
    assert place.website_kind == LinkKind.WEB_PROPIA


def test_without_website_means_no_website():
    assert parse_place(_panel(), url="https://www.google.com/maps/place/G").to_business().website_status == WebsiteStatus.SIN_WEB


def test_whatsapp_in_reviews_or_other_businesses_is_ignored():
    outside = '<div class="review">Me atendió por <a href="https://wa.me/56911111111">wa.me/56911111111</a></div>'
    place = parse_place(_panel(outside=outside), url="https://www.google.com/maps/place/G")
    assert place.whatsapp.candidates == []
    assert "https://wa.me/56911111111" not in place.links


def test_whatsapp_link_in_business_panel_counts():
    extra = '<a href="https://api.whatsapp.com/send?phone=56987654321">WhatsApp</a>'
    place = parse_place(_panel(extra_info=extra), url="https://www.google.com/maps/place/G")
    assert [(c.number, c.source) for c in place.whatsapp.candidates] == [("56987654321", WhatsAppSource.MAPS)]


# --- Dirección, horario, pestaña Información y bloqueos -----------------------------------------


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("Venus 2797, 2830680 Rancagua, O'Higgins", ("Rancagua", "Rancagua")),
        ("Avenida Presidente Frei 750, Loc. 1035, 2820785 Rancagua, O'Higgins", ("Rancagua", "Rancagua")),
        ("Av. Providencia 1234, 7500000 Providencia, Región Metropolitana", ("Providencia", "Providencia")),
        ("Pje. San Abraham 01263, Machali", ("Machali", "Machalí")),
        ("Av. Valparaíso 123, Viña del Mar, Valparaíso", ("Viña del Mar", None)),  # fuera del catálogo
        ("Pje. San Abraham 01263", (None, None)),
        ("1234 SW 8th St, Miami, FL 33135, Estados Unidos", ("Miami", None)),
        ("Calle de Alcalá 50, 28014 Madrid, España", ("Madrid", None)),
        ("Av. Corrientes 1234, C1043AAZ CABA, Argentina", ("CABA", None)),
        ("", (None, None)),
        (None, (None, None)),
    ],
)
def test_parse_address(address, expected, communes):
    assert parse_address(address, communes) == expected


def test_parse_hours_with_split_schedule_and_closed_days():
    html = (
        '<div><button aria-label="lunes, De 9 a. m. a 1 p. m., De 3 a 7 p. m., Copiar el horario"></button>'
        '<button aria-label="domingo, Cerrado, Copiar el horario"></button>'
        '<button aria-label="sábado, De 10 a. m. a 2 p. m."></button></div>'
    )
    hours = parse_hours(LexborHTMLParser(html).body)
    assert hours == {
        "lunes": "De 9 a. m. a 1 p. m., De 3 a 7 p. m.",
        "sábado": "De 10 a. m. a 2 p. m.",
        "domingo": "Cerrado",
    }


def test_parse_about_sections():
    about = (
        '<div role="main"><h1>Gasfiter</h1>'
        "<div><h2>Del propietario</h2><p>Gasfitería certificada SEC con 15 años en Rancagua.</p></div>"
        '<div><h2>Opciones de servicio</h2><ul><li aria-label="Ofrece servicio a domicilio">A domicilio</li>'
        "<li>Servicio en el sitio</li></ul></div>"
        "<div><h2>Reseñas</h2><ul><li>no es del negocio</li></ul></div></div>"
    )
    sections = parse_about(about)
    assert sections == {
        "Del propietario": ["Gasfitería certificada SEC con 15 años en Rancagua."],
        "Opciones de servicio": ["Ofrece servicio a domicilio", "Servicio en el sitio"],
    }
    place = parse_place(_panel(), url="https://www.google.com/maps/place/G", about_html=about)
    assert place.description == "Gasfitería certificada SEC con 15 años en Rancagua."
    assert place.services == ["Ofrece servicio a domicilio", "Servicio en el sitio"]


@pytest.mark.parametrize(
    ("url", "text", "blocked"),
    [
        ("https://www.google.com/sorry/index?continue=https://www.google.com/maps", "", True),
        ("https://www.google.com/maps/search/x", "Nuestros sistemas han detectado tráfico inusual en tu red.", True),
        ("https://www.google.com/maps/search/x", "Our systems have detected unusual traffic from your computer", True),
        ("https://www.google.com/maps/search/x", "No soy un robot", True),
        ("https://www.google.com/maps/search/x", "Resultados\nCerrajería XYZ", False),
        ("https://www.google.com/maps/search/x", "", False),
    ],
)
def test_detect_block(url, text, blocked):
    assert (detect_block(url, text) is not None) is blocked


# --- Paso de Maps del pipeline (con una fuente simulada, sin navegador) ---------------------------


class FakeMapsSource(MapsSource):
    def __init__(self, places: dict[str, MapsPlace | Exception], listings: list[MapsListing]) -> None:
        self.places = places
        self.listings = listings

    async def search(self, query: str, limit: int) -> list[MapsListing]:
        return self.listings[:limit]

    async def get_details(self, listing: MapsListing) -> MapsPlace:
        outcome = self.places[listing.url]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _place(url: str, name: str) -> MapsPlace:
    return parse_place(_panel().replace("Gasfiter Rancagua", name), url=url)


URL_A = "https://www.google.com/maps/place/A/data=!4m7!3m6!1s0x1:0x2!19sChIJaaaaaaaaaaaa?hl=es"
URL_A_OTHER_VIEW = "https://www.google.com/maps/place/A/@-34.1,-70.7,15z/data=!4m7!3m6!1s0x1:0x2!19sChIJaaaaaaaaaaaa?entry=ttu"
URL_B = "https://www.google.com/maps/place/B/data=!4m7!3m6!1s0x3:0x4!19sChIJbbbbbbbbbbbb"
URL_C = "https://www.google.com/maps/place/C/data=!4m7!3m6!1s0x5:0x6!19sChIJcccccccccccc"


@pytest.fixture
def repo() -> Repository:
    engine = create_db_engine(":memory:")
    init_db(engine)
    return Repository(engine)


def test_maps_step_saves_and_deduplicates_across_searches(repo):
    first = FakeMapsSource(
        {URL_A: _place(URL_A, "Negocio A"), URL_B: _place(URL_B, "Negocio B")},
        [MapsListing(position=1, name="A", url=URL_A), MapsListing(position=2, name="B", url=URL_B)],
    )
    result = asyncio.run(run_maps_step(first, repo, "gasfíters en Rancagua", 10))
    assert result.search.status == SearchStatus.TERMINADA
    assert [b["nuevo"] for b in result.businesses] == [True, True]

    # Otra búsqueda encuentra el mismo negocio A con otra URL de vista: no se duplica.
    second = FakeMapsSource(
        {URL_A_OTHER_VIEW: _place(URL_A_OTHER_VIEW, "Negocio A")},
        [MapsListing(position=1, name="A", url=URL_A_OTHER_VIEW)],
    )
    again = asyncio.run(run_maps_step(second, repo, "gasfíter Rancagua", 10))
    assert [b["nuevo"] for b in again.businesses] == [False]
    assert len(repo.businesses_for_search(result.search.id)) == 2
    assert repo.businesses_for_search(again.search.id)[0][1].id == repo.businesses_for_search(result.search.id)[0][1].id


def test_maps_step_continues_after_a_failing_place(repo):
    source = FakeMapsSource(
        {URL_A: MapsPlaceError("La ficha no cargó: A"), URL_B: _place(URL_B, "Negocio B")},
        [MapsListing(position=1, name="A", url=URL_A), MapsListing(position=2, name="B", url=URL_B)],
    )
    result = asyncio.run(run_maps_step(source, repo, "x", 10))
    assert result.search.status == SearchStatus.TERMINADA
    assert result.businesses[0]["error"] == "La ficha no cargó: A"
    assert result.businesses[1]["nombre"] == "Negocio B"
    assert result.search.businesses_found == 1


def test_maps_step_stops_and_marks_blocked(repo):
    source = FakeMapsSource(
        {
            URL_A: _place(URL_A, "Negocio A"),
            URL_B: MapsBlockedError("Google mostró su página de verificación (captcha)."),
            URL_C: _place(URL_C, "Negocio C"),
        },
        [
            MapsListing(position=1, name="A", url=URL_A),
            MapsListing(position=2, name="B", url=URL_B),
            MapsListing(position=3, name="C", url=URL_C),
        ],
    )
    result = asyncio.run(run_maps_step(source, repo, "x", 10))
    stored = repo.get_search(result.search.id)
    assert stored.status == SearchStatus.BLOQUEADO
    assert "captcha" in stored.error
    assert [b["nombre"] for b in result.businesses] == ["Negocio A"]  # lo de antes del bloqueo queda guardado
    assert stored.businesses_found == 1


def test_updating_from_maps_does_not_erase_existing_data(repo):
    with_phone = _place(URL_A, "Negocio A")
    repo.upsert_business(with_phone.to_business())
    without_phone = with_phone.model_copy(update={"phone_raw": None, "phone_e164": None, "rating": 4.9})
    updated, created = repo.upsert_business(without_phone.to_business())
    assert created is False
    assert updated.phone_e164 == "+56987654321"
    assert updated.rating == 4.9
