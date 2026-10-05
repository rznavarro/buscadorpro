"""Lectura del HTML de Google Maps con Python, sin IA.

El navegador solo lleva la página hasta la lista o la ficha correcta; aquí se extraen
los datos. Se usan atributos estables (`data-item-id`, `aria-label`, `role`) y no las
clases internas de Google, que cambian seguido.
"""

import re
from typing import NamedTuple

from selectolax.lexbor import LexborHTMLParser, LexborNode

from app.extract.html_facts import CommuneCatalog, fold_accents
from app.extract.links import (
    LinkKind,
    canonical_maps_url,
    classify_link,
    maps_place_key,
    normalize_url,
    unwrap_google_redirect,
)
from app.extract.phones import normalize_phone
from app.extract.socials import extract_socials
from app.extract.whatsapp import extract_whatsapp
from app.models import WhatsAppSource
from app.sources.base import MapsPlace

# --- Bloqueos ----------------------------------------------------------------------

_BLOCK_TEXT = re.compile(
    r"tr[aá]fico inusual|unusual traffic|no soy un robot|i'?m not a robot|recaptcha|captcha", re.IGNORECASE
)


def detect_block(url: str, text: str = "") -> str | None:
    """Motivo del bloqueo si Google muestra un captcha o un aviso de tráfico inusual.

    Pasar `text` solo cuando la página no mostró el contenido esperado: así el nombre de
    un negocio o una reseña que diga "captcha" no se confunde con un bloqueo.
    """
    if re.search(r"google\.[a-z.]+/sorry/|/sorry/index", url or ""):
        return "Google mostró su página de verificación (captcha)."
    match = _BLOCK_TEXT.search(text or "")
    if match:
        return f'Google mostró un aviso de bloqueo ("{match.group(0)}").'
    return None


# --- Lista de resultados -----------------------------------------------------------------

_END_OF_LIST = re.compile(r"final de la lista|end of the list", re.IGNORECASE)


_PLACE_LINK = 'a[href*="/maps/place/"]'
_CARD_PHONE = re.compile(r"^\+?[\d\s().-]{7,}$")


class FeedCard(NamedTuple):
    """Un resultado de la lista de Maps, con lo que muestra la tarjeta sin abrir la ficha."""

    name: str | None
    url: str
    website: str | None = None  # el botón "Sitio web" de la tarjeta, si lo tiene
    phone_e164: str | None = None


def _card_of(link: LexborNode) -> LexborNode:
    """El bloque de la tarjeta: el ancestro más grande que contiene solo este resultado."""
    node = link
    while node.parent is not None and len(node.parent.css(_PLACE_LINK)) == 1:
        node = node.parent
    return node


def _card_details(card: LexborNode, region: str) -> tuple[str | None, str | None]:
    website = None
    for link in card.css('a[data-value="Sitio web"], a[aria-label^="Visitar el sitio web"], a[aria-label^="Visit"]'):
        href = (link.attributes.get("href") or "").strip()
        if href:
            website = normalize_url(unwrap_google_redirect(href)) or href
            break
    phone = None
    for text in card.text(separator="\n", strip=True).splitlines():
        if _CARD_PHONE.match(text.strip()):
            parsed = normalize_phone(text, region)
            if parsed:
                phone = parsed.e164
                break
    return website, phone


def parse_feed(html: str, region: str = "CL") -> list[FeedCard]:
    """Cada resultado (nombre, ficha, web y teléfono de la tarjeta), en orden y sin repetir el mismo lugar."""
    tree = LexborHTMLParser(html or "")
    results: list[FeedCard] = []
    seen: set[str] = set()
    for link in tree.css(_PLACE_LINK):
        url = (link.attributes.get("href") or "").strip()
        key = maps_place_key(url) or canonical_maps_url(url)
        if not url or key in seen:
            continue
        seen.add(key)
        name = (link.attributes.get("aria-label") or link.text(strip=True) or "").strip() or None
        website, phone = _card_details(_card_of(link), region)
        results.append(FeedCard(name, url, website, phone))
    return results


def feed_reached_end(html: str) -> bool:
    """True si Maps ya mostró "Has llegado al final de la lista"."""
    return bool(_END_OF_LIST.search(LexborHTMLParser(html or "").text(separator=" ")))


# --- Ficha de un negocio ---------------------------------------------------------------------

