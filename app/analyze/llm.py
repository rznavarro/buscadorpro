"""Análisis con IA (sección 8.4): la IA solo razona sobre evidencia; nunca extrae datos.

Recibe capturas (escritorio y celular), los hechos medidos, los hallazgos automáticos y el
texto visible, y puntúa los 8 parámetros de la rúbrica que le corresponden. Velocidad (5) y
enlaces (9) los calcula el código y la IA nunca los toca.

Queda apagado mientras no haya `ANTHROPIC_API_KEY` en `.env` (decisión de Joaquín: por
ahora no se gasta dinero en IA).

SDK: browser-use fija `anthropic==0.76.0`. Esa versión no trae como parámetros con nombre
`output_config.format` ni `fallbacks`, así que se envían con `extra_body` (mecanismo estándar
del SDK) y la respuesta se lee como JSON crudo, para no depender de los tipos de esa versión.
"""

import base64
import json
from pathlib import Path
from typing import Any, Protocol

import anthropic
import httpx
from pydantic import BaseModel, Field, ValidationError

from app.analyze.scorer import ParamScore, ai_params, load_rubric
from app.config import Settings

PROMPT_VERSION = "2026-10-04.1"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_VISIBLE_TEXT = 6000


# --- Entrada y salida ------------------------------------------------------------------


class EvidencePackage(BaseModel):
    """Todo lo que ve la IA de un negocio y su web."""

    business: dict[str, Any]  # nombre, rubro, ciudad, rating, reseñas
    url: str
    facts: dict[str, Any]
    findings: list[dict[str, Any]]
    code_scores: list[ParamScore]
    visible_text: str
    screenshots: dict[str, Path]  # etiqueta → ruta absoluta de la captura


class ParamJudgment(BaseModel):
    id: int
    puntaje: int
    justificacion: str
    evidencia: list[str]


class Problem(BaseModel):
    titulo: str
    impacto: str
    parametro: int
    evidencia: list[str]


class Opportunity(BaseModel):
    titulo: str
    descripcion: str
    evidencia: list[str]


class Strength(BaseModel):
    titulo: str
    evidencia: list[str]


class Differentiators(BaseModel):
    ok: bool
    detalle: str


class LLMAnalysis(BaseModel):
    parametros: list[ParamJudgment]
    problemas: list[Problem]
    oportunidades: list[Opportunity]
    puntos_fuertes: list[Strength]
    diferenciadores: Differentiators


class LLMResult(BaseModel):
    analysis: LLMAnalysis
    model: str
    prompt_version: str = PROMPT_VERSION
    raw: dict[str, Any]
    usage: dict[str, Any] = Field(default_factory=dict)
    discarded: list[str] = Field(default_factory=list)  # lo que se descartó por no traer evidencia


class LLMError(RuntimeError):
    """La IA no entregó un análisis usable (rechazo, respuesta cortada o formato inválido)."""


class LLMClient(Protocol):
    """Interfaz para cambiar de proveedor o modelo sin tocar el resto del sistema."""

    model_name: str

    async def analyze(self, evidence: EvidencePackage) -> LLMResult: ...


# --- Prompt (versionado) -----------------------------------------------------------------


