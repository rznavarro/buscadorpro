"""Análisis con IA sin gastar: Anthropic se simula con httpx.MockTransport.

Así se prueba la petición exacta que se enviaría y cómo se lee la respuesta, sin clave real.
"""

import asyncio
import json

import httpx
import pytest

from app.analyze.llm import (
    AnthropicLLMClient,
    EvidencePackage,
    LLMAnalysis,
    LLMError,
    build_llm_client,
    clean_analysis,
    output_schema,
    system_prompt,
)
from app.analyze.report import build_analysis
from app.analyze.findings import FindingsReport
from app.analyze.collector import PageMetrics, WebCollection, WebVerification
from app.analyze.scorer import ParamScore
from app.config import Settings
from app.models import Business, OpportunityLevel, WebsiteStatus

AI_IDS = [1, 2, 3, 4, 6, 7, 8, 10]


def ai_json(score: int = 4, **overrides) -> dict:
    data = {
        "parametros": [
            {"id": i, "puntaje": score, "justificacion": f"Parámetro {i}", "evidencia": [f"captura escritorio: detalle {i}"]}
            for i in AI_IDS
        ],
        "problemas": [
            {"titulo": "No se entiende qué hace el negocio", "impacto": "El cliente se va", "parametro": 1, "evidencia": ["H1: Bienvenidos"]},
            {"titulo": "Sin fotos de trabajos", "impacto": "No genera confianza", "parametro": 3, "evidencia": ["0 imágenes propias"]},
            {"titulo": "Problema inventado", "impacto": "?", "parametro": 2, "evidencia": []},
        ],
        "oportunidades": [{"titulo": "Botón de WhatsApp flotante", "descripcion": "Contacto en un toque", "evidencia": ["sin WhatsApp en la web"]}],
        "puntos_fuertes": [{"titulo": "Teléfono visible", "evidencia": ["tel:+56993557317"]}],
        "diferenciadores": {"ok": False, "detalle": "No explica qué lo hace distinto"},
    }
    data.update(overrides)
    return data


def api_response(content: dict | str, stop_reason: str = "end_turn") -> dict:
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    return {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "content": [{"type": "thinking", "thinking": "", "signature": "s"}, {"type": "text", "text": text}],
        "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 12000, "output_tokens": 3000, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 2500},
    }


def evidence(tmp_path) -> EvidencePackage:
    shot = tmp_path / "escritorio.jpg"
    shot.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    return EvidencePackage(
        business={"nombre": "Cerrajería XYZ", "rubro": "Cerrajero", "ciudad": "Rancagua", "rating": 4.7, "resenas": 183},
        url="https://cerrajeriaxyz.cl/",
        facts={"https": True},
        findings=[{"codigo": "boton_falso", "titulo": "El botón “Llamar” no hace nada al tocarlo"}],
        code_scores=[ParamScore(id=5, nombre="Velocidad de carga", puntaje=4, justificacion="5,1 s", fuente="codigo")],
        visible_text="Bienvenidos " * 2000,
        screenshots={"escritorio": shot, "celular": tmp_path / "no-existe.jpg"},
    )


def run_client(tmp_path, response: httpx.Response, sent: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if sent is not None:
            sent.append(request)
        return response

    async def go():
        settings = Settings(_env_file=None, anthropic_api_key="sk-ant-test", llm_model="claude-opus-5-5", llm_effort="medium")
        client = AnthropicLLMClient(settings, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        client._client = client._client.with_options(max_retries=0)
        return await client.analyze(evidence(tmp_path))

    return asyncio.run(go())


def test_request_sent_to_anthropic(tmp_path):
    sent: list[httpx.Request] = []
    run_client(tmp_path, httpx.Response(200, json=api_response(ai_json())), sent)
    request = sent[0]
    body = json.loads(request.content)
    assert request.url.path == "/v1/messages"
    assert request.headers["anthropic-beta"] == "server-side-fallback-2026-07-01"
    assert body["model"] == "claude-opus-5-5"
    assert body["fallbacks"] == "default"
    assert body["output_config"]["effort"] == "medium"
    assert body["output_config"]["format"] == {"type": "json_schema", "schema": output_schema()}
    assert "temperature" not in body  # los modelos actuales no la aceptan
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    content = body["messages"][0]["content"]
    images = [block for block in content if block["type"] == "image"]
    assert len(images) == 1  # la captura que no existe se omite
    assert images[0]["source"]["media_type"] == "image/jpeg"
    texts = " ".join(block["text"] for block in content if block["type"] == "text")
    assert "El botón “Llamar” no hace nada al tocarlo" in texts
    assert len(texts) < 12000  # el texto visible va recortado


def test_response_is_read_and_cleaned(tmp_path):
    result = run_client(tmp_path, httpx.Response(200, json=api_response(ai_json())))
    assert [p.id for p in result.analysis.parametros] == AI_IDS
    assert [p.titulo for p in result.analysis.problemas] == ["No se entiende qué hace el negocio", "Sin fotos de trabajos"]
    assert result.discarded == ["Problema sin evidencia: Problema inventado"]
    assert result.model == "claude-opus-5-5"
    assert result.usage["output_tokens"] == 3000


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(200, json=api_response(ai_json(), stop_reason="refusal")), "se negó"),
        (httpx.Response(200, json=api_response(ai_json(), stop_reason="max_tokens")), "se cortó"),
        (httpx.Response(200, json=api_response("esto no es json")), "formato inválido"),
        (httpx.Response(401, json={"type": "error", "error": {"type": "authentication_error", "message": "bad key"}}), "no es válida"),
        (httpx.Response(429, json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}}), "límite de uso"),
    ],
)
def test_errors_are_explained(tmp_path, response, message):
    with pytest.raises(LLMError, match=message):
        run_client(tmp_path, response)


