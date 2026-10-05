"""WhatsApp (sección 7): detecta enlaces explícitos del negocio con regex sobre el HTML.

Reglas:
- Solo cuenta un enlace explícito de WhatsApp o el número configurado en un plugin de
  WhatsApp. Nunca se construye un wa.me a partir de un teléfono.
- `chat.whatsapp.com` (invitaciones a grupos) se ignora.
- Los acortadores (wa.link…) solo cuentan si su redirección termina en WhatsApp.
- Cada candidato guarda el enlace original tal cual como evidencia.
"""

import html as html_lib
import re
from typing import Any, NamedTuple
from collections.abc import Iterable, Iterator
from urllib.parse import unquote, urljoin

import httpx
from pydantic import BaseModel, Field
from selectolax.lexbor import LexborHTMLParser, LexborNode

from app.extract.phones import canonical_number, normalize_phone
from app.models import (
    WhatsAppCandidate,
    WhatsAppConfidence,
    WhatsAppPlacement,
    WhatsAppSource,
    WhatsAppStatus,
)

# --- Patrones ---------------------------------------------------------------------

_START = r"(?<![\w.-])(?:(?:https?:)?//)?"
_PHONE_PARAM = r"[^\s\"'<>]*?(?<=[?&])phone=(?P<n>[^&\s\"'<>#]*)"
_NUMBER_LINKS = (
    re.compile(_START + r"(?:www\.)?wa\.me/(?!message/)(?:c/)?(?P<n>\+?[\d%().-]+)", re.IGNORECASE),
    re.compile(_START + r"(?:api|web)\.whatsapp\.com/send/?\?" + _PHONE_PARAM, re.IGNORECASE),
    re.compile(r"(?<![\w.-])whatsapp://send/?\?" + _PHONE_PARAM, re.IGNORECASE),
)
_MESSAGE_LINK = re.compile(_START + r"(?:www\.)?wa\.me/message/[A-Za-z0-9]+", re.IGNORECASE)
_SHORTLINK = re.compile(_START + r"(?:www\.)?(?:wa\.link|walink\.co)/[A-Za-z0-9_-]+", re.IGNORECASE)
_GROUP_INVITE = re.compile(_START + r"chat\.whatsapp\.com/[A-Za-z0-9_-]+", re.IGNORECASE)
_URL_TAIL = re.compile(r"[^\s\"'<>`\\]*")

# Número dentro de la configuración de un plugin: {"telephone":"569…"}, whatsapp: "+569…"
_PLUGIN_NUMBER = re.compile(
    r"[\"']?\b(?:telephone|phone|phone_?number|number|numero|whatsapp|whatsapp_?number|wa_?number)\b[\"']?"
    r"\s*[:=]\s*[\"'](?P<n>\+?\d[\d\s().-]{5,22}\d)[\"']",
    re.IGNORECASE,
)
_PLUGIN_DATA_ATTR = re.compile(r"^data-(?:number|phone|telephone|whatsapp|wa-?number|whatsapp-number)$", re.IGNORECASE)
_WA_CONTEXT = re.compile(r"whatsapp|joinchat|\bwa[-_]|ht[-_]ctc|click.?to.?chat|getbutton|\bwpp", re.IGNORECASE)
_FLOATING_SCRIPT = re.compile(r"joinchat|ht[-_]ctc|getbutton|float|flotante|whatsapp[-_]?widget|wa[-_]widget", re.IGNORECASE)

# Ubicación en la página
_FLOATING_TOKEN = re.compile(r"float|flotante|fixed|sticky|joinchat|ht-ctc|wh-widget|getbutton", re.IGNORECASE)
_FIXED_STYLE = re.compile(r"position\s*:\s*fixed", re.IGNORECASE)
_HEADER_TOKENS = frozenset(
    {"header", "site-header", "main-header", "navbar", "topbar", "top-bar", "masthead", "elementor-location-header"}
)
_FOOTER_TOKENS = frozenset(
    {"footer", "site-footer", "main-footer", "pie", "pie-de-pagina", "elementor-location-footer"}
)

