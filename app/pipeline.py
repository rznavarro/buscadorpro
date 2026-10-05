"""Pipeline: BUSCAR → EXTRAER → VERIFICAR → ANALIZAR → GUARDAR.

- Paso de Maps (Fase 2): buscar, leer cada ficha y guardar sin duplicar.
- Paso de verificación (Fase 3): ¿la web existe? ¿cuál es su URL final? ¿qué WhatsApp
  publica? Se combina con lo de Maps para calcular la confianza del WhatsApp.

Cada negocio se procesa por separado: si uno falla se anota el error y se sigue con los demás.
Si Google bloquea, la búsqueda se detiene y queda como BLOQUEADO.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.analyze.collector import WebCollector, WebVerification
from app.analyze.findings import build_findings
from app.analyze.llm import LLMClient, LLMError
from app.analyze.report import build_analysis, evidence_package
from app.db.repository import Repository
from app.dedupe import (
    Duplicate,
    business_fingerprints,
    ensure_fingerprints,
    find_duplicate,
    listing_fingerprints,
    mark_duplicate,
    register_lead,
)
from app.extract.links import LinkKind, classify_link, maps_place_key
from app.extract.phones import is_probable_mobile, region_for_phone
from app.extract.whatsapp import choose_whatsapp
from app.logs import log
from app.models import (
    Business,
    SearchRun,
    SearchStatus,
    WebsiteAnalysis,
    WebsiteStatus,
    WhatsAppCandidate,
    WhatsAppSource,
    WhatsAppStatus,
    utcnow,
)
from app.sources.base import MapsBlockedError, MapsListing, MapsPlace, MapsPlaceError, MapsSource

Progress = Callable[[str], None]
_WEB_SOURCES = {WhatsAppSource.WEB, WhatsAppSource.LINKTREE}


class ReviewTimeout(RuntimeError):
    """La revisión a fondo de una web superó el tiempo máximo (`WEB_REVIEW_MAX_SECONDS`)."""


# --- WhatsApp: combinar fuentes ---------------------------------------------------------


def stored_candidates(business: Business) -> list[WhatsAppCandidate]:
    return [WhatsAppCandidate.model_validate(raw) for raw in business.whatsapp_candidates or []]


def whatsapp_changes(
    business: Business,
    *,
    replace_sources: set[WhatsAppSource],
    fresh: list[WhatsAppCandidate],
    broken: list[str],
    stored: list[WhatsAppCandidate] | None = None,
    field_sources: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Recalcula el WhatsApp con TODAS las fuentes: lo guardado de otras fuentes + lo nuevo.

    Así una fuente nunca borra lo que encontró otra (por ejemplo, volver a leer Maps no
    pisa el WhatsApp encontrado en la web). `stored` y `field_sources` reemplazan a los del
    negocio cuando ya se conocen valores más recientes.
    """
    previous = stored if stored is not None else stored_candidates(business)
    kept = [c for c in previous if c.source not in replace_sources]
    sources = dict(field_sources if field_sources is not None else business.field_sources or {})
    previous_broken = [] if replace_sources & _WEB_SOURCES else list(sources.get("whatsapp_broken_web") or [])
    decision = choose_whatsapp([*kept, *fresh], business.phone_e164, [*previous_broken, *broken])
    changes = decision.business_fields(complete=True)
    if replace_sources & _WEB_SOURCES:
        sources["whatsapp_broken_web"] = broken
    if decision.url:
        sources["whatsapp_url"] = str(decision.source)
    sources["whatsapp_confidence"] = decision.confidence_reason
    changes["field_sources"] = sources
    return changes


# --- Paso de Maps ---------------------------------------------------------------------


@dataclass
class MapsStepResult:
    search: SearchRun
    businesses: list[dict[str, Any]] = field(default_factory=list)


