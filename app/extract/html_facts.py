"""Hechos deterministas de una página (sección 8.1), leídos del HTML sin navegador ni IA.

Lo que necesita un navegador o la red (tiempos de carga, capturas, scroll horizontal,
enlaces e imágenes rotas, PageSpeed) se mide en la Fase 3.
"""

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, Field
from selectolax.lexbor import LexborHTMLParser, LexborNode

from app.extract.links import LinkKind, classify_link, normalize_url, unwrap_google_redirect
from app.extract.phones import find_phones, phone_from_tel_href
from app.extract.socials import extract_socials
from app.extract.whatsapp import WhatsAppExtraction, extract_whatsapp
from app.models import WhatsAppSource

# --- Modelos ------------------------------------------------------------------------


class BuilderHint(BaseModel):
    name: str
    evidence: str


class TrustSignal(BaseModel):
    kind: str  # testimonios, reseñas, experiencia, certificaciones, garantía
    snippet: str


class HtmlFacts(BaseModel):
    page_url: str | None = None
    title: str | None = None
    meta_description: str | None = None
    lang: str | None = None
    viewport: str | None = None
    favicon: str | None = None
    canonical: str | None = None
    h1: list[str] = Field(default_factory=list)
    h2: list[str] = Field(default_factory=list)
    open_graph: dict[str, str] = Field(default_factory=dict)
    json_ld_types: list[str] = Field(default_factory=list)
    local_business: dict[str, Any] | None = None
    phones: list[str] = Field(default_factory=list)  # E.164: enlaces tel: y texto visible
    tel_links: list[str] = Field(default_factory=list)  # href tal cual
    emails: list[str] = Field(default_factory=list)
    forms: int = 0
    whatsapp: WhatsAppExtraction
    whatsapp_floating: bool = False
    socials: dict[str, list[str]] = Field(default_factory=dict)
    menu_items: list[str] = Field(default_factory=list)
    internal_links: list[str] = Field(default_factory=list)
    images: int = 0
    images_without_alt: int = 0
    has_map_embed: bool = False
    maps_links: list[str] = Field(default_factory=list)
    generator: str | None = None
    builders: list[BuilderHint] = Field(default_factory=list)
    copyright_years: list[int] = Field(default_factory=list)
    communes_mentioned: list[str] = Field(default_factory=list)
    trust_signals: list[TrustSignal] = Field(default_factory=list)
    visible_text: str = ""


# --- Texto visible ------------------------------------------------------------------

_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg", "head", "iframe", "object", "canvas"})
_BLOCK_TAGS = frozenset(
    {
        "address", "article", "aside", "blockquote", "br", "button", "dd", "div", "dl", "dt",
        "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6",
        "header", "hr", "label", "li", "main", "nav", "ol", "option", "p", "pre", "section",
        "table", "td", "th", "tr", "ul",
    }
)


def visible_text(tree: LexborHTMLParser) -> str:
    """Texto que ve el visitante, una línea por bloque (párrafo, ítem, título…)."""
    parts: list[str] = []
    stack: list[LexborNode | str] = [tree.body or tree.root]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            parts.append(item)
        elif item.is_text_node:
            parts.append(item.text_content or "")
        elif item.is_element_node and item.tag not in _SKIP_TAGS:
            if item.tag in _BLOCK_TAGS:
                parts.append("\n")
                stack.append("\n")
            stack.extend(reversed(list(item.iter(include_text=True))))
    lines = (" ".join(line.split()) for line in "".join(parts).split("\n"))
    return "\n".join(line for line in lines if line)


# --- Comunas ------------------------------------------------------------------------


class CommuneCatalog(BaseModel):
    names: list[str]
    ambiguous: frozenset[str] = frozenset()


@lru_cache
def load_communes(path: Path) -> CommuneCatalog:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    names = [name for region in data["regiones"].values() for name in region]
    return CommuneCatalog(names=list(dict.fromkeys(names)), ambiguous=frozenset(data.get("ambiguas", [])))


def fold_accents(text: str) -> str:
    """Sin tildes ni ñ (Ñuñoa → Nunoa), conservando mayúsculas."""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


_COMMUNE_CUE = re.compile(r"\b(?:comunas?|sector(?:es)?|cobertura|atendemos)\b", re.IGNORECASE)


