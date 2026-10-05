"""Leads diarios: la lista de cada día, sus acciones (Existe / No existe) y la exportación.

Un lead es un negocio nuevo con web propia que abre y con WhatsApp VERIFICADO o con un
celular para "Probar WhatsApp" (sección 7, cambios del 2026-10-04). Los descartados y los repetidos no se
muestran ni vuelven a aparecer: siguen guardados para que la deduplicación los salte en
búsquedas futuras (ver `app/dedupe.py`).
"""

import csv
import io
import re
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from pydantic import BaseModel

from app.analyze.findings import status_findings
from app.analyze.scorer import opportunity_level
from app.db.repository import Repository
from app.extract.phones import is_probable_mobile, pretty_number
from app.models import (
    Business,
    SearchRun,
    WebsiteAnalysis,
    WebsiteStatus,
    WhatsAppCandidate,
    WhatsAppCheck,
    WhatsAppConfidence,
    WhatsAppPlacement,
    WhatsAppSource,
    WhatsAppStatus,
    utcnow,
)
from app.pipeline import is_lead, whatsapp_changes

DISCARD_REASON = "El WhatsApp no existe (lo comprobaste al abrir el chat)"


class LeadView(BaseModel):
    """Lo que muestra la página por cada lead."""

    id: int
    name: str
    category: str | None
    city: str | None
    address: str | None
    query: str
    position: int
    # WhatsApp: "confirmado" (por Joaquín), "verificado" (publicado por el negocio) o "por_probar" (celular)
    whatsapp_state: str
    whatsapp_url: str | None  # enlace para abrir el chat (verificado) o probarlo (celular)
    whatsapp_number: str | None  # número legible: +56 9 9355 7317
    confidence: str | None
    confidence_reason: str | None
    broken_button: bool
    whatsapp_check: str | None
    phone: str | None
    website: str | None
    website_status: str
    website_reason: str | None
    rating: float | None
    reviews: int | None
    maps_url: str
    instagram: str | None
    facebook: str | None
    discarded: bool
    # Revisión de la web (hallazgos automáticos y, si hay IA, puntaje)
    findings: list[dict[str, Any]] = []
    score: int | None = None
    opportunity: str | None = None
    screenshots: dict[str, str] = {}
    review_pending: bool = False  # la web abre pero todavía no se revisó a fondo
    review_error: str | None = None
    problems: list[dict[str, Any]] = []  # problemas según la IA
    opportunities: list[dict[str, Any]] = []
    strengths: list[dict[str, Any]] = []

    @property
    def serious_findings(self) -> int:
        return sum(1 for finding in self.findings if finding.get("gravedad") == "alta")

    @property
    def has_whatsapp(self) -> bool:
        return self.whatsapp_state in ("verificado", "confirmado")

    @property
    def bad_website(self) -> bool:
        """Web propia que abre pero está mal: score < 50 (con IA) o, sin IA, algún problema grave."""
        if self.website_status != "OK":
            return False
        if self.score is not None:
            return self.score < 50
        return self.serious_findings > 0


class DuplicateView(BaseModel):
    """Un negocio que se ocultó por repetido, con el motivo (para revisarlo si hace falta)."""

    id: int
    name: str
    reason: str
    maps_url: str
    query: str


def _digits(url: str | None) -> str | None:
    match = re.search(r"wa\.me/(\d+)", url or "")
    return match.group(1) if match else None


