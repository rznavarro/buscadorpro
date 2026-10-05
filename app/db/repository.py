"""Repositorio: única puerta de entrada a SQLite para el resto del sistema."""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine
from sqlmodel import Session, col, func, select

from app.extract.links import canonical_maps_url, maps_place_key
from app.models import (
    Business,
    ContactedLead,
    Fingerprint,
    SearchResult,
    SearchRun,
    WebsiteAnalysis,
    utcnow,
)


def _as_utc(value: datetime) -> datetime:
    # SQLite devuelve las fechas sin zona horaria; se guardan siempre en UTC.
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class Repository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def _session(self) -> Session:
        return Session(self.engine, expire_on_commit=False)

    # --- Búsquedas -------------------------------------------------------------

    def create_search(self, query: str, limit: int) -> SearchRun:
        with self._session() as session:
            search = SearchRun(query=query, limit=limit)
            session.add(search)
            session.commit()
            session.refresh(search)
            return search

    def get_search(self, search_id: int) -> SearchRun | None:
        with self._session() as session:
            return session.get(SearchRun, search_id)

    def list_searches(self, limit: int = 50) -> list[SearchRun]:
        with self._session() as session:
            statement = select(SearchRun).order_by(col(SearchRun.created_at).desc(), col(SearchRun.id).desc())
            return list(session.exec(statement.limit(limit)))

    def update_search(self, search_id: int, **changes: Any) -> SearchRun:
        with self._session() as session:
            search = session.get(SearchRun, search_id)
            if search is None:
                raise LookupError(f"No existe la búsqueda {search_id}")
            for field, value in changes.items():
                if field not in SearchRun.model_fields:
                    raise AttributeError(f"SearchRun no tiene el campo {field!r}")
                setattr(search, field, value)
            session.add(search)
            session.commit()
            session.refresh(search)
            return search

    # --- Negocios ------------------------------------------------------------------

    def find_business(self, *, place_id: str | None = None, google_maps_url: str | None = None) -> Business | None:
        """Busca por place_id; si no lo hay o no coincide, por URL canónica de Maps."""
        with self._session() as session:
            if place_id:
                found = session.exec(select(Business).where(Business.place_id == place_id)).first()
                if found:
                    return found
            if google_maps_url:
                canonical = canonical_maps_url(google_maps_url)
                return session.exec(select(Business).where(Business.google_maps_url == canonical)).first()
            return None

    def upsert_business(self, business: Business) -> tuple[Business, bool]:
        """Guarda el negocio sin duplicarlo. Devuelve (negocio guardado, si es nuevo).

        Si ya existe (mismo place_id o misma URL canónica de Maps), solo se actualizan los
        campos que se pasaron explícitamente al crear `business`.
        """
        business.google_maps_url = canonical_maps_url(business.google_maps_url)
        business.place_id = business.place_id or maps_place_key(business.google_maps_url)
        existing = self.find_business(place_id=business.place_id, google_maps_url=business.google_maps_url)

        with self._session() as session:
            if existing is None:
                session.add(business)
                session.commit()
                session.refresh(business)
                return business, True

            changes = business.model_dump(exclude_unset=True, exclude={"id", "first_seen_at", "updated_at"})
            changes["google_maps_url"] = business.google_maps_url
            if "field_sources" in changes:
                # La evidencia de cada fuente se suma; no se borra la que dejó otra fuente.
                changes["field_sources"] = {**(existing.field_sources or {}), **changes["field_sources"]}
            if business.place_id:
                changes["place_id"] = business.place_id
            for field, value in changes.items():
                setattr(existing, field, value)
            existing.updated_at = utcnow()
            session.add(existing)
            session.commit()
            session.refresh(existing)
            return existing, False

    def get_business(self, business_id: int) -> Business | None:
        with self._session() as session:
            return session.get(Business, business_id)

    def update_business(self, business_id: int, **changes: Any) -> Business:
        with self._session() as session:
            business = session.get(Business, business_id)
            if business is None:
                raise LookupError(f"No existe el negocio {business_id}")
            for field, value in changes.items():
                if field not in Business.model_fields:
                    raise AttributeError(f"Business no tiene el campo {field!r}")
                setattr(business, field, value)
            business.updated_at = utcnow()
            session.add(business)
            session.commit()
            session.refresh(business)
            return business

    def link_search_result(self, search_id: int, business_id: int, position: int) -> None:
        """Vincula un negocio a una búsqueda con su posición en Maps (sin duplicar)."""
        with self._session() as session:
            link = session.get(SearchResult, (search_id, business_id))
            if link is None:
                session.add(SearchResult(search_id=search_id, business_id=business_id, position=position))
            else:
                link.position = position
                session.add(link)
            session.commit()

    def businesses_for_search(self, search_id: int) -> list[tuple[int, Business]]:
        """(posición, negocio) de una búsqueda, en el orden de Maps."""
        with self._session() as session:
            statement = (
                select(SearchResult.position, Business)
                .join(Business, col(Business.id) == SearchResult.business_id)
                .where(SearchResult.search_id == search_id)
                .order_by(col(SearchResult.position))
            )
            return [(position, business) for position, business in session.exec(statement)]

    def searches_between(self, start: datetime, end: datetime) -> list[SearchRun]:
        """Búsquedas creadas en [start, end) (fechas en UTC), de la más antigua a la más nueva."""
        with self._session() as session:
            statement = (
                select(SearchRun)
                .where(col(SearchRun.created_at) >= start, col(SearchRun.created_at) < end)
                .order_by(col(SearchRun.created_at), col(SearchRun.id))
            )
            return list(session.exec(statement))

    def first_search_of(self, business_id: int) -> tuple[str, int] | None:
        """(consulta, posición) de la primera búsqueda que encontró al negocio."""
        with self._session() as session:
            statement = (
                select(SearchRun.query, SearchResult.position)
                .join(SearchResult, col(SearchResult.search_id) == SearchRun.id)
                .where(SearchResult.business_id == business_id)
                .order_by(col(SearchRun.created_at))
            )
            row = session.exec(statement).first()
            return (row[0], row[1]) if row else None

    def found_by_searches(self, search_ids: list[int]) -> list[tuple[int, int, Business]]:
        """(id de búsqueda, posición, negocio) de esas búsquedas."""
        if not search_ids:
            return []
        with self._session() as session:
            statement = (
                select(SearchResult.search_id, SearchResult.position, Business)
                .join(Business, col(Business.id) == SearchResult.business_id)
                .where(col(SearchResult.search_id).in_(search_ids))
                .order_by(col(SearchResult.search_id), col(SearchResult.position))
            )
            return [(search_id, position, business) for search_id, position, business in session.exec(statement)]

    # --- No repetir: huellas y contactados -------------------------------------------------

    def set_fingerprints(
        self, prints: list[tuple[str, str]], *, business_id: int | None = None, contacted_id: int | None = None
    ) -> None:
        """Reemplaza las huellas de un negocio o de un contactado."""
        with self._session() as session:
            owner = (
                col(Fingerprint.business_id) == business_id
                if business_id is not None
                else col(Fingerprint.contacted_id) == contacted_id
            )
            for old in session.exec(select(Fingerprint).where(owner)):
                session.delete(old)
            for kind, value in dict.fromkeys(prints):
                session.add(Fingerprint(kind=kind, value=value, business_id=business_id, contacted_id=contacted_id))
            session.commit()

    def find_fingerprint(
        self, kind: str, value: str, *, exclude_business_id: int | None = None, contacted_only: bool = False
    ) -> Fingerprint | None:
        """Huella con ese valor que no sea del propio negocio: primero la de un contactado, luego la más antigua."""
        with self._session() as session:
            statement = select(Fingerprint).where(Fingerprint.kind == kind, Fingerprint.value == value)
            if exclude_business_id is not None:
                statement = statement.where(
                    (col(Fingerprint.business_id).is_(None)) | (col(Fingerprint.business_id) != exclude_business_id)
                )
            if contacted_only:
                statement = statement.where(col(Fingerprint.contacted_id).is_not(None))
            statement = statement.order_by(col(Fingerprint.contacted_id).is_(None), col(Fingerprint.id))
            return session.exec(statement).first()

    def fingerprinted_business_ids(self) -> set[int]:
        """Negocios que ya tienen sus huellas guardadas."""
        with self._session() as session:
            statement = select(Fingerprint.business_id).where(col(Fingerprint.business_id).is_not(None)).distinct()
            return {business_id for business_id in session.exec(statement) if business_id is not None}

    def list_businesses(self) -> list[Business]:
        with self._session() as session:
            return list(session.exec(select(Business).order_by(col(Business.id))))

    def upsert_contacted(self, lead: ContactedLead) -> tuple[ContactedLead, bool]:
        """Guarda un contactado; si su número ya estaba, actualiza su estado. (contactado, es nuevo)."""
        with self._session() as session:
            existing = session.exec(select(ContactedLead).where(ContactedLead.phone_e164 == lead.phone_e164)).first()
            if existing is None:
                session.add(lead)
                session.commit()
                session.refresh(lead)
                return lead, True
            for field in ("name", "phone_raw", "status", "contacted_on", "note"):
                value = getattr(lead, field)
                if value:
                    setattr(existing, field, value)
            session.add(existing)
            session.commit()
            session.refresh(existing)
            return existing, False

    def get_contacted(self, contacted_id: int) -> ContactedLead | None:
        with self._session() as session:
            return session.get(ContactedLead, contacted_id)

    def list_contacted(self) -> list[ContactedLead]:
        with self._session() as session:
            return list(session.exec(select(ContactedLead).order_by(col(ContactedLead.id).desc())))

    def count_contacted(self) -> int:
        with self._session() as session:
            return session.exec(select(func.count()).select_from(ContactedLead)).one()

    # --- Análisis de webs -------------------------------------------------------------

    def add_analysis(self, analysis: WebsiteAnalysis) -> WebsiteAnalysis:
        with self._session() as session:
            session.add(analysis)
            session.commit()
            session.refresh(analysis)
            return analysis

    def latest_analysis(self, business_id: int, *, successful_only: bool = False) -> WebsiteAnalysis | None:
        with self._session() as session:
            statement = select(WebsiteAnalysis).where(WebsiteAnalysis.business_id == business_id)
            if successful_only:
                statement = statement.where(col(WebsiteAnalysis.error).is_(None))
            statement = statement.order_by(col(WebsiteAnalysis.analyzed_at).desc(), col(WebsiteAnalysis.id).desc())
            return session.exec(statement).first()

    def list_analyses(self, business_id: int) -> list[WebsiteAnalysis]:
        """Todos los análisis de un negocio, del más nuevo al más viejo."""
        with self._session() as session:
            statement = (
                select(WebsiteAnalysis)
                .where(WebsiteAnalysis.business_id == business_id)
                .order_by(col(WebsiteAnalysis.analyzed_at).desc(), col(WebsiteAnalysis.id).desc())
            )
            return list(session.exec(statement))

    def needs_analysis(self, business_id: int, *, max_age_days: int, now: datetime | None = None) -> bool:
        """True si no hay un análisis exitoso de hace menos de `max_age_days` días."""
        latest = self.latest_analysis(business_id, successful_only=True)
        if latest is None:
            return True
        now = _as_utc(now or utcnow())
        return now - _as_utc(latest.analyzed_at) >= timedelta(days=max_age_days)
