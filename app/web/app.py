"""Página local de Vortexia Prospector: buscar leads, verlos por día, filtrarlos y ver su informe.

Se abre con `uv run python -m app.cli serve`. FastAPI + Jinja2 + HTMX, sin build de
JavaScript (sección 3). Las búsquedas corren en segundo plano, de a una (una sola sesión de
navegador para Maps, sección 2.6), y la página muestra el avance cada 2 segundos. Las
revisiones de webs que se piden desde la página (Re-analizar) también corren en segundo
plano, de a una, con su propio navegador sin ventana.
"""

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import quote, urlencode

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.business_report import business_report
from app.config import Settings, get_settings
from app.db.repository import Repository
from app.db.schema import create_db_engine, init_db
from app.dedupe import ImportResult, import_contacted, unmark_duplicate
from app.extract.phones import pretty_number
from app.leads import (
    FILTERS,
    SORTS,
    confirm_whatsapp,
    days_with_searches,
    discard_lead,
    duplicates_for_day,
    filter_leads,
    lead_for_business,
    leads_csv,
    leads_for_day,
    restore_lead,
    searches_for_day,
)
from app.models import SearchStatus, WebsiteStatus
from app.logs import log, read_today
from app.pipeline import LeadsProgress, friendly_error, review_website_with_retry, run_leads_job

WEB_DIR = Path(__file__).parent
Runner = Callable[[LeadsProgress, Repository], Awaitable[LeadsProgress]]
Reviewer = Callable[[int, Repository], Awaitable[None]]

_DAYS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
_MONTHS = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)
_KEEP = object()  # "no cambiar este parámetro" al armar una URL de la lista


def spanish_date(day: date) -> str:
    """"sábado 4 de octubre"."""
    return f"{_DAYS[day.weekday()]} {day.day} de {_MONTHS[day.month - 1]}"


def local_time(moment: datetime) -> str:
    """"04-10-2026 22:15" en la hora de este computador (SQLite devuelve UTC sin zona)."""
    moment =moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    return f"{moment.astimezone():%d-%m-%Y %H:%M}"


async def browser_runner(progress: LeadsProgress, repo: Repository) -> LeadsProgress:
    """Búsqueda real: Google Maps en el navegador + verificación de cada web."""
    from app.analyze.collector import WebCollector
    from app.analyze.llm import build_llm_client
    from app.sources.browser_use_maps import BrowserUseMapsSource

    settings = get_settings()
    async with (
        BrowserUseMapsSource(settings, on_progress=progress.say) as source,
        WebCollector(settings) as collector,
    ):
        return await run_leads_job(
            source,
            collector,
            repo,
            progress,
            llm=build_llm_client(settings),  # None sin clave: solo hallazgos automáticos, costo $0
            concurrency=settings.web_concurrency,
        )


async def browser_reviewer(business_id: int, repo: Repository) -> None:
    """Re-analizar: revisa de nuevo la web (estado, WhatsApp, capturas, hallazgos y, si hay clave, IA)."""
    from app.analyze.collector import WebCollector
    from app.analyze.llm import build_llm_client

    settings = get_settings()
    business = repo.get_business(business_id)
    if business is None or not business.website:
        return
    log.info("Re-analizando la web de %s (%s)", business.business_name, business.website)
    async with WebCollector(settings) as collector:
        analysis = await review_website_with_retry(
            collector, repo, business, llm=build_llm_client(settings), refresh_business=True
        )
    log.info("Web de %s revisada: %s problemas detectados", business.business_name, len(analysis.findings))


class JobManager:
    """Una búsqueda de leads a la vez, en segundo plano."""

    def __init__(self, repo: Repository, runner: Runner) -> None:
        self.repo = repo
        self.runner = runner
        self.current: LeadsProgress | None = None
        self.task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def start(self, query: str, target: int) -> LeadsProgress:
        if self.running:
            raise RuntimeError("Ya hay una búsqueda en curso. Espera a que termine o detenla.")
        progress = LeadsProgress(query=query, target=target)
        self.current = progress
        self.task = asyncio.create_task(self._run(progress))
        return progress

    async def _run(self, progress: LeadsProgress) -> None:
        log.info("[%s] Búsqueda iniciada desde la página (meta: %s leads)", progress.query, progress.target)
        try:
            await self.runner(progress, self.repo)
        except Exception as exc:  # por ejemplo, el navegador no pudo abrirse
            log.exception("[%s] No se pudo completar la búsqueda", progress.query)
            progress.status = SearchStatus.FALLIDA
            progress.error = friendly_error(exc)
            progress.say(f"✖ No se pudo completar la búsqueda: {progress.error}")

    def stop(self) -> None:
        if self.current is not None and self.running and not self.current.stop_requested:
            self.current.stop_requested = True
            self.current.say("Deteniendo… termina el negocio que está revisando y se guarda todo.")


