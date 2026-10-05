import asyncio
from pathlib import Path

import httpx
import pytest

from app.extract.whatsapp import (
    choose_whatsapp,
    extract_whatsapp,
    normalize_whatsapp_number,
    resolve_shortlinks,
)
from app.models import (
    WhatsAppCandidate,
    WhatsAppConfidence,
    WhatsAppPlacement,
    WhatsAppSource,
    WhatsAppStatus,
)

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def numbers(html: str, region: str = "CL") -> list[str | None]:
    return [c.number for c in extract_whatsapp(html, region=region).candidates]


# --- Enlaces que SÍ son un WhatsApp del negocio ------------------------------------------

POSITIVE_CASES = [
    ("wa.me", '<a href="https://wa.me/56987654321">WA</a>', "CL", "56987654321"),
    ("wa.me con +", '<a href="https://wa.me/+56987654321">WA</a>', "CL", "56987654321"),
    ("wa.me con texto", '<a href="https://wa.me/56987654321?text=Hola%20quiero%20cotizar">WA</a>', "CL", "56987654321"),
    ("wa.me sin https", '<a href="wa.me/56987654321">WA</a>', "CL", "56987654321"),
    ("wa.me catálogo /c/", '<a href="https://wa.me/c/56987654321">Catálogo</a>', "CL", "56987654321"),
    ("wa.me con %2B", '<a href="https://wa.me/%2B56987654321">WA</a>', "CL", "56987654321"),
    ("api.whatsapp.com", '<a href="https://api.whatsapp.com/send?phone=56987654321">WA</a>', "CL", "56987654321"),
    (
        "api.whatsapp.com con /send/ y parámetros",
        '<a href="https://api.whatsapp.com/send/?phone=56987654321&amp;text&amp;type=phone_number&amp;app_absent=0">WA</a>',
        "CL",
        "56987654321",
    ),
    (
        "api.whatsapp.com con phone después de text",
        '<a href="https://api.whatsapp.com/send?text=Hola&amp;phone=56987654321">WA</a>',
        "CL",
        "56987654321",
    ),
    ("web.whatsapp.com", '<a href="https://web.whatsapp.com/send?phone=56987654321">WA</a>', "CL", "56987654321"),
    ("whatsapp://", '<a href="whatsapp://send?phone=56987654321">WA</a>', "CL", "56987654321"),
    (
        "JSON con barras escapadas",
        '<script>var cfg = {"url":"https:\\/\\/wa.me\\/56987654321"};</script>',
        "CL",
        "56987654321",
    ),
    (
        "JSON con \\u002F (Next.js)",
        '<script type="application/json">{"url":"https:\\u002F\\u002Fwa.me\\u002F56987654321"}</script>',
        "CL",
        "56987654321",
    ),
    (
        "onclick",
        "<button onclick=\"window.open('https://wa.me/56987654321','_blank')\">WA</button>",
        "CL",
        "56987654321",
    ),
    ("atributo data-href", '<div data-href="https://wa.me/56987654321"></div>', "CL", "56987654321"),
    ("enlace escrito como texto", "<p>Escríbenos a wa.me/56987654321 cuando quieras.</p>", "CL", "56987654321"),
    ("redirección /url?q= de Google", '<a href="/url?q=https://wa.me/56987654321&amp;sa=U">WA</a>', "CL", "56987654321"),
    (
        "redirección de Google codificada",
        '<a href="https://www.google.com/url?q=https%3A%2F%2Fwa.me%2F56987654321&amp;sa=D">WA</a>',
        "CL",
        "56987654321",
    ),
    (
        "plugin Joinchat (JSON en data-settings)",
        '<div class="joinchat" data-settings=\'{"telephone":"56987654321","mobile_only":false}\'></div>',
        "CL",
        "56987654321",
    ),
    (
        "plugin Click to Chat (data-number)",
        '<div class="ht-ctc ht-ctc-chat" data-number="56987654321"></div>',
        "CL",
        "56987654321",
    ),
    (
        "plugin GetButton (opciones en script)",
        '<script>var options = { whatsapp: "+56 9 8765 4321", call_to_action: "Escríbenos" };</script>',
        "CL",
        "56987654321",
    ),
    (
        "plugin con whatsapp_number en JSON",
        '<script>var widgetConfig = {"whatsapp_number":"+56 9 8765 4321","position":"right"};</script>',
        "CL",
        "56987654321",
    ),
    ("Argentina móvil", '<a href="https://wa.me/5491123456789">WA</a>', "AR", "5491123456789"),
    ("Argentina Córdoba", '<a href="https://api.whatsapp.com/send?phone=5493512345678">WA</a>', "AR", "5493512345678"),
]


