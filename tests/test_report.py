"""Fase 6: informe por negocio, filtros, exportar con filtros, caché de análisis y Re-analizar."""

import asyncio
import time
from datetime import date, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db.repository import Repository
from app.db.schema import create_db_engine, init_db
from app.leads import FILTERS, LeadView, filter_leads, leads_for_day
from app.models import Business, WebsiteAnalysis, WebsiteStatus, utcnow
from app.pipeline import LeadsProgress, review_website, review_websites, run_leads_job
from app.web.app import create_app
from tests.test_review import SITE, FakeLLM, MeasuredCollector, OnePlace, place, run


@pytest.fixture
def repo() -> Repository:
    engine = create_db_engine(":memory:")
    init_db(engine)
    return Repository(engine)


# --- Filtros ---------------------------------------------------------------------------------


def lead(**changes) -> LeadView:
    base = dict(
        id=1, name="X", category=None, city=None, address=None, query="q", position=1,
        whatsapp_state="verificado", whatsapp_url="https://wa.me/56993557317", whatsapp_number=None,
        confidence="ALTA", confidence_reason=None, broken_button=False, whatsapp_check=None, phone=None,
        website="https://x.cl/", website_status="OK", website_reason=None, rating=4.8, reviews=150,
        maps_url="https://www.google.com/maps/place/x", instagram=None, facebook=None, discarded=False,
    )
    return LeadView(**{**base, **changes})


SERIOUS = [{"titulo": "Botón falso", "gravedad": "alta"}]


@pytest.mark.parametrize(
    ("key", "matching", "not_matching"),
    [
        ("whatsapp", lead(whatsapp_state="confirmado"), lead(whatsapp_state="por_probar")),
        ("probar", lead(whatsapp_state="por_probar"), lead()),
        ("web_mala", lead(score=42), lead(score=72, findings=SERIOUS)),  # con IA manda el puntaje
        ("web_mala", lead(findings=SERIOUS), lead(findings=[{"titulo": "x", "gravedad": "media"}])),  # sin IA
        ("web_mala", lead(findings=SERIOUS), lead(website_status="CAIDO", findings=SERIOUS)),  # solo webs que abren
        ("rating", lead(rating=4.6), lead(rating=4.5)),
        ("resenas", lead(reviews=101), lead(reviews=100)),
        ("whatsapp_web_mala", lead(score=30), lead(whatsapp_state="por_probar", score=30)),
        ("oportunidad", lead(opportunity="ALTA"), lead(opportunity="MEDIA")),
    ],
)
def test_each_filter(key, matching, not_matching):
    assert filter_leads([matching], [key]) == [matching]
    assert filter_leads([not_matching], [key]) == []


def test_filters_combine_and_unknown_ones_are_ignored():
    a = lead(id=1, rating=4.9, reviews=300, query="cerrajeros")
    b = lead(id=2, rating=4.9, reviews=20, query="cerrajeros")
    c = lead(id=3, rating=4.9, reviews=300, query="gasfíters")
    assert filter_leads([a, b, c], ["rating", "resenas"]) == [a, c]
    assert filter_leads([a, b, c], ["rating", "resenas"], "cerrajeros") == [a]
    assert filter_leads([a, b, c], ["no-existe"]) == [a, b, c]
    assert set(FILTERS) == {"whatsapp", "probar", "web_mala", "rating", "resenas", "whatsapp_web_mala", "oportunidad"}
    # Todos los leads tienen web que abre: ya no hay filtros "con web" ni "sin web".
    assert filter_leads([a, b, c], ["sin_web"]) == [a, b, c]


# --- Lista con filtros y CSV -------------------------------------------------------------------


def run_sites(repo, tmp_path, places, query="x"):
    """Como `run` de test_review, pero toda web abre (cada negocio tiene la suya)."""
    def handler(request):
        return httpx.Response(200, text=SITE, headers={"content-type": "text/html"})

    async def go():
        settings = Settings(_env_file=None, data_dir=tmp_path, web_max_internal_pages=0)
        async with MeasuredCollector(settings, transport=httpx.MockTransport(handler), use_browser=False) as collector:
            return await run_leads_job(OnePlace(places), collector, repo, LeadsProgress(query=query, target=10))

    return asyncio.run(go())


@pytest.fixture
def three_leads(repo, tmp_path):
    run_sites(repo, tmp_path, [
        place("Bueno", "https://bueno.cl/"),
        place("Regular", "https://regular.cl/", rating=4.2),
        place("Pocas", "https://pocas.cl/", reviews=12),
    ])
    return repo


