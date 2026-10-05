"""Revisión automática de las webs de los leads al buscar (sin IA, con IA simulada y con error)."""

import asyncio
import zlib
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient

from app.analyze.collector import PageMetrics, WebCollection, WebCollector, WebVerification
from app.analyze.llm import LLMAnalysis, LLMError, LLMResult
from app.config import Settings
from app.db.repository import Repository
from app.db.schema import create_db_engine, init_db
from app.extract.whatsapp import extract_whatsapp
from app.leads import leads_for_day
from app.models import OpportunityLevel, WebsiteStatus, WhatsAppSource
from app.pipeline import LeadsProgress, run_leads_job
from app.sources.base import MapsListing, MapsPlace, MapsSource
from app.web.app import create_app

FILLER = "<p>" + "Cerrajería a domicilio en Rancagua, apertura de puertas y cambio de chapas. " * 6 + "</p>"
SITE = f"<html><head><title>Inicio</title></head><body><h1>Bienvenidos</h1>{FILLER}<p>© 2018</p></body></html>"


class OnePlace(MapsSource):
    def __init__(self, places: list[MapsPlace]) -> None:
        self.places = places

    async def search(self, query, limit):
        return [MapsListing(position=i + 1, name=p.name, url=p.maps_url) for i, p in enumerate(self.places)]

    async def get_details(self, listing):
        return next(p for p in self.places if p.maps_url == listing.url)


def place(name: str, website: str | None, phone: str | None = None, rating: float = 4.8, reviews: int = 150) -> MapsPlace:
    # Un celular distinto por negocio: si compartieran número, serían repetidos.
    phone = phone or f"+5699{zlib.crc32(name.encode()) % 10_000_000:07d}"
    return MapsPlace(
        maps_url=f"https://www.google.com/maps/place/{name}",
        name=name,
        phone_raw=phone,
        phone_e164=phone,
        website=website,
        website_kind="web_propia" if website else None,
        rating=rating,
        review_count=reviews,
        whatsapp=extract_whatsapp("", source=WhatsAppSource.MAPS),
    )


class MeasuredCollector(WebCollector):
    """Collector real (httpx simulado) que además devuelve mediciones de navegador inventadas."""

    async def collect(self, url, *, region=None):
        collection = await super().collect(url, region=region)
        if collection.verification.status == WebsiteStatus.OK:
            collection.desktop = PageMetrics(viewport="1440x900", load_ms=6500, fake_ctas_first_screen=["Llamar"],
                                             screenshot="screenshots/x/escritorio.jpg")
            collection.mobile = PageMetrics(viewport="390x844", zoomed_out_on_mobile=True, layout_width=980,
                                            screenshot="screenshots/x/celular.jpg")
        return collection


class FakeLLM:
    model_name = "claude-opus-5-5"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    async def analyze(self, evidence):
        self.calls += 1
        if self.fail:
            raise LLMError("Anthropic pidió esperar (límite de uso); se intentará en el próximo análisis.")
        analysis = LLMAnalysis.model_validate(
            {
                "parametros": [
                    {"id": i, "puntaje": 3, "justificacion": "mal", "evidencia": ["captura"]} for i in (1, 2, 3, 4, 6, 7, 8, 10)
                ],
                "problemas": [{"titulo": "No se entiende qué hace", "impacto": "Se van", "parametro": 1, "evidencia": ["H1"]}],
                "oportunidades": [{"titulo": "Rediseño", "descripcion": "Hero claro", "evidencia": ["captura"]}],
                "puntos_fuertes": [{"titulo": "Teléfono visible", "evidencia": ["tel"]}],
                "diferenciadores": {"ok": False, "detalle": "No"},
            }
        )
        return LLMResult(analysis=analysis, model=self.model_name, raw={})


@pytest.fixture
def repo(tmp_path) -> Repository:
    engine = create_db_engine(":memory:")
    init_db(engine)
    return Repository(engine)


