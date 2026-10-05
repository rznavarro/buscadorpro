"""Hallazgos automáticos (sin IA) sobre webs simuladas."""

from datetime import date

import pytest

from app.analyze.collector import BrokenLink, PageMetrics, WebCollection, WebVerification
from app.analyze.findings import build_findings, status_findings
from app.config import get_settings
from app.extract.html_facts import extract_html_facts, load_communes
from app.extract.whatsapp import extract_whatsapp
from app.models import Business, WebsiteStatus, WhatsAppSource

TODAY = date(2026, 10, 4)
FILLER = "Cerrajería a domicilio con atención rápida. " * 10

BAD_HTML = f"""<html><head><title>Inicio</title></head><body>
<h1>Bienvenidos</h1><h1>Servicios</h1><p>{FILLER}</p>
<p>© 2019 Cerrajería XYZ</p></body></html>"""

GOOD_HTML = f"""<html lang="es"><head><title>Cerrajero en Rancagua 24 horas | Cerrajería XYZ</title>
<meta name="description" content="Cerrajería XYZ en Rancagua y Machalí.">
<meta name="viewport" content="width=device-width, initial-scale=1">
<script type="application/ld+json">{{"@type": "Locksmith", "name": "Cerrajería XYZ"}}</script></head><body>
<nav><a href="/servicios/">Servicios</a><a href="/contacto/">Contacto</a></nav>
<h1>Cerrajero en Rancagua</h1><p>{FILLER}</p>
<p>Cerrajería XYZ atiende en Rancagua, Machalí y Graneros. Horario: lunes a domingo, 24 horas.</p>
<h2>Testimonios</h2><p>“Excelente servicio” — Ana</p><p>4,9 estrellas en Google: 120 reseñas.</p>
<p>Más de 15 años de experiencia. Garantía de 6 meses.</p>
<p>Llámanos: <a href="tel:+56993557317">+56 9 9355 7317</a></p>
<a class="wa-float" href="https://wa.me/56993557317">WhatsApp</a>
<iframe src="https://www.google.com/maps/embed?pb=1"></iframe>
<p>© {TODAY.year} Cerrajería XYZ</p></body></html>"""


def collection(html: str, url: str, *, desktop: PageMetrics, mobile: PageMetrics, broken_links=(), broken_images=(), broken_wa=()) -> WebCollection:
    facts = extract_html_facts(html, page_url=url, communes=load_communes(get_settings().communes_file))
    wa = extract_whatsapp(html, source=WhatsAppSource.WEB, page_url=url)
    verification = WebVerification(
        url=url, final_url=url, status=WebsiteStatus.OK, https=url.startswith("https"),
        whatsapp_candidates=wa.candidates, whatsapp_broken=list(broken_wa), whatsapp_floating=wa.floating_button,
    )
    return WebCollection(
        verification=verification, facts=facts, desktop=desktop, mobile=mobile,
        broken_links=list(broken_links), links_checked=10, broken_images=list(broken_images), images_checked=5,
    )


def page(**overrides) -> PageMetrics:
    base = {"viewport": "1440x900", "load_ms": 1200, "requests": 30, "weight_kb": 800,
            "screenshot": "screenshots/x/escritorio.jpg", "screenshot_full": "screenshots/x/escritorio-completa.jpg",
            "layout_width": 1440, "scroll_width": 1440}
    return PageMetrics(**{**base, **overrides})


@pytest.fixture
def bad():
    return collection(
        BAD_HTML,
        "http://cerrajeriaxyz.cl/",
        desktop=page(load_ms=7200, fake_ctas_first_screen=["Llamar"]),
        mobile=page(viewport="390x844", zoomed_out_on_mobile=True, layout_width=980, scroll_width=980,
                    screenshot="screenshots/x/celular.jpg"),
        broken_links=[BrokenLink(url="http://cerrajeriaxyz.cl/trabajos/", reason="La web responde con error 404 (página no encontrada)")],
        broken_images=[BrokenLink(url="http://cerrajeriaxyz.cl/foto.jpg", reason="La web responde con error 404 (página no encontrada)")],
        broken_wa=["https://wa.link/roto"],
    )