_PLACEMENT_RANK = {
    WhatsAppPlacement.FLOTANTE: 0,
    WhatsAppPlacement.HEADER: 0,
    WhatsAppPlacement.CUERPO: 1,
    WhatsAppPlacement.FOOTER: 2,
}
_SOURCE_RANK = {
    WhatsAppSource.MANUAL: -1,  # confirmado por Joaquín al abrir el chat
    WhatsAppSource.MAPS: 0,
    WhatsAppSource.WEB: 1,
    WhatsAppSource.LINKTREE: 2,
    WhatsAppSource.OTRA: 3,
}
_MAX_EVIDENCE = 500
_CONTEXT_WINDOW = 300  # caracteres alrededor de un número de plugin donde debe aparecer "whatsapp"


# --- Resultados ---------------------------------------------------------------------


class ShortLink(BaseModel):
    """Acortador (wa.link…) pendiente de seguir con httpx."""

    url: str
    placement: WhatsAppPlacement
    evidence: str


class IgnoredLink(BaseModel):
    evidence: str
    reason: str
    # Es un botón de WhatsApp del negocio que no funciona (número inválido, acortador caído).
    # Una invitación a un grupo se ignora, pero no es un botón roto.
    broken: bool = False


class WhatsAppExtraction(BaseModel):
    source: WhatsAppSource
    page_url: str | None = None
    # Hay un botón flotante de WhatsApp, aunque su número ya aparezca en otra parte.
    floating_button: bool = False
    candidates: list[WhatsAppCandidate] = Field(default_factory=list)
    shortlinks: list[ShortLink] = Field(default_factory=list)
    ignored: list[IgnoredLink] = Field(default_factory=list)

    def broken_evidence(self) -> list[str]:
        """Botones de WhatsApp publicados que no funcionan, con su enlace original."""
        found = [c.evidence for c in self.candidates if c.broken]
        return found + [i.evidence for i in self.ignored if i.broken]


class WhatsAppDecision(BaseModel):
    """Lo que se guarda en el negocio: estado, enlace principal, confianza y todos los candidatos."""

    status: WhatsAppStatus
    url: str | None = None
    source: WhatsAppSource | None = None
    evidence: str | None = None
    candidates: list[WhatsAppCandidate] = Field(default_factory=list)
    multiple_numbers: bool = False
    confidence: WhatsAppConfidence | None = None
    # Por qué tiene esa confianza, en palabras simples.
    confidence_reason: str | None = None
    broken_button: bool = False
    broken_evidence: list[str] = Field(default_factory=list)

    def business_fields(self, *, complete: bool = False) -> dict[str, Any]:
        """Columnas de `businesses` que salen de esta decisión.

        `complete=False` (una sola fuente, por ejemplo Maps): solo lo que aporta algo, para
        no borrar lo que otra fuente ya encontró. `complete=True` (decisión con todos los
        candidatos combinados): todas las columnas, incluso vacías.
        """
        fields: dict[str, Any] = {
            "whatsapp_status": self.status,
            "whatsapp_url": self.url,
            "whatsapp_source": self.source,
            "whatsapp_evidence": self.evidence,
            "whatsapp_candidates": [c.model_dump(mode="json") for c in self.candidates],
            "whatsapp_multiple_numbers": self.multiple_numbers,
            "whatsapp_confidence": self.confidence,
            "whatsapp_broken_button": self.broken_button,
        }
        if complete:
            return fields
        partial = {k: v for k, v in fields.items() if v not in (None, [], False)}
        if self.status == WhatsAppStatus.NO_ENCONTRADO:
            partial.pop("whatsapp_status", None)
        return partial


# --- Normalización ---------------------------------------------------------------------


class NormalizedNumber(NamedTuple):
    number: str  # código de país + número, solo dígitos
    note: str | None
    broken: bool  # tal como está escrito, el enlace no abre el chat


