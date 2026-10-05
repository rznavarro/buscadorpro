"""URLs: desenvolver redirecciones de Google, normalizar y clasificar enlaces.

Sección 5, paso 5: el "sitio web" que entrega Maps puede ser una web propia, una red
social, un enlace de WhatsApp o una página tipo Linktree.
"""

import re
from enum import StrEnum
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from selectolax.lexbor import LexborHTMLParser

from app.extract.socials import classify_social


class LinkKind(StrEnum):
    WEB_PROPIA = "web_propia"
    RED_SOCIAL = "red_social"
    WHATSAPP = "whatsapp"
    AGREGADOR = "agregador"  # Linktree y similares
    GOOGLE = "google"  # fichas o enlaces de Google Maps
    INVALIDO = "invalido"


WHATSAPP_HOSTS = frozenset(
    {"wa.me", "api.whatsapp.com", "web.whatsapp.com", "wa.link", "walink.co", "chat.whatsapp.com"}
)
LINK_AGGREGATOR_HOSTS = frozenset(
    {
        "linktr.ee",
        "beacons.ai",
        "linkin.bio",
        "taplink.cc",
        "bio.link",
        "lnk.bio",
        "campsite.bio",
        "solo.to",
        "linkr.bio",
        "msha.ke",
        "allmylinks.com",
        "linkbio.co",
    }
)
GOOGLE_MAPS_HOSTS = frozenset({"maps.app.goo.gl", "g.page", "maps.google.com", "g.co"})

_GOOGLE_HOST = re.compile(r"(?:^|\.)google\.[a-z]{2,3}(?:\.[a-z]{2})?$")
_TRACKING_PARAMS = re.compile(r"^(?:utm_.*|fbclid|gclid|gbraid|wbraid|mc_cid|mc_eid|igshid)$", re.IGNORECASE)


def _host(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _is_google_host(host: str) -> bool:
    return bool(_GOOGLE_HOST.search(host))


def unwrap_google_redirect(url: str) -> str:
    """`https://www.google.com/url?q=https://negocio.cl/&sa=…` → `https://negocio.cl/`.

    También acepta la forma relativa `/url?q=…` que aparece en el panel de Maps.
    """
    for _ in range(3):  # a veces vienen anidadas
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").lower()
        if parts.path != "/url" or (host and not _is_google_host(host)):
            break
        params = parse_qs(parts.query)
        target = (params.get("q") or params.get("url") or [""])[0]
        if not target:
            break
        url = target
    return url


def normalize_url(url: str | None, base_url: str | None = None) -> str | None:
    """URL absoluta http(s), sin fragmento ni parámetros de seguimiento. None si no es web."""
    url = (url or "").strip()
    if not url:
        return None
    if base_url:
        url = urljoin(base_url, url)
    if url.startswith("//"):
        url = "https:" + url
    if not re.match(r"^[a-z][a-z0-9+.-]*:", url, re.IGNORECASE):
        # "negocio.cl" o "www.negocio.cl/contacto" sin esquema
        if re.match(r"^[\w-]+(\.[\w-]+)+(/|$)", url):
            url = "https://" + url
        else:
            return None
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    netloc = parts.hostname.lower()
    if parts.port and parts.port not in (80, 443):
        netloc += f":{parts.port}"
    params = parse_qs(parts.query, keep_blank_values=True)
    query = urlencode(
        [(key, value) for key, values in params.items() for value in values if not _TRACKING_PARAMS.match(key)]
    )
    return urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", query, ""))


def classify_link(url: str | None) -> LinkKind:
    """Qué tipo de enlace es (sin descargar nada)."""
    url = unwrap_google_redirect((url or "").strip())
    if re.match(r"^whatsapp://", url, re.IGNORECASE):
        return LinkKind.WHATSAPP
    normalized = normalize_url(url)
    if not normalized:
        return LinkKind.INVALIDO
    host = _host(normalized)
    if host in WHATSAPP_HOSTS:
        return LinkKind.WHATSAPP
    if host in LINK_AGGREGATOR_HOSTS:
        return LinkKind.AGREGADOR
    if classify_social(normalized):
        return LinkKind.RED_SOCIAL
    if host in GOOGLE_MAPS_HOSTS or (_is_google_host(host) and urlsplit(normalized).path.startswith("/maps")):
        return LinkKind.GOOGLE
    if host == "goo.gl" and urlsplit(normalized).path.startswith("/maps"):
        return LinkKind.GOOGLE
    return LinkKind.WEB_PROPIA


def extract_hrefs(html: str, base_url: str | None = None) -> list[str]:
    """Todos los `href` de enlaces de la página, en bruto (sin clasificar), sin repetir."""
    tree = LexborHTMLParser(html or "")
    hrefs: dict[str, None] = {}
    for node in tree.css("a[href], area[href]"):
        href = (node.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:")):
            continue
        href = unwrap_google_redirect(href)
        if base_url and not re.match(r"^[a-z][a-z0-9+.-]*:", href, re.IGNORECASE):
            href = urljoin(base_url, href)
        hrefs.setdefault(href, None)
    return list(hrefs)


# --- Google Maps ------------------------------------------------------------------

_MAPS_FTID = re.compile(r"!1s(0x[0-9a-f]+:0x[0-9a-f]+)", re.IGNORECASE)
# place_id oficial: en búsquedas (query_place_id=…) y en las URLs de la lista de Maps (!19sChIJ…)
_MAPS_PLACE_ID = re.compile(r"(?:place_id[:=]|query_place_id=|!19s)(ChI[A-Za-z0-9_-]{10,})")
_MAPS_CID = re.compile(r"[?&]cid=(\d+)")
_MAPS_COORDS = re.compile(r"/@-?\d+(?:\.\d+)?,-?\d+(?:\.\d+)?(?:,[\d.]+[a-z]?)*")
_MAPS_KEPT_PARAMS = frozenset({"cid", "q", "query", "query_place_id", "ftid"})


def maps_place_key(url: str) -> str | None:
    """Identificador estable de un lugar dentro de una URL de Maps, si lo trae.

    Ejemplos: "ftid:0x9662…:0x1a2b…", "place_id:ChIJ…", "cid:1234567890".
    """
    for prefix, pattern in (("place_id", _MAPS_PLACE_ID), ("ftid", _MAPS_FTID), ("cid", _MAPS_CID)):
        match = pattern.search(url or "")
        if match:
            return f"{prefix}:{match.group(1).lower() if prefix == 'ftid' else match.group(1)}"
    return None


def canonical_maps_url(url: str) -> str:
    """URL de una ficha de Maps sin coordenadas de la vista ni parámetros de sesión.

    La misma ficha abierta desde dos búsquedas distintas queda con la misma URL.
    """
    parts = urlsplit(unwrap_google_redirect(url.strip()))
    host = (parts.hostname or "").lower()
    if not _is_google_host(host):
        return urlunsplit(("https", host, parts.path, parts.query, ""))
    path = _MAPS_COORDS.sub("", parts.path)
    params = parse_qs(parts.query)
    keep = [(key, value) for key, values in params.items() for value in values if key in _MAPS_KEPT_PARAMS]
    return urlunsplit(("https", "www.google.com", path, urlencode(keep), ""))