def make_client(repo, tmp_path, reviewer=None) -> TestClient:
    async def no_runner(progress, repo):
        return progress

    return TestClient(create_app(Settings(_env_file=None, data_dir=tmp_path), repo=repo, runner=no_runner, reviewer=reviewer))


def test_list_filters_and_export_respect_the_chosen_filters(three_leads, tmp_path):
    today = date.today().isoformat()
    with make_client(three_leads, tmp_path) as client:
        page = client.get(f"/?dia={today}&filtro=rating").text
        assert "✓ Rating &gt; 4,5" in page  # el filtro se ve activo
        assert "2 leads</b> (de 3)" in page
        assert ">Regular<" not in page and ">Bueno<" in page and ">Pocas<" in page
        assert "Quitar filtros" in page
        assert "Sin web" not in page and "Con web" not in page
        # La lista que se recarga sola mantiene los filtros.
        assert f'hx-get="/leads?dia={today}&amp;orden=whatsapp&amp;filtro=rating"' in page

        csv = client.get(f"/exportar.csv?dia={today}&filtro=rating&filtro=resenas").content.decode("utf-8-sig")
        assert csv.count("\n") == 2  # títulos + 1 lead
        assert "Bueno" in csv and "Regular" not in csv and "Pocas" not in csv

        nothing = client.get(f"/?dia={today}&filtro=probar&filtro=whatsapp").text
        assert "Ningún lead de este día cumple los filtros" in nothing


def test_search_chips_filter_by_query(repo, tmp_path):
    run_sites(repo, tmp_path, [place("Bueno", "https://bueno.cl/")], query="cerrajeros en Rancagua")
    run_sites(repo, tmp_path, [place("Otro", "https://otro.cl/")], query="gasfíters en Rancagua")
    with make_client(repo, tmp_path) as client:
        page = client.get("/").text
        assert "Búsquedas:" in page and "gasfíters en Rancagua" in page
        only = client.get("/?busqueda=gasfíters en Rancagua").text
        assert ">Otro<" in only and ">Bueno<" not in only


# --- Informe del negocio -----------------------------------------------------------------------


def test_report_page_shows_everything_for_a_reviewed_web(repo, tmp_path):
    run(repo, tmp_path, [place("Bueno", "https://bueno.cl/")], llm=FakeLLM())
    business = leads_for_day(repo, date.today())[0]
    with make_client(repo, tmp_path) as client:
        html = client.get(f"/negocio/{business.id}").text
    assert "<h2>Bueno</h2>" in html
    assert "Abrir web" in html and "Abrir en Maps" in html
    assert "34/100" in html  # web score con color
    assert "Oportunidad alta" in html
    # Problemas (IA y automáticos), oportunidades y puntos fuertes
    assert "No se entiende qué hace" in html
    assert "El botón “Llamar” no hace nada al tocarlo" in html
    assert "Rediseño" in html and "Teléfono visible" in html
    # Los 10 parámetros de la rúbrica
    for name in ("Primera impresión / Hero", "Velocidad de carga", "Coherencia y funcionalidad de enlaces"):
        assert name in html
    assert "medido por código" in html
    # SEO local, checklist, capturas, fuentes, evidencia técnica y Re-analizar
    assert "SEO local" in html and "Checklist de información" in html and "Descripción para Google" in html
    assert 'src="/capturas/x/escritorio.jpg"' in html
    assert "De dónde salió cada dato" in html and "Google Maps" in html
    assert "Evidencia técnica de la última revisión" in html
    assert "Re-analizar web" in html


def test_report_page_without_ai_says_what_is_missing(repo, tmp_path):
    run(repo, tmp_path, [place("Bueno", "https://bueno.cl/")])
    business = leads_for_day(repo, date.today())[0]
    with make_client(repo, tmp_path) as client:
        html = client.get(f"/negocio/{business.id}").text
    assert "Sin score: el puntaje total lo da la IA (apagada)" in html
    assert "Las redacta la IA a partir de los problemas (apagada" in html
    assert "la IA está apagada" in html  # en los parámetros de IA de la rúbrica