def business_summary(position: int, business: Business, created: bool, place: MapsPlace) -> dict[str, Any]:
    """Resumen legible de un negocio (lo que imprime el comando `maps`)."""
    return {
        "posicion": position,
        "nuevo": created,
        "nombre": business.business_name,
        "categoria": business.category,
        "telefono": business.phone_raw,
        "telefono_e164": business.phone_e164,
        "whatsapp_estado": str(business.whatsapp_status),
        "whatsapp_url": business.whatsapp_url,
        "whatsapp_confianza": str(business.whatsapp_confidence) if business.whatsapp_confidence else None,
        "whatsapp_evidencia": business.whatsapp_evidence,
        "web": business.website,
        "web_estado": str(business.website_status) if business.website_status else "POR VERIFICAR",
        "sitio_web_en_maps": place.website_raw,
        "instagram": business.instagram,
        "facebook": business.facebook,
        "direccion": business.address,
        "ciudad": business.city,
        "comuna": business.commune,
        "horario": business.opening_hours,
        "rating": business.rating,
        "resenas": business.review_count,
        "descripcion": business.description,
        "servicios": business.services,
        "maps_url": business.google_maps_url,
        "place_id": business.place_id,
    }


async def run_maps_step(
    source: MapsSource,
    repo: Repository,
    query: str,
    limit: int,
    *,
    progress: Progress = lambda message: None,
) -> MapsStepResult:
    search = repo.create_search(query, limit)
    repo.update_search(search.id, phase="Buscando en Google Maps")
    result = MapsStepResult(search=search)
    saved: list[Business] = []
    status, error = SearchStatus.TERMINADA, None

    try:
        listings = await source.search(query, limit)
        progress(f"{len(listings)} resultados en la lista de Maps.")
        repo.update_search(search.id, phase=f"Leyendo {len(listings)} fichas")
        for listing in listings:
            progress(f"[{listing.position}/{len(listings)}] {listing.name or listing.url}")
            try:
                place = await source.get_details(listing)
            except MapsPlaceError as exc:
                progress(f"   ⚠ {exc}")
                result.businesses.append(
                    {"posicion": listing.position, "nombre": listing.name, "maps_url": listing.url, "error": str(exc)}
                )
                continue
            existing = repo.find_business(place_id=place.place_key, google_maps_url=place.maps_url)
            business, created = repo.upsert_business(place.to_business())
            previous = stored_candidates(existing) if existing is not None else []
            if any(c.source != WhatsAppSource.MAPS for c in previous):
                # Ya tenía WhatsApp de su web o Linktree: se combina en vez de reemplazarlo.
                business = repo.update_business(
                    business.id,
                    **whatsapp_changes(
                        business,
                        replace_sources={WhatsAppSource.MAPS},
                        fresh=place.whatsapp.candidates,
                        broken=place.whatsapp.broken_evidence(),
                        stored=previous,
                    ),
                )
            repo.link_search_result(search.id, business.id, listing.position)
            saved.append(business)
            result.businesses.append(business_summary(listing.position, business, created, place))
            repo.update_search(search.id, businesses_found=len(saved))
    except MapsBlockedError as exc:
        status, error = SearchStatus.BLOQUEADO, str(exc)
        progress(f"⛔ BLOQUEADO: {exc}")
    except Exception as exc:  # cualquier otro fallo deja la búsqueda como FALLIDA, con su motivo
        log.exception("[%s] La búsqueda en Maps falló", query)
        status, error = SearchStatus.FALLIDA, friendly_error(exc)
        progress(f"✖ La búsqueda falló: {error}")

    result.search = repo.update_search(
        search.id,
        status=status,
        error=error,
        phase="Maps terminado" if status == SearchStatus.TERMINADA else "Detenida",
        finished_at=utcnow(),
        businesses_found=len(saved),
        websites_found=sum(1 for b in saved if b.website and b.website_status is None),
        whatsapp_verified=sum(1 for b in saved if b.whatsapp_status == WhatsAppStatus.VERIFICADO),
    )
    return result


# --- Paso de verificación de la web ---------------------------------------------------------