_RATING = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s+estrellas?\s*$", re.IGNORECASE)
_REVIEWS = re.compile(r"^\s*(\d[\d.\s]*)\s+reseñas?\s*$", re.IGNORECASE)
_LABEL_PREFIX = re.compile(r"^\s*(?:Dirección|Teléfono|Sitio web|Address|Phone|Website)\s*:\s*", re.IGNORECASE)
_DAYS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
_HOURS_LABEL = re.compile(
    r"^\s*(lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo)\s*,\s*(.+?)(?:\s*,\s*Copiar el horario)?\s*$",
    re.IGNORECASE,
)
_GOOGLE_HELP_HOSTS = re.compile(r"^https?://(?:support|policies|accounts)\.google\.", re.IGNORECASE)

# Secciones de la pestaña "Información" que no describen al negocio
_NOT_ABOUT = re.compile(
    r"^(?:fotos|resumen de reseñas|reseñas|otras personas|resultados web|saca el máximo|horas punta)", re.IGNORECASE
)
_DESCRIPTION_HEADINGS = re.compile(r"^(?:del propietario|descripción|acerca de)", re.IGNORECASE)
_SERVICE_HEADINGS = re.compile(r"servicio", re.IGNORECASE)


def _clean(text: str | None) -> str | None:
    value = " ".join((text or "").split())
    return value or None


def _label_value(node: LexborNode | None) -> str | None:
    """Valor de un botón de la ficha: "Dirección: Venus 2797, …" → "Venus 2797, …"."""
    if node is None:
        return None
    label = node.attributes.get("aria-label") or node.text(separator=" ") or ""
    return _clean(_LABEL_PREFIX.sub("", label))


def _header_block(h1: LexborNode) -> LexborNode:
    """Bloque del encabezado (nombre, rating, categoría), sin las secciones de abajo."""
    node = h1
    for _ in range(6):
        parent = node.parent
        if parent is None or not parent.is_element_node:
            break
        node = parent
        if node.css_first('[aria-label*="estrellas"], [aria-label*="reseña"], button[jsaction*="category"]'):
            return node
    return h1.parent or h1


def _first_label(root: LexborNode, pattern: re.Pattern[str]) -> re.Match[str] | None:
    for node in root.css("[aria-label]"):
        match = pattern.match(node.attributes.get("aria-label") or "")
        if match:
            return match
    return None


def parse_hours(root: LexborNode) -> dict[str, str]:
    """{"lunes": "De 8 a. m. a 8:30 p. m.", …} desde las etiquetas "lunes, …, Copiar el horario"."""
    found: dict[str, str] = {}
    for node in root.css("[aria-label]"):
        match = _HOURS_LABEL.match(node.attributes.get("aria-label") or "")
        if match:
            day = next(d for d in _DAYS if fold_accents(d) == fold_accents(match.group(1).lower()))
            found.setdefault(day, _clean(match.group(2)) or "")
    return {day: found[day] for day in _DAYS if day in found}


_POSTAL_CODE = re.compile(r"^(?:[A-Z]?\d{4,7}[A-Z]{0,3})\s+")
_NOT_LOCALITY = re.compile(
    # Regiones de Chile
    r"\bregi[oó]n\b|metropolitana|o'?higgins|valpara[ií]so|b[ií]o\s?b[ií]o|\bmaule\b|araucan[ií]a|los lagos|los r[ií]os|"
    r"coquimbo|antofagasta|atacama|tarapac[aá]|arica|ñuble|ays[eé]n|magallanes|^provincia de|"
    # Países (Maps los agrega al final de la dirección)
    r"^(?:chile|argentina|per[uú]|colombia|m[eé]xico|espa[ñn]a|spain|uruguay|paraguay|bolivia|ecuador|venezuela|"
    r"brasil|brazil|panam[aá]|costa rica|guatemala|honduras|el salvador|nicaragua|cuba|puerto rico|"
    r"rep[uú]blica dominicana|canad[aá]|canada|reino unido|united kingdom|uk|francia|france|italia|italy|"
    r"alemania|germany|portugal|estados unidos|united states|usa|ee\.?\s?uu\.?|eeuu)$",
    re.IGNORECASE,
)


def parse_address(address: str | None, communes: CommuneCatalog | None = None) -> tuple[str | None, str | None]:
    """(ciudad, comuna) desde la dirección de Maps. Si no se puede saber, None.

    "Venus 2797, 2830680 Rancagua, O'Higgins" → ("Rancagua", "Rancagua").
    """
    parts = [part.strip() for part in (address or "").split(",") if part.strip()]
    locality = None
    for part in reversed(parts[1:]):  # la primera parte siempre es la calle
        candidate = _POSTAL_CODE.sub("", part).strip()
        if not candidate or re.search(r"\d", candidate) or _NOT_LOCALITY.search(candidate):
            continue
        locality = candidate
        break
    commune = None
    if locality and communes:
        by_folded = {fold_accents(name).lower(): name for name in communes.names}
        commune = by_folded.get(fold_accents(locality).lower())
    return locality, commune


