from pathlib import Path

import pytest

from app.extract.links import (
    LinkKind,
    canonical_maps_url,
    classify_link,
    extract_hrefs,
    maps_place_key,
    normalize_url,
    unwrap_google_redirect,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("/url?q=https://www.negocio.cl/&opi=79508299&sa=U", "https://www.negocio.cl/"),
        ("https://www.google.com/url?q=https://negocio.cl/contacto&sa=D", "https://negocio.cl/contacto"),
        ("https://www.google.cl/url?sa=t&url=https://negocio.cl/", "https://negocio.cl/"),
        ("https://www.google.com/url?q=https%3A%2F%2Fnegocio.cl%2F%3Fa%3D1", "https://negocio.cl/?a=1"),
        # anidada
        ("/url?q=https://www.google.com/url?q%3Dhttps://negocio.cl/", "https://negocio.cl/"),
        # no es una redirección de Google: queda igual
        ("https://negocio.cl/url?q=https://otro.cl/", "https://negocio.cl/url?q=https://otro.cl/"),
        ("https://negocio.cl/", "https://negocio.cl/"),
    ],
)
def test_unwrap_google_redirect(url, expected):
    assert unwrap_google_redirect(url) == expected


@pytest.mark.parametrize(
    ("url", "base", "expected"),
    [
        ("negocio.cl", None, "https://negocio.cl/"),
        ("www.negocio.cl/contacto", None, "https://www.negocio.cl/contacto"),
        ("HTTPS://WWW.Negocio.CL/Servicios#precios", None, "https://www.negocio.cl/Servicios"),
        ("https://negocio.cl/?utm_source=google&utm_medium=maps&id=3", None, "https://negocio.cl/?id=3"),
        ("https://negocio.cl/?fbclid=abc", None, "https://negocio.cl/"),
        ("/contacto/", "https://negocio.cl/servicios/", "https://negocio.cl/contacto/"),
        ("//cdn.negocio.cl/logo.png", None, "https://cdn.negocio.cl/logo.png"),
        ("mailto:hola@negocio.cl", None, None),
        ("javascript:void(0)", None, None),
        ("", None, None),
    ],
)
def test_normalize_url(url, base, expected):
    assert normalize_url(url, base) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.cerrajeriaxyz.cl/", LinkKind.WEB_PROPIA),
        ("cerrajeriaxyz.cl", LinkKind.WEB_PROPIA),
        ("https://sites.google.com/view/cerrajeriaxyz", LinkKind.WEB_PROPIA),
        ("https://www.instagram.com/cerrajeriaxyz/", LinkKind.RED_SOCIAL),
        ("https://m.facebook.com/cerrajeriaxyz", LinkKind.RED_SOCIAL),
        ("https://wa.me/56987654321", LinkKind.WHATSAPP),
        ("https://api.whatsapp.com/send?phone=56987654321", LinkKind.WHATSAPP),
        ("whatsapp://send?phone=56987654321", LinkKind.WHATSAPP),
        ("https://wa.link/abc123", LinkKind.WHATSAPP),
        ("https://linktr.ee/cerrajeriaxyz", LinkKind.AGREGADOR),
        ("https://beacons.ai/cerrajeriaxyz", LinkKind.AGREGADOR),
        ("https://maps.app.goo.gl/AbCd123", LinkKind.GOOGLE),
        ("https://www.google.com/maps/place/Cerrajeria/@-34.17,-70.74,17z", LinkKind.GOOGLE),
        ("/url?q=https://www.instagram.com/cerrajeriaxyz/&sa=U", LinkKind.RED_SOCIAL),
        ("/url?q=https://wa.me/56987654321&sa=U", LinkKind.WHATSAPP),
        ("", LinkKind.INVALIDO),
        ("tel:+56722234567", LinkKind.INVALIDO),
    ],
)
def test_classify_link(url, expected):
    assert classify_link(url) == expected