def lead_view(business: Business, query: str, position: int, analysis: WebsiteAnalysis | None = None) -> LeadView:
    sources = business.field_sources or {}
    has_own_website = business.website_status in (WebsiteStatus.OK, None) and bool(business.website)
    if analysis is not None:
        findings, score = analysis.findings, analysis.website_score
        opportunity = analysis.opportunity_level
    else:
        findings = [f.model_dump() for f in status_findings(business)]
        score, opportunity = None, None
    if not has_own_website:
        opportunity = opportunity_level(
            None, has_own_website=False, rating=business.rating, reviews=business.review_count
        )
    if business.whatsapp_status == WhatsAppStatus.VERIFICADO:
        state = "confirmado" if business.whatsapp_check == WhatsAppCheck.EXISTE else "verificado"
        url = business.whatsapp_url
    elif is_probable_mobile(business.phone_e164):
        # Enlace de prueba: no se guarda ni se marca como verificado (sección 7).
        state, url = "por_probar", f"https://wa.me/{business.phone_e164.lstrip('+')}"
    else:
        state, url = "sin_whatsapp", None
    website_reason = str(sources.get("website_status") or "").removeprefix("web: ") or None
    return LeadView(
        id=business.id,
        name=business.business_name,
        category=business.category,
        city=business.city,
        address=business.address,
        query=query,
        position=position,
        whatsapp_state=state,
        whatsapp_url=url,
        whatsapp_number=pretty_number(_digits(url)),
        confidence=str(business.whatsapp_confidence) if business.whatsapp_confidence else None,
        confidence_reason=sources.get("whatsapp_confidence"),
        broken_button=business.whatsapp_broken_button,
        whatsapp_check=str(business.whatsapp_check) if business.whatsapp_check else None,
        phone=business.phone_raw,
        website=business.website,
        website_status=str(business.website_status) if business.website_status else "POR VERIFICAR",
        website_reason=website_reason if business.website_status != WebsiteStatus.OK else None,
        rating=business.rating,
        reviews=business.review_count,
        maps_url=business.google_maps_url,
        instagram=business.instagram,
        facebook=business.facebook,
        discarded=business.discarded_at is not None,
        findings=findings,
        score=score,
        opportunity=str(opportunity) if opportunity else None,
        screenshots=analysis.screenshots if analysis else {},
        review_pending=analysis is None and business.website_status == WebsiteStatus.OK,
        review_error=analysis.error if analysis else None,
        problems=analysis.main_problems if analysis else [],
        opportunities=analysis.opportunities if analysis else [],
        strengths=analysis.strengths if analysis else [],
    )


# --- Orden ------------------------------------------------------------------------

_WHATSAPP_ORDER = {"confirmado": 0, "verificado": 1, "por_probar": 3, "sin_whatsapp": 4}
_OPPORTUNITY_ORDER = {"ALTA": 0, "MEDIA": 1, "BAJA": 3}
SORTS = {
    "whatsapp": "WhatsApp (confirmados primero)",
    "oportunidad": "Más problemas en su web primero",
    "resenas": "Más reseñas",
    "rating": "Mejor rating",
    "nombre": "Nombre",
}


def _whatsapp_rank(lead: LeadView) -> int:
    rank = _WHATSAPP_ORDER[lead.whatsapp_state]
    if lead.whatsapp_state == "verificado" and lead.confidence != "ALTA":
        rank = 2  # verificado con confianza media, después de los de confianza alta
    return rank


def sort_leads(leads: list[LeadView], order: str = "whatsapp") -> list[LeadView]:
    reviews = lambda lead: -(lead.reviews or 0)  # noqa: E731
    keys = {
        "whatsapp": lambda lead: (_whatsapp_rank(lead), reviews(lead)),
        "oportunidad": lambda lead: (
            _OPPORTUNITY_ORDER.get(lead.opportunity or "", 2),  # sin nivel (sin IA) va entre MEDIA y BAJA
            -lead.serious_findings,
            -len(lead.findings),
            _whatsapp_rank(lead),
            reviews(lead),
        ),
        "resenas": lambda lead: (reviews(lead), _whatsapp_rank(lead)),
        "rating": lambda lead: (-(lead.rating or 0), reviews(lead)),
        "nombre": lambda lead: lead.name.lower(),
    }
    return sorted(leads, key=keys.get(order, keys["whatsapp"]))


# --- Filtros rápidos (sección 10) ------------------------------------------------------------

# Todos los leads tienen web que abre (decisión del 2026-10-04): no hay filtros "con web" ni "sin web".
FILTERS: dict[str, tuple[str, str, Callable[[LeadView], bool]]] = {
    # clave: (etiqueta, explicación, condición)
    "whatsapp": ("Con WhatsApp", "WhatsApp verificado o confirmado por ti", lambda lead: lead.has_whatsapp),
    "probar": ("Celular por probar", "No publica WhatsApp, pero tiene un celular", lambda lead: lead.whatsapp_state == "por_probar"),
    "web_mala": (
        "Web mala",
        "Con IA, puntaje menor a 50; sin IA, al menos un problema grave detectado",
        lambda lead: lead.bad_website,
    ),
    "rating": ("Rating > 4,5", "Más de 4,5 estrellas en Google", lambda lead: (lead.rating or 0) > 4.5),
    "resenas": ("Más de 100 reseñas", "Más de 100 reseñas en Google", lambda lead: (lead.reviews or 0) > 100),
    "whatsapp_web_mala": (
        "WhatsApp + web mala",
        "Tiene WhatsApp y su web está mala: el mejor lead para ofrecer un rediseño",
        lambda lead: lead.has_whatsapp and lead.bad_website,
    ),
    "oportunidad": (
        "Grandes oportunidades",
        "Oportunidad ALTA: web mala (puntaje < 50 con IA), con buen rating y reseñas",
        lambda lead: lead.opportunity == "ALTA",
    ),
}