def run(repo, tmp_path, places, llm=None, collector_cls=MeasuredCollector) -> LeadsProgress:
    routes = {
        "https://bueno.cl/": httpx.Response(200, text=SITE, headers={"content-type": "text/html"}),
        "https://caido.cl/": httpx.Response(500, text="error", headers={"content-type": "text/html"}),
    }

    def handler(request):
        return routes.get(str(request.url), httpx.Response(404))

    async def go():
        settings = Settings(_env_file=None, data_dir=tmp_path, web_max_internal_pages=0)
        async with collector_cls(settings, transport=httpx.MockTransport(handler), use_browser=False) as collector:
            return await run_leads_job(OnePlace(places), collector, repo, LeadsProgress(query="x", target=10), llm=llm)

    return asyncio.run(go())


def test_leads_with_working_web_are_reviewed_without_ai(repo, tmp_path):
    progress = run(repo, tmp_path, [place("Bueno", "https://bueno.cl/"), place("Caido", "https://caido.cl/"), place("SinWeb", None)])
    assert (progress.webs_total, progress.webs_done) == (1, 1)  # solo la web que abre
    assert progress.message.endswith(
        "1 webs revisadas. 2 sin web o con la web caída (saltados). Prueba otra búsqueda para completar el día."
    )
    leads = {lead.name: lead for lead in leads_for_day(repo, date.today())}
    assert set(leads) == {"Bueno"}  # sin web o con la web caída no son leads (decisión del 2026-10-04)

    bueno = leads["Bueno"]
    codes = [f["codigo"] for f in bueno.findings]
    assert codes[:3] == ["boton_falso", "sin_whatsapp_web", "no_adaptada_celular"]
    assert "copyright_antiguo" in codes
    assert bueno.score is None and bueno.opportunity is None  # sin IA no hay puntaje
    assert bueno.screenshots == {"escritorio": "screenshots/x/escritorio.jpg", "celular": "screenshots/x/celular.jpg"}


def test_with_ai_the_lead_gets_a_score(repo, tmp_path):
    llm = FakeLLM()
    run(repo, tmp_path, [place("Bueno", "https://bueno.cl/")], llm=llm)
    lead = leads_for_day(repo, date.today())[0]
    assert llm.calls == 1
    # 8 × 3 (IA) + velocidad 2 (6,5 s) + enlaces 8 (1 botón falso)
    assert lead.score == 34
    assert lead.opportunity == OpportunityLevel.ALTA
    assert lead.problems[0]["titulo"] == "No se entiende qué hace"


def test_ai_error_keeps_the_automatic_findings(repo, tmp_path):
    run(repo, tmp_path, [place("Bueno", "https://bueno.cl/")], llm=FakeLLM(fail=True))
    lead = leads_for_day(repo, date.today())[0]
    assert lead.score is None
    assert lead.findings  # los hallazgos automáticos siguen ahí
    assert "límite de uso" in lead.review_error


def test_stop_skips_the_web_review(repo, tmp_path):
    async def go():
        settings = Settings(_env_file=None, data_dir=tmp_path)
        async with MeasuredCollector(settings, transport=httpx.MockTransport(lambda r: httpx.Response(404)), use_browser=False) as c:
            return await run_leads_job(OnePlace([place("Bueno", "https://bueno.cl/")]), c, repo,
                                       LeadsProgress(query="x", target=10, stop_requested=True))

    progress = asyncio.run(go())
    assert progress.webs_total == 0


def test_page_shows_findings_and_score(repo, tmp_path):
    run(repo, tmp_path, [place("Bueno", "https://bueno.cl/")], llm=FakeLLM())
    app = create_app(Settings(_env_file=None, data_dir=tmp_path), repo=repo, runner=lambda p, r: None)
    with TestClient(app) as client:
        html = client.get("/leads").text
    assert "El botón “Llamar” no hace nada al tocarlo" in html
    assert "Puntaje 34/100" in html
    assert "Oportunidad alta" in html
    assert "/capturas/x/escritorio.jpg" in html
    assert "Según la IA" in html


def test_page_says_ai_is_off_without_key(repo, tmp_path):
    run(repo, tmp_path, [place("Bueno", "https://bueno.cl/")])
    app = create_app(Settings(_env_file=None, data_dir=tmp_path), repo=repo, runner=lambda p, r: None)
    with TestClient(app) as client:
        html = client.get("/leads").text
    assert "Análisis con IA apagado" in html
    assert "Puntaje" not in html
