from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import inspect, text

from app.db.repository import Repository
from app.db.schema import SCHEMA_VERSION, create_db_engine, init_db
from app.models import (
    Business,
    SearchStatus,
    WebsiteAnalysis,
    WhatsAppCandidate,
    WhatsAppPlacement,
    WhatsAppSource,
    WhatsAppStatus,
)

MAPS_URL = (
    "https://www.google.com/maps/place/Cerrajer%C3%ADa+XYZ/@-34.17,-70.74,17z/"
    "data=!4m6!3m5!1s0x9663431b0b0d5b5b:0x1a2b3c4d5e6f7a8b!8m2?entry=ttu"
)
SAME_PLACE_OTHER_VIEW = MAPS_URL.replace("@-34.17,-70.74,17z", "@-34.20,-70.70,12z").replace("entry=ttu", "hl=es")


@pytest.fixture
def repo() -> Repository:
    engine = create_db_engine(":memory:")
    init_db(engine)
    return Repository(engine)


def test_init_db_creates_all_tables(repo):
    tables = set(inspect(repo.engine).get_table_names())
    assert {"searches", "businesses", "search_results", "website_analyses", "schema_version"} <= tables
    with repo.engine.connect() as conn:
        assert conn.execute(text("SELECT MAX(version) FROM schema_version")).scalar() == SCHEMA_VERSION


def test_database_from_phase_2_is_migrated_without_losing_data(tmp_path):
    db_path = tmp_path / "prospector.db"
    engine = create_db_engine(db_path)
    init_db(engine)
    repo = Repository(engine)
    business, _ = repo.upsert_business(Business(google_maps_url=MAPS_URL, business_name="Cerrajería XYZ"))
    # Simula la base de la Fase 2 (versión 1), sin las columnas que se agregaron después.
    with engine.begin() as conn:
        for statement in (
            "DROP INDEX ix_businesses_whatsapp_confidence",
            "DROP INDEX ix_businesses_discarded_at",
            "DROP INDEX ix_searches_created_at",
            "ALTER TABLE businesses DROP COLUMN whatsapp_confidence",
            "ALTER TABLE businesses DROP COLUMN whatsapp_broken_button",
            "ALTER TABLE businesses DROP COLUMN whatsapp_check",
            "ALTER TABLE businesses DROP COLUMN whatsapp_checked_at",
            "ALTER TABLE businesses DROP COLUMN discarded_at",
            "ALTER TABLE businesses DROP COLUMN discard_reason",
            "ALTER TABLE searches DROP COLUMN leads_found",
            "ALTER TABLE website_analyses DROP COLUMN findings",
            "DROP INDEX ix_businesses_duplicate_of_id",
            "ALTER TABLE businesses DROP COLUMN duplicate_of_id",
            "ALTER TABLE businesses DROP COLUMN duplicate_reason",
            "DROP TABLE fingerprints",
            "DROP TABLE contacted_leads",
            "UPDATE schema_version SET version = 1",
        ):
            conn.execute(text(statement))

    init_db(create_db_engine(db_path))
    migrated_repo = Repository(create_db_engine(db_path))
    migrated = migrated_repo.get_business(business.id)
    assert migrated.business_name == "Cerrajería XYZ"
    assert migrated.whatsapp_confidence is None
    assert migrated.whatsapp_broken_button is False
    assert migrated.discarded_at is None
    assert migrated.duplicate_reason is None
    assert migrated_repo.create_search("x", 5).leads_found == 0
    assert migrated_repo.count_contacted() == 0  # la tabla nueva se creó


def test_init_db_is_idempotent(tmp_path):
    db_path = tmp_path / "prospector.db"
    init_db(create_db_engine(db_path))
    engine = create_db_engine(db_path)
    init_db(engine)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT MAX(version) FROM schema_version")).scalar() == SCHEMA_VERSION
        assert conn.execute(text("SELECT COUNT(*) FROM schema_version")).scalar() == 1


# --- Búsquedas ----------------------------------------------------------------------


def test_search_lifecycle(repo):
    search = repo.create_search("cerrajeros en Rancagua", 20)
    assert search.status == SearchStatus.EN_CURSO
    repo.update_search(search.id, status=SearchStatus.TERMINADA, businesses_found=12, phase="listo")
    stored = repo.get_search(search.id)
    assert (stored.status, stored.businesses_found, stored.phase) == (SearchStatus.TERMINADA, 12, "listo")


def test_update_search_rejects_unknown_fields(repo):
    search = repo.create_search("x", 5)
    with pytest.raises(AttributeError):
        repo.update_search(search.id, campo_inventado=1)


def test_list_searches_newest_first(repo):
    first = repo.create_search("uno", 5)
    second = repo.create_search("dos", 5)
    assert [s.id for s in repo.list_searches()] == [second.id, first.id]