def parse_about(html: str | None) -> dict[str, list[str]]:
    """Secciones de la pestaña "Información": {título: [ítems tal cual]}."""
    if not html:
        return {}
    tree = LexborHTMLParser(html)
    sections: dict[str, list[str]] = {}
    for heading in tree.css("h2"):
        title = _clean(heading.text(separator=" "))
        if not title or _NOT_ABOUT.match(title) or heading.parent is None:
            continue
        container = heading.parent
        items = [text for li in container.css("li") if (text := _clean(li.attributes.get("aria-label") or li.text(separator=" ")))]
        if not items:
            body = _clean(container.text(separator=" "))
            if body and body != title:
                items = [_clean(body.removeprefix(title)) or body]
        if items:
            sections[title] = list(dict.fromkeys(items))
    return sections


def _business_html(main: LexborNode) -> str:
    """Solo las partes de la ficha que publica el negocio: datos y acciones.

    Se dejan fuera las reseñas y "Otras personas también buscan", que traen datos de
    otras personas u otros negocios.
    """
    regions = [
        node
        for node in main.css('[role="region"][aria-label]')
        if re.match(r"^(?:Información de|Acciones para|Information for|Actions for)\b", node.attributes["aria-label"])
    ]
    if regions:
        return "".join(node.html or "" for node in regions)
    return "".join(node.html or "" for node in main.css("[data-item-id]"))


def parse_place(
    html: str,
    *,
    url: str,
    region: str = "CL",
    communes: CommuneCatalog | None = None,
    about_html: str | None = None,
) -> MapsPlace:
    """Datos de la ficha. Lanza ValueError si el HTML no es una ficha (no tiene nombre)."""
    tree = LexborHTMLParser(html or "")
    main = tree.css_first('div[role="main"]') or tree.body or tree.root
    h1 = main.css_first("h1")
    name = _clean(h1.text(separator=" ")) if h1 else None
    if h1 is None or not name:
        raise ValueError("La página no es una ficha de Maps: no tiene nombre.")

    header = _header_block(h1)
    rating_match = _first_label(header, _RATING)
    reviews_match = _first_label(header, _REVIEWS)
    category_node = header.css_first('button[jsaction*="category"]')

    address = _label_value(main.css_first('[data-item-id="address"]'))
    city, commune = parse_address(address, communes)

    phone_node = main.css_first('[data-item-id^="phone:tel:"]')
    phone_raw = phone_e164 = None
    if phone_node is not None:
        phone_raw = _label_value(phone_node)
        phone = normalize_phone(phone_node.attributes["data-item-id"].removeprefix("phone:tel:"), region) or (
            normalize_phone(phone_raw, region)
        )
        phone_e164 = phone.e164 if phone else None

    website_node = main.css_first('a[data-item-id="authority"]')
    website_raw = (website_node.attributes.get("href") or "").strip() or None if website_node else None
    website = website_kind = None
    if website_raw:
        website_kind = classify_link(website_raw)
        website = normalize_url(unwrap_google_redirect(website_raw)) if website_kind != LinkKind.WHATSAPP else None

    business_html = _business_html(main)
    business_tree = LexborHTMLParser(business_html)
    links: dict[str, None] = {}
    for node in business_tree.css("a[href]"):
        href = unwrap_google_redirect((node.attributes.get("href") or "").strip())
        if href and not href.startswith("#") and not _GOOGLE_HELP_HOSTS.match(href):
            links.setdefault(href, None)

    about = parse_about(about_html)
    description = next(
        (" ".join(items) for title, items in about.items() if _DESCRIPTION_HEADINGS.match(title)), None
    )
    services = [item for title, items in about.items() if _SERVICE_HEADINGS.search(title) for item in items]

    return MapsPlace(
        maps_url=canonical_maps_url(url),
        place_key=maps_place_key(url),
        name=name,
        category=_clean(category_node.text(separator=" ")) if category_node else None,
        address=address,
        city=city,
        commune=commune,
        phone_raw=phone_raw,
        phone_e164=phone_e164,
        website_raw=website_raw,
        website=website,
        website_kind=website_kind,
        socials=extract_socials(links),
        whatsapp=extract_whatsapp(business_html, source=WhatsAppSource.MAPS, page_url=url, region=region),
        rating=float(rating_match.group(1).replace(",", ".")) if rating_match else None,
        review_count=int(re.sub(r"\D", "", reviews_match.group(1))) if reviews_match else None,
        opening_hours=parse_hours(main),
        description=description,
        services=list(dict.fromkeys(services)),
        about=about,
        links=list(links),
    )