@lru_cache(maxsize=8)
def _commune_patterns(catalog_key: tuple[tuple[str, ...], frozenset[str]]) -> dict[str, re.Pattern[str]]:
    names, ambiguous = catalog_key
    patterns = {}
    for name in names:
        folded = re.escape(fold_accents(name))
        if name in ambiguous:
            # Solo con mayúscula inicial o todo en mayúsculas: "Colina", "COLINA"; no "colina".
            patterns[name] = re.compile(rf"(?<!\w)(?:{folded}|{folded.upper()})(?!\w)")
        else:
            patterns[name] = re.compile(rf"(?<!\w){folded}(?!\w)", re.IGNORECASE)
    return patterns


def find_communes(text: str, catalog: CommuneCatalog) -> list[str]:
    """Comunas mencionadas, en orden de aparición. Las ambiguas necesitan contexto en su línea."""
    patterns = _commune_patterns((tuple(catalog.names), catalog.ambiguous))
    found: dict[str, tuple[int, int]] = {}
    for line_number, line in enumerate(fold_accents(text).split("\n")):
        hits = {name: match.start() for name, pattern in patterns.items() if (match := pattern.search(line))}
        if not hits:
            continue
        has_context = any(name not in catalog.ambiguous for name in hits) or bool(_COMMUNE_CUE.search(line))
        for name, position in hits.items():
            if name in catalog.ambiguous and not has_context:
                continue
            found.setdefault(name, (line_number, position))
    return sorted(found, key=found.__getitem__)


# --- Señales de confianza, antigüedad y plantillas -------------------------------------

_TRUST_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("testimonios", re.compile(r"\btestimonios?\b|\blo que dicen\b|\bnuestros clientes opinan\b|\bopiniones de (?:nuestros )?clientes\b", re.IGNORECASE)),
    ("reseñas", re.compile(r"\brese[ñn]as?\b|\breviews?\b|\bcalificaci[oó]n(?:es)?\b|★", re.IGNORECASE)),
    ("experiencia", re.compile(r"\b\d{1,3}\+?\s*años de (?:experiencia|trayectoria)\b|\bm[aá]s de \d{1,3} años\b|\bdesde (?:el (?:año )?)?(?:19|20)\d{2}\b", re.IGNORECASE)),
    ("certificaciones", re.compile(r"\bSEC\b|\b[Cc]ertificad[oa]s?\b|\b[Cc]ertificaci[oó]n(?:es)?\b|\bISO \d{3,5}\b|\b[Aa]utorizad[oa]s? (?:por (?:la )?)?SEC\b")),
    ("garantía", re.compile(r"\bgarant[ií]as?\b|\bgarantizad[oa]s?\b", re.IGNORECASE)),
)
_MAX_SNIPPETS_PER_SIGNAL = 3

_COPYRIGHT = re.compile(
    r"(?:©|\(c\)|&copy;|copyright)\s*(?:(?:19|20)\d{2}\s*[-–—]\s*)?((?:19|20)\d{2})"
    r"|((?:19|20)\d{2})[^\n]{0,40}derechos reservados"
    r"|derechos reservados[^\n]{0,40}?((?:19|20)\d{2})",
    re.IGNORECASE,
)

# (nombre, patrón sobre el HTML en minúsculas)
_BUILDERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("WordPress", re.compile(r"wp-content/|wp-includes/")),
    ("Elementor", re.compile(r"elementor")),
    ("Divi", re.compile(r"et_pb_|/themes/divi")),
    ("Wix", re.compile(r"static\.wixstatic\.com|wix\.com website builder|parastorage\.com")),
    ("Shopify", re.compile(r"cdn\.shopify\.com|shopify\.theme")),
    ("PrestaShop", re.compile(r"prestashop")),
    ("Squarespace", re.compile(r"static1\.squarespace\.com")),
    ("Webflow", re.compile(r"data-wf-page|webflow\.(?:com|io)")),
    ("Jimdo", re.compile(r"jimdo")),
    ("GoDaddy Website Builder", re.compile(r"img1\.wsimg\.com")),
    ("Google Sites", re.compile(r"sites\.google\.com/|gstatic\.com/atari")),
    ("Tokko Broker", re.compile(r"tokkobroker")),
    ("BuscadorProp", re.compile(r"buscadorprop")),
    ("Houzez", re.compile(r"houzez")),
    ("Lovable", re.compile(r"lovable\.(?:app|dev)|gpteng\.co|content=\"lovable")),
    ("Joomla", re.compile(r"/media/jui/|/media/system/js/|joomla!")),
    ("Blogger", re.compile(r"blogger\.com/static|\.blogspot\.com")),
)
_WP_THEME = re.compile(r"wp-content/themes/([a-z0-9_-]+)/")

