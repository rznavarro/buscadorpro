import pytest

from app.analyze.web_status import (
    detect_dead_site,
    http_error_reason,
    is_bot_wall,
    needs_render,
    pick_internal_pages,
)


@pytest.mark.parametrize(
    ("title", "text", "expected"),
    [
        ("cerrajeriaxyz.cl", "Este dominio está a la venta. Contáctanos para comprarlo.", "El dominio está a la venta"),
        ("Domain for sale", "Buy this domain today", "El dominio está a la venta"),
        ("Account Suspended", "This account has been suspended. Contact your hosting provider.", "La cuenta de hosting está suspendida"),
        ("", "Sitio web suspendido", "La cuenta de hosting está suspendida"),
        ("Parked", "This domain is parked free, courtesy of GoDaddy. parked free", "El dominio está estacionado (sin web)"),
        ("Error de base de datos", "Error establishing a database connection", "La web muestra un error de base de datos (WordPress caído)"),
        ("Index of /", "Index of /\nName Last modified Size", "El servidor muestra una carpeta vacía (Index of /)"),
        ("Gasfiter XYZ", "Sitio en construcción. Vuelve pronto.", "La web está en construcción o en mantenimiento"),
        ("", "Welcome to nginx!", "La web muestra la página por defecto del hosting"),
    ],
)
def test_dead_sites_are_detected(title, text, expected):
    assert detect_dead_site(title, text) == expected


def test_real_site_mentioning_coming_soon_is_not_dead():
    text = "Cerrajería en Rancagua. " * 30 + "Próximamente: nueva sucursal en Machalí."
    assert detect_dead_site("Cerrajería XYZ", text) is None


def test_normal_site_is_not_dead():
    assert detect_dead_site("Gasfiter Rancagua", "Gasfitería a domicilio en Rancagua. Llámanos.") is None


@pytest.mark.parametrize(
    ("title", "text", "expected"),
    [
        ("Just a moment...", "Checking your browser before accessing", True),
        ("Attention Required! | Cloudflare", "", True),
        ("Cerrajería XYZ", "Abrimos puertas", False),
    ],
)
def test_bot_wall(title, text, expected):
    assert is_bot_wall(title, text) is expected


def test_needs_render_for_javascript_sites():
    assert needs_render("") is True
    assert needs_render("Cargando…") is True
    assert needs_render("Gasfitería a domicilio. " * 20) is False


def test_http_error_reason_is_readable():
    assert http_error_reason(404) == "La web responde con error 404 (página no encontrada)"
    assert http_error_reason(599) == "La web responde con error 599 (error del servidor)"


def test_pick_internal_pages_prefers_contact():
    links = [
        "https://negocio.cl/",
        "https://negocio.cl/blog/novedades/",
        "https://negocio.cl/nosotros/",
        "https://negocio.cl/servicios/",
        "https://negocio.cl/contacto/",
        "https://negocio.cl/wp-content/uploads/catalogo.pdf",
        "https://negocio.cl/servicios/cerrajeria/",
        "https://negocio.cl/cotizacion",
    ]
    assert pick_internal_pages(links, "https://negocio.cl/", 3) == [
        "https://negocio.cl/contacto/",
        "https://negocio.cl/cotizacion",
        "https://negocio.cl/servicios/",
    ]


def test_pick_internal_pages_respects_limit_and_accents():
    links = ["https://negocio.cl/ubicación/", "https://negocio.cl/quiénes-somos/"]
    assert pick_internal_pages(links, "https://negocio.cl/", 1) == ["https://negocio.cl/ubicación/"]
    assert pick_internal_pages(links, "https://negocio.cl/", 0) == []


def test_login_wall_reason():
    from app.analyze.web_status import login_wall_reason

    assert "Google" in login_wall_reason("https://accounts.google.com/v3/signin/identifier?continue=x")
    assert login_wall_reason("https://sites.google.com/view/cerrajero") is None
    assert login_wall_reason("https://negocio.cl/") is None
    assert login_wall_reason(None) is None