def test_report_page_for_a_mobile_without_whatsapp_has_copy_and_try(repo, tmp_path):
    run(repo, tmp_path, [place("Bueno", "https://bueno.cl/"), place("SinWeb", None)])
    business = leads_for_day(repo, date.today())[0]
    with make_client(repo, tmp_path) as client:
        html = client.get(f"/negocio/{business.id}").text
        assert "ABRIR WHATSAPP" not in html  # sección 10: solo si es VERIFICADO
        assert "Copiar teléfono" in html and "Probar WhatsApp" in html
        assert "No publica ningún enlace de WhatsApp" in html
        # Un negocio sin web no es lead, pero su informe igual se puede abrir.
        no_web = repo.find_business(google_maps_url="https://www.google.com/maps/place/SinWeb")
        assert "No tiene web para revisar." in client.get(f"/negocio/{no_web.id}").text


def test_report_page_of_a_verified_whatsapp_shows_the_evidence(repo, tmp_path):
    from app.extract.whatsapp import extract_whatsapp
    from app.models import WhatsAppSource
    from app.sources.base import MapsPlace

    maps_place = MapsPlace(
        maps_url="https://www.google.com/maps/place/JT", name="JT Keys", phone_raw="+56957734621",
        phone_e164="+56957734621",
        whatsapp=extract_whatsapp('<a href="http://wa.me/56957734621">WA</a>', source=WhatsAppSource.MAPS),
    )
    business, _ = repo.upsert_business(maps_place.to_business())
    with make_client(repo, tmp_path) as client:
        html = client.get(f"/negocio/{business.id}").text
    assert "ABRIR WHATSAPP" in html and "https://wa.me/56957734621" in html
    assert "+56 9 5773 4621" in html  # número legible en la lista de WhatsApp encontrados
    assert "http://wa.me/56957734621" in html  # evidencia: el enlace tal cual


def test_report_page_of_a_duplicate_explains_why(repo, tmp_path):
    business, _ = repo.upsert_business(
        Business(google_maps_url="https://www.google.com/maps/place/d", business_name="Dup", duplicate_reason="Tiene el mismo número que «X».")
    )
    with make_client(repo, tmp_path) as client:
        html = client.get(f"/negocio/{business.id}").text
        assert "Repetido (no aparece en tus leads): Tiene el mismo número que «X»." in html
        assert client.get("/negocio/999").status_code == 404


# --- Re-analizar ---------------------------------------------------------------------------------


def wait_reviews(client: TestClient) -> None:
    reviews = client.app.state.reviews
    for _ in range(200):
        if not reviews.running:
            return
        time.sleep(0.02)
    raise AssertionError("La revisión no terminó")


def test_reanalyze_runs_in_background_and_refreshes_the_report(three_leads, tmp_path):
    repo = three_leads
    bueno = next(lead for lead in leads_for_day(repo, date.today()) if lead.name == "Bueno")
    calls: list[int] = []

    async def fake_reviewer(business_id, repo):
        calls.append(business_id)
        await asyncio.sleep(0.05)
        repo.add_analysis(WebsiteAnalysis(business_id=business_id, url_analyzed="https://bueno.cl/", findings=[]))

    with make_client(repo, tmp_path, fake_reviewer) as client:
        box = client.post(f"/negocio/{bueno.id}/reanalizar").text
        assert "Revisando la web" in box or "En cola" in box
        wait_reviews(client)
        done = client.get(f"/negocio/{bueno.id}/revision")
        assert done.headers.get("HX-Refresh") == "true"
        html = client.get(f"/negocio/{bueno.id}").text
    assert calls == [bueno.id]
    assert "Revisiones anteriores" in html  # ahora hay 2 revisiones
    assert "No se detectaron problemas." in html  # se muestra la nueva


def test_reanalyze_error_is_shown_and_web_less_business_is_rejected(three_leads, tmp_path):
    repo = three_leads
    leads = {lead.name: lead for lead in leads_for_day(repo, date.today())}

    async def failing(business_id, repo):
        raise RuntimeError("Target closed")

    no_web, _ = repo.upsert_business(
        Business(google_maps_url="https://www.google.com/maps/place/SinWeb", business_name="SinWeb", website_status=WebsiteStatus.SIN_WEB)
    )
    with make_client(repo, tmp_path, failing) as client:
        client.post(f"/negocio/{leads['Bueno'].id}/reanalizar")
        wait_reviews(client)
        html = client.get(f"/negocio/{leads['Bueno'].id}").text
        assert "La última revisión falló" in html
        assert client.post(f"/negocio/{no_web.id}/reanalizar").status_code == 400


