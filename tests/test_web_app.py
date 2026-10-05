"""Página local: buscar, ver avance, marcar Existe / No existe y exportar (sin navegador ni internet)."""

import asyncio
import time
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db.repository import Repository
from app.db.schema import create_db_engine, init_db
from app.extract.whatsapp import extract_whatsapp
from app.models import Business, SearchStatus, WebsiteStatus, WhatsAppSource
from app.pipeline import LeadsProgress
from app.web.app import create_app, spanish_date


@pytest.fixture
def repo() -> Repository:
    engine = create_db_engine(":memory:")
    init_db(engine)
    return Repository(engine)


async def fake_runner(progress: LeadsProgress, repo: Repository) -> LeadsProgress:
    """Simula una búsqueda: guarda un lead con celular y otro con WhatsApp verificado."""
    search = repo.create_search(progress.query, progress.target)
    progress.search_id = search.id
    from app.sources.base import MapsPlace

    for i, (phone, wa) in enumerate([("+56993557317", None), ("+56722234567", "56987654321")], start=1):
        await asyncio.sleep(0.05)
        place = MapsPlace(
            maps_url=f"https://www.google.com/maps/place/lead{i}",
            name=f"Lead {i}",
            phone_raw=phone,
            phone_e164=phone,
            whatsapp=extract_whatsapp(f'<a href="https://wa.me/{wa}">WA</a>' if wa else "", source=WhatsAppSource.MAPS),
        )
        business, _ = repo.upsert_business(place.to_business())
        # Solo los negocios con web que abre son leads.
        repo.update_business(business.id, website=f"https://lead{i}.cl/", website_status=WebsiteStatus.OK)
        repo.link_search_result(search.id, business.id, i)
        progress.leads += 1
        progress.say(f"✔ Lead {i}")
    progress.status = SearchStatus.TERMINADA
    repo.update_search(search.id, status=SearchStatus.TERMINADA, leads_found=progress.leads)
    return progress


@pytest.fixture
def client(repo, tmp_path):
    app = create_app(Settings(_env_file=None, data_dir=tmp_path), repo=repo, runner=fake_runner)
    with TestClient(app) as test_client:
        yield test_client


def wait_until_done(client: TestClient) -> None:
    jobs = client.app.state.jobs
    for _ in range(100):
        if not jobs.running:
            return
        time.sleep(0.05)
    raise AssertionError("la búsqueda no terminó")


def test_home_without_leads(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "VORTEXIA" in page.text
    assert "Todavía no hay leads hoy" in page.text


def test_search_runs_in_background_and_leads_appear(client):
    response = client.post("/buscar", data={"consulta": "  cerrajeros   en Rancagua ", "cantidad": "20"}, follow_redirects=False)
    assert response.status_code == 303
    wait_until_done(client)
    progress = client.get("/progreso")
    assert "cerrajeros en Rancagua" in progress.text
    assert "Terminada" in progress.text
    assert progress.headers["HX-Trigger"] == "leads-actualizados"
    leads = client.get("/leads").text
    assert "Lead 1" in leads and "Lead 2" in leads
    assert "Probar WhatsApp" in leads and "Abrir WhatsApp" in leads


def test_only_one_search_at_a_time(client):
    client.post("/buscar", data={"consulta": "uno", "cantidad": "5"})
    second = client.post("/buscar", data={"consulta": "dos", "cantidad": "5"}, follow_redirects=False)
    assert "Ya%20hay%20una%20b%C3%BAsqueda%20en%20curso" in second.headers["location"]
    wait_until_done(client)


def test_empty_search_is_rejected(client):
    response = client.post("/buscar", data={"consulta": "   ", "cantidad": "5"}, follow_redirects=False)
    assert "aviso=" in response.headers["location"]


def test_exists_and_not_exists_buttons(client, repo):
    client.post("/buscar", data={"consulta": "cerrajeros", "cantidad": "5"})
    wait_until_done(client)
    lead = repo.find_business(google_maps_url="https://www.google.com/maps/place/lead1")

    confirmed = client.post(f"/negocio/{lead.id}/existe")
    assert confirmed.status_code == 200
    assert "Existe · confirmado por ti" in confirmed.text

    discarded = client.post(f"/negocio/{lead.id}/no-existe")
    assert "descartado" in discarded.text
    assert "Lead 1" not in client.get("/leads").text

    restored = client.post(f"/negocio/{lead.id}/deshacer")
    assert "Probar WhatsApp" in restored.text
    assert "Lead 1" in client.get("/leads").text


def test_unknown_business_is_404(client):
    assert client.post("/negocio/999/existe").status_code == 404


def test_csv_export(client):
    client.post("/buscar", data={"consulta": "cerrajeros", "cantidad": "5"})
    wait_until_done(client)
    export = client.get("/exportar.csv")
    assert export.status_code == 200
    assert f'leads-{date.today().isoformat()}.csv' in export.headers["content-disposition"]
    text = export.content.decode("utf-8")
    assert text.startswith("﻿Negocio;Rubro")
    assert "Lead 2" in text


def test_previous_days_are_listed(client):
    client.post("/buscar", data={"consulta": "gasfíters en Rancagua", "cantidad": "5"})
    wait_until_done(client)
    home = client.get("/").text
    assert f"{spanish_date(date.today())} (2)" in home


def test_spanish_date():
    assert spanish_date(date(2026, 10, 4)) == "domingo 4 de octubre"