MAPS_URL_A = (
    "https://www.google.com/maps/place/Cerrajer%C3%ADa+XYZ/@-34.1701,-70.7406,17z/"
    "data=!3m1!4b1!4m6!3m5!1s0x9663431b0b0d5b5b:0x1a2b3c4d5e6f7a8b!8m2!3d-34.17!4d-70.74?entry=ttu&g_ep=abc"
)
MAPS_URL_B = (
    "https://www.google.com/maps/place/Cerrajer%C3%ADa+XYZ/@-34.1755,-70.7311,15z/"
    "data=!3m1!4b1!4m6!3m5!1s0x9663431B0B0D5B5B:0x1A2B3C4D5E6F7A8B!8m2!3d-34.17!4d-70.74?hl=es"
)


def test_maps_place_key_variants():
    assert maps_place_key(MAPS_URL_A) == "ftid:0x9663431b0b0d5b5b:0x1a2b3c4d5e6f7a8b"
    assert maps_place_key(MAPS_URL_B) == maps_place_key(MAPS_URL_A)
    assert maps_place_key("https://www.google.com/maps/search/?api=1&query_place_id=ChIJN1t_tDeuEmsRUsoyG83frY4") == (
        "place_id:ChIJN1t_tDeuEmsRUsoyG83frY4"
    )
    assert maps_place_key("https://maps.google.com/?cid=1234567890123") == "cid:1234567890123"
    # Las URLs de la lista de Maps traen el place_id oficial: tiene prioridad sobre el ftid.
    assert maps_place_key(MAPS_URL_A.replace("!8m2", "!19sChIJsxKenW1DY5YR6SOAf3cp158!8m2")) == (
        "place_id:ChIJsxKenW1DY5YR6SOAf3cp158"
    )
    assert maps_place_key("https://www.google.com/maps/place/Cerrajeria") is None


def test_canonical_maps_url_drops_view_and_session_params():
    canonical = canonical_maps_url(MAPS_URL_A)
    assert "@-34" not in canonical
    assert "entry=" not in canonical and "g_ep=" not in canonical
    assert "!1s0x9663431b0b0d5b5b:0x1a2b3c4d5e6f7a8b" in canonical
    assert canonical.startswith("https://www.google.com/maps/place/")


def test_canonical_maps_url_is_stable_across_views():
    url_zoomed = MAPS_URL_A.replace("@-34.1701,-70.7406,17z", "@-34.1801,-70.7506,12z")
    assert canonical_maps_url(url_zoomed) == canonical_maps_url(MAPS_URL_A)


def test_extract_hrefs_from_maps_panel_unwraps_google_redirects():
    hrefs = extract_hrefs((FIXTURES / "maps_panel.html").read_text(encoding="utf-8"))
    assert hrefs == [
        "https://www.cerrajeriaxyz.cl/",
        "https://wa.me/56987654321",
        "https://www.instagram.com/cerrajeriaxyz/",
    ]
    assert [classify_link(h) for h in hrefs] == [LinkKind.WEB_PROPIA, LinkKind.WHATSAPP, LinkKind.RED_SOCIAL]


def test_extract_hrefs_from_linktree():
    hrefs = extract_hrefs((FIXTURES / "linktree.html").read_text(encoding="utf-8"))
    kinds = {classify_link(h): h for h in hrefs}
    assert kinds == {
        LinkKind.WHATSAPP: "https://wa.me/56987654321",
        LinkKind.RED_SOCIAL: "https://instagram.com/cerrajeriaxyz",
        LinkKind.WEB_PROPIA: "https://cerrajeriaxyz.cl",
    }


def test_extract_hrefs_resolves_relative_links_and_skips_anchors():
    html = '<a href="/contacto">C</a><a href="#top">T</a><a href="javascript:void(0)">J</a>'
    assert extract_hrefs(html, base_url="https://negocio.cl/servicios/") == ["https://negocio.cl/contacto"]