def filter_leads(leads: list[LeadView], filters: Iterable[str] = (), query: str | None = None) -> list[LeadView]:
    """Leads que cumplen TODOS los filtros elegidos (y, si se indica, de esa búsqueda)."""
    checks = [FILTERS[key][2] for key in filters if key in FILTERS]
    return [
        lead
        for lead in leads
        if (not query or lead.query == query) and all(check(lead) for check in checks)
    ]


# --- Días -------------------------------------------------------------------------


def _day_range_utc(day: date) -> tuple[datetime, datetime]:
    """Inicio y fin del día en la hora de este computador, convertidos a UTC (como se guardan)."""
    start = datetime.combine(day, time.min).astimezone()  # medianoche local
    end = datetime.combine(day + timedelta(days=1), time.min).astimezone()
    return start.astimezone(UTC), end.astimezone(UTC)


def local_day(moment: datetime) -> date:
    """Día local de una fecha guardada (SQLite la devuelve sin zona horaria: es UTC)."""
    return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).astimezone().date()


def leads_for_day(repo: Repository, day: date, *, order: str = "whatsapp", include_discarded: bool = False) -> list[LeadView]:
    """Leads encontrados ese día (en todas las búsquedas del día), sin repetir."""
    start, end = _day_range_utc(day)
    searches = {s.id: s for s in repo.searches_between(start, end)}
    seen: set[int] = set()
    leads: list[LeadView] = []
    for search_id, position, business in repo.found_by_searches(list(searches)):
        if business.id in seen:
            continue
        seen.add(business.id)
        if is_lead(business) or (include_discarded and business.discarded_at is not None):
            analysis = repo.latest_analysis(business.id)
            leads.append(lead_view(business, searches[search_id].query, position, analysis))
    return sort_leads(leads, order)


def searches_for_day(repo: Repository, day: date) -> list[SearchRun]:
    """Búsquedas hechas ese día, de la más antigua a la más nueva."""
    return repo.searches_between(*_day_range_utc(day))


def duplicates_for_day(repo: Repository, day: date) -> list[DuplicateView]:
    """Negocios de las búsquedas del día que se ocultaron por repetidos."""
    start, end = _day_range_utc(day)
    searches = {s.id: s for s in repo.searches_between(start, end)}
    seen: set[int] = set()
    duplicates: list[DuplicateView] = []
    for search_id, _, business in repo.found_by_searches(list(searches)):
        if business.id in seen or not business.duplicate_reason:
            continue
        seen.add(business.id)
        duplicates.append(
            DuplicateView(
                id=business.id,
                name=business.business_name,
                reason=business.duplicate_reason,
                maps_url=business.google_maps_url,
                query=searches[search_id].query,
            )
        )
    return duplicates


def lead_for_business(repo: Repository, business_id: int) -> LeadView | None:
    """Un lead suelto (para refrescar su fila en la página)."""
    business = repo.get_business(business_id)
    if business is None:
        return None
    query, position = repo.first_search_of(business_id) or ("", 0)
    return lead_view(business, query, position, repo.latest_analysis(business_id))


def days_with_searches(repo: Repository, limit: int = 30) -> list[tuple[date, int, list[str]]]:
    """(día, leads vigentes, búsquedas) de los últimos días con búsquedas, del más nuevo al más viejo.

    Los leads se cuentan con la regla de hoy (sin descartados, repetidos ni negocios sin web),
    no con el número que se guardó al terminar cada búsqueda.
    """
    days: dict[date, list[SearchRun]] = {}
    for search in repo.list_searches(limit=500):
        days.setdefault(local_day(search.created_at), []).append(search)
    result = []
    for day, searches in sorted(days.items(), reverse=True)[:limit]:
        found = {business.id: business for _, _, business in repo.found_by_searches([s.id for s in searches])}
        total = sum(1 for business in found.values() if is_lead(business))
        queries = list(dict.fromkeys(s.query for s in sorted(searches, key=lambda s: s.created_at)))
        result.append((day, total, queries))
    return result


# --- Acciones de Joaquín -------------------------------------------------------------


def _check_number(business: Business) -> str | None:
    """Número que Joaquín abrió: el WhatsApp verificado o, si no hay, el celular a probar."""
    if business.whatsapp_status == WhatsAppStatus.VERIFICADO and _digits(business.whatsapp_url):
        return _digits(business.whatsapp_url)
    if is_probable_mobile(business.phone_e164):
        return business.phone_e164.lstrip("+")
    return None