@pytest.mark.parametrize(("html", "region", "expected"), [c[1:] for c in POSITIVE_CASES], ids=[c[0] for c in POSITIVE_CASES])
def test_detects_explicit_whatsapp_links(html, region, expected):
    assert numbers(html, region) == [expected]


# --- Lo que NO es un WhatsApp del negocio ----------------------------------------------

NEGATIVE_CASES = [
    ("invitación a grupo", '<a href="https://chat.whatsapp.com/AbCdEf123">Grupo</a>'),
    ("botón comentado", '<!-- <a href="https://wa.me/56987654321">viejo</a> -->'),
    (
        "telephone de datos estructurados",
        '<script type="application/ld+json">{"@type":"Plumber","telephone":"+56987654321"}</script>',
    ),
    ("número inválido", '<a href="https://wa.me/123">WA</a>'),
    ("dominio parecido", '<a href="https://kiwa.me/56987654321">otro</a>'),
    ("solo enlace tel:", '<a href="tel:+56987654321">Llamar</a>'),
    ("teléfono junto a la palabra WhatsApp", "<p>WhatsApp: +56 9 8765 4321</p>"),
    ("data-number sin contexto de WhatsApp", '<div class="contador" data-number="56987654321"></div>'),
    (
        "teléfono en script lejos de la palabra whatsapp",
        '<script>var a = "whatsapp";' + " " * 400 + 'var tracking = {"phone":"+56722234567"};</script>',
    ),
]


@pytest.mark.parametrize("html", [c[1] for c in NEGATIVE_CASES], ids=[c[0] for c in NEGATIVE_CASES])
def test_never_invents_a_whatsapp(html):
    assert numbers(html) == []


def test_group_invites_are_recorded_as_ignored():
    result = extract_whatsapp('<a href="https://chat.whatsapp.com/AbCdEf123">Grupo</a>')
    assert [i.evidence for i in result.ignored] == ["https://chat.whatsapp.com/AbCdEf123"]


# --- Normalización ---------------------------------------------------------------------


def test_argentina_mobile_with_15_is_converted_to_549_and_flagged():
    candidate = extract_whatsapp('<a href="https://wa.me/54111523456789">WA</a>', region="AR").candidates[0]
    assert candidate.number == "5491123456789"
    assert candidate.url == "https://wa.me/5491123456789"
    assert "54111523456789" in candidate.note


def test_argentina_landline_keeps_its_format():
    # WhatsApp Business puede usar un fijo: sin el 9 no se agrega nada.
    assert normalize_whatsapp_number("541141234567", "AR") == ("541141234567", None, False)


def test_number_without_country_code_is_completed_with_region_and_flagged_as_broken():
    candidate = extract_whatsapp('<a href="https://wa.me/987654321">WA</a>').candidates[0]
    assert candidate.number == "56987654321"
    assert "no trae código de país" in candidate.note
    assert candidate.broken is True


def test_argentina_15_is_normalized_but_not_marked_broken():
    candidate = extract_whatsapp('<a href="https://wa.me/54111523456789">WA</a>', region="AR").candidates[0]
    assert candidate.broken is False