def normalize_whatsapp_number(raw: str, region: str = "CL") -> NormalizedNumber | None:
    """Número de WhatsApp normalizado, o None si no es válido.

    Si el enlace no trae código de país, se completa con la región por defecto, se deja
    una nota y se marca como roto: tal como está escrito, ese enlace no abre el chat.
    """
    digits = re.sub(r"\D", "", unquote(raw or ""))
    if len(digits) < 7:
        return None
    phone = normalize_phone("+" + digits, region)
    if phone and canonical_number(digits) == phone.digits and digits != phone.digits:
        # Celular mexicano con el "1" antiguo (521…): WhatsApp lo usa así, se conserva tal cual.
        return NormalizedNumber(digits, None, False)
    if phone:
        note = None if phone.digits == digits else f"El enlace usa {digits}; el formato correcto para WhatsApp es {phone.digits}."
        return NormalizedNumber(phone.digits, note, False)
    phone = normalize_phone(digits, region)
    if phone:
        note = (
            f"El número del enlace no trae código de país ({digits}); se completó con el de {region.upper()}. "
            "Tal como está en la web, ese botón no abre el chat correcto."
        )
        return NormalizedNumber(phone.digits, note, True)
    return None


def _scan_variants(text: str) -> list[str]:
    """El texto tal cual, con entidades HTML y barras escapadas de JSON resueltas, y decodificado."""
    text = html_lib.unescape(text).replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")
    variants = [text]
    if "%" in text:
        decoded = unquote(text)
        if decoded != text:
            variants.append(decoded)
    return variants


def _full_url(text: str, match: re.Match[str]) -> str:
    tail = _URL_TAIL.match(text, match.end())
    return (match.group(0) + (tail.group(0) if tail else ""))[:_MAX_EVIDENCE]


def _links_in(text: str) -> Iterator[tuple[str, str | None, str]]:
    """(tipo, número en bruto, enlace completo) de cada enlace de WhatsApp en un texto."""
    for variant in _scan_variants(text):
        for pattern in _NUMBER_LINKS:
            for match in pattern.finditer(variant):
                yield "number", match.group("n"), _full_url(variant, match)
        for kind, pattern in (("message", _MESSAGE_LINK), ("shortlink", _SHORTLINK), ("group", _GROUP_INVITE)):
            for match in pattern.finditer(variant):
                yield kind, None, _full_url(variant, match)


def _https(url: str) -> str:
    url = url.strip()
    if url.startswith("//"):
        return "https:" + url
    return url if re.match(r"^https?://", url, re.IGNORECASE) else "https://" + url


# --- Ubicación en la página ---------------------------------------------------------------


def _class_tokens(node: LexborNode) -> set[str]:
    attrs = node.attributes or {}
    return {token.lower() for token in f"{attrs.get('class') or ''} {attrs.get('id') or ''}".split()}


def _placement(node: LexborNode | None) -> WhatsAppPlacement:
    """Flotante si algún ancestro lo es; si no, footer; si no, header o menú; si no, cuerpo.

    El footer gana al header: un menú o el título de un widget dentro del footer es footer.
    """
    in_header = in_footer = False
    while node is not None and node.is_element_node:
        attrs = node.attributes or {}
        tokens = _class_tokens(node)
        if _FIXED_STYLE.search(attrs.get("style") or "") or any(_FLOATING_TOKEN.search(t) for t in tokens):
            return WhatsAppPlacement.FLOTANTE
        role = (attrs.get("role") or "").lower()
        if node.tag == "footer" or role == "contentinfo" or tokens & _FOOTER_TOKENS:
            in_footer = True
        elif node.tag in ("header", "nav") or role in ("banner", "navigation") or tokens & _HEADER_TOKENS:
            in_header = True
        node = node.parent
    if in_footer:
        return WhatsAppPlacement.FOOTER
    return WhatsAppPlacement.HEADER if in_header else WhatsAppPlacement.CUERPO


def _has_wa_context(node: LexborNode) -> bool:
    attrs = node.attributes or {}
    return bool(_WA_CONTEXT.search(" ".join([*_class_tokens(node), *attrs.keys()])))


# --- Extracción ---------------------------------------------------------------------------


