"""Informe completo de un negocio (sección 10, pantalla `/negocio/{id}`).

Junta en un solo lugar lo que ya está guardado: datos de Maps, WhatsApp con todas sus
fuentes, el último análisis de la web (hallazgos, rúbrica, SEO local, checklist, capturas)
y de dónde salió cada dato. No calcula nada nuevo ni inventa datos: si algo no se midió,
se dice.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.analyze.scorer import load_rubric
from app.db.repository import Repository
from app.extract.phones import pretty_number
from app.leads import LeadView, lead_for_business
from app.models import Business, WebsiteAnalysis, WhatsAppCandidate

SEO_LABELS = {
    "titulo_con_ciudad": "Título con servicio y ciudad",
    "h1_unico": "Un solo título principal (H1)",
    "meta_descripcion": "Descripción para Google",
    "cobertura": "Comunas o zona de cobertura",
    "nombre_direccion_telefono": "Nombre, dirección y teléfono visibles",
    "datos_estructurados": "Datos estructurados de negocio local",
    "mapa": "Mapa o enlace a su ficha de Google",
}
CHECKLIST_LABELS = {
    "servicios": "Servicios",
    "cobertura": "Comunas / cobertura",
    "telefono": "Teléfono",
    "whatsapp": "WhatsApp",
    "horarios": "Horarios",
    "diferenciadores": "Diferenciadores",
    "testimonios": "Testimonios",
    "resenas": "Reseñas",
    "confianza": "Elementos de confianza",
}
FIELD_LABELS = {
    "business_name": "Nombre",
    "category": "Rubro",
    "phone_raw": "Teléfono",
    "phone_e164": "Teléfono (formato internacional)",
    "whatsapp_url": "WhatsApp",
    "whatsapp_confidence": "Confianza del WhatsApp",
    "whatsapp_broken_web": "Botones de WhatsApp rotos en su web",
    "website": "Web",
    "website_maps": "Lo que Maps mostraba como «Sitio web»",
    "website_status": "Estado de la web",
    "instagram": "Instagram",
    "facebook": "Facebook",
    "address": "Dirección",
    "city": "Ciudad",
    "commune": "Comuna",
    "opening_hours": "Horario",
    "rating": "Rating",
    "review_count": "Reseñas",
    "services": "Servicios",
    "description": "Descripción",
}
SOURCE_NAMES = {
    "maps": "Google Maps",
    "web": "su web",
    "linktree": "su Linktree",
    "manual": "confirmado por ti",
    "otra": "otra fuente enlazada por el negocio",
}
PLACEMENT_NAMES = {"flotante": "botón flotante", "header": "menú o encabezado", "cuerpo": "contenido", "footer": "pie de página"}


class RubricRow(BaseModel):
    id: int
    nombre: str
    evalua: str  # "codigo" o "ia"
    puntaje: int | None = None
    justificacion: str | None = None
    evidencia: list[str] = []


class CheckRow(BaseModel):
    nombre: str
    ok: bool | None
    detalle: str


class SourceRow(BaseModel):
    dato: str
    fuente: str


class CandidateRow(BaseModel):
    number: str | None
    url: str
    source: str
    placement: str
    evidence: str
    page_url: str | None
    note: str | None
    broken: bool


class AnalysisRow(BaseModel):
    id: int
    analyzed_at: datetime
    score: int | None
    findings: int
    model_name: str | None
    error: str | None


@dataclass
class BusinessReport:
    lead: LeadView
    business: Business
    analysis: WebsiteAnalysis | None
    rubric: list[RubricRow] = field(default_factory=list)
    seo: list[CheckRow] = field(default_factory=list)
    checklist: list[CheckRow] = field(default_factory=list)
    sources: list[SourceRow] = field(default_factory=list)
    candidates: list[CandidateRow] = field(default_factory=list)
    history: list[AnalysisRow] = field(default_factory=list)

    @property
    def facts(self) -> dict[str, Any]:
        return self.analysis.facts if self.analysis else {}


def rubric_rows(analysis: WebsiteAnalysis | None) -> list[RubricRow]:
    """Los 10 parámetros de la rúbrica, con su puntaje si se midió (los de IA quedan vacíos sin IA)."""
    scores = analysis.rubric_scores if analysis else {}
    rows = []
    for param in load_rubric()["parametros"]:
        score = scores.get(str(param["id"])) or {}
        rows.append(
            RubricRow(
                id=param["id"],
                nombre=param["nombre"],
                evalua=param["evalua"],
                puntaje=score.get("puntaje"),
                justificacion=score.get("justificacion"),
                evidencia=score.get("evidencia") or [],
            )
        )
    return rows


def _checks(items: dict[str, Any], labels: dict[str, str]) -> list[CheckRow]:
    ordered = [key for key in labels if key in items] + [key for key in items if key not in labels]
    return [
        CheckRow(nombre=labels.get(key, key), ok=items[key].get("ok"), detalle=items[key].get("detalle", ""))
        for key in ordered
    ]


def _source_text(value: Any) -> str:
    if isinstance(value, list):
        return " · ".join(str(item) for item in value) if value else "ninguno"
    text = str(value)
    return SOURCE_NAMES.get(text, text)


def source_rows(business: Business) -> list[SourceRow]:
    """De dónde salió cada dato (regla 2), en palabras simples."""
    sources = business.field_sources or {}
    ordered = [key for key in FIELD_LABELS if key in sources] + [key for key in sources if key not in FIELD_LABELS]
    return [SourceRow(dato=FIELD_LABELS.get(key, key), fuente=_source_text(sources[key])) for key in ordered]


def candidate_rows(business: Business) -> list[CandidateRow]:
    """Todos los WhatsApp encontrados, con su fuente y la evidencia exacta."""
    rows = []
    for raw in business.whatsapp_candidates or []:
        candidate = WhatsAppCandidate.model_validate(raw)
        rows.append(
            CandidateRow(
                number=pretty_number(candidate.number),
                url=candidate.url,
                source=SOURCE_NAMES.get(str(candidate.source), str(candidate.source)),
                placement=PLACEMENT_NAMES.get(str(candidate.placement), str(candidate.placement)),
                evidence=candidate.evidence,
                page_url=candidate.page_url,
                note=candidate.note,
                broken=candidate.broken,
            )
        )
    return rows


def business_report(repo: Repository, business_id: int) -> BusinessReport | None:
    business = repo.get_business(business_id)
    lead = lead_for_business(repo, business_id)
    if business is None or lead is None:
        return None
    analyses = repo.list_analyses(business_id)
    analysis = analyses[0] if analyses else None
    return BusinessReport(
        lead=lead,
        business=business,
        analysis=analysis,
        rubric=rubric_rows(analysis),
        seo=_checks(analysis.seo_local, SEO_LABELS) if analysis else [],
        checklist=_checks(analysis.info_checklist, CHECKLIST_LABELS) if analysis else [],
        sources=source_rows(business),
        candidates=candidate_rows(business),
        history=[
            AnalysisRow(
                id=item.id,
                analyzed_at=item.analyzed_at,
                score=item.website_score,
                findings=len(item.findings),
                model_name=item.model_name,
                error=item.error,
            )
            for item in analyses
        ],
    )
