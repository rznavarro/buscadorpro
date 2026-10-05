"""Línea de comandos de Vortexia Prospector.

Uso: uv run python -m app.cli <comando>
"""

import argparse
import asyncio
import json
import sys

from app.browser.engine import BrowserEngine
from app.config import get_settings
from app.logs import log, setup_logging


def _progress(message: str) -> None:
    # El avance va a stderr; stdout queda solo con el JSON.
    print(message, file=sys.stderr, flush=True)


async def _browser_check(url: str, seconds: float) -> int:
    async with BrowserEngine() as engine:
        page = await engine.page()
        await page.goto(url, wait_until="domcontentloaded")
        print(f"Título: {await page.title()}")
        await asyncio.sleep(seconds)
    print("Navegador cerrado correctamente.")
    return 0


async def _maps(query: str, limit: int) -> int:
    from app.models import SearchStatus
    from app.pipeline import run_maps_step
    from app.sources.browser_use_maps import BrowserUseMapsSource

    settings = get_settings()
    repo = _repository()

    async with BrowserUseMapsSource(settings, on_progress=_progress) as source:
        result = await run_maps_step(source, repo, query, limit, progress=_progress)

    search = result.search
    print(
        json.dumps(
            {
                "busqueda": {
                    "id": search.id,
                    "consulta": search.query,
                    "limite": search.limit,
                    "estado": str(search.status),
                    "error": search.error,
                    "negocios_encontrados": search.businesses_found,
                    "webs_encontradas": search.websites_found,
                    "whatsapp_verificados": search.whatsapp_verified,
                },
                "negocios": result.businesses,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if search.status == SearchStatus.BLOQUEADO:
        _progress(
            "\n⛔ Google bloqueó la búsqueda (captcha o tráfico inusual). Se detuvo sin intentar resolverlo.\n"
            "   Espera un rato antes de volver a buscar."
        )
        return 2
    return 0 if search.status == SearchStatus.TERMINADA else 1


def _repository():
    from app.db.repository import Repository
    from app.db.schema import create_db_engine, init_db

    engine = create_db_engine(get_settings().db_path)
    init_db(engine)
    return Repository(engine)


def _facts_summary(collection) -> dict:
    """Lo que imprime `facts`: primero web y WhatsApp, después el resto de los hechos."""
    from app.extract.whatsapp import choose_whatsapp

    verification = collection.verification
    decision = choose_whatsapp(verification.whatsapp_candidates, None, verification.whatsapp_broken)
    facts = collection.facts
    summary = {
        "web": {
            "url_pedida": verification.url,
            "url_final": verification.final_url,
            "estado": str(verification.status),
            "motivo": verification.reason,
            "http": verification.http_status,
            "https": verification.https,
            "redirecciones": verification.redirects,
            "abierta_con_navegador": verification.rendered,
        },
        "whatsapp": {
            "estado": str(decision.status),
            "url": decision.url,
            "confianza": str(decision.confidence) if decision.confidence else None,
            "por_que": decision.confidence_reason,
            "evidencia": decision.evidence,
            "boton_flotante": verification.whatsapp_floating,
            "varios_numeros": decision.multiple_numbers,
            "botones_rotos": decision.broken_evidence,
            "candidatos": [c.model_dump(mode="json", exclude_none=True) for c in decision.candidates],
        },
        "paginas_revisadas": verification.pages_visited,
        "linktree": verification.linktree_urls,
        "redes": verification.socials,
        "telefonos": verification.phones,
        "correos": verification.emails,
    }
    if facts is not None:
        summary["hechos"] = facts.model_dump(mode="json", exclude={"whatsapp", "visible_text", "internal_links"})
        summary["hechos"]["enlaces_internos"] = len(facts.internal_links)
    summary["escritorio"] = collection.desktop.model_dump() if collection.desktop else None
    summary["celular"] = collection.mobile.model_dump() if collection.mobile else None
    summary["enlaces_rotos"] = {
        "revisados": collection.links_checked,
        "rotos": [b.model_dump() for b in collection.broken_links],
    }
    summary["imagenes_rotas"] = {
        "revisadas": collection.images_checked,
        "rotas": [b.model_dump() for b in collection.broken_images],
    }
    summary["pagespeed_movil"] = collection.psi_mobile_score
    summary["notas"] = collection.notes
    return summary


async def _facts(url: str) -> int:
    from app.analyze.collector import WebCollector

    _progress(f"Revisando {url} …")
    async with WebCollector() as collector:
        collection = await collector.collect(url)
    print(json.dumps(_facts_summary(collection), ensure_ascii=False, indent=2))
    if collection.desktop and collection.desktop.screenshot:
        _progress(f"\nCapturas guardadas en: {(get_settings().data_dir / collection.desktop.screenshot).parent}")
    return 0


async def _verify(search_id: int) -> int:
    from app.analyze.collector import WebCollector
    from app.pipeline import run_verify_step

    settings = get_settings()
    repo = _repository()
    search = repo.get_search(search_id)
    if search is None:
        _progress(f"No existe la búsqueda {search_id}. Las búsquedas guardadas son:")
        for item in repo.list_searches():
            _progress(f"  {item.id}: {item.query} ({item.businesses_found} negocios)")
        return 1
    _progress(f"Verificando webs y WhatsApp de la búsqueda {search.id}: {search.query}")
    async with WebCollector(settings) as collector:
        results = await run_verify_step(
            collector, repo, search_id, concurrency=settings.web_concurrency, progress=_progress
        )
    print(json.dumps(results, ensure_ascii=False, indent=2))
    with_both = sum(1 for r in results if r["web_estado"] == "OK" and r["whatsapp_estado"] == "VERIFICADO")
    _progress(f"\n{with_both} de {len(results)} negocios tienen web que abre y WhatsApp verificado.")
    return 0


async def _analyze(url: str) -> int:
    from urllib.parse import urlsplit

    from app.analyze.collector import WebCollector
    from app.analyze.findings import build_findings
    from app.analyze.llm import LLMError, build_llm_client
    from app.analyze.report import code_scores, evidence_package
    from app.analyze.scorer import ParamScore, param_name, total_score
    from app.models import Business

    settings = get_settings()
    llm = build_llm_client(settings)
    _progress(f"Revisando {url} …" + ("" if llm else " (IA apagada: solo hallazgos automáticos, costo $0)"))
    async with WebCollector(settings) as collector:
        collection = await collector.collect(url)
    title = collection.facts.title if collection.facts else None
    business = Business(google_maps_url=url, business_name=title or urlsplit(url).hostname or url)
    findings = build_findings(collection, business)
    scores = code_scores(collection, findings)
    summary: dict = {
        "web": {
            "url_final": collection.verification.final_url,
            "estado": str(collection.verification.status),
            "motivo": collection.verification.reason,
        },
        "problemas_detectados": [f.model_dump(exclude_none=True) for f in findings.hallazgos],
        "seo_local": {key: item.model_dump() for key, item in findings.seo_local.items()},
        "checklist": {key: item.model_dump() for key, item in findings.checklist.items()},
    }
    if llm is not None and collection.desktop is not None:
        try:
            result = await llm.analyze(evidence_package(business, collection, findings, settings))
        except LLMError as exc:
            summary["ia"] = {"error": str(exc)}
        else:
            scores += [
                ParamScore(id=j.id, nombre=param_name(j.id), puntaje=j.puntaje, justificacion=j.justificacion,
                           evidencia=j.evidencia, fuente="ia")
                for j in result.analysis.parametros
            ]
            summary["ia"] = {
                "modelo": result.model,
                "problemas": [p.model_dump() for p in result.analysis.problemas],
                "oportunidades": [o.model_dump() for o in result.analysis.oportunidades],
                "puntos_fuertes": [s.model_dump() for s in result.analysis.puntos_fuertes],
                "descartado_por_falta_de_evidencia": result.discarded,
            }
    else:
        summary["ia"] = "Apagada: no hay ANTHROPIC_API_KEY en .env."
    summary["rubrica"] = {
        "puntajes": [s.model_dump() for s in sorted(scores, key=lambda s: s.id)],
        "score_total": total_score(scores),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


async def _run(query: str, limit: int) -> int:
    from app.analyze.collector import WebCollector
    from app.analyze.llm import build_llm_client
    from app.leads import lead_view
    from app.models import SearchStatus
    from app.pipeline import LeadsProgress, is_lead, run_leads_job
    from app.sources.browser_use_maps import BrowserUseMapsSource

    class PrintedProgress(LeadsProgress):
        def say(self, message: str) -> None:
            super().say(message)
            _progress(message)

    settings = get_settings()
    repo = _repository()
    progress = PrintedProgress(query=query, target=limit)
    async with (
        BrowserUseMapsSource(settings, on_progress=progress.say) as source,
        WebCollector(settings) as collector,
    ):
        await run_leads_job(
            source, collector, repo, progress, llm=build_llm_client(settings), concurrency=settings.web_concurrency
        )
    leads = [
        lead_view(business, query, position, repo.latest_analysis(business.id))
        for position, business in repo.businesses_for_search(progress.search_id or 0)
        if is_lead(business)
    ]
    print(json.dumps([lead.model_dump() for lead in leads], ensure_ascii=False, indent=2, default=str))
    return {SearchStatus.TERMINADA: 0, SearchStatus.BLOQUEADO: 2}.get(progress.status, 1)


def _import_contacted(path: str) -> int:
    from pathlib import Path

    from app.dedupe import import_contacted

    file = Path(path)
    if not file.is_file():
        _progress(f"No existe el archivo {file}")
        return 1
    repo = _repository()
    result = import_contacted(repo, file.read_text(encoding="utf-8-sig"), get_settings().default_region)
    print(f"Contactados nuevos: {result.added} · actualizados: {result.updated} · total en la lista: {repo.count_contacted()}")
    if result.flagged:
        names = ", ".join(business.business_name for business in result.flagged)
        print(f"Leads guardados que ya estaban contactados (se ocultan): {names}")
    if result.invalid:
        print(f"{len(result.invalid)} líneas sin un número válido (no se guardaron):")
        for line in result.invalid:
            print(f"   {line}")
    return 0


def _port_in_use(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _serve(port: int, open_browser: bool) -> int:
    import threading
    import webbrowser

    import uvicorn

    from app.web.app import create_app

    url = f"http://127.0.0.1:{port}/"
    if _port_in_use(port):
        # Ya hay una ventana del programa abierta (por ejemplo, doble clic dos veces): se usa esa.
        _progress(f"Vortexia Prospector ya está abierto en {url}; se abre esa página.")
        if open_browser:
            webbrowser.open(url)
        return 0
    log.info("Página abierta en %s", url)
    _progress(f"Vortexia Prospector abierto en {url}  (para cerrarlo: Ctrl+C en esta ventana)")
    if open_browser:
        threading.Timer(1.5, webbrowser.open, args=[url]).start()
    # Sin recarga automática: así Playwright puede abrir el navegador desde el servidor en Windows.
    uvicorn.run(create_app(), host="127.0.0.1", port=port, log_level="warning")
    return 0


def main(argv: list[str] | None = None) -> int:
    # La consola de Windows no usa UTF-8 por defecto: sin esto, los acentos salen mal.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    setup_logging(get_settings().data_dir)  # registro del día en data/logs/

    parser = argparse.ArgumentParser(prog="python -m app.cli", description="Vortexia Prospector")
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser(
        "browser-check",
        help="Abre el navegador en una página, muestra su título y lo cierra.",
    )
    check.add_argument("--url", default="https://example.com", help="Página a abrir")
    check.add_argument("--seconds", type=float, default=3.0, help="Segundos que el navegador queda abierto")

    maps = commands.add_parser("maps", help='Busca en Google Maps e imprime los negocios en JSON. Ej: "cerrajeros en Rancagua"')
    maps.add_argument("query", help="Qué buscar, por ejemplo: cerrajeros en Rancagua")
    maps.add_argument("--limit", type=int, default=None, help="Máximo de negocios (por defecto: SEARCH_LIMIT_DEFAULT)")

    facts = commands.add_parser("facts", help="Revisa una web: si abre, su WhatsApp, capturas, tiempos y enlaces rotos.")
    facts.add_argument("url", help="Dirección de la web, por ejemplo https://ejemplo.cl")

    verify = commands.add_parser(
        "verify", help="Revisa la web y el WhatsApp de todos los negocios de una búsqueda guardada."
    )
    verify.add_argument("busqueda", type=int, help="Número de la búsqueda (el 'id' que muestra el comando maps)")

    analyze = commands.add_parser(
        "analyze", help="Revisa una web y muestra sus problemas (con IA solo si hay clave en .env)."
    )
    analyze.add_argument("url", help="Dirección de la web, por ejemplo https://ejemplo.cl")

    run = commands.add_parser("run", help='Junta leads nuevos de una búsqueda (lo mismo que el botón de la página).')
    run.add_argument("query", help="Qué buscar, por ejemplo: cerrajeros en Rancagua")
    run.add_argument("--limit", type=int, default=None, help="Cuántos leads nuevos (por defecto: SEARCH_LIMIT_DEFAULT)")

    serve = commands.add_parser("serve", help="Abre la página de Vortexia Prospector en tu navegador.")
    serve.add_argument("--port", type=int, default=8000, help="Puerto local (por defecto 8000)")
    serve.add_argument("--no-abrir", action="store_true", help="No abrir el navegador automáticamente")

    contacted = commands.add_parser(
        "import-contactados", help="Agrega a la lista de ya contactados las filas de un archivo de texto (copiadas de tu planilla)."
    )
    contacted.add_argument("archivo", help="Archivo .txt con una fila por línea: nombre, teléfono, estado, hora, fecha")

    args = parser.parse_args(argv)
    if args.command == "serve":
        return _serve(args.port, not args.no_abrir)
    if args.command == "import-contactados":
        return _import_contacted(args.archivo)
    if args.command == "browser-check":
        return asyncio.run(_browser_check(args.url, args.seconds))
    if args.command == "facts":
        return asyncio.run(_facts(args.url))
    if args.command == "analyze":
        return asyncio.run(_analyze(args.url))
    if args.command == "run":
        settings = get_settings()
        limit = args.limit or settings.search_limit_default
        if not 1 <= limit <= settings.search_limit_max:
            parser.error(f"--limit debe estar entre 1 y {settings.search_limit_max} (SEARCH_LIMIT_MAX).")
        return asyncio.run(_run(args.query, limit))
    if args.command == "verify":
        return asyncio.run(_verify(args.busqueda))
    if args.command == "maps":
        settings = get_settings()
        limit = args.limit or settings.search_limit_default
        if not 1 <= limit <= settings.search_limit_max:
            parser.error(f"--limit debe estar entre 1 y {settings.search_limit_max} (SEARCH_LIMIT_MAX).")
        return asyncio.run(_maps(args.query, limit))
    return 1


if __name__ == "__main__":
    sys.exit(main())