class _Collector:
    def __init__(self, source: WhatsAppSource, page_url: str | None, region: str) -> None:
        self.source = source
        self.page_url = page_url
        self.region = region
        self.candidates: dict[str, WhatsAppCandidate] = {}
        self.shortlinks: dict[str, ShortLink] = {}
        self.ignored: dict[str, IgnoredLink] = {}
        self.floating_button = False
        # En la pasada de respaldo solo se agregan enlaces nuevos: su ubicación es una suposición.
        self.only_new = False

    def _keep(self, key: str, candidate: WhatsAppCandidate) -> None:
        current = self.candidates.get(key)
        if current is not None and self.only_new:
            return
        self.floating_button = self.floating_button or candidate.placement == WhatsAppPlacement.FLOTANTE
        if current is None or _PLACEMENT_RANK[candidate.placement] < _PLACEMENT_RANK[current.placement]:
            self.candidates[key] = candidate

    def add_number(self, raw: str, placement: WhatsAppPlacement, evidence: str) -> None:
        normalized = normalize_whatsapp_number(raw, self.region)
        if normalized is None:
            self.ignored.setdefault(
                evidence, IgnoredLink(evidence=evidence, reason="Número de WhatsApp no válido", broken=True)
            )
            return
        self._keep(
            normalized.number,
            WhatsAppCandidate(
                number=normalized.number,
                url=f"https://wa.me/{normalized.number}",
                source=self.source,
                placement=placement,
                evidence=evidence[:_MAX_EVIDENCE],
                page_url=self.page_url,
                note=normalized.note,
                broken=normalized.broken,
            ),
        )

    def scan(self, text: str, placement: WhatsAppPlacement, evidence: str | None = None) -> None:
        """Busca enlaces de WhatsApp. `evidence`: el atributo exacto, si viene de uno."""
        for kind, number, link in _links_in(text):
            proof = evidence or link
            if kind == "number":
                if number is not None:
                    self.add_number(number, placement, proof)
            elif kind == "message":
                url = _https(link).split("?")[0]
                self._keep(
                    url.lower(),
                    WhatsAppCandidate(
                        number=None, url=url, source=self.source, placement=placement,
                        evidence=proof[:_MAX_EVIDENCE], page_url=self.page_url,
                    ),
                )
            elif kind == "shortlink":
                url = _https(link)
                current = self.shortlinks.get(url.lower())
                if current is not None and self.only_new:
                    continue
                self.floating_button = self.floating_button or placement == WhatsAppPlacement.FLOTANTE
                if current is None or _PLACEMENT_RANK[placement] < _PLACEMENT_RANK[current.placement]:
                    self.shortlinks[url.lower()] = ShortLink(url=url, placement=placement, evidence=proof[:_MAX_EVIDENCE])
            else:
                self.ignored.setdefault(
                    link, IgnoredLink(evidence=link, reason="Invitación a un grupo de WhatsApp, no el contacto del negocio")
                )

    def scan_plugin_config(self, text: str, placement: WhatsAppPlacement, *, nearby_context: bool = False) -> None:
        """Números en la configuración de un plugin de WhatsApp.

        `nearby_context`: en scripts largos, la palabra "whatsapp" (o el plugin) debe estar
        cerca del número; así no se toma el teléfono fijo de otra configuración.
        """
        for variant in _scan_variants(text):
            for match in _PLUGIN_NUMBER.finditer(variant):
                if nearby_context:
                    window = variant[max(0, match.start() - _CONTEXT_WINDOW) : match.end() + _CONTEXT_WINDOW]
                    if not _WA_CONTEXT.search(window):
                        continue
                self.add_number(match.group("n"), placement, match.group(0))

    def result(self) -> WhatsAppExtraction:
        ordered = sorted(self.candidates.values(), key=lambda c: (c.number is None, _PLACEMENT_RANK[c.placement]))
        return WhatsAppExtraction(
            source=self.source,
            page_url=self.page_url,
            floating_button=self.floating_button,
            candidates=ordered,
            shortlinks=list(self.shortlinks.values()),
            ignored=list(self.ignored.values()),
        )