def verification_changes(business: Business, verification: WebVerification) -> dict[str, Any]:
    """Qué cambia en el negocio después de revisar su web."""
    sources = dict(business.field_sources or {})
    changes: dict[str, Any] = {"website_status": verification.status}
    sources["website_status"] = f"web: {verification.reason or 'la web abre correctamente'}"

    working_url = verification.final_url if verification.status == WebsiteStatus.OK else None
    if working_url and working_url != business.website:
        sources.setdefault("website_maps", business.website)
        changes["website"] = working_url
        sources["website"] = (
            f"web propia enlazada desde su Linktree ({verification.linktree_urls[0]})"
            if verification.website_from_linktree
            else "web (URL final después de redirecciones)"
        )
    for network in ("instagram", "facebook"):
        profiles = verification.socials.get(network)
        if profiles and not getattr(business, network):
            changes[network] = profiles[0]
            sources[network] = "web"

    changes.update(
        whatsapp_changes(
            business,
            replace_sources=_WEB_SOURCES,
            fresh=verification.whatsapp_candidates,
            broken=verification.whatsapp_broken,
            field_sources=sources,
        )
    )
    return changes


def verify_summary(business: Business, verification: WebVerification | None, error: str | None = None) -> dict[str, Any]:
    """Lo más importante para Joaquín: web y WhatsApp, con su evidencia."""
    sources = business.field_sources or {}
    return {
        "nombre": business.business_name,
        "web": business.website,
        "web_estado": str(business.website_status) if business.website_status else "POR VERIFICAR",
        "web_motivo": (verification.reason if verification else None) or error,
        "whatsapp_estado": str(business.whatsapp_status),
        "whatsapp_url": business.whatsapp_url,
        "whatsapp_confianza": str(business.whatsapp_confidence) if business.whatsapp_confidence else None,
        "whatsapp_por_que": sources.get("whatsapp_confidence"),
        "whatsapp_fuente": str(business.whatsapp_source) if business.whatsapp_source else None,
        "whatsapp_evidencia": business.whatsapp_evidence,
        "whatsapp_varios_numeros": business.whatsapp_multiple_numbers,
        "whatsapp_boton_roto": business.whatsapp_broken_button,
        "telefono": business.phone_raw,
        "paginas_revisadas": verification.pages_visited if verification else [],
        "linktree": verification.linktree_urls if verification else [],
    }


async def verify_business(
    collector: WebCollector, repo: Repository, business: Business, *, progress: Progress = lambda message: None
) -> tuple[Business, WebVerification | None, str | None]:
    """Revisa la web del negocio y recalcula su WhatsApp. Devuelve (negocio, verificación, error)."""
    if not business.website:
        # Sin web que revisar: igual se recalcula la confianza del WhatsApp de Maps.
        changes = whatsapp_changes(business, replace_sources=set(), fresh=[], broken=[])
        return repo.update_business(business.id, **changes), None, None
    # El país del teléfono decide cómo completar números sin código de país (Miami → +1).
    region = region_for_phone(business.phone_e164, collector.settings.default_region)
    limit = collector.settings.web_verify_max_seconds
    try:
        verification = await asyncio.wait_for(collector.verify(business.website, region=region), timeout=limit)
    except TimeoutError:
        error = f"La revisión de su web tardó más de {limit} segundos y se cortó"
        log.warning("%s: %s (%s)", business.business_name, error, business.website)
        progress(f"   ⚠ {error}: {business.business_name}")
        return business, None, error
    except Exception as exc:  # una web problemática no detiene las demás
        log.exception("No se pudo revisar la web de %s (%s)", business.business_name, business.website)
        progress(f"   ⚠ No se pudo revisar la web de {business.business_name}: {friendly_error(exc)}")
        return business, None, friendly_error(exc)
    return repo.update_business(business.id, **verification_changes(business, verification)), verification, None