def confirm_whatsapp(repo: Repository, business_id: int) -> Business:
    """"✓ Existe": Joaquín abrió el chat y el WhatsApp existe. Queda verificado por él."""
    business = repo.get_business(business_id)
    if business is None:
        raise LookupError(f"No existe el negocio {business_id}")
    number = _check_number(business)
    if number is None:
        raise ValueError("Este negocio no tiene un WhatsApp ni un celular para confirmar.")
    now = utcnow()
    manual = WhatsAppCandidate(
        number=number,
        url=f"https://wa.me/{number}",
        source=WhatsAppSource.MANUAL,
        placement=WhatsAppPlacement.CUERPO,
        evidence=f"Confirmado por Joaquín el {now.astimezone():%d-%m-%Y %H:%M}",
    )
    changes = whatsapp_changes(business, replace_sources={WhatsAppSource.MANUAL}, fresh=[manual], broken=[])
    changes.update(
        whatsapp_check=WhatsAppCheck.EXISTE,
        whatsapp_checked_at=now,
        whatsapp_confidence=WhatsAppConfidence.ALTA,
        discarded_at=None,
        discard_reason=None,
    )
    return repo.update_business(business_id, **changes)


def discard_lead(repo: Repository, business_id: int, reason: str = DISCARD_REASON) -> Business:
    """"✗ No existe": el lead se descarta y no vuelve a aparecer."""
    business = repo.get_business(business_id)
    if business is None:
        raise LookupError(f"No existe el negocio {business_id}")
    changes = whatsapp_changes(business, replace_sources={WhatsAppSource.MANUAL}, fresh=[], broken=[])
    changes.update(
        whatsapp_check=WhatsAppCheck.NO_EXISTE,
        whatsapp_checked_at=utcnow(),
        discarded_at=utcnow(),
        discard_reason=reason,
    )
    return repo.update_business(business_id, **changes)


def restore_lead(repo: Repository, business_id: int) -> Business:
    """Deshace un descarte o una confirmación."""
    business = repo.get_business(business_id)
    if business is None:
        raise LookupError(f"No existe el negocio {business_id}")
    changes = whatsapp_changes(business, replace_sources={WhatsAppSource.MANUAL}, fresh=[], broken=[])
    changes.update(whatsapp_check=None, whatsapp_checked_at=None, discarded_at=None, discard_reason=None)
    return repo.update_business(business_id, **changes)


# --- Exportar ---------------------------------------------------------------------

CSV_COLUMNS = [
    ("Negocio", "name"),
    ("Rubro", "category"),
    ("Ciudad", "city"),
    ("WhatsApp", "whatsapp_url"),
    ("Número", "whatsapp_number"),
    ("Estado WhatsApp", "whatsapp_state"),
    ("Confianza", "confidence"),
    ("Por qué", "confidence_reason"),
    ("Botón de WhatsApp roto", "broken_button"),
    ("Teléfono", "phone"),
    ("Web", "website"),
    ("Estado web", "website_status"),
    ("Motivo web", "website_reason"),
    ("Problemas detectados", "findings"),
    ("Puntaje web (IA)", "score"),
    ("Oportunidad", "opportunity"),
    ("Rating", "rating"),
    ("Reseñas", "reviews"),
    ("Dirección", "address"),
    ("Instagram", "instagram"),
    ("Facebook", "facebook"),
    ("Google Maps", "maps_url"),
    ("Búsqueda", "query"),
]
_STATE_NAMES = {
    "confirmado": "Confirmado por ti",
    "verificado": "Verificado (publicado por el negocio)",
    "por_probar": "Por probar (celular)",
    "sin_whatsapp": "Sin WhatsApp",
}


def leads_csv(leads: list[LeadView]) -> str:
    """CSV con ";" y BOM: así Excel en español lo abre bien, con tildes y columnas separadas."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow([title for title, _ in CSV_COLUMNS])
    for lead in leads:
        row = []
        for _, attr in CSV_COLUMNS:
            value = getattr(lead, attr)
            if attr == "whatsapp_state":
                value = _STATE_NAMES.get(value, value)
            elif attr == "findings":
                value = " | ".join(finding.get("titulo", "") for finding in value)
            elif isinstance(value, bool):
                value = "sí" if value else "no"
            elif isinstance(value, float):
                value = f"{value:.1f}".replace(".", ",")
            row.append("" if value is None else value)
        writer.writerow(row)
    return "﻿" + buffer.getvalue()