# Constructores que se reconocen por el dominio donde está publicada la web
_BUILDER_HOSTS: tuple[tuple[str, str], ...] = (
    ("wixsite.com", "Wix"),
    ("manus.space", "Manus"),
    ("lovable.app", "Lovable"),
    ("webflow.io", "Webflow"),
    ("squarespace.com", "Squarespace"),
    ("business.site", "Google Business Site"),
    ("negocio.site", "Google Business Site"),
    ("sites.google.com", "Google Sites"),
    ("blogspot.com", "Blogger"),
    ("wordpress.com", "WordPress.com"),
    ("godaddysites.com", "GoDaddy Website Builder"),
    ("jimdosite.com", "Jimdo"),
    ("mitiendanube.com", "Tiendanube"),
    ("myshopify.com", "Shopify"),
    ("vercel.app", "Vercel"),
    ("netlify.app", "Netlify"),
)

_LOCAL_BUSINESS_TYPES = frozenset(
    {
        "LocalBusiness", "HomeAndConstructionBusiness", "Locksmith", "Plumber", "Electrician",
        "HVACBusiness", "RoofingContractor", "GeneralContractor", "HousePainter", "MovingCompany",
        "ProfessionalService", "AutomotiveBusiness", "AutoRepair", "Store", "HealthAndBeautyBusiness",
        "MedicalBusiness", "Dentist", "LegalService", "RealEstateAgent", "FoodEstablishment",
        "Restaurant", "LodgingBusiness", "EmergencyService", "FinancialService", "ChildCare",
        "DryCleaningOrLaundry", "TravelAgency", "SelfStorage",
    }
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def _snippet(line: str, match: re.Match[str], width: int = 160) -> str:
    if len(line) <= width:
        return line
    start = max(0, match.start() - width // 2)
    return ("…" if start else "") + line[start : start + width].strip() + "…"


def find_trust_signals(text: str) -> list[TrustSignal]:
    signals: list[TrustSignal] = []
    for kind, pattern in _TRUST_PATTERNS:
        count = 0
        for line in text.split("\n"):
            match = pattern.search(line)
            if match:
                signals.append(TrustSignal(kind=kind, snippet=_snippet(line, match)))
                count += 1
                if count == _MAX_SNIPPETS_PER_SIGNAL:
                    break
    return signals


def find_copyright_years(text: str) -> list[int]:
    years = {int(year) for match in _COPYRIGHT.finditer(text) for year in match.groups() if year}
    return sorted(years)


def detect_builders(html: str, page_url: str | None = None) -> list[BuilderHint]:
    lowered = html.lower()
    hints = []
    host = (urlsplit(page_url or "").hostname or "").lower()
    for domain, name in _BUILDER_HOSTS:
        if host == domain or host.endswith("." + domain):
            hints.append(BuilderHint(name=name, evidence=host))
    for name, pattern in _BUILDERS:
        match = pattern.search(lowered)
        if match and all(hint.name != name for hint in hints):
            hints.append(BuilderHint(name=name, evidence=match.group(0)))
    theme = _WP_THEME.search(lowered)
    if theme:
        hints.append(BuilderHint(name=f"Tema WordPress: {theme.group(1)}", evidence=theme.group(0)))
    return hints


# --- Extracción principal -----------------------------------------------------------


def _text(node: LexborNode | None) -> str | None:
    if node is None:
        return None
    value = " ".join((node.text(separator=" ") or "").split())
    return value or None


def _json_ld_objects(tree: LexborHTMLParser) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    for script in tree.css("script"):
        if (script.attributes.get("type") or "").strip().lower() != "application/ld+json":
            continue
        try:
            data = json.loads(script.text() or "")
        except ValueError:
            continue
        stack = [data]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(reversed(item))
            elif isinstance(item, dict):
                if "@graph" in item:
                    stack.extend(reversed(item["@graph"] if isinstance(item["@graph"], list) else [item["@graph"]]))
                if "@type" in item:
                    objects.append(item)
    return objects


def _types_of(item: dict[str, Any]) -> list[str]:
    value = item.get("@type")
    return [str(t) for t in (value if isinstance(value, list) else [value]) if t]


def _same_site(url: str, page_host: str) -> bool:
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    return host == page_host


def extract_html_facts(
    html: str,
    *,
    page_url: str | None = None,
    region: str = "CL",
    communes: CommuneCatalog | None = None,
) -> HtmlFacts:
    """Hechos de la página: SEO básico, contacto, WhatsApp, redes, plantilla, confianza, comunas."""
    html = html or ""
    tree = LexborHTMLParser(html)
    text = visible_text(tree)
    page_host = (urlsplit(page_url or "").hostname or "").lower().removeprefix("www.")

    # <head>
    meta: dict[str, str] = {}
    for node in tree.css("meta"):
        key = (node.attributes.get("name") or node.attributes.get("property") or "").strip().lower()
        content = (node.attributes.get("content") or "").strip()
        if key and content:
            meta.setdefault(key, content)
    link_rels: dict[str, str] = {}
    for node in tree.css("link[rel][href]"):
        for rel in (node.attributes.get("rel") or "").lower().split():
            link_rels.setdefault(rel, node.attributes.get("href") or "")
    html_node = tree.css_first("html")
    favicon = link_rels.get("icon") or link_rels.get("shortcut") or link_rels.get("apple-touch-icon")

    # Datos estructurados
    json_ld = _json_ld_objects(tree)
    json_ld_types = list(dict.fromkeys(t for item in json_ld for t in _types_of(item)))
    local_business = next(
        (
            item
            for item in json_ld
            if any(t in _LOCAL_BUSINESS_TYPES or t.endswith("Business") for t in _types_of(item))
        ),
        None,
    )

    # Enlaces
    tel_links: list[str] = []
    emails: dict[str, None] = {}
    absolute_links: list[str] = []
    maps_links: dict[str, None] = {}
    internal_links: dict[str, None] = {}
    for node in tree.css("a[href]"):
        href = (node.attributes.get("href") or "").strip()
        lowered = href.lower()
        if lowered.startswith(("tel:", "callto:")):
            tel_links.append(href)
        elif lowered.startswith("mailto:"):
            address = href[7:].split("?")[0].strip().lower()
            if address:
                emails.setdefault(address, None)
        else:
            absolute = normalize_url(unwrap_google_redirect(href), page_url)
            if not absolute:
                continue
            absolute_links.append(absolute)
            if classify_link(absolute) == LinkKind.GOOGLE:
                maps_links.setdefault(absolute, None)
            elif page_host and _same_site(absolute, page_host):
                internal_links.setdefault(absolute.split("#")[0], None)
    for address in _EMAIL.findall(text):
        emails.setdefault(address.lower(), None)

    phones: dict[str, None] = {}
    for href in tel_links:
        phone = phone_from_tel_href(href, region)
        if phone:
            phones.setdefault(phone.e164, None)
    for phone in find_phones(text, region):
        phones.setdefault(phone.e164, None)

    # Menú: enlaces del primer <nav>, o del <header> si no hay <nav>
    menu_root = tree.css_first("nav") or tree.css_first("header")
    menu_items = list(
        dict.fromkeys(label for node in (menu_root.css("a") if menu_root else []) if (label := _text(node)))
    )[:40]

    images = tree.css("img")
    whatsapp = extract_whatsapp(html, source=WhatsAppSource.WEB, page_url=page_url, region=region)

    return HtmlFacts(
        page_url=page_url,
        title=_text(tree.css_first("title")),
        meta_description=meta.get("description"),
        lang=(html_node.attributes.get("lang") or None) if html_node else None,
        viewport=meta.get("viewport"),
        favicon=normalize_url(favicon, page_url) if favicon else None,
        canonical=normalize_url(link_rels["canonical"], page_url) if link_rels.get("canonical") else None,
        h1=[t for node in tree.css("h1") if (t := _text(node))],
        h2=[t for node in tree.css("h2") if (t := _text(node))][:30],
        open_graph={key: value for key, value in meta.items() if key.startswith("og:")},
        json_ld_types=json_ld_types,
        local_business=local_business,
        phones=list(phones),
        tel_links=tel_links,
        emails=list(emails),
        forms=len(tree.css("form")),
        whatsapp=whatsapp,
        whatsapp_floating=whatsapp.floating_button,
        socials=extract_socials(absolute_links),
        menu_items=menu_items,
        internal_links=list(internal_links)[:300],
        images=len(images),
        images_without_alt=sum(1 for img in images if not (img.attributes.get("alt") or "").strip()),
        has_map_embed=any(
            re.search(r"google\.[a-z.]+/maps|maps\.google\.", node.attributes.get("src") or "")
            for node in tree.css("iframe")
        ),
        maps_links=list(maps_links),
        generator=meta.get("generator"),
        builders=detect_builders(html, page_url),
        copyright_years=find_copyright_years(text),
        communes_mentioned=find_communes(text, communes) if communes else [],
        trust_signals=find_trust_signals(text),
        visible_text=text,
    )
