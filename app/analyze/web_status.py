"""Reglas para decidir si una web "existe" de verdad. Python puro, sin red ni navegador.

Una web que carga pero muestra "dominio a la venta", "cuenta suspendida" o la página por
defecto del hosting no le sirve al negocio: para Vortexia cuenta como CAÍDA.
"""

import re
from urllib.parse import urlsplit

from app.extract.html_facts import fold_accents

# Siempre indican que la web no funciona, sin importar cuánto texto tenga la página.
_DEAD_SITE: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "El dominio está a la venta",
        re.compile(
            r"este dominio (?:esta|se encuentra) (?:a la venta|en venta)|domain (?:is )?for sale|buy this domain"
            r"|compra(?:r)? este dominio"
        ),
    ),
    (
        "La cuenta de hosting está suspendida",
        re.compile(r"account (?:has been )?suspended|cuenta (?:ha sido )?suspendida|sitio (?:web )?suspendido|website suspended"),
    ),
    (
        "El dominio está estacionado (sin web)",
        re.compile(r"parked (?:domain|free)|dominio estacionado|domain parking|sedoparking|parkingcrew|bodis\.com"),
    ),
    (
        "La web muestra un error de base de datos (WordPress caído)",
        re.compile(r"error establishing a database connection|error al establecer una conexion con la base de datos"),
    ),
    ("El servidor muestra una carpeta vacía (Index of /)", re.compile(r"^index of /", re.MULTILINE)),
)

# Solo cuentan si la página casi no tiene texto: una web normal puede decir "próximamente".
_PLACEHOLDER: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "La web está en construcción o en mantenimiento",
        re.compile(
            r"en construccion|under construction|coming soon|proximamente|en mantenimiento|maintenance mode"
            r"|estamos trabajando en (?:nuestro|el) sitio"
        ),
    ),
    (
        "La web muestra la página por defecto del hosting",
        re.compile(
            r"default web site page|apache2 \w+ default page|welcome to nginx|it works!"
            r"|congratulations! your (?:website|domain)|felicidades.{0,40}(?:tu sitio|tu dominio)"
        ),
    ),
)
_PLACEHOLDER_MAX_TEXT = 400

_BOT_WALL = re.compile(
    r"just a moment|attention required|checking your browser|verifica(?:ndo)? que eres (?:un )?humano"
    r"|ddos protection|enable javascript and cookies to continue",
    re.IGNORECASE,
)

_HTTP_REASONS = {
    400: "petición rechazada",
    401: "pide usuario y contraseña",
    403: "acceso prohibido",
    404: "página no encontrada",
    410: "la página fue eliminada",
    429: "demasiadas visitas, el servidor rechazó la revisión",
    500: "error interno del servidor",
    502: "el servidor no responde",
    503: "servidor caído o en mantenimiento",
    504: "el servidor tardó demasiado",
}


def http_error_reason(status_code: int) -> str:
    return f"La web responde con error {status_code} ({_HTTP_REASONS.get(status_code, 'error del servidor')})"


def detect_dead_site(title: str | None, visible_text: str | None) -> str | None:
    """Motivo, si la página que carga no es una web real del negocio."""
    text = fold_accents(f"{title or ''}\n{visible_text or ''}").lower()
    for reason, pattern in _DEAD_SITE:
        if pattern.search(text):
            return reason
    if len(visible_text or "") <= _PLACEHOLDER_MAX_TEXT:
        for reason, pattern in _PLACEHOLDER:
            if pattern.search(text):
                return reason
    return None


# Páginas de inicio de sesión: si la web termina ahí, el sitio no es público.
_LOGIN_HOSTS: dict[str, str] = {
    "accounts.google.com": "Google",
    "login.microsoftonline.com": "Microsoft",
    "login.live.com": "Microsoft",
    "users.wix.com": "Wix",
}


def login_wall_reason(final_url: str | None) -> str | None:
    """Motivo, si la web redirige a una página para iniciar sesión (por ejemplo, un Google Sites privado)."""
    host = (urlsplit(final_url or "").hostname or "").lower()
    service = _LOGIN_HOSTS.get(host)
    if service is None:
        return None
    return f"La web no es pública: pide iniciar sesión en {service}, así que los clientes no pueden verla"


def is_bot_wall(title: str | None, visible_text: str | None) -> bool:
    """La web muestra una verificación anti-bots (Cloudflare u otra) en vez de su contenido."""
    return bool(_BOT_WALL.search(f"{title or ''}\n{(visible_text or '')[:2000]}"))


def needs_render(visible_text: str) -> bool:
    """La web se arma con JavaScript: el HTML que llega casi no trae texto."""
    return len(visible_text.strip()) < 200


# Páginas internas donde suele estar el WhatsApp, en orden de prioridad (sección 5, paso 6).
_PAGE_PRIORITY: tuple[re.Pattern[str], ...] = (
    re.compile(r"contact|cotiza|presupuesto|ubicacion|donde-estamos|agenda|reserva"),
    re.compile(r"servicio|service|trabajos|productos|precios"),
    re.compile(r"nosotros|quienes|empresa|about|historia|equipo"),
)
_NOT_A_PAGE = re.compile(r"\.(?:pdf|jpe?g|png|gif|webp|svg|zip|rar|docx?|xlsx?|pptx?|mp4|mp3)$", re.IGNORECASE)


def pick_internal_pages(links: list[str], home_url: str, max_pages: int) -> list[str]:
    """Hasta `max_pages` páginas internas relevantes: contacto primero, luego servicios y nosotros."""
    home_path = urlsplit(home_url).path.rstrip("/")
    ranked: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    for link in links:
        parts = urlsplit(link)
        path = fold_accents(parts.path).lower().rstrip("/")
        if path == home_path or not path or _NOT_A_PAGE.search(path) or path in seen:
            continue
        for priority, pattern in enumerate(_PAGE_PRIORITY):
            if pattern.search(path):
                seen.add(path)
                ranked.append((priority, len(path), link))
                break
    return [link for _, _, link in sorted(ranked)[:max_pages]]