def test_broken_buttons_are_collected_with_their_evidence():
    html = '<a href="https://wa.me/987654321">A</a><a href="https://wa.me/123">B</a><a href="https://chat.whatsapp.com/X1">G</a>'
    result = extract_whatsapp(html)
    assert result.broken_evidence() == ["https://wa.me/987654321", "https://wa.me/123"]


def test_evidence_is_the_exact_href():
    href = "https://api.whatsapp.com/send?phone=56987654321&text=Hola"
    candidate = extract_whatsapp(f'<a href="{href.replace("&", "&amp;")}">WA</a>').candidates[0]
    assert candidate.evidence == href
    assert candidate.url == "https://wa.me/56987654321"


def test_message_links_count_but_have_no_number():
    candidate = extract_whatsapp('<a href="https://wa.me/message/ABCDEFG1234">WA</a>').candidates[0]
    assert candidate.number is None
    assert candidate.url == "https://wa.me/message/ABCDEFG1234"


# --- Ubicación en la página ------------------------------------------------------------


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ('<header><a href="https://wa.me/56987654321">WA</a></header>', WhatsAppPlacement.HEADER),
        ('<div class="site-header"><a href="https://wa.me/56987654321">WA</a></div>', WhatsAppPlacement.HEADER),
        ('<footer><a href="https://wa.me/56987654321">WA</a></footer>', WhatsAppPlacement.FOOTER),
        ('<main><p><a href="https://wa.me/56987654321">WA</a></p></main>', WhatsAppPlacement.CUERPO),
        ('<div class="whatsapp-float"><a href="https://wa.me/56987654321">WA</a></div>', WhatsAppPlacement.FLOTANTE),
        ('<a style="position: fixed; bottom: 0" href="https://wa.me/56987654321">WA</a>', WhatsAppPlacement.FLOTANTE),
        # Un botón flotante dentro del footer sigue siendo flotante.
        ('<footer><div class="btn-float"><a href="https://wa.me/56987654321">WA</a></div></footer>', WhatsAppPlacement.FLOTANTE),
        # Un menú dentro del footer es footer, no header.
        ('<footer><div><nav><a href="https://wa.me/56987654321">WA</a></nav></div></footer>', WhatsAppPlacement.FOOTER),
        ('<nav><a href="https://wa.me/56987654321">WA</a></nav>', WhatsAppPlacement.HEADER),
    ],
)
def test_placement(html, expected):
    assert extract_whatsapp(html).candidates[0].placement == expected


# --- Fixtures ----------------------------------------------------------------------------


def test_fixture_cerrajeria_rancagua():
    result = extract_whatsapp(fixture("web_cerrajeria_rancagua.html"))
    found = {c.number: c.placement for c in result.candidates}
    assert found == {"56987654321": WhatsAppPlacement.HEADER, "56976543210": WhatsAppPlacement.FOOTER}
    assert result.floating_button  # Joinchat con el mismo número del header
    assert "56911111111" not in found  # botón comentado
    assert "56722234567" not in found  # teléfono fijo de los datos estructurados
    assert [i.evidence for i in result.ignored] == ["https://chat.whatsapp.com/AbCdEfGh123"]

    decision = choose_whatsapp(result.candidates, phone_e164="+56722234567")
    assert decision.status == WhatsAppStatus.VERIFICADO
    assert decision.url == "https://wa.me/56987654321"
    assert decision.multiple_numbers is True


def test_fixture_gasfiter_argentina():
    result = extract_whatsapp(fixture("web_gasfiter_ar.html"), region="AR")
    found = {c.number: c.placement for c in result.candidates}
    assert found == {"5493512345678": WhatsAppPlacement.FLOTANTE, "5491123456789": WhatsAppPlacement.HEADER}