async def run_verify_step(
    collector: WebCollector,
    repo: Repository,
    search_id: int,
    *,
    concurrency: int = 3,
    progress: Progress = lambda message: None,
) -> list[dict[str, Any]]:
    """Revisa la web de cada negocio de una búsqueda (con concurrencia limitada)."""
    rows = repo.businesses_for_search(search_id)
    semaphore = asyncio.Semaphore(concurrency)
    repo.update_search(search_id, phase=f"Verificando {len(rows)} webs")

    async def verify_one(position: int, business: Business) -> dict[str, Any]:
        async with semaphore:
            if business.website:
                progress(f"[{position}] Revisando {business.website}")
            updated, verification, error = await verify_business(collector, repo, business, progress=progress)
        return {"posicion": position, **verify_summary(updated, verification, error)}

    results = await asyncio.gather(*(verify_one(position, business) for position, business in rows))
    businesses = [repo.get_business(b.id) for _, b in rows]
    repo.update_search(
        search_id,
        phase="Webs verificadas",
        websites_found=sum(1 for b in businesses if b and b.website_status == WebsiteStatus.OK),
        whatsapp_verified=sum(1 for b in businesses if b and b.whatsapp_status == WhatsAppStatus.VERIFICADO),
    )
    return sorted(results, key=lambda item: item["posicion"])


# --- Leads diarios (Fase 4 nueva) ----------------------------------------------------------


_FRIENDLY_ERRORS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("has been closed", "Target closed", "browser not connected"),
        "Se cerró la ventana del navegador durante la búsqueda. Lo encontrado hasta ese momento quedó guardado. "
        "No cierres la ventana de Chromium mientras busca.",
    ),
    (
        ("ERR_INTERNET_DISCONNECTED", "ERR_NETWORK_CHANGED", "ERR_NAME_NOT_RESOLVED", "getaddrinfo failed"),
        "Parece que no hay conexión a internet. Revisa tu conexión y vuelve a intentarlo.",
    ),
    (
        ("Executable doesn't exist", "playwright install"),
        "Falta instalar el navegador del programa. Ejecuta: uv run playwright install chromium",
    ),
    (
        ("CDP", "No se pudo conectar", "Timed out waiting for CDP"),
        "El navegador no pudo abrirse. Cierra las ventanas de Chromium que hayan quedado abiertas y vuelve a intentarlo.",
    ),
)


def friendly_error(exc: BaseException) -> str:
    """El motivo de un fallo en palabras simples (el detalle técnico queda en el registro del día)."""
    message = str(exc)
    if isinstance(exc, ReviewTimeout):
        return message
    for hints, text in _FRIENDLY_ERRORS:
        if any(hint in message for hint in hints):
            return text
    return f"{type(exc).__name__}: {message.splitlines()[0][:300] if message else ''}"


def is_lead(business: Business) -> bool:
    """Lead = web propia que abre + (WhatsApp verificado o un celular para "Probar WhatsApp").

    Decisión de Joaquín (2026-10-04): solo interesan negocios con web. Sin web, solo redes o
    con la web caída, el negocio queda fuera. Nunca cuenta uno descartado ni repetido.
    """
    if business.discarded_at is not None or business.duplicate_reason:
        return False
    if business.website_status != WebsiteStatus.OK:
        return False
    return business.whatsapp_status == WhatsAppStatus.VERIFICADO or is_probable_mobile(business.phone_e164)


def card_has_website(listing: MapsListing) -> bool:
    """¿La tarjeta de la lista de Maps muestra una web que podría ser propia?

    Sin botón "Sitio web", o si ese botón lleva a Facebook, Instagram, WhatsApp o Google, el
    negocio no tiene web propia: se salta sin abrir la ficha. Un Linktree sí se abre (puede
    enlazar a su web). Si no se leyó la tarjeta (por ejemplo, Maps abrió la ficha directo),
    se abre la ficha.
    """
    if not listing.card_seen:
        return True
    if not listing.website:
        return False
    return classify_link(listing.website) in (LinkKind.WEB_PROPIA, LinkKind.AGREGADOR)