class ReviewManager:
    """Revisiones de webs pedidas desde la página (Re-analizar), de a una y en segundo plano."""

    def __init__(self, repo: Repository, reviewer: Reviewer) -> None:
        self.repo = repo
        self.reviewer = reviewer
        self.queue: list[int] = []
        self.current: int | None = None
        self.errors: dict[int, str] = {}  # última revisión que falló, por negocio
        self.done = 0
        self.total = 0
        self.task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def enqueue(self, business_ids: Iterable[int]) -> int:
        """Agrega negocios a la cola (sin repetir). Devuelve cuántos se agregaron."""
        if not self.running:
            self.done = self.total = 0
        added = 0
        for business_id in business_ids:
            if business_id == self.current or business_id in self.queue:
                continue
            self.queue.append(business_id)
            self.errors.pop(business_id, None)
            added += 1
        self.total += added
        if added and not self.running:
            self.task = asyncio.create_task(self._work())
        return added

    def status(self, business_id: int) -> str | None:
        """"revisando", "en_cola" o None."""
        if business_id == self.current:
            return "revisando"
        if business_id in self.queue:
            return "en_cola"
        return None

    async def _work(self) -> None:
        while self.queue:
            self.current = self.queue.pop(0)
            try:
                await self.reviewer(self.current, self.repo)
            except Exception as exc:  # una web problemática no detiene las demás
                log.exception("No se pudo re-analizar la web del negocio %s", self.current)
                self.errors[self.current] = friendly_error(exc)
            finally:
                self.current = None
                self.done += 1