def test_fixture_plugins():
    result = extract_whatsapp(fixture("web_plugins.html"))
    assert set(numbers(fixture("web_plugins.html"))) == {
        "56965432109",  # Click to Chat flotante
        "56954321098",  # JSON de Next.js con \/
        "56943210987",  # /
        "56932109876",  # onclick
        "56987654321",  # wa.me sin código de país
        "56921098765",  # redirección de Google
        None,  # wa.me/message
    }
    assert result.candidates[0].number == "56965432109"
    assert [s.url for s in result.shortlinks] == ["https://wa.link/abc123", "https://wa.link/zzz999"]
    assert [i.evidence for i in result.ignored] == ["https://wa.me/123"]


def test_fixture_without_whatsapp_is_not_confirmed():
    result = extract_whatsapp(fixture("web_sin_whatsapp.html"))
    assert result.candidates == []
    decision = choose_whatsapp(result.candidates, phone_e164="+56987654321")
    assert decision.status == WhatsAppStatus.NO_CONFIRMADO
    assert decision.url is None


def test_fixture_linktree():
    result = extract_whatsapp(fixture("linktree.html"), source=WhatsAppSource.LINKTREE)
    assert [(c.number, c.source) for c in result.candidates] == [("56987654321", WhatsAppSource.LINKTREE)]


def test_fixture_maps_panel():
    result = extract_whatsapp(fixture("maps_panel.html"), source=WhatsAppSource.MAPS)
    assert [(c.number, c.source) for c in result.candidates] == [("56987654321", WhatsAppSource.MAPS)]


# --- Acortadores -------------------------------------------------------------------------


