"""Puntaje según la rúbrica fija de Vortexia (sección 8.2) y nivel de oportunidad (8.5).

Velocidad (5) y enlaces (9) los calcula el código con tablas fijas; la IA nunca los
sobrescribe. El score total (10–100) solo existe cuando la IA puntuó los otros 8.
"""

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from app.models import OpportunityLevel

RUBRIC_PATH = Path(__file__).parent / "rubric.yaml"
CODE_PARAMS = (5, 9)


class ParamScore(BaseModel):
    id: int
    nombre: str
    puntaje: int
    justificacion: str
    evidencia: list[str] = []
    fuente: str  # "codigo" o "ia"


@lru_cache
def load_rubric(path: Path = RUBRIC_PATH) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def rubric_version() -> str:
    return str(load_rubric()["version"])


def param_name(param_id: int) -> str:
    return next(p["nombre"] for p in load_rubric()["parametros"] if p["id"] == param_id)


def ai_params() -> list[dict[str, Any]]:
    """Parámetros que puntúa la IA (todos menos los de código)."""
    return [p for p in load_rubric()["parametros"] if p["evalua"] == "ia"]


def speed_score(load_ms: int | None, psi_mobile: int | None) -> ParamScore | None:
    """Parámetro 5. Con PageSpeed: round(psi/10), mínimo 1. Sin PageSpeed: tabla por segundos."""
    rubric = load_rubric()["velocidad"]
    if psi_mobile is not None:
        score = max(1, round(psi_mobile / 10))
        reason = f"PageSpeed móvil: {psi_mobile}/100."
    elif load_ms is not None:
        seconds = load_ms / 1000
        score = next((row["puntaje"] for row in rubric["por_tiempo"] if seconds < row["menos_de"]), rubric["resto"])
        reason = f"Cargó en {seconds:.1f} s (medido desde este computador: es referencial).".replace(".", ",", 1)
    else:
        return None
    return ParamScore(id=5, nombre=param_name(5), puntaje=score, justificacion=reason, fuente="codigo")


def links_score(errors: list[str]) -> ParamScore:
    """Parámetro 9. Errores = enlaces internos rotos + botones de WhatsApp rotos + botones falsos."""
    count = len(errors)
    score = 2
    for row in load_rubric()["enlaces"]:
        if row.get("resto") or count <= row["hasta"]:
            score = row["puntaje"]
            break
    reason = "Ningún enlace o botón roto." if not count else f"{count} enlace(s) o botón(es) que no funcionan."
    return ParamScore(id=9, nombre=param_name(9), puntaje=score, justificacion=reason, evidencia=errors[:10], fuente="codigo")


def total_score(scores: list[ParamScore]) -> int | None:
    """Suma de los 10 parámetros (10–100). None si falta alguno (por ejemplo, sin IA)."""
    by_id = {s.id: s.puntaje for s in scores}
    expected = {p["id"] for p in load_rubric()["parametros"]}
    if set(by_id) != expected:
        return None
    return sum(by_id.values())


def opportunity_level(
    score: int | None, *, has_own_website: bool, rating: float | None, reviews: int | None
) -> OpportunityLevel | None:
    """Sección 8.5. Decisión: un score < 50 sin el rating o las reseñas de ALTA queda MEDIA."""
    rules = load_rubric()["oportunidad"]
    good_reputation = (rating or 0) >= rules["alta_rating_minimo"] and (reviews or 0) >= rules["alta_resenas_minimo"]
    if not has_own_website:
        return OpportunityLevel.ALTA if good_reputation else OpportunityLevel.MEDIA
    if score is None:
        return None
    if score < rules["alta_score_menor_a"]:
        return OpportunityLevel.ALTA if good_reputation else OpportunityLevel.MEDIA
    if score < rules["baja_score_desde"]:
        return OpportunityLevel.MEDIA
    return OpportunityLevel.BAJA