def system_prompt() -> str:
    """Instrucciones fijas + rúbrica. No cambia entre negocios: se guarda en caché."""
    rubric = load_rubric()
    params = []
    for param in ai_params():
        bands = "\n".join(f"    {band}: {text}" for band, text in param["tramos"].items())
        params.append(f"- Parámetro {param['id']} — {param['nombre']}\n  Se basa en: {param['se_basa_en']}\n{bands}")
    return f"""Eres un auditor de sitios web de negocios locales para Vortexia, una agencia que diseña y rediseña webs.
Tu trabajo es evaluar la web de un negocio con la rúbrica fija de Vortexia (versión {rubric['version']}) y detectar problemas,
oportunidades y puntos fuertes. Responde siempre en español.

Reglas obligatorias:
1. Todo puntaje, problema, oportunidad y punto fuerte necesita evidencia observable: un texto que aparece en la web, un elemento
   concreto, un hecho medido o lo que se ve en una captura (indica cuál). Si no tienes evidencia, no lo incluyas.
2. No inventes nada: ni servicios, ni datos de contacto, ni problemas. Solo vale lo que está en las capturas, los hechos o el texto.
3. Los parámetros 5 (velocidad) y 9 (enlaces) ya vienen calculados por código: no los puntúes ni los contradigas.
4. Los "hallazgos automáticos" son hechos medidos: úsalos como evidencia, sin repetirlos palabra por palabra.
5. Escribe problemas y oportunidades en lenguaje de negocio: lo que siente o no encuentra el cliente que entra a la web.
   Nada de código, etiquetas HTML ni detalles técnicos en el título o el impacto; lo técnico va en "evidencia".
6. Las oportunidades deben ser cosas que un rediseño web de Vortexia resuelve (rediseño visual, botón de WhatsApp, SEO local,
   testimonios, mejor presentación de servicios, galería de trabajos reales, adaptación al celular, etc.).
7. Puntúa con los tramos de la rúbrica de forma consistente: si dudas entre dos tramos, elige el que mejor describe la evidencia.

Rúbrica (cada parámetro de 1 a 10; tú puntúas solo estos 8):
{chr(10).join(params)}

Qué entregar:
- "parametros": exactamente un elemento por cada parámetro de arriba (ids {', '.join(str(p['id']) for p in ai_params())}),
  con puntaje 1–10, una justificación breve y su evidencia.
- "problemas": de 3 a 5, ordenados del que más afecta al negocio al que menos, con el parámetro de la rúbrica relacionado.
- "oportunidades": de 3 a 6.
- "puntos_fuertes": de 1 a 3 puntos positivos reales de la web.
- "diferenciadores": si la web explica qué hace distinto a este negocio de su competencia (ok) y por qué.
"""


def output_schema() -> dict[str, Any]:
    """Esquema JSON estricto de la respuesta."""
    ids = [p["id"] for p in ai_params()]
    evidence = {"type": "array", "items": {"type": "string"}}

    def obj(properties: dict[str, Any]) -> dict[str, Any]:
        return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}

    return obj(
        {
            "parametros": {
                "type": "array",
                "items": obj(
                    {
                        "id": {"type": "integer", "enum": ids},
                        "puntaje": {"type": "integer", "enum": list(range(1, 11))},
                        "justificacion": {"type": "string"},
                        "evidencia": evidence,
                    }
                ),
            },
            "problemas": {
                "type": "array",
                "items": obj(
                    {
                        "titulo": {"type": "string"},
                        "impacto": {"type": "string"},
                        "parametro": {"type": "integer", "enum": list(range(1, 11))},
                        "evidencia": evidence,
                    }
                ),
            },
            "oportunidades": {
                "type": "array",
                "items": obj({"titulo": {"type": "string"}, "descripcion": {"type": "string"}, "evidencia": evidence}),
            },
            "puntos_fuertes": {"type": "array", "items": obj({"titulo": {"type": "string"}, "evidencia": evidence})},
            "diferenciadores": obj({"ok": {"type": "boolean"}, "detalle": {"type": "string"}}),
        }
    )


def user_content(evidence: EvidencePackage) -> list[dict[str, Any]]:
    """Datos del negocio, hechos, hallazgos, texto visible y capturas."""
    data = {
        "negocio": evidence.business,
        "url": evidence.url,
        "puntajes_calculados_por_codigo": [s.model_dump() for s in evidence.code_scores],
        "hallazgos_automaticos": evidence.findings,
        "hechos": evidence.facts,
    }
    content: list[dict[str, Any]] = [
        {"type": "text", "text": "Datos medidos de la web (JSON):\n" + json.dumps(data, ensure_ascii=False, sort_keys=True)},
        {"type": "text", "text": "Texto visible de la página de inicio (recortado):\n" + evidence.visible_text[:MAX_VISIBLE_TEXT]},
    ]
    for label, path in evidence.screenshots.items():
        if not path.exists():
            continue
        media_type = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        content.append({"type": "text", "text": f"Captura: {label}"})
        content.append(
            {
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": base64.standard_b64encode(path.read_bytes()).decode()},
            }
        )
    content.append({"type": "text", "text": "Evalúa esta web con la rúbrica y entrega el JSON pedido."})
    return content


# --- Validación (regla 4: sin evidencia, se descarta) ----------------------------------------