@pytest.fixture
def good():
    return collection(
        GOOD_HTML,
        "https://cerrajeriaxyz.cl/",
        desktop=page(ctas_first_screen=["Contacto"], whatsapp_first_screen=True, tel_first_screen=True),
        mobile=page(viewport="390x844", whatsapp_first_screen=True, layout_width=390, scroll_width=390),
    )


BUSINESS = Business(google_maps_url="https://www.google.com/maps/place/x", business_name="Cerrajería XYZ", city="Rancagua")


def test_bad_site_findings(bad):
    report = build_findings(bad, BUSINESS, TODAY)
    codes = [f.codigo for f in report.hallazgos]
    assert set(codes) >= {
        "boton_falso", "whatsapp_roto", "sin_whatsapp_web", "no_adaptada_celular", "lenta", "sin_https",
        "sin_testimonios", "enlaces_rotos", "imagenes_rotas", "copyright_antiguo", "sin_telefono_click",
        "sin_cobertura", "seo_local_debil",
    }
    # Ordenados por gravedad: primero los de gravedad alta
    severities = [f.gravedad for f in report.hallazgos]
    assert severities == sorted(severities, key={"alta": 0, "media": 1, "baja": 2}.get)
    fake = next(f for f in report.hallazgos if f.codigo == "boton_falso")
    assert fake.titulo == "El botón “Llamar” no hace nada al tocarlo"
    assert fake.captura == "screenshots/x/escritorio.jpg"
    slow = next(f for f in report.hallazgos if f.codigo == "lenta")
    assert slow.titulo == "La web tarda 7,2 s en cargar" and slow.gravedad == "alta"  # tabla: < 9 s → 2
    old = next(f for f in report.hallazgos if f.codigo == "copyright_antiguo")
    assert old.titulo == "El pie de página muestra el año 2019: la web se ve desactualizada"
    # Errores para el parámetro 9: enlace roto + WhatsApp roto + botón falso
    assert len(report.link_errors) == 3


def test_findings_are_written_for_the_business_owner(bad):
    for finding in build_findings(bad, BUSINESS, TODAY).hallazgos:
        assert "<" not in finding.titulo and "http" not in finding.titulo  # lo técnico va en la evidencia
        assert finding.gravedad in ("alta", "media", "baja")


def test_good_site_has_no_findings(good):
    report = build_findings(good, BUSINESS, TODAY)
    assert report.hallazgos == []
    assert all(item.ok for item in report.seo_local.values())
    assert {key for key, item in report.checklist.items() if item.ok is not True} == {"diferenciadores"}


def test_whatsapp_hidden_on_mobile(good):
    good.mobile.whatsapp_first_screen = False
    codes = [f.codigo for f in build_findings(good, BUSINESS, TODAY).hallazgos]
    assert codes == ["whatsapp_escondido_celular"]


def test_mobile_overflow(good):
    good.mobile.horizontal_overflow = True
    good.mobile.scroll_width = 1200
    finding = build_findings(good, BUSINESS, TODAY).hallazgos[0]
    assert finding.codigo == "se_sale_celular"
    assert "1200 px" in finding.evidencia[0]


def test_template_builders_are_low_severity(good):
    good.facts.builders = [*good.facts.builders, *extract_html_facts("", page_url="https://x.wixsite.com/a").builders]
    finding = build_findings(good, BUSINESS, TODAY).hallazgos[-1]
    assert (finding.codigo, finding.gravedad) == ("plantilla", "baja")


@pytest.mark.parametrize(
    ("status", "extra", "code"),
    [
        (WebsiteStatus.SIN_WEB, {}, "sin_web"),
        (WebsiteStatus.SOLO_REDES, {"facebook": "https://www.facebook.com/x"}, "solo_redes"),
        (WebsiteStatus.CAIDO, {"website": "https://x.cl/", "field_sources": {"website_status": "web: El dominio está a la venta"}}, "web_caida"),
        (WebsiteStatus.OK, {"website": "https://x.cl/"}, None),
    ],
)
def test_status_findings(status, extra, code):
    business = Business(google_maps_url="https://www.google.com/maps/place/x", business_name="X", website_status=status, **extra)
    findings = status_findings(business)
    assert [f.codigo for f in findings] == ([code] if code else [])
    if code == "solo_redes":
        assert findings[0].titulo == "Usa Facebook en vez de una web propia"
    if code == "web_caida":
        assert findings[0].detalle == "El dominio está a la venta"
