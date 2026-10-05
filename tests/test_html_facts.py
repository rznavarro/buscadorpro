import json
from pathlib import Path

import pytest
from selectolax.lexbor import LexborHTMLParser

from app.config import get_settings
from app.extract.html_facts import (
    CommuneCatalog,
    detect_builders,
    extract_html_facts,
    find_communes,
    find_copyright_years,
    find_trust_signals,
    load_communes,
    visible_text,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def communes() -> CommuneCatalog:
    return load_communes(get_settings().communes_file)


@pytest.fixture(scope="module")
def cerrajeria(communes):
    html = (FIXTURES / "web_cerrajeria_rancagua.html").read_text(encoding="utf-8")
    return extract_html_facts(html, page_url="https://cerrajeriarancagua.cl/", communes=communes)


# --- Página completa -----------------------------------------------------------------


def test_seo_basics(cerrajeria):
    assert cerrajeria.title == "Cerrajería Rancagua 24/7 | Cerrajero a domicilio"
    assert cerrajeria.meta_description.startswith("Cerrajero en Rancagua y Machalí")
    assert cerrajeria.lang == "es-CL"
    assert cerrajeria.viewport == "width=device-width, initial-scale=1"
    assert cerrajeria.canonical == "https://cerrajeriarancagua.cl/"
    assert cerrajeria.favicon == "https://cerrajeriarancagua.cl/wp-content/uploads/2019/03/favicon.png"
    assert cerrajeria.h1 == ["Cerrajero en Rancagua"]
    assert cerrajeria.h2 == ["Nuestros servicios", "¿Por qué elegirnos?", "Testimonios"]
    assert cerrajeria.open_graph["og:title"] == "Cerrajería Rancagua 24/7"


def test_structured_data(cerrajeria):
    assert cerrajeria.json_ld_types == ["Locksmith"]
    assert cerrajeria.local_business["name"] == "Cerrajería Rancagua"


def test_contact_facts(cerrajeria):
    assert cerrajeria.phones == ["+56722234567", "+56987654321"]
    assert cerrajeria.tel_links == ["tel:+56722234567"]
    assert cerrajeria.emails == ["contacto@cerrajeriarancagua.cl"]
    assert cerrajeria.forms == 1
    assert cerrajeria.whatsapp_floating is True
    assert [c.number for c in cerrajeria.whatsapp.candidates] == ["56987654321", "56976543210"]


def test_socials_without_share_buttons(cerrajeria):
    assert cerrajeria.socials == {
        "facebook": ["https://www.facebook.com/cerrajeriarancagua"],
        "instagram": ["https://www.instagram.com/cerrajeriarancagua"],
    }


def test_navigation_links_and_images(cerrajeria):
    assert cerrajeria.menu_items == ["Inicio", "Servicios", "Nosotros", "Contacto"]
    assert "https://cerrajeriarancagua.cl/servicios/" in cerrajeria.internal_links
    assert all(link.startswith("https://cerrajeriarancagua.cl/") for link in cerrajeria.internal_links)
    assert (cerrajeria.images, cerrajeria.images_without_alt) == (2, 1)
    assert cerrajeria.has_map_embed is True


def test_builder_and_age(cerrajeria):
    assert cerrajeria.generator == "WordPress 6.4.2"
    assert [b.name for b in cerrajeria.builders] == ["WordPress", "Elementor", "Tema WordPress: astra"]
    assert cerrajeria.copyright_years == [2019]


def test_communes_and_trust(cerrajeria):
    assert cerrajeria.communes_mentioned == ["Rancagua", "Machalí", "Graneros", "Doñihue"]
    assert {s.kind for s in cerrajeria.trust_signals} == {"testimonios", "experiencia", "certificaciones", "garantía"}
    sec = next(s for s in cerrajeria.trust_signals if s.kind == "certificaciones")
    assert sec.snippet == "Instalador autorizado SEC para cerraduras eléctricas."


def test_facts_are_json_serializable(cerrajeria):
    json.dumps(cerrajeria.model_dump(mode="json"))


# --- Texto visible ------------------------------------------------------------------


def test_visible_text_skips_scripts_styles_and_comments():
    html = (
        "<html><head><title>T</title><style>.a{}</style></head><body>"
        "<p>Hola <b>mundo</b></p><script>var x = 1;</script><!-- oculto --><ul><li>Uno</li><li>Dos</li></ul>"
        "</body></html>"
    )
    assert visible_text(LexborHTMLParser(html)) == "Hola mundo\nUno\nDos"


# --- Comunas --------------------------------------------------------------------------


def test_commune_catalog_has_both_regions(communes):
    data = json.loads(get_settings().communes_file.read_text(encoding="utf-8"))
    assert len(data["regiones"]["Metropolitana de Santiago"]) == 52
    assert len(data["regiones"]["Libertador General Bernardo O'Higgins"]) == 33
    assert set(communes.ambiguous) <= set(communes.names)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Cerrajero en Rancagua", ["Rancagua"]),
        ("CERRAJERÍA EN RANCAGUA Y MACHALÍ", ["Rancagua", "Machalí"]),
        ("Atendemos Nunoa y Providencia", ["Ñuñoa", "Providencia"]),  # sin tildes
        ("cerrajeros rancagua 24 horas", ["Rancagua"]),
        ("¡Feliz Navidad a todos nuestros clientes!", []),  # ambigua sin contexto
        ("Despacho a la comuna de Navidad", ["Navidad"]),  # ambigua con contexto
        ("Subimos la colina para llegar", []),  # en minúscula no es comuna
        ("Atendemos en Colina", ["Colina"]),
        ("Av. Independencia 1234", []),
        ("Rancagua\nAv. Independencia 1234", ["Rancagua"]),  # el contexto es por línea
        ("Cobertura: Rancagua, Graneros, Olivar, Requínoa", ["Rancagua", "Graneros", "Olivar", "Requínoa"]),
    ],
)
def test_find_communes(text, expected, communes):
    assert find_communes(text, communes) == expected


# --- Antigüedad, confianza y plantillas ----------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("© 2019 Cerrajería XYZ", [2019]),
        ("Copyright 2015 - 2021 Negocio", [2021]),
        ("Todos los derechos reservados 2018", [2018]),
        ("2017 · Todos los derechos reservados", [2017]),
        ("Fundada en 1998", []),
    ],
)
def test_copyright_years(text, expected):
    assert find_copyright_years(text) == expected


def test_trust_signals_need_real_words():
    assert find_trust_signals("Ver sección de precios") == []
    kinds = [s.kind for s in find_trust_signals("Más de 20 años de trayectoria\nCertificación SEC clase A\n★★★★★ 4,9")]
    assert kinds == ["reseñas", "experiencia", "certificaciones"]


@pytest.mark.parametrize(
    ("html", "builder"),
    [
        ('<img src="https://static.wixstatic.com/media/a.jpg">', "Wix"),
        ('<link href="https://cdn.shopify.com/s/files/theme.css">', "Shopify"),
        ('<script src="https://cdn.gpteng.co/gptengineer.js"></script>', "Lovable"),
        ('<div class="et_pb_section">', "Divi"),
        ('<script src="https://img1.wsimg.com/blobby/go/x.js"></script>', "GoDaddy Website Builder"),
        ('<link href="/wp-content/themes/houzez/style.css">', "Houzez"),
    ],
)
def test_detect_builders(html, builder):
    assert builder in [b.name for b in detect_builders(html)]


def test_no_builder_on_handmade_html():
    assert detect_builders("<html><body><p>Hola</p></body></html>") == []