def clean_analysis(analysis: LLMAnalysis) -> tuple[LLMAnalysis, list[str]]:
    """Descarta lo que no trae evidencia, ajusta cantidades y deja un puntaje por parámetro."""
    discarded: list[str] = []
    allowed = {p["id"] for p in ai_params()}

    def has_evidence(items: list[str]) -> bool:
        return any(item.strip() for item in items)

    params: dict[int, ParamJudgment] = {}
    for judgment in analysis.parametros:
        if judgment.id not in allowed or judgment.id in params:
            discarded.append(f"Parámetro {judgment.id} repetido o que no le corresponde a la IA")
            continue
        if not has_evidence(judgment.evidencia):
            discarded.append(f"Parámetro {judgment.id} sin evidencia")
            continue
        params[judgment.id] = judgment.model_copy(update={"puntaje": min(10, max(1, judgment.puntaje))})

    def keep(items: list[Any], kind: str, limit: int) -> list[Any]:
        kept = []
        for item in items:
            if has_evidence(item.evidencia):
                kept.append(item)
            else:
                discarded.append(f"{kind} sin evidencia: {item.titulo}")
        return kept[:limit]

    cleaned = LLMAnalysis(
        parametros=[params[i] for i in sorted(params)],
        problemas=keep(analysis.problemas, "Problema", 5),
        oportunidades=keep(analysis.oportunidades, "Oportunidad", 6),
        puntos_fuertes=keep(analysis.puntos_fuertes, "Punto fuerte", 3),
        diferenciadores=analysis.diferenciadores,
    )
    return cleaned, discarded


# --- Implementación con Anthropic ---------------------------------------------------------


class AnthropicLLMClient:
    """Claude por la API de Anthropic. El modelo y el esfuerzo se configuran en `.env`."""

    def __init__(self, settings: Settings, *, http_client: httpx.AsyncClient | None = None) -> None:
        self.model_name = settings.llm_model
        self.effort = settings.llm_effort
        self._client = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key, http_client=http_client, timeout=300.0, max_retries=2
        )

    async def analyze(self, evidence: EvidencePackage) -> LLMResult:
        try:
            response = await self._client.beta.messages.with_raw_response.create(
                model=self.model_name,
                max_tokens=16000,
                betas=[FALLBACK_BETA],
                # Instrucciones + rúbrica fijas primero, marcadas para caché (no cambian entre negocios).
                system=[{"type": "text", "text": system_prompt(), "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user_content(evidence)}],
                extra_body={
                    # Sin temperatura: los modelos actuales no la aceptan. La consistencia sale de la
                    # rúbrica con tramos, el formato fijo y los parámetros calculados por código.
                    "output_config": {"effort": self.effort, "format": {"type": "json_schema", "schema": output_schema()}},
                    # Si el modelo rechaza la petición, el servidor la reintenta con otro modelo.
                    "fallbacks": "default",
                },
            )
        except anthropic.AuthenticationError as exc:
            raise LLMError("La clave de Anthropic (ANTHROPIC_API_KEY) no es válida.") from exc
        except anthropic.RateLimitError as exc:
            raise LLMError("Anthropic pidió esperar (límite de uso); se intentará en el próximo análisis.") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Anthropic respondió con error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError("No se pudo conectar con Anthropic.") from exc

        data = response.http_response.json()
        stop_reason = data.get("stop_reason")
        if stop_reason == "refusal":
            raise LLMError("El modelo se negó a analizar esta web.")
        if stop_reason == "max_tokens":
            raise LLMError("La respuesta de la IA se cortó antes de terminar.")
        text = next((block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"), "")
        try:
            analysis = LLMAnalysis.model_validate_json(text)
        except ValidationError as exc:
            raise LLMError("La IA respondió con un formato inválido.") from exc
        cleaned, discarded = clean_analysis(analysis)
        return LLMResult(
            analysis=cleaned,
            model=data.get("model", self.model_name),
            raw=data,
            usage=data.get("usage") or {},
            discarded=discarded,
        )


def build_llm_client(settings: Settings) -> LLMClient | None:
    """El cliente de IA si hay clave en `.env`; si no, None (la IA queda apagada, costo $0)."""
    if not settings.ai_enabled:
        return None
    return AnthropicLLMClient(settings)
