"""No repetir leads: huellas de cada lead ya visto o ya contactado.

Un negocio es REPETIDO si comparte cualquiera de estas huellas con un lead que ya apareció
(aunque esté descartado) o con alguien que Joaquín ya contactó por su cuenta:

- número: teléfono o WhatsApp (los números se comparan en su forma canónica);
- web: el dominio de su sitio, sin "www" (en plataformas compartidas como Linktree o
  Google Sites, el dominio más la ruta que identifica al negocio);
- red: su perfil de Instagram o Facebook.

Así no se repite un negocio aunque Maps lo muestre con otra ficha (sucursales, fichas
duplicadas), ni se revisa dos veces la misma web o el mismo WhatsApp.
"""

import re
from typing import NamedTuple
from urllib.parse import urlsplit

from app.db.repository import Repository
from app.extract.links import LINK_AGGREGATOR_HOSTS, LinkKind, classify_link, normalize_url, unwrap_google_redirect
from app.extract.phones import canonical_number, is_probable_mobile, normalize_phone, pretty_number
from app.extract.socials import classify_social
from app.models import Business, ContactedLead, WebsiteStatus, WhatsAppStatus

NUMBER, WEB, SOCIAL = "numero", "web", "red"
Prints = list[tuple[str, str]]

# Plataformas donde muchos negocios comparten el dominio: la ruta identifica al negocio.
# (dominio → cuántos segmentos de la ruta forman parte de la huella)
_SHARED_HOSTS: dict[str, int] = {
    **{host: 1 for host in LINK_AGGREGATOR_HOSTS},
    "sites.google.com": 2,
    "google.com": 2,
    "bit.ly": 1,
    "tinyurl.com": 1,
    "cutt.ly": 1,
    "booksy.com": 3,
    "fresha.com": 3,
    "agendapro.com": 3,
    "reservo.cl": 2,
    "doctoralia.cl": 3,
    "doctoralia.com": 3,
    "doctoralia.com.ar": 3,
    "doctoralia.com.mx": 3,
    "mercadolibre.cl": 3,
    "mercadolibre.com.ar": 3,
    "mercadolibre.com.mx": 3,
    "pedidosya.cl": 3,
    "pedidosya.com.ar": 3,
    "rappi.cl": 3,
    "ubereats.com": 4,
    "tripadvisor.cl": 2,
    "tripadvisor.com": 2,
    "booking.com": 3,
    "airbnb.cl": 2,
    "airbnb.com": 2,
    "yelp.com": 2,
    "paginasamarillas.cl": 3,
    "amarillas.cl": 3,
}
# Subdominios de plataformas: el subdominio identifica al usuario y la ruta, a su sitio.
_SHARED_SUFFIXES: dict[str, int] = {"wixsite.com": 1}


def _shared_segments(host: str) -> int | None:
    if host in _SHARED_HOSTS:
        return _SHARED_HOSTS[host]
    for suffix, segments in _SHARED_SUFFIXES.items():
        if host.endswith("." + suffix):
            return segments
    return None


def site_key(url: str | None) -> str | None:
    """Huella de una web: "https://www.negocio.cl/contacto" → "negocio.cl".

    En plataformas compartidas se agrega la ruta: "linktr.ee/negocio", "sites.google.com/view/negocio".
    """
    normalized = normalize_url(unwrap_google_redirect(url or ""))
    if not normalized:
        return None
    parts = urlsplit(normalized)
    host = (parts.hostname or "").lower().removeprefix("www.")
    segments = _shared_segments(host)
    if segments is None:
        return host or None
    path = [segment.lower() for segment in parts.path.split("/") if segment][:segments]
    if segments and not path:
        return None  # la portada de una plataforma (linktr.ee/) no identifica a ningún negocio
    return "/".join([host, *path])


def number_key(raw: str | None) -> str | None:
    """Huella de un número: solo dígitos con código de país, en forma canónica."""
    digits = re.sub(r"\D", "", raw or "")
    return canonical_number(digits) if len(digits) >= 8 else None


def _wa_number(url: str | None) -> str | None:
    match = re.search(r"(?:wa\.me/|phone=)\+?(\d{8,})", url or "")
    return match.group(1) if match else None