# --- Negocios y deduplicación ----------------------------------------------------------


def test_same_place_from_another_view_is_not_duplicated(repo):
    first, created = repo.upsert_business(Business(google_maps_url=MAPS_URL, business_name="Cerrajería XYZ"))
    second, created_again = repo.upsert_business(
        Business(google_maps_url=SAME_PLACE_OTHER_VIEW, business_name="Cerrajería XYZ")
    )
    assert created is True and created_again is False
    assert first.id == second.id
    assert first.place_id == "ftid:0x9663431b0b0d5b5b:0x1a2b3c4d5e6f7a8b"
    assert "@-34" not in first.google_maps_url


def test_dedup_by_explicit_place_id(repo):
    repo.upsert_business(Business(google_maps_url="https://www.google.com/maps/place/A", business_name="A", place_id="place_id:ChIJabc"))
    other, created = repo.upsert_business(
        Business(google_maps_url="https://www.google.com/maps/place/A-renombrado", business_name="A", place_id="place_id:ChIJabc")
    )
    assert created is False
    assert other.google_maps_url == "https://www.google.com/maps/place/A-renombrado"


def test_upsert_only_overwrites_fields_that_were_passed(repo):
    repo.upsert_business(
        Business(google_maps_url=MAPS_URL, business_name="Cerrajería XYZ", rating=4.7, review_count=183, phone_raw="72 223 4567")
    )
    updated, _ = repo.upsert_business(Business(google_maps_url=MAPS_URL, business_name="Cerrajería XYZ", review_count=190))
    assert (updated.rating, updated.review_count, updated.phone_raw) == (4.7, 190, "72 223 4567")


def test_json_fields_and_enums_round_trip(repo):
    candidate = WhatsAppCandidate(
        number="56987654321",
        url="https://wa.me/56987654321",
        source=WhatsAppSource.WEB,
        placement=WhatsAppPlacement.HEADER,
        evidence="https://wa.me/56987654321?text=Hola",
    )
    saved, _ = repo.upsert_business(
        Business(
            google_maps_url=MAPS_URL,
            business_name="Cerrajería XYZ",
            whatsapp_status=WhatsAppStatus.VERIFICADO,
            whatsapp_source=WhatsAppSource.WEB,
            whatsapp_candidates=[candidate.model_dump(mode="json")],
            opening_hours={"lunes": "09:00–19:00"},
            services=["Apertura de puertas"],
            field_sources={"whatsapp_url": "web"},
        )
    )
    stored = repo.get_business(saved.id)
    assert stored.whatsapp_status == WhatsAppStatus.VERIFICADO
    assert WhatsAppCandidate.model_validate(stored.whatsapp_candidates[0]) == candidate
    assert stored.opening_hours == {"lunes": "09:00–19:00"}
    assert stored.services == ["Apertura de puertas"]


def test_search_results_keep_maps_order_without_duplicates(repo):
    search = repo.create_search("cerrajeros en Rancagua", 20)
    a, _ = repo.upsert_business(Business(google_maps_url="https://www.google.com/maps/place/A", business_name="A"))
    b, _ = repo.upsert_business(Business(google_maps_url="https://www.google.com/maps/place/B", business_name="B"))
    repo.link_search_result(search.id, b.id, 1)
    repo.link_search_result(search.id, a.id, 2)
    repo.link_search_result(search.id, b.id, 1)  # repetido
    assert [(pos, biz.business_name) for pos, biz in repo.businesses_for_search(search.id)] == [(1, "B"), (2, "A")]


# --- Caché de análisis -----------------------------------------------------------------


def test_needs_analysis_respects_cache_window(repo):
    business, _ = repo.upsert_business(Business(google_maps_url=MAPS_URL, business_name="Cerrajería XYZ"))
    now = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
    assert repo.needs_analysis(business.id, max_age_days=30, now=now) is True

    repo.add_analysis(
        WebsiteAnalysis(business_id=business.id, url_analyzed="https://x.cl/", analyzed_at=now - timedelta(days=10), website_score=42)
    )
    assert repo.needs_analysis(business.id, max_age_days=30, now=now) is False
    assert repo.needs_analysis(business.id, max_age_days=30, now=now + timedelta(days=25)) is True


def test_failed_analysis_does_not_count_as_cache(repo):
    business, _ = repo.upsert_business(Business(google_maps_url=MAPS_URL, business_name="Cerrajería XYZ"))
    now = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
    repo.add_analysis(WebsiteAnalysis(business_id=business.id, url_analyzed="https://x.cl/", analyzed_at=now, error="timeout"))
    assert repo.needs_analysis(business.id, max_age_days=30, now=now) is True
    assert repo.latest_analysis(business.id).error == "timeout"