def test_pending_webs_can_be_reviewed_from_the_list(repo, tmp_path):
    # Detener antes de revisar: la web abre pero no tiene análisis.
    async def go():
        settings = Settings(_env_file=None, data_dir=tmp_path)
        transport = httpx.MockTransport(lambda r: httpx.Response(200, text=SITE, headers={"content-type": "text/html"}))
        async with MeasuredCollector(settings, transport=transport, use_browser=False) as collector:
            await run_leads_job(OnePlace([place("Bueno", "https://bueno.cl/")]), collector, repo,
                                LeadsProgress(query="x", target=1), review_webs=False)

    asyncio.run(go())
    calls: list[int] = []

    async def fake_reviewer(business_id, repo):
        calls.append(business_id)

    with make_client(repo, tmp_path, fake_reviewer) as client:
        page = client.get("/").text
        assert "Revisar 1 webs pendientes" in page
        response = client.post("/revisar-pendientes", data={"dia": date.today().isoformat()}, follow_redirects=False)
        assert response.status_code == 303 and "Revisando%201%20webs" in response.headers["location"]
        wait_reviews(client)
        assert "Revisión de webs terminada: 1 de 1" in client.get("/revisiones").text
    assert len(calls) == 1


# --- Caché de análisis y Re-analizar en el pipeline -------------------------------------------------


def test_recent_analysis_is_not_repeated_but_an_old_one_is(repo, tmp_path):
    run(repo, tmp_path, [place("Bueno", "https://bueno.cl/")])
    business = repo.get_business(leads_for_day(repo, date.today())[0].id)

    def review_again() -> LeadsProgress:
        progress = LeadsProgress(query="x", target=1)

        async def go():
            settings = Settings(_env_file=None, data_dir=tmp_path, web_max_internal_pages=0)
            transport = httpx.MockTransport(lambda r: httpx.Response(200, text=SITE, headers={"content-type": "text/html"}))
            async with MeasuredCollector(settings, transport=transport, use_browser=False) as collector:
                await review_websites(collector, repo, [business], progress)

        asyncio.run(go())
        return progress

    assert review_again().webs_total == 0  # analizada hace un rato: se usa la que hay
    old = repo.latest_analysis(business.id)
    with repo._session() as session:
        stored = session.get(WebsiteAnalysis, old.id)
        stored.analyzed_at = utcnow() - timedelta(days=31)
        session.add(stored)
        session.commit()
    assert review_again().webs_total == 1  # más de 30 días: se revisa de nuevo


def test_reanalyze_updates_the_business_when_the_web_went_down(repo, tmp_path):
    run(repo, tmp_path, [place("Bueno", "https://bueno.cl/")])
    business = repo.get_business(leads_for_day(repo, date.today())[0].id)
    assert business.website_status == WebsiteStatus.OK

    async def go():
        settings = Settings(_env_file=None, data_dir=tmp_path, web_max_internal_pages=0)
        transport = httpx.MockTransport(lambda r: httpx.Response(500, text="error", headers={"content-type": "text/html"}))
        async with MeasuredCollector(settings, transport=transport, use_browser=False) as collector:
            return await review_website(collector, repo, business, refresh_business=True)

    analysis = asyncio.run(go())
    assert repo.get_business(business.id).website_status == WebsiteStatus.CAIDO
    assert analysis.error.startswith("La web no abrió al revisarla")
    assert [f["codigo"] for f in analysis.findings] == ["web_caida"]  # no se inventan problemas de una web que no abrió


def test_a_lead_marked_as_duplicate_after_saving_is_not_reviewed(repo, tmp_path):
    # Error real (2026-10-04): un repetido detectado con el WhatsApp de su web igual se revisaba a fondo.
    business, _ = repo.upsert_business(
        Business(google_maps_url="https://www.google.com/maps/place/d", business_name="Dup", phone_e164="+56993557317",
                 website="https://bueno.cl/", website_status=WebsiteStatus.OK)
    )
    repo.update_business(business.id, duplicate_reason="Tiene el mismo número que «X».")
    progress = LeadsProgress(query="x", target=1)

    async def go():
        settings = Settings(_env_file=None, data_dir=tmp_path)
        async with MeasuredCollector(settings, transport=httpx.MockTransport(lambda r: httpx.Response(404)), use_browser=False) as c:
            await review_websites(c, repo, [business], progress)  # `business` es la copia vieja, sin la marca

    asyncio.run(go())
    assert progress.webs_total == 0
    assert repo.latest_analysis(business.id) is None