def test_ai_cannot_score_code_parameters_or_repeat_them():
    raw = ai_json()
    raw["parametros"].append({"id": 4, "puntaje": 10, "justificacion": "otra vez", "evidencia": ["x"]})
    analysis = LLMAnalysis.model_validate(raw)
    analysis.parametros.append(analysis.parametros[0].model_copy(update={"id": 5}))  # el 5 es de código
    cleaned, discarded = clean_analysis(analysis)
    assert [p.id for p in cleaned.parametros] == AI_IDS
    assert next(p for p in cleaned.parametros if p.id == 4).puntaje == 4  # el repetido se descarta
    assert len(discarded) == 3  # el repetido, el 5 y el problema sin evidencia


def test_schema_and_prompt_follow_the_rubric():
    schema = output_schema()
    assert schema["properties"]["parametros"]["items"]["properties"]["id"]["enum"] == AI_IDS
    assert schema["additionalProperties"] is False
    prompt = system_prompt()
    assert "Primera impresión / Hero" in prompt and "1-3: En 5 segundos no se entiende" in prompt
    assert "Velocidad de carga" not in prompt.split("Rúbrica")[1]  # la IA no puntúa los parámetros de código


def test_ai_is_off_without_key():
    assert build_llm_client(Settings(_env_file=None, anthropic_api_key="")) is None
    assert build_llm_client(Settings(_env_file=None, anthropic_api_key="sk-ant-x")) is not None


# --- Informe con y sin IA ------------------------------------------------------------------


def _collection() -> WebCollection:
    verification = WebVerification(url="https://x.cl/", final_url="https://x.cl/", status=WebsiteStatus.OK)
    desktop = PageMetrics(viewport="1440x900", load_ms=5100, screenshot="screenshots/x/escritorio.jpg")
    return WebCollection(verification=verification, desktop=desktop)


BUSINESS = Business(id=1, google_maps_url="https://www.google.com/maps/place/x", business_name="X", rating=4.7, review_count=183)


def test_report_without_ai_has_no_total_score():
    analysis = build_analysis(BUSINESS, _collection(), FindingsReport(link_errors=["Enlace roto: /a"]))
    assert analysis.website_score is None
    assert analysis.model_name is None
    assert set(analysis.rubric_scores) == {"5", "9"}
    assert analysis.rubric_scores["5"]["puntaje"] == 4  # 5,1 s → 4
    assert analysis.rubric_scores["9"]["puntaje"] == 8  # 1 error → 8
    assert analysis.opportunity_level is None
    assert analysis.screenshots == {"escritorio": "screenshots/x/escritorio.jpg"}


def test_report_with_ai_sums_all_parameters(tmp_path):
    result = run_client(tmp_path, httpx.Response(200, json=api_response(ai_json(score=4))))
    analysis = build_analysis(BUSINESS, _collection(), FindingsReport(), llm=result)
    # 8 parámetros de IA con 4 + velocidad 4 (5,1 s) + enlaces 10 (sin errores)
    assert analysis.website_score == 8 * 4 + 4 + 10
    assert analysis.rubric_scores["5"]["fuente"] == "codigo"
    assert analysis.rubric_scores["1"]["fuente"] == "ia"
    assert analysis.opportunity_level == OpportunityLevel.ALTA  # 46 < 50, 4,7 ★ y 183 reseñas
    assert analysis.model_name == "claude-opus-5-5"
    assert analysis.info_checklist["diferenciadores"]["ok"] is False
    assert len(analysis.main_problems) == 2


def test_report_with_incomplete_ai_has_no_score(tmp_path):
    raw = ai_json()
    raw["parametros"] = raw["parametros"][:5]
    result = run_client(tmp_path, httpx.Response(200, json=api_response(raw)))
    analysis = build_analysis(BUSINESS, _collection(), FindingsReport(), llm=result)
    assert analysis.website_score is None
    assert "no puntuó todos los parámetros" in analysis.error