@dataclass
class LeadsProgress:
    """Avance de una búsqueda de leads, para mostrarlo en vivo y poder detenerla."""

    query: str
    target: int
    search_id: int | None = None
    leads: int = 0
    reviewed: int = 0  # fichas abiertas
    skipped: int = 0  # negocios ya revisados otro día (no se abren)
    duplicates: int = 0  # repetidos: mismo número, web o red que un lead anterior o un contactado
    no_website: int = 0  # sin web propia o con la web caída: no son leads
    status: SearchStatus = SearchStatus.EN_CURSO
    message: str = "Preparando el navegador…"
    error: str | None = None
    log: list[str] = field(default_factory=list)
    stop_requested: bool = False
    webs_total: int = 0  # webs de leads que se revisan después de Maps
    webs_done: int = 0

    def say(self, message: str) -> None:
        self.message = message
        self.log.append(message)
        del self.log[:-200]
        log.info("[%s] %s", self.query, message.strip())


async def review_website(
    collector: WebCollector,
    repo: Repository,
    business: Business,
    *,
    llm: LLMClient | None = None,
    refresh_business: bool = False,
) -> WebsiteAnalysis:
    """Revisión completa de la web de un lead: capturas, mediciones, hallazgos y, si hay IA, puntaje.

    Con `refresh_business` (botón Re-analizar) también se actualiza lo que la web dice del
    negocio: si abre o se cayó, su URL final y el WhatsApp que publica.
    """
    region = region_for_phone(business.phone_e164, collector.settings.default_region)
    limit = collector.settings.web_review_max_seconds
    try:
        collection = await asyncio.wait_for(collector.collect(business.website or "", region=region), timeout=limit)
    except TimeoutError as exc:
        raise ReviewTimeout(f"La revisión a fondo tardó más de {limit} segundos y se cortó") from exc
    if refresh_business:
        business = repo.update_business(business.id, **verification_changes(business, collection.verification))
    findings = build_findings(collection, business)
    llm_result, error = None, None
    if collection.verification.status != WebsiteStatus.OK:
        error = f"La web no abrió al revisarla: {collection.verification.reason}"
    elif llm is not None and collection.desktop is not None:
        try:
            llm_result = await llm.analyze(evidence_package(business, collection, findings, collector.settings))
        except LLMError as exc:
            error = str(exc)
    return repo.add_analysis(build_analysis(business, collection, findings, llm=llm_result, error=error))


async def review_website_with_retry(
    collector: WebCollector,
    repo: Repository,
    business: Business,
    *,
    llm: LLMClient | None = None,
    refresh_business: bool = False,
) -> WebsiteAnalysis:
    """`review_website` con reintento si falla algo pasajero (por ejemplo, el navegador se cayó).

    No se reintenta una revisión que se cortó por tiempo: tardaría lo mismo otra vez.
    """
    attempts = collector.settings.web_retries + 1
    for attempt in range(1, attempts + 1):
        try:
            return await review_website(collector, repo, business, llm=llm, refresh_business=refresh_business)
        except ReviewTimeout:
            raise
        except Exception:
            if attempt == attempts:
                raise
            log.warning("Falló la revisión a fondo de %s; se reintenta", business.business_name, exc_info=True)
            await asyncio.sleep(collector.settings.web_retry_pause_seconds)
    raise AssertionError("inalcanzable")


async def review_websites(
    collector: WebCollector,
    repo: Repository,
    businesses: list[Business],
    progress: LeadsProgress,
    *,
    llm: LLMClient | None = None,
    concurrency: int = 3,
) -> None:
    """Revisa en paralelo (con límite) las webs que abren de los leads encontrados.

    Caché: una web con un análisis exitoso de hace menos de `REANALYZE_AFTER_DAYS` días no se
    vuelve a revisar (para eso está el botón Re-analizar).
    """
    max_age = collector.settings.reanalyze_after_days
    # Se leen de nuevo: alguno pudo quedar marcado como repetido o descartado después de guardarse.
    current = [repo.get_business(b.id) for b in businesses]
    pending = [
        b
        for b in current
        if b is not None
        and b.website
        and b.website_status == WebsiteStatus.OK
        and is_lead(b)
        and repo.needs_analysis(b.id, max_age_days=max_age)
    ]
    if not pending:
        return
    progress.webs_total = len(pending)
    with_ai = " con IA" if llm is not None else " (sin IA: hallazgos automáticos)"
    progress.say(f"Revisando las webs de {len(pending)} leads{with_ai}…")
    semaphore = asyncio.Semaphore(concurrency)

    async def one(business: Business) -> None:
        async with semaphore:
            if progress.stop_requested:
                return
            try:
                analysis = await review_website_with_retry(collector, repo, business, llm=llm)
                found = len(analysis.findings)
                score = f" · puntaje {analysis.website_score}/100" if analysis.website_score is not None else ""
                progress.say(f"   Web revisada: {business.business_name} ({found} problemas detectados{score})")
            except Exception as exc:  # una web problemática no detiene las demás
                log.exception("No se pudo revisar a fondo la web de %s (%s)", business.business_name, business.website)
                progress.say(f"   ⚠ No se pudo revisar la web de {business.business_name}: {friendly_error(exc)}")
            finally:
                progress.webs_done += 1

    await asyncio.gather(*(one(business) for business in pending))


