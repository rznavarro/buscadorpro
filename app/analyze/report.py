"""Arma el análisis de una web (fila de `website_analyses`) a partir de lo medido y, si hay, la IA."""

from typing import Any

from app.analyze.collector import WebCollection
from app.analyze.findings import CheckItem, FindingsReport
from app.analyze.llm import EvidencePackage, LLMResult
from app.analyze.scorer import (
    ParamScore,
    links_score,
    opportunity_level,
    param_name,
    rubric_version,
    speed_score,
    total_score,
)
from app.config import Settings
from app.models import Business, WebsiteAnalysis


def code_scores(collection: WebCollection, findings: FindingsReport) -> list[ParamScore]:
    """Parámetros 5 (velocidad) y 9 (enlaces): siempre por código, nunca por la IA."""
    scores = [links_score(findings.link_errors)]
    speed = speed_score(collection.desktop.load_ms if collection.desktop else None, collection.psi_mobile_score)
    if speed is not None:
        scores.insert(0, speed)
    return scores


def compact_facts(collection: WebCollection) -> dict[str, Any]:
    """Hechos medidos, sin el texto completo de la página (para guardar y para la IA)."""
    verification, facts = collection.verification, collection.facts
    data: dict[str, Any] = {
        "url_final": verification.final_url,
        "https": verification.https,
        "abierta_con_navegador": verification.rendered,
        "paginas_revisadas": verification.pages_visited,
        "whatsapp_en_web": [c.number for c in verification.whatsapp_candidates if c.number],
        "boton_whatsapp_flotante": verification.whatsapp_floating,
        "redes": verification.socials,
        "escritorio": collection.desktop.model_dump() if collection.desktop else None,
        "celular": collection.mobile.model_dump() if collection.mobile else None,
        "enlaces_rotos": [b.model_dump() for b in collection.broken_links],
        "enlaces_revisados": collection.links_checked,
        "imagenes_rotas": [b.model_dump() for b in collection.broken_images],
        "imagenes_revisadas": collection.images_checked,
        "pagespeed_movil": collection.psi_mobile_score,
        "notas": collection.notes,
    }
    if facts is not None:
        data["pagina"] = facts.model_dump(mode="json", exclude={"whatsapp", "visible_text", "internal_links"})
        data["pagina"]["enlaces_internos"] = len(facts.internal_links)
    return data


def screenshots(collection: WebCollection) -> dict[str, str]:
    shots: dict[str, str] = {}
    for label, metrics in (("escritorio", collection.desktop), ("celular", collection.mobile)):
        if metrics is None:
            continue
        if metrics.screenshot:
            shots[label] = metrics.screenshot
        if metrics.screenshot_full:
            shots[f"{label}_completa"] = metrics.screenshot_full
    return shots


def evidence_package(
    business: Business, collection: WebCollection, findings: FindingsReport, settings: Settings
) -> EvidencePackage:
    """Lo que ve la IA: datos del negocio, hechos, hallazgos, texto visible y capturas."""
    shots = screenshots(collection)
    return EvidencePackage(
        business={
            "nombre": business.business_name,
            "rubro": business.category,
            "ciudad": business.city,
            "rating": business.rating,
            "resenas": business.review_count,
        },
        url=collection.verification.final_url or collection.verification.url,
        facts=compact_facts(collection),
        findings=[f.model_dump(exclude={"captura"}) for f in findings.hallazgos],
        code_scores=code_scores(collection, findings),
        visible_text=collection.facts.visible_text if collection.facts else "",
        screenshots={
            label: settings.data_dir / path
            for label, path in shots.items()
            if label in ("escritorio", "celular", "escritorio_completa", "celular_completa")
        },
    )


def build_analysis(
    business: Business,
    collection: WebCollection,
    findings: FindingsReport,
    *,
    llm: LLMResult | None = None,
    error: str | None = None,
) -> WebsiteAnalysis:
    """Fila de `website_analyses`. Sin IA: hechos, hallazgos y puntajes de código, sin score total."""
    scores = code_scores(collection, findings)
    checklist: dict[str, CheckItem] = dict(findings.checklist)
    problems: list[dict[str, Any]] = []
    opportunities: list[dict[str, Any]] = []
    strengths: list[dict[str, Any]] = []
    if llm is not None:
        scores += [
            ParamScore(
                id=j.id, nombre=param_name(j.id), puntaje=j.puntaje,
                justificacion=j.justificacion, evidencia=j.evidencia, fuente="ia",
            )
            for j in llm.analysis.parametros
        ]
        problems = [p.model_dump() for p in llm.analysis.problemas]
        opportunities = [o.model_dump() for o in llm.analysis.oportunidades]
        strengths = [s.model_dump() for s in llm.analysis.puntos_fuertes]
        differentiators = llm.analysis.diferenciadores
        checklist["diferenciadores"] = CheckItem(ok=differentiators.ok, detalle=differentiators.detalle)

    score = total_score(scores) if llm is not None else None
    if llm is not None and score is None:
        error = error or "La IA no puntuó todos los parámetros con evidencia: no hay score total."
    return WebsiteAnalysis(
        business_id=business.id,
        url_analyzed=collection.verification.url,
        final_url=collection.verification.final_url,
        rubric_version=rubric_version(),
        model_name=llm.model if llm else None,
        website_score=score,
        rubric_scores={str(s.id): s.model_dump() for s in sorted(scores, key=lambda s: s.id)},
        facts=compact_facts(collection),
        findings=[f.model_dump() for f in findings.hallazgos],
        seo_local={key: item.model_dump() for key, item in findings.seo_local.items()},
        info_checklist={key: item.model_dump() for key, item in checklist.items()},
        main_problems=problems,
        opportunities=opportunities,
        strengths=strengths,
        opportunity_level=opportunity_level(
            score, has_own_website=True, rating=business.rating, reviews=business.review_count
        ),
        screenshots=screenshots(collection),
        load_time_ms=collection.desktop.load_ms if collection.desktop else None,
        psi_mobile_score=collection.psi_mobile_score,
        raw_llm_output=(
            {"prompt_version": llm.prompt_version, "usage": llm.usage, "descartado": llm.discarded, "respuesta": llm.raw}
            if llm
            else None
        ),
        error=error,
    )