def extract_whatsapp(
    html: str,
    *,
    source: WhatsAppSource = WhatsAppSource.WEB,
    page_url: str | None = None,
    region: str = "CL",
) -> WhatsAppExtraction:
    """Todos los WhatsApp explícitos de una página: atributos, scripts, texto y HTML completo."""
    html = html or ""
    tree = LexborHTMLParser(html)
    collector = _Collector(source, page_url, region)

    for node in tree.css("*"):
        attrs = node.attributes or {}
        if node.tag == "script":
            code = node.text() or ""
            is_json_ld = (attrs.get("type") or "").lower() == "application/ld+json"
            floating = not is_json_ld and _FLOATING_SCRIPT.search(code)
            collector.scan(code, WhatsAppPlacement.FLOTANTE if floating else WhatsAppPlacement.CUERPO)
            # El "telephone" de los datos estructurados no es un WhatsApp: solo plugins.
            if not is_json_ld and _WA_CONTEXT.search(code):
                collector.scan_plugin_config(code, WhatsAppPlacement.FLOTANTE, nearby_context=True)
            continue
        if node.tag == "style":
            continue

        placement: WhatsAppPlacement | None = None
        wa_context = _has_wa_context(node)
        for name, value in attrs.items():
            if not value:
                continue
            is_plugin_attr = wa_context and _PLUGIN_DATA_ATTR.match(name)
            if not (is_plugin_attr or wa_context or re.search(r"wa\.|whatsapp", value, re.IGNORECASE)):
                continue
            placement = placement or _placement(node)
            collector.scan(value, placement, evidence=value)
            if is_plugin_attr:
                collector.add_number(value, placement, f'{name}="{value}"')
            elif wa_context and value.lstrip().startswith(("{", "[")):
                collector.scan_plugin_config(value, placement)

        own_text = node.text(deep=False) or ""
        if re.search(r"wa\.|whatsapp", own_text, re.IGNORECASE):
            collector.scan(own_text, placement or _placement(node))

    # Red de seguridad: el HTML completo sin comentarios (un botón comentado no está publicado).
    collector.only_new = True
    collector.scan(re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL), WhatsAppPlacement.CUERPO)
    return collector.result()


# --- Acortadores ------------------------------------------------------------------------

_META_REFRESH = re.compile(r"<meta[^>]+http-equiv=[\"']?refresh[^>]*url=([^\"'>\s]+)", re.IGNORECASE)


async def _follow_to_whatsapp(url: str, client: httpx.AsyncClient, max_hops: int) -> str | None:
    """Sigue redirecciones (Location o meta refresh) hasta un enlace de WhatsApp con número."""
    for _ in range(max_hops):
        try:
            response = await client.get(url, follow_redirects=False)
        except httpx.HTTPError:
            return None
        location = response.headers.get("location")
        if not location and response.status_code == 200:
            refresh = _META_REFRESH.search(response.text[:20000])
            location = html_lib.unescape(refresh.group(1)) if refresh else None
        if not location:
            return None
        url = urljoin(url, location)
        if any(pattern.search(text) for text in _scan_variants(url) for pattern in _NUMBER_LINKS):
            return url
    return None


async def resolve_shortlinks(
    extraction: WhatsAppExtraction,
    client: httpx.AsyncClient,
    *,
    region: str = "CL",
    max_hops: int = 5,
) -> WhatsAppExtraction:
    """Sigue cada acortador. Solo se convierte en candidato si termina en un WhatsApp válido."""
    if not extraction.shortlinks:
        return extraction
    collector = _Collector(extraction.source, extraction.page_url, region)
    collector.floating_button = extraction.floating_button
    for candidate in extraction.candidates:
        collector._keep(candidate.number or candidate.url.lower(), candidate)
    for ignored in extraction.ignored:
        collector.ignored[ignored.evidence] = ignored

    for shortlink in extraction.shortlinks:
        final_url = await _follow_to_whatsapp(shortlink.url, client, max_hops)
        if final_url is None:
            collector.ignored[shortlink.evidence] = IgnoredLink(
                evidence=shortlink.evidence, reason="El acortador no lleva a un WhatsApp válido", broken=True
            )
            continue
        collector.scan(final_url, shortlink.placement, evidence=f"{shortlink.evidence} → {final_url}")

    return collector.result()