def link_fingerprints(url: str | None) -> Prints:
    """Huellas de un enlace suelto (el "sitio web" de una ficha o de una tarjeta de Maps)."""
    if not url:
        return []
    kind = classify_link(url)
    if kind == LinkKind.WHATSAPP:
        url = unwrap_google_redirect(url)
        number = number_key(_wa_number(url))
        if number:
            return [(NUMBER, number)]
        # Sin número a la vista (wa.link/…, wa.me/message/…): el propio enlace es la huella.
        shortlink = re.search(r"(?:wa\.link|walink\.co)/[^/?#\s]+|wa\.me/message/[^/?#\s]+", url, re.IGNORECASE)
        return [(WEB, shortlink.group(0).lower())] if shortlink else []
    if kind == LinkKind.RED_SOCIAL:
        social = classify_social(unwrap_google_redirect(url))
        return [(SOCIAL, social[1].lower())] if social else []
    if kind in (LinkKind.WEB_PROPIA, LinkKind.AGREGADOR):
        key = site_key(url)
        return [(WEB, key)] if key else []
    return []


def _ordered(prints: Prints) -> Prints:
    """Sin repetir, con los números primero (son la huella más segura)."""
    order = {NUMBER: 0, WEB: 1, SOCIAL: 2}
    return sorted(dict.fromkeys(p for p in prints if p[1]), key=lambda p: order.get(p[0], 9))


def business_fingerprints(business: Business) -> Prints:
    """Todas las huellas de un negocio: teléfono, WhatsApp, web y redes."""
    prints: Prints = []
    numbers = [business.phone_e164, _wa_number(business.whatsapp_url)]
    numbers += [candidate.get("number") for candidate in business.whatsapp_candidates or []]
    prints += [(NUMBER, key) for key in map(number_key, numbers) if key]
    for url in (business.website, (business.field_sources or {}).get("website_maps")):
        prints += link_fingerprints(url)
    for url in (business.instagram, business.facebook):
        social = classify_social(url) if url else None
        if social:
            prints.append((SOCIAL, social[1].lower()))
    return _ordered(prints)


def listing_fingerprints(website: str | None, phone_e164: str | None) -> Prints:
    """Huellas visibles en la tarjeta de la lista de Maps, antes de abrir la ficha."""
    prints = link_fingerprints(website)
    number = number_key(phone_e164)
    if number:
        prints.append((NUMBER, number))
    return _ordered(prints)


# --- Buscar repetidos -------------------------------------------------------------------


class Duplicate(NamedTuple):
    kind: str
    value: str
    business_id: int | None  # el lead anterior (None si es un contactado)
    contacted_id: int | None
    reason: str  # en palabras simples, para Joaquín


def _what(kind: str, value: str) -> str:
    if kind == NUMBER:
        return f"el mismo número ({pretty_number(value)})"
    if kind == WEB:
        return f"la misma web ({value})"
    return f"la misma red social ({value})"


def _contacted_detail(contacted: ContactedLead) -> str:
    detail = ", ".join(part for part in (contacted.status, contacted.contacted_on) if part)
    return f" ({detail})" if detail else ""


def find_duplicate(
    repo: Repository, prints: Prints, *, exclude_business_id: int | None = None, contacted_only: bool = False
) -> Duplicate | None:
    """El primer lead anterior o contactado que comparte una huella, con el motivo."""
    for kind, value in prints:
        found = repo.find_fingerprint(
            kind, value, exclude_business_id=exclude_business_id, contacted_only=contacted_only
        )
        if found is None:
            continue
        if found.contacted_id is not None:
            contacted = repo.get_contacted(found.contacted_id)
            if contacted is None:
                continue
            reason = f"Tiene {_what(kind, value)} que «{contacted.name}», a quien ya contactaste{_contacted_detail(contacted)}."
            return Duplicate(kind, value, None, contacted.id, reason)
        previous = repo.get_business(found.business_id) if found.business_id else None
        if previous is None:
            continue
        state = " (lo descartaste)" if previous.discarded_at else ", que ya apareció en tus leads"
        reason = f"Tiene {_what(kind, value)} que «{previous.business_name}»{state}."
        return Duplicate(kind, value, previous.id, None, reason)
    return None


def mark_duplicate(repo: Repository, business: Business, duplicate: Duplicate) -> Business:
    return repo.update_business(
        business.id, duplicate_of_id=duplicate.business_id, duplicate_reason=duplicate.reason
    )


def register_lead(repo: Repository, business: Business) -> None:
    """Guarda las huellas de un lead para que no vuelva a aparecer en otra búsqueda."""
    repo.set_fingerprints(business_fingerprints(business), business_id=business.id)


def unmark_duplicate(repo: Repository, business_id: int) -> Business:
    """"No es repetido": vuelve a la lista y sus huellas cuentan desde ahora."""
    business = repo.update_business(business_id, duplicate_of_id=None, duplicate_reason=None)
    register_lead(repo, business)
    return business