def create_app(
    settings: Settings | None = None,
    *,
    repo: Repository | None = None,
    runner: Runner | None = None,
    reviewer: Reviewer | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    if repo is None:
        engine = create_db_engine(settings.db_path)
        init_db(engine)
        repo = Repository(engine)
    jobs = JobManager(repo, runner or browser_runner)
    reviews = ReviewManager(repo, reviewer or browser_reviewer)

    app = FastAPI(title="Vortexia Prospector")
    app.state.jobs = jobs
    app.state.reviews = reviews
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    # Solo las capturas de las webs revisadas (nunca la base de datos).
    app.mount(
        "/capturas",
        StaticFiles(directory=settings.data_dir / "screenshots", check_dir=False),
        name="capturas",
    )
    templates = Jinja2Templates(directory=WEB_DIR / "templates")
    templates.env.policies["json.dumps_kwargs"] = {"ensure_ascii": False, "sort_keys": False}  # tildes legibles
    templates.env.globals["ai_enabled"] = settings.ai_enabled
    templates.env.globals["contacted_count"] = repo.count_contacted
    templates.env.globals["review_status"] = reviews.status
    templates.env.filters["numero"] = pretty_number
    templates.env.filters["fecha"] = spanish_date
    templates.env.filters["hora"] = local_time
    templates.env.filters["coma"] = lambda value: f"{value:.1f}".replace(".", ",") if value is not None else ""
    templates.env.filters["captura"] = lambda path: "/capturas/" + str(path).removeprefix("screenshots/")

    def parse_day(dia: str | None) -> date:
        if not dia:
            return date.today()
        try:
            return date.fromisoformat(dia)
        except ValueError as exc:
            raise HTTPException(400, "Fecha inválida") from exc

    def leads_context(day: date, order: str, filters: list[str] | None = None, query: str | None = None) -> dict:
        order = order if order in SORTS else "whatsapp"
        active = [key for key in dict.fromkeys(filters or []) if key in FILTERS]
        query = query or None
        all_leads = leads_for_day(repo, day, order=order)
        leads = filter_leads(all_leads, active, query)

        def url(path: str = "/", *, toggle: str | None = None, search: object = _KEEP, clear: bool = False) -> str:
            """URL de la lista con el estado actual, cambiando un filtro, la búsqueda o quitando los filtros."""
            chosen = [] if clear else [key for key in active if key != toggle]
            if toggle and toggle not in active:
                chosen.append(toggle)
            chosen_query = query if search is _KEEP else search
            params = [("dia", day.isoformat()), ("orden", order), *(("filtro", key) for key in chosen)]
            if chosen_query:
                params.append(("busqueda", str(chosen_query)))
            return f"{path}?{urlencode(params)}"

        return {
            "day": day,
            "today": date.today(),
            "order": order,
            "sorts": SORTS,
            "filters": {key: (label, help_text) for key, (label, help_text, _) in FILTERS.items()},
            "active": active,
            "search_query": query,
            "searches": searches_for_day(repo, day),
            "url": url,
            "all_count": len(all_leads),
            "leads": leads,
            "duplicates": duplicates_for_day(repo, day),
            "verified": sum(1 for lead in leads if lead.has_whatsapp),
            "to_test": sum(1 for lead in leads if lead.whatsapp_state == "por_probar"),
            "high_opportunity": sum(1 for lead in leads if lead.opportunity == "ALTA"),
            "pending_reviews": [lead.id for lead in all_leads if lead.review_pending and not reviews.status(lead.id)],
        }

    def row_response(request: Request, business_id: int) -> HTMLResponse:
        lead = lead_for_business(repo, business_id)
        if lead is None:
            raise HTTPException(404, "No existe ese negocio")
        return templates.TemplateResponse(request, "_row.html", {"lead": lead})

    Filters = Annotated[list[str], Query()]

    @app.get("/", response_class=HTMLResponse)
    def index(
        request: Request,
        dia: str | None = None,
        orden: str = "whatsapp",
        filtro: Filters = [],  # noqa: B006 (FastAPI copia el valor por defecto)
        busqueda: str | None = None,
        aviso: str | None = None,
    ):
        context = leads_context(parse_day(dia), orden, filtro, busqueda)
        context.update(
            job=jobs.current,
            running=jobs.running,
            reviews=reviews,
            days=days_with_searches(repo),
            default_target=settings.search_limit_default,
            max_target=settings.search_limit_max,
            notice=aviso,
        )
        return templates.TemplateResponse(request, "index.html", context)

    @app.get("/leads", response_class=HTMLResponse)
    def leads_table(
        request: Request,
        dia: str | None = None,
        orden: str = "whatsapp",
        filtro: Filters = [],  # noqa: B006
        busqueda: str | None = None,
    ):
        return templates.TemplateResponse(request, "_leads.html", leads_context(parse_day(dia), orden, filtro, busqueda))

    @app.post("/buscar")
    async def start_search(consulta: str = Form(...), cantidad: int = Form(20)):
        # async: la búsqueda se lanza como tarea en el mismo event loop del servidor.
        query = " ".join(consulta.split())
        if not query:
            return RedirectResponse(f"/?aviso={quote('Escribe qué quieres buscar.')}", status_code=303)
        target = max(1, min(cantidad, settings.search_limit_max))
        try:
            jobs.start(query, target)
        except RuntimeError as exc:
            return RedirectResponse(f"/?aviso={quote(str(exc))}", status_code=303)
        return RedirectResponse("/", status_code=303)

    @app.get("/progreso", response_class=HTMLResponse)
    def progress(request: Request):
        response = templates.TemplateResponse(
            request, "_progress.html", {"job": jobs.current, "running": jobs.running}
        )
        if jobs.current is not None:
            response.headers["HX-Trigger"] = "leads-actualizados"  # la lista se recarga con los leads nuevos
        return response

    @app.post("/detener", response_class=HTMLResponse)
    def stop(request: Request):
        jobs.stop()
        return templates.TemplateResponse(request, "_progress.html", {"job": jobs.current, "running": jobs.running})

    # --- Acciones sobre un lead ------------------------------------------------------------

    @app.post("/negocio/{business_id}/existe", response_class=HTMLResponse)
    def whatsapp_exists(request: Request, business_id: int):
        try:
            confirm_whatsapp(repo, business_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return row_response(request, business_id)

    @app.post("/negocio/{business_id}/no-existe", response_class=HTMLResponse)
    def whatsapp_missing(request: Request, business_id: int):
        try:
            discard_lead(repo, business_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        return row_response(request, business_id)

    @app.post("/negocio/{business_id}/deshacer", response_class=HTMLResponse)
    def undo(request: Request, business_id: int):
        try:
            restore_lead(repo, business_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        return row_response(request, business_id)

    @app.post("/negocio/{business_id}/no-repetido")
    async def not_duplicate(business_id: int):
        try:
            business = unmark_duplicate(repo, business_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        if business.website and business.website_status != WebsiteStatus.OK:
            # Se marcó como repetido antes de revisar su web: se revisa ahora y, si abre, entra a la lista.
            reviews.enqueue([business_id])
            return Response(status_code=204, headers={"HX-Refresh": "true"})
        # La lista se recarga sola, con los filtros que estén puestos.
        return Response(status_code=204, headers={"HX-Trigger": "leads-actualizados"})

    # --- Informe del negocio y Re-analizar --------------------------------------------------

    def review_box(request: Request, business_id: int) -> HTMLResponse:
        business = repo.get_business(business_id)
        if business is None:
            raise HTTPException(404, "No existe ese negocio")
        return templates.TemplateResponse(
            request,
            "_revision.html",
            {"business": business, "status": reviews.status(business_id), "error": reviews.errors.get(business_id)},
        )

    @app.get("/negocio/{business_id}", response_class=HTMLResponse)
    def business_page(request: Request, business_id: int):
        report = business_report(repo, business_id)
        if report is None:
            raise HTTPException(404, "No existe ese negocio")
        return templates.TemplateResponse(
            request,
            "negocio.html",
            {
                "report": report,
                "lead": report.lead,
                "business": report.business,
                "analysis": report.analysis,
                "status": reviews.status(business_id),
                "error": reviews.errors.get(business_id),
            },
        )

    @app.post("/negocio/{business_id}/reanalizar", response_class=HTMLResponse)
    async def reanalyze(request: Request, business_id: int):
        business = repo.get_business(business_id)
        if business is None:
            raise HTTPException(404, "No existe ese negocio")
        if not business.website:
            raise HTTPException(400, "Este negocio no tiene web para revisar.")
        reviews.enqueue([business_id])
        return review_box(request, business_id)

    @app.get("/negocio/{business_id}/revision", response_class=HTMLResponse)
    def review_state(request: Request, business_id: int):
        response = review_box(request, business_id)
        if reviews.status(business_id) is None:
            response.headers["HX-Refresh"] = "true"  # terminó: se recarga el informe con el análisis nuevo
        return response

    @app.post("/revisar-pendientes")
    async def review_pending(dia: str | None = Form(None)):
        day = parse_day(dia)
        pending = [lead.id for lead in leads_for_day(repo, day) if lead.review_pending]
        added = reviews.enqueue(pending)
        notice = f"Revisando {added} webs en segundo plano. La lista se actualiza sola." if added else "No hay webs pendientes."
        return RedirectResponse(f"/?dia={day.isoformat()}&aviso={quote(notice)}", status_code=303)

    @app.get("/revisiones", response_class=HTMLResponse)
    def reviews_progress(request: Request):
        response = templates.TemplateResponse(request, "_reviews.html", {"reviews": reviews})
        response.headers["HX-Trigger"] = "leads-actualizados"
        return response

    # --- Ya contactados ------------------------------------------------------------------

    def contacted_page(request: Request, result: ImportResult | None = None, text: str = "") -> HTMLResponse:
        return templates.TemplateResponse(
            request, "contactados.html", {"contacted": repo.list_contacted(), "result": result, "text": text}
        )

    @app.get("/contactados", response_class=HTMLResponse)
    def contacted(request: Request):
        return contacted_page(request)

    @app.post("/contactados", response_class=HTMLResponse)
    def import_contacted_list(request: Request, lista: str = Form("")):
        result = import_contacted(repo, lista, settings.default_region)
        # Las líneas que no se pudieron leer quedan en el cuadro para corregirlas.
        return contacted_page(request, result, "\n".join(result.invalid))

    # --- Registro del día ---------------------------------------------------------------------

    @app.get("/registro", response_class=HTMLResponse)
    def daily_log(request: Request):
        lines = list(reversed(read_today(settings.data_dir)))
        return templates.TemplateResponse(request, "registro.html", {"lines": lines, "today": date.today()})

    # --- Exportar -------------------------------------------------------------------------

    @app.get("/exportar.csv")
    def export(
        dia: str | None = None,
        orden: str = "whatsapp",
        filtro: Filters = [],  # noqa: B006
        busqueda: str | None = None,
    ):
        day = parse_day(dia)
        leads = filter_leads(leads_for_day(repo, day, order=orden), filtro, busqueda or None)
        return Response(
            leads_csv(leads).encode("utf-8"),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="leads-{day.isoformat()}.csv"'},
        )

    return app
