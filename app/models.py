"""Modelos del proyecto: estados, candidatos de WhatsApp y las tablas de SQLite (sección 6).

Las tablas son modelos SQLModel (pydantic v2 + SQLAlchemy). Los campos JSON guardan
listas o diccionarios tal cual; las fechas se guardan en UTC.
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel
from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(UTC)


# --- Estados -------------------------------------------------------------------


class SearchStatus(StrEnum):
    EN_CURSO = "EN_CURSO"
    TERMINADA = "TERMINADA"
    FALLIDA = "FALLIDA"
    DETENIDA = "DETENIDA"
    BLOQUEADO = "BLOQUEADO"


class WhatsAppStatus(StrEnum):
    VERIFICADO = "VERIFICADO"
    NO_CONFIRMADO = "NO CONFIRMADO"
    NO_ENCONTRADO = "NO ENCONTRADO"


class WhatsAppSource(StrEnum):
    """Dónde se encontró el enlace, en orden de confianza (sección 7)."""

    MANUAL = "manual"  # Joaquín abrió el chat y confirmó que existe
    MAPS = "maps"
    WEB = "web"
    LINKTREE = "linktree"
    OTRA = "otra"


class WhatsAppCheck(StrEnum):
    """Lo que Joaquín comprobó a mano al abrir (o probar) el WhatsApp."""

    EXISTE = "EXISTE"
    NO_EXISTE = "NO_EXISTE"


class WhatsAppPlacement(StrEnum):
    """En qué parte de la página estaba el enlace."""

    FLOTANTE = "flotante"
    HEADER = "header"
    CUERPO = "cuerpo"
    FOOTER = "footer"


class WhatsAppConfidence(StrEnum):
    """Qué tan seguros estamos de que el WhatsApp VERIFICADO existe (sin iniciar sesión en WhatsApp).

    ALTA: el mismo número aparece en 2 fuentes independientes, o coincide con el teléfono de Maps.
    MEDIA: aparece en una sola fuente.
    """

    ALTA = "ALTA"
    MEDIA = "MEDIA"


class WebsiteStatus(StrEnum):
    OK = "OK"
    CAIDO = "CAIDO"
    SIN_WEB = "SIN_WEB"
    SOLO_REDES = "SOLO_REDES"
    BLOQUEADO = "BLOQUEADO"


class OpportunityLevel(StrEnum):
    ALTA = "ALTA"
    MEDIA = "MEDIA"
    BAJA = "BAJA"


# --- WhatsApp -------------------------------------------------------------------


class WhatsAppCandidate(BaseModel):
    """Un enlace explícito de WhatsApp encontrado en una fuente, con su evidencia."""

    # Código de país + número, solo dígitos (569XXXXXXXX). None en enlaces wa.me/message/…,
    # que abren el chat correcto pero no muestran el número.
    number: str | None
    # https://wa.me/<number>, o el enlace original si no trae número.
    url: str
    source: WhatsAppSource
    placement: WhatsAppPlacement
    # El enlace o fragmento original, tal cual apareció.
    evidence: str
    page_url: str | None = None
    # Aviso para Joaquín, por ejemplo si el enlace original estaba mal escrito.
    note: str | None = None
    # El botón tal como está publicado no abre el chat (por ejemplo, le falta el código de país).
    broken: bool = False


# --- Tablas ---------------------------------------------------------------------


def _json_field(default_factory: Any) -> Any:
    return Field(default_factory=default_factory, sa_column=Column(JSON, nullable=False))


class SearchRun(SQLModel, table=True):
    __tablename__ = "searches"

    id: int | None = Field(default=None, primary_key=True)
    query: str
    limit: int
    status: SearchStatus = Field(default=SearchStatus.EN_CURSO, index=True)
    phase: str = ""
    businesses_found: int = 0
    websites_found: int = 0
    whatsapp_verified: int = 0
    opportunities_found: int = 0
    leads_found: int = 0  # negocios nuevos que cuentan como lead (WhatsApp verificado o celular)
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow, index=True)
    finished_at: datetime | None = None


class Business(SQLModel, table=True):
    __tablename__ = "businesses"

    id: int | None = Field(default=None, primary_key=True)
    # Identificador estable de Maps (place_id o ftid); puede faltar.
    place_id: str | None = Field(default=None, unique=True, index=True)
    google_maps_url: str = Field(unique=True, index=True)
    business_name: str
    category: str | None = None
    phone_raw: str | None = None
    phone_e164: str | None = None
    whatsapp_url: str | None = None
    whatsapp_status: WhatsAppStatus = Field(default=WhatsAppStatus.NO_ENCONTRADO, index=True)
    whatsapp_source: WhatsAppSource | None = None
    whatsapp_evidence: str | None = None
    whatsapp_candidates: list[dict[str, Any]] = _json_field(list)
    whatsapp_multiple_numbers: bool = False
    whatsapp_confidence: WhatsAppConfidence | None = Field(default=None, index=True)
    # Hay un botón de WhatsApp publicado que no funciona (número mal escrito, acortador caído).
    whatsapp_broken_button: bool = False
    # Confirmación manual de Joaquín (sección 7): abrió el chat y vio si existe.
    whatsapp_check: WhatsAppCheck | None = None
    whatsapp_checked_at: datetime | None = None
    # Lead descartado (por ejemplo, el WhatsApp no existe): no se muestra ni vuelve a aparecer.
    discarded_at: datetime | None = Field(default=None, index=True)
    discard_reason: str | None = None
    # Repetido: comparte número, web o red social con un lead ya visto o un contacto ya hecho.
    duplicate_of_id: int | None = Field(default=None, index=True)
    duplicate_reason: str | None = None
    website: str | None = None
    website_status: WebsiteStatus | None = Field(default=None, index=True)
    instagram: str | None = None
    facebook: str | None = None
    address: str | None = None
    city: str | None = Field(default=None, index=True)
    commune: str | None = Field(default=None, index=True)
    opening_hours: dict[str, Any] = _json_field(dict)
    rating: float | None = Field(default=None, index=True)
    review_count: int | None = Field(default=None, index=True)
    services: list[str] = _json_field(list)
    description: str | None = None
    # De dónde salió cada dato: {"phone_raw": "maps", "whatsapp_url": "web", ...}
    field_sources: dict[str, Any] = _json_field(dict)
    first_seen_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class SearchResult(SQLModel, table=True):
    __tablename__ = "search_results"

    search_id: int = Field(foreign_key="searches.id", primary_key=True)
    business_id: int = Field(foreign_key="businesses.id", primary_key=True)
    position: int


class ContactedLead(SQLModel, table=True):
    """Lead que Joaquín ya contactó por su cuenta (importado de su planilla). No debe repetirse."""

    __tablename__ = "contacted_leads"

    id: int | None = Field(default=None, primary_key=True)
    name: str
    phone_raw: str
    phone_e164: str = Field(index=True, unique=True)
    status: str | None = None  # contactado, demo enviada, respuesta positiva, venta…
    contacted_on: str | None = None  # fecha y hora tal como venían en la planilla
    note: str | None = None
    imported_at: datetime = Field(default_factory=utcnow)


class Fingerprint(SQLModel, table=True):
    """Huella de un lead ya visto o contactado: número, web, red social o ficha de Maps.

    Un negocio nuevo que comparte cualquier huella con otro ya prospectado es un repetido.
    """

    __tablename__ = "fingerprints"

    id: int | None = Field(default=None, primary_key=True)
    kind: str = Field(index=True)  # "numero", "web", "red", "maps"
    value: str = Field(index=True)
    business_id: int | None = Field(default=None, foreign_key="businesses.id", index=True)
    contacted_id: int | None = Field(default=None, foreign_key="contacted_leads.id", index=True)
    created_at: datetime = Field(default_factory=utcnow)


class WebsiteAnalysis(SQLModel, table=True):
    __tablename__ = "website_analyses"

    id: int | None = Field(default=None, primary_key=True)
    business_id: int = Field(foreign_key="businesses.id", index=True)
    url_analyzed: str
    final_url: str | None = None
    analyzed_at: datetime = Field(default_factory=utcnow, index=True)
    rubric_version: str | None = None
    model_name: str | None = None
    website_score: int | None = Field(default=None, index=True)
    rubric_scores: dict[str, Any] = _json_field(dict)
    facts: dict[str, Any] = _json_field(dict)
    # Hallazgos automáticos (sin IA): problemas medidos, con su evidencia.
    findings: list[dict[str, Any]] = _json_field(list)
    seo_local: dict[str, Any] = _json_field(dict)
    info_checklist: dict[str, Any] = _json_field(dict)
    main_problems: list[dict[str, Any]] = _json_field(list)
    opportunities: list[dict[str, Any]] = _json_field(list)
    strengths: list[dict[str, Any]] = _json_field(list)
    opportunity_level: OpportunityLevel | None = Field(default=None, index=True)
    screenshots: dict[str, Any] = _json_field(dict)
    load_time_ms: int | None = None
    psi_mobile_score: int | None = None
    raw_llm_output: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    error: str | None = None