def _already_prospected(business: Business) -> bool:
    """Leads (web que abre + WhatsApp verificado o celular) y descartados: sus huellas no deben repetirse."""
    if business.duplicate_reason:
        return False
    if business.discarded_at is not None:
        return True
    return business.website_status == WebsiteStatus.OK and (
        business.whatsapp_status == WhatsAppStatus.VERIFICADO or is_probable_mobile(business.phone_e164)
    )


def ensure_fingerprints(repo: Repository) -> int:
    """Guarda las huellas de los leads que todavía no las tienen (por ejemplo, de antes de este cambio)."""
    done = repo.fingerprinted_business_ids()
    added = 0
    for business in repo.list_businesses():
        if business.id not in done and _already_prospected(business):
            register_lead(repo, business)
            added += 1
    return added


def flag_contacted(repo: Repository) -> list[Business]:
    """Marca como repetidos los leads guardados que ya contactaste (su número está en tu lista)."""
    flagged = []
    for business in repo.list_businesses():
        if business.duplicate_reason or business.discarded_at is not None:
            continue
        numbers = [p for p in business_fingerprints(business) if p[0] == NUMBER]
        duplicate = find_duplicate(repo, numbers, exclude_business_id=business.id, contacted_only=True)
        if duplicate:
            flagged.append(mark_duplicate(repo, business, duplicate))
    return flagged


# --- Importar la lista de ya contactados --------------------------------------------------


class ContactedRow(NamedTuple):
    name: str
    phone_raw: str
    phone_e164: str
    status: str | None
    contacted_on: str | None
    note: str | None


class ImportResult(NamedTuple):
    added: int
    updated: int
    invalid: list[str]  # líneas sin un número válido
    flagged: list[Business]  # leads guardados que resultaron estar ya contactados


_PHONE_CELL = re.compile(r"^\+?[\d\s().-]{7,}$")
_PHONE_IN_TEXT = re.compile(r"\+?\d[\d\s().-]{6,}\d")
_TIME = re.compile(r"^\d{1,2}[.:]\d{2}\s*(?:[ap]\.?\s?m\.?)?$", re.IGNORECASE)
_NOTES = {"checked", "check", "ok", "✓", "✔", "listo"}


def _row_from_cells(cells: list[str], phone_at: int, phone_e164: str) -> ContactedRow:
    before = [cell for cell in cells[:phone_at] if cell]
    after = [cell for cell in cells[phone_at + 1 :] if cell]
    notes = [cell for cell in after if cell.lower() in _NOTES]
    rest = [cell for cell in after if cell.lower() not in _NOTES]
    status = rest.pop(0) if rest and not _TIME.match(rest[0]) else None
    return ContactedRow(
        name=" ".join(before) or "(sin nombre)",
        phone_raw=cells[phone_at],
        phone_e164=phone_e164,
        status=status.lower() if status else None,
        contacted_on=", ".join(rest) or None,
        note=", ".join(notes) or None,
    )


def parse_contacted(text: str, region: str = "CL") -> tuple[list[ContactedRow], list[str]]:
    """Lee la lista pegada desde la planilla: nombre, teléfono, estado, hora, fecha… (separados por tabulación).

    Devuelve (filas válidas, líneas sin número válido). Nunca inventa un número: si no se
    puede leer, la línea queda en la lista de inválidas.
    """
    rows: list[ContactedRow] = []
    invalid: list[str] = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        cells = [cell.strip() for cell in re.split(r"\t| {2,}", line.strip())]
        phone_at, phone = None, None
        for index, cell in enumerate(cells):
            if _PHONE_CELL.match(cell):
                phone = normalize_phone(cell, region)
                if phone:
                    phone_at = index
                    break
        if phone_at is not None and phone is not None:
            rows.append(_row_from_cells(cells, phone_at, phone.e164))
            continue
        # Sin columnas: se busca el número dentro del texto.
        match = _PHONE_IN_TEXT.search(line)
        phone = normalize_phone(match.group(0), region) if match else None
        if match and phone:
            parts = [line[: match.start()].strip(), match.group(0).strip(), line[match.end() :].strip()]
            rows.append(_row_from_cells(parts, 1, phone.e164))
        else:
            invalid.append(line.strip())
    return rows, invalid


def import_contacted(repo: Repository, text: str, region: str = "CL") -> ImportResult:
    """Guarda la lista de ya contactados (sin duplicar) y marca los leads guardados que coinciden."""
    rows, invalid = parse_contacted(text, region)
    added = updated = 0
    for row in rows:
        contacted, created = repo.upsert_contacted(ContactedLead(**row._asdict()))
        if created:
            added += 1
        else:
            updated += 1
        key = number_key(contacted.phone_e164)
        repo.set_fingerprints([(NUMBER, key)] if key else [], contacted_id=contacted.id)
    return ImportResult(added, updated, invalid, flag_contacted(repo))