def _shortener_transport(requested: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        routes = {
            "/abc123": httpx.Response(301, headers={"location": "https://api.whatsapp.com/send?phone=56998765432"}),
            "/zzz999": httpx.Response(302, headers={"location": "https://wa.link/"}),
            "/": httpx.Response(200, text="<html>Crea tu enlace de WhatsApp</html>"),
            "/meta": httpx.Response(
                200, text='<meta http-equiv="refresh" content="0; url=https://wa.me/56981234567">'
            ),
        }
        if request.url.path in routes:
            return routes[request.url.path]
        raise httpx.ConnectError("sin conexión", request=request)

    return httpx.MockTransport(handler)


def test_shortlinks_only_count_when_they_end_in_whatsapp():
    html = (
        '<a href="https://wa.link/abc123">ok</a>'
        '<a href="https://wa.link/zzz999">roto</a>'
        '<a href="https://wa.link/meta">meta</a>'
        '<a href="https://wa.link/caido">caído</a>'
    )
    requested: list[str] = []

    async def run():
        async with httpx.AsyncClient(transport=_shortener_transport(requested)) as client:
            return await resolve_shortlinks(extract_whatsapp(html), client)

    result = asyncio.run(run())
    by_number = {c.number: c for c in result.candidates}
    assert set(by_number) == {"56998765432", "56981234567"}
    assert by_number["56998765432"].evidence == (
        "https://wa.link/abc123 → https://api.whatsapp.com/send?phone=56998765432"
    )
    assert result.shortlinks == []
    assert {i.evidence for i in result.ignored} == {"https://wa.link/zzz999", "https://wa.link/caido"}
    # Nunca se visita WhatsApp: basta con ver a dónde redirige el acortador.
    assert all("whatsapp.com" not in url for url in requested)


# --- Elección del WhatsApp principal ---------------------------------------------------------


def _candidate(number, source, placement=WhatsAppPlacement.CUERPO):
    return WhatsAppCandidate(
        number=number,
        url=f"https://wa.me/{number}" if number else "https://wa.me/message/X",
        source=source,
        placement=placement,
        evidence="e",
    )


def test_priority_maps_over_web_over_linktree():
    decision = choose_whatsapp(
        [
            _candidate("56911111111", WhatsAppSource.LINKTREE),
            _candidate("56922222222", WhatsAppSource.WEB, WhatsAppPlacement.HEADER),
            _candidate("56933333333", WhatsAppSource.MAPS),
        ]
    )
    assert decision.url == "https://wa.me/56933333333"
    assert decision.source == WhatsAppSource.MAPS
    assert decision.multiple_numbers is True
    assert [c.number for c in decision.candidates] == ["56933333333", "56922222222", "56911111111"]


def test_priority_floating_or_header_over_footer():
    decision = choose_whatsapp(
        [
            _candidate("56911111111", WhatsAppSource.WEB, WhatsAppPlacement.FOOTER),
            _candidate("56922222222", WhatsAppSource.WEB, WhatsAppPlacement.FLOTANTE),
        ]
    )
    assert decision.url == "https://wa.me/56922222222"


def test_numbered_link_preferred_over_message_link():
    decision = choose_whatsapp(
        [_candidate(None, WhatsAppSource.WEB, WhatsAppPlacement.HEADER), _candidate("56911111111", WhatsAppSource.WEB)]
    )
    assert decision.url == "https://wa.me/56911111111"
    assert decision.multiple_numbers is False


def test_same_number_from_two_sources_is_not_multiple():
    decision = choose_whatsapp(
        [_candidate("56911111111", WhatsAppSource.MAPS), _candidate("56911111111", WhatsAppSource.WEB)]
    )
    assert decision.multiple_numbers is False


def test_without_link_or_phone_is_not_found():
    assert choose_whatsapp([], phone_e164=None).status == WhatsAppStatus.NO_ENCONTRADO


def test_phone_alone_is_never_verified():
    decision = choose_whatsapp([], phone_e164="+56987654321")
    assert decision.status == WhatsAppStatus.NO_CONFIRMADO
    assert decision.url is None
    assert decision.confidence is None


# --- Confianza (¿el WhatsApp existe?) ----------------------------------------------------------


def test_confidence_high_when_two_independent_sources_agree():
    decision = choose_whatsapp(
        [_candidate("56911111111", WhatsAppSource.WEB), _candidate("56911111111", WhatsAppSource.MAPS)]
    )
    assert decision.confidence == WhatsAppConfidence.ALTA
    assert decision.confidence_reason == "El mismo número aparece en Google Maps y su web."


def test_confidence_high_when_whatsapp_matches_maps_phone():
    decision = choose_whatsapp([_candidate("56957734621", WhatsAppSource.MAPS)], phone_e164="+56957734621")
    assert decision.confidence == WhatsAppConfidence.ALTA
    assert "coincide con el teléfono" in decision.confidence_reason


def test_confidence_medium_with_a_single_source():
    decision = choose_whatsapp([_candidate("56911111111", WhatsAppSource.WEB)], phone_e164="+56722234567")
    assert decision.confidence == WhatsAppConfidence.MEDIA
    assert decision.confidence_reason == "Publicado solo en su web."


def test_confidence_counts_sources_of_the_chosen_number_only():
    decision = choose_whatsapp(
        [_candidate("56911111111", WhatsAppSource.MAPS), _candidate("56922222222", WhatsAppSource.WEB)]
    )
    assert decision.confidence == WhatsAppConfidence.MEDIA
    assert decision.multiple_numbers is True


def test_broken_button_is_reported_even_without_a_working_whatsapp():
    decision = choose_whatsapp([], phone_e164="+56987654321", broken_evidence=["https://wa.link/caido"])
    assert decision.status == WhatsAppStatus.NO_CONFIRMADO
    assert decision.broken_button is True
    assert decision.broken_evidence == ["https://wa.link/caido"]


def test_partial_business_fields_do_not_erase_other_sources():
    fields = choose_whatsapp([], phone_e164=None).business_fields()
    assert fields == {}  # NO ENCONTRADO desde una sola fuente no pisa nada
    complete = choose_whatsapp([], phone_e164=None).business_fields(complete=True)
    assert complete["whatsapp_status"] == WhatsAppStatus.NO_ENCONTRADO
    assert complete["whatsapp_url"] is None