def merge_candidates(*groups: Iterable[WhatsAppCandidate]) -> list[WhatsAppCandidate]:
    """Une candidatos de varias páginas o fuentes: uno por (fuente, número), con su mejor ubicación."""
    best: dict[tuple[WhatsAppSource, str], WhatsAppCandidate] = {}
    for group in groups:
        for candidate in group:
            key = (candidate.source, candidate.number or candidate.url.lower())
            current = best.get(key)
            if current is None or _PLACEMENT_RANK[candidate.placement] < _PLACEMENT_RANK[current.placement]:
                best[key] = candidate
    return list(best.values())


# --- Decisión --------------------------------------------------------------------------


_SOURCE_NAMES = {
    WhatsAppSource.MANUAL: "tu confirmación",
    WhatsAppSource.MAPS: "Google Maps",
    WhatsAppSource.WEB: "su web",
    WhatsAppSource.LINKTREE: "su Linktree",
    WhatsAppSource.OTRA: "otra fuente",
}


def _confidence(
    best: WhatsAppCandidate, ordered: list[WhatsAppCandidate], phone_e164: str | None
) -> tuple[WhatsAppConfidence, str]:
    """ALTA si el número aparece en 2 fuentes independientes o coincide con el teléfono de Maps."""
    if best.source == WhatsAppSource.MANUAL:
        return WhatsAppConfidence.ALTA, "Confirmado por ti: abriste el chat y el WhatsApp existe."
    if best.number is None:
        return WhatsAppConfidence.MEDIA, "Enlace de WhatsApp sin número visible (wa.me/message)."
    sources = list(dict.fromkeys(c.source for c in ordered if c.number == best.number))
    names = " y ".join(_SOURCE_NAMES[s] for s in sources)
    if len(sources) >= 2:
        return WhatsAppConfidence.ALTA, f"El mismo número aparece en {names}."
    if phone_e164 and canonical_number(best.number) == canonical_number(phone_e164):
        return WhatsAppConfidence.ALTA, f"Publicado en {names} y coincide con el teléfono de la ficha de Maps."
    return WhatsAppConfidence.MEDIA, f"Publicado solo en {names}."


def choose_whatsapp(
    candidates: Iterable[WhatsAppCandidate],
    phone_e164: str | None = None,
    broken_evidence: Iterable[str] = (),
) -> WhatsAppDecision:
    """Elige el WhatsApp principal: Maps > web (flotante o header > cuerpo > footer) > Linktree > otras.

    Sin enlace explícito: NO CONFIRMADO si hay teléfono, NO ENCONTRADO si no hay nada.
    `broken_evidence`: botones de WhatsApp publicados que no funcionan (de `broken_evidence()`).
    """
    ranked = sorted(
        enumerate(candidates),
        key=lambda item: (
            _SOURCE_RANK[item[1].source],
            item[1].number is None,
            _PLACEMENT_RANK[item[1].placement],
            item[0],
        ),
    )
    ordered = [candidate for _, candidate in ranked]
    broken = list(dict.fromkeys([*(c.evidence for c in ordered if c.broken), *broken_evidence]))
    if not ordered:
        status = WhatsAppStatus.NO_CONFIRMADO if phone_e164 else WhatsAppStatus.NO_ENCONTRADO
        return WhatsAppDecision(status=status, broken_button=bool(broken), broken_evidence=broken)
    best = ordered[0]
    numbers = {candidate.number for candidate in ordered if candidate.number}
    confidence, reason = _confidence(best, ordered, phone_e164)
    return WhatsAppDecision(
        status=WhatsAppStatus.VERIFICADO,
        url=best.url,
        source=best.source,
        evidence=best.evidence,
        candidates=ordered,
        multiple_numbers=len(numbers) > 1,
        confidence=confidence,
        confidence_reason=reason,
        broken_button=bool(broken),
        broken_evidence=broken,
    )