async def run_leads_job(
    source: MapsSource,
    collector: WebCollector,
    repo: Repository,
    progress: LeadsProgress,
    *,
    max_listings: int = 120,
    review_webs: bool = True,
    llm: LLMClient | None = None,
    concurrency: int = 3,
) -> LeadsProgress:
    """Junta `progress.target` leads NUEVOS para la búsqueda, sin repetir.

    Se salta lo ya revisado otro día, los negocios SIN WEB PROPIA (o con la web caída) y los
    REPETIDOS: negocios con el mismo número, web o red social que un lead anterior o que alguien
    que Joaquín ya contactó. Se revisa en tres momentos: con la tarjeta de la lista (sin abrir
    la ficha), con la ficha y después de leer su web (donde suele aparecer el WhatsApp).

    Por cada resultado de Maps: abre la ficha, revisa su web y su WhatsApp, y cuenta el lead
    si su web abre y tiene WhatsApp verificado o celular. Para al llegar a la meta, al final de la lista,
    si Joaquín pulsa Detener o si Google bloquea. Después revisa a fondo las webs que abren
    (capturas, hallazgos automáticos y, si hay clave, el análisis con IA).
    """
    search = repo.create_search(progress.query, progress.target)
    progress.search_id = search.id
    repo.update_search(search.id, phase="Buscando en Google Maps")
    ensure_fingerprints(repo)
    saved: list[Business] = []
    reached_target = False

    def skip_duplicate(name: str, duplicate: Duplicate, business: Business | None = None) -> None:
        if business is not None:
            mark_duplicate(repo, business, duplicate)
        progress.duplicates += 1
        progress.say(f"Repetido, se salta: {name}. {duplicate.reason}")

    def skip_without_website(message: str) -> None:
        progress.no_website += 1
        progress.say(message)

    try:
        async for listing in source.iter_search(progress.query, max_results=max_listings):
            if progress.stop_requested:
                progress.status = SearchStatus.DETENIDA
                break
            known = repo.find_business(place_id=maps_place_key(listing.url), google_maps_url=listing.url)
            if known is not None:
                progress.skipped += 1
                progress.say(f"Ya revisado antes, se salta: {listing.name or 'negocio'}")
                continue
            if not card_has_website(listing):
                # Solo interesan negocios con web: sin botón "Sitio web" ni se abre la ficha.
                skip_without_website(f"Sin web propia, se salta: {listing.name or 'negocio'}")
                continue
            duplicate = find_duplicate(repo, listing_fingerprints(listing.website, listing.phone_e164))
            if duplicate:
                skip_duplicate(listing.name or "negocio", duplicate)
                continue

            progress.reviewed += 1
            progress.say(f"[{progress.leads}/{progress.target}] Abriendo: {listing.name or listing.url}")
            try:
                place = await source.get_details(listing)
            except MapsPlaceError as exc:
                progress.say(f"   ⚠ {exc}")
                continue
            business, _ = repo.upsert_business(place.to_business())
            repo.link_search_result(search.id, business.id, listing.position)
            duplicate = find_duplicate(repo, business_fingerprints(business), exclude_business_id=business.id)
            if duplicate:
                skip_duplicate(business.business_name, duplicate, business)
                continue
            if not business.website:
                skip_without_website(f"Sin web propia, se salta: {business.business_name}")
                continue
            progress.say(f"   Revisando su web: {business.website}")
            business, verification, error = await verify_business(collector, repo, business, progress=progress.say)
            saved.append(business)
            if business.website_status == WebsiteStatus.SOLO_REDES:
                skip_without_website(f"Solo tiene redes (sin web propia), se salta: {business.business_name}")
                continue
            if business.website_status != WebsiteStatus.OK:
                reason = (verification.reason if verification else None) or error or "no se pudo revisar"
                skip_without_website(f"Su web no funciona, se salta: {business.business_name} ({reason})")
                continue

            if is_lead(business):
                # Su web puede publicar un WhatsApp o una red que ya conocemos.
                duplicate = find_duplicate(repo, business_fingerprints(business), exclude_business_id=business.id)
                if duplicate:
                    skip_duplicate(business.business_name, duplicate, business)
                    continue
                register_lead(repo, business)
                progress.leads += 1
                kind = "WhatsApp verificado" if business.whatsapp_status == WhatsAppStatus.VERIFICADO else "celular para probar"
                progress.say(f"✔ Lead {progress.leads}/{progress.target}: {business.business_name} ({kind})")
            repo.update_search(
                search.id,
                phase=f"{progress.leads} de {progress.target} leads",
                businesses_found=len(saved),
                leads_found=progress.leads,
            )
            if progress.leads >= progress.target:
                reached_target = True
                break
            if progress.stop_requested:
                progress.status = SearchStatus.DETENIDA
                break
    except MapsBlockedError as exc:
        log.warning("[%s] Google bloqueó la búsqueda: %s", progress.query, exc)
        progress.status, progress.error = SearchStatus.BLOQUEADO, str(exc)
    except Exception as exc:  # cualquier otro fallo deja la búsqueda como FALLIDA, con su motivo
        log.exception("[%s] La búsqueda falló", progress.query)
        progress.status, progress.error = SearchStatus.FALLIDA, friendly_error(exc)

    if progress.status == SearchStatus.EN_CURSO:
        progress.status = SearchStatus.TERMINADA

    # Las webs no pasan por Google: se revisan aunque Maps haya terminado con bloqueo o fallo.
    if review_webs and not progress.stop_requested:
        repo.update_search(search.id, phase="Revisando las webs de los leads")
        await review_websites(collector, repo, saved, progress, llm=llm, concurrency=concurrency)
        if progress.stop_requested and progress.status == SearchStatus.TERMINADA:
            progress.status = SearchStatus.DETENIDA
    webs = f" {progress.webs_done} webs revisadas." if progress.webs_total else ""
    if progress.no_website:
        webs += f" {progress.no_website} sin web o con la web caída (saltados)."
    if progress.duplicates:
        webs += f" {progress.duplicates} repetidos saltados."

    if progress.status == SearchStatus.TERMINADA and reached_target:
        progress.say(f"Listo: {progress.leads} leads nuevos.{webs}")
    elif progress.status == SearchStatus.TERMINADA:
        progress.say(
            f"La búsqueda no tenía más negocios nuevos: {progress.leads} de {progress.target} leads.{webs} "
            "Prueba otra búsqueda para completar el día."
        )
    elif progress.status == SearchStatus.DETENIDA:
        progress.say(f"Detenida por ti: {progress.leads} leads guardados.{webs}")
    elif progress.status == SearchStatus.BLOQUEADO:
        progress.say(f"⛔ Google bloqueó la búsqueda: {progress.error} Se guardaron {progress.leads} leads.")
    else:
        progress.say(f"✖ La búsqueda falló: {progress.error}")

    repo.update_search(
        search.id,
        status=progress.status,
        error=progress.error,
        phase=progress.message,
        finished_at=utcnow(),
        businesses_found=len(saved),
        leads_found=progress.leads,
        websites_found=sum(1 for b in saved if b.website_status == WebsiteStatus.OK),
        whatsapp_verified=sum(1 for b in saved if b.whatsapp_status == WhatsAppStatus.VERIFICADO),
    )
    return progress
