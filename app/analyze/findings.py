"""Hallazgos automáticos (sin IA): problemas medidos en la web, gratis y con evidencia.

Cada hallazgo se escribe en lenguaje de negocio (lo que siente o no encuentra el cliente que
entra a la web, sección 8.4) y guarda aparte su evidencia técnica. También calcula el SEO
local y el checklist de información (sección 8.3) que se pueden medir sin juicio humano.
"""

import re
from datetime import date

from pydantic import BaseModel, Field

from app.analyze.collector import WebCollection
from app.analyze.scorer import speed_score
from app.extract.html_facts import fold_accents
from app.models import Business, WebsiteStatus, WhatsAppSource

SEVERITY_ORDER = {"alta": 0, "media": 1, "baja": 2}
# Constructores que se ven como plantilla (WordPress o Elementor solos no lo son necesariamente).
_TEMPLATE_BUILDERS = {
    "Wix", "Manus", "Lovable", "GoDaddy Website Builder", "Google Sites", "Google Business Site",
    "Jimdo", "Blogger", "WordPress.com", "Squarespace", "Tiendanube",
}
_HOURS = re.compile(r"\b(?:lunes|horario|24 horas|24/7|abierto|atencion de)\b", re.IGNORECASE)
_SERVICES = re.compile(r"\bservicios?\b", re.IGNORECASE)


class Finding(BaseModel):
    codigo: str
    titulo: str  # lenguaje de negocio
    detalle: str | None = None
    evidencia: list[str] = Field(default_factory=list)  # técnica: se muestra plegada
    gravedad: str  # "alta", "media" o "baja"
    parametro: int | None = None  # parámetro de la rúbrica relacionado
    captura: str | None = None  # ruta relativa a data/


class CheckItem(BaseModel):
    ok: bool | None  # None: lo decide la IA
    detalle: str


class FindingsReport(BaseModel):
    hallazgos: list[Finding] = Field(default_factory=list)
    seo_local: dict[str, CheckItem] = Field(default_factory=dict)
    checklist: dict[str, CheckItem] = Field(default_factory=dict)
    link_errors: list[str] = Field(default_factory=list)  # para el parámetro 9


def _sorted(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: SEVERITY_ORDER.get(f.gravedad, 9))


# --- Negocios sin web que revisar --------------------------------------------------------


def status_findings(business: Business) -> list[Finding]:
    """Hallazgos que salen del estado de la web, sin abrirla (sin web, solo redes, caída)."""
    reason = str((business.field_sources or {}).get("website_status") or "").removeprefix("web: ")
    if business.website_status == WebsiteStatus.SIN_WEB:
        return [
            Finding(
                codigo="sin_web",
                titulo="No tiene web propia",
                detalle="Quien lo busca en Google solo encuentra su ficha de Maps.",
                gravedad="alta",
            )
        ]
    if business.website_status == WebsiteStatus.SOLO_REDES:
        networks = [name for name, url in (("Instagram", business.instagram), ("Facebook", business.facebook)) if url]
        where = " y ".join(networks) or "redes sociales o una página de enlaces"
        return [
            Finding(
                codigo="solo_redes",
                titulo=f"Usa {where} en vez de una web propia",
                detalle="No tiene un sitio propio que muestre sus servicios y genere confianza.",
                evidencia=[str((business.field_sources or {}).get("website_maps") or business.website or "")],
                gravedad="alta",
            )
        ]
    if business.website_status == WebsiteStatus.CAIDO:
        return [
            Finding(
                codigo="web_caida",
                titulo="Su web no funciona",
                detalle=reason or "La web no abre.",
                evidencia=[business.website or ""],
                gravedad="alta",
            )
        ]
    return []


# --- Webs revisadas ------------------------------------------------------------------------


def build_findings(collection: WebCollection, business: Business | None = None, today: date | None = None) -> FindingsReport:
    """Hallazgos, SEO local y checklist de una web revisada con `collect`."""
    today = today or date.today()
    verification, facts = collection.verification, collection.facts
    if verification.status != WebsiteStatus.OK:
        # No abrió al revisarla (por ejemplo, al re-analizar): no hay nada más que medir.
        if verification.status == WebsiteStatus.CAIDO:
            return FindingsReport(
                hallazgos=[
                    Finding(
                        codigo="web_caida",
                        titulo="Su web no funciona",
                        detalle=verification.reason or "La web no abre.",
                        evidencia=[verification.url],
                        gravedad="alta",
                    )
                ]
            )
        if verification.status == WebsiteStatus.SOLO_REDES:
            return FindingsReport(
                hallazgos=[
                    Finding(
                        codigo="solo_redes",
                        titulo="Su web lleva a redes sociales en vez de a un sitio propio",
                        detalle="No tiene un sitio propio que muestre sus servicios y genere confianza.",
                        evidencia=[verification.final_url or verification.url],
                        gravedad="alta",
                    )
                ]
            )
        return FindingsReport()  # bloqueada por una protección anti-bots: no se pudo medir
    desktop, mobile = collection.desktop, collection.mobile
    findings: list[Finding] = []
    link_errors: list[str] = []
    text = fold_accents((facts.visible_text if facts else "") or "").lower()
    city = fold_accents(business.city or "").lower() if business and business.city else ""

    # --- Contacto y llamados a la acción (parámetro 7) ---
    fake = list(dict.fromkeys([*(desktop.fake_ctas_first_screen if desktop else []), *(mobile.fake_ctas_first_screen if mobile else [])]))
    if fake:
        names = ", ".join(f"“{t}”" for t in fake[:3])
        findings.append(
            Finding(
                codigo="boton_falso",
                titulo=f"El botón {names} no hace nada al tocarlo" if len(fake) == 1 else f"Los botones {names} no hacen nada al tocarlos",
                detalle="Se ven como botones, pero no son enlaces: el cliente los toca y no pasa nada.",
                evidencia=[f"Elemento sin enlace en la primera pantalla: {t}" for t in fake],
                gravedad="alta",
                parametro=7,
                captura=desktop.screenshot if desktop else None,
            )
        )
        link_errors.extend(f"Botón sin enlace: {t}" for t in fake)

    web_whatsapp = [c for c in verification.whatsapp_candidates if c.source in (WhatsAppSource.WEB, WhatsAppSource.LINKTREE)]
    if verification.whatsapp_broken:
        findings.append(
            Finding(
                codigo="whatsapp_roto",
                titulo="El botón de WhatsApp de la web no funciona",
                detalle="Un cliente que quiere escribir por WhatsApp no logra abrir el chat.",
                evidencia=verification.whatsapp_broken[:5],
                gravedad="alta",
                parametro=7,
            )
        )
        link_errors.extend(f"WhatsApp roto: {e}" for e in verification.whatsapp_broken)
    if not web_whatsapp:
        findings.append(
            Finding(
                codigo="sin_whatsapp_web",
                titulo="La web no tiene un botón de WhatsApp",
                detalle="Para escribirle, el cliente tiene que buscar y copiar el número a mano.",
                gravedad="alta",
                parametro=7,
            )
        )
    elif mobile and not mobile.whatsapp_first_screen:
        findings.append(
            Finding(
                codigo="whatsapp_escondido_celular",
                titulo="En el celular, el WhatsApp no se ve al entrar",
                detalle="Hay que bajar o buscarlo; muchos clientes se van antes de encontrarlo.",
                gravedad="media",
                parametro=7,
                captura=mobile.screenshot,
            )
        )
    no_cta = all(
        page is None or (not page.ctas_first_screen and not page.whatsapp_first_screen and not page.tel_first_screen)
        for page in (desktop, mobile)
    )
    if (desktop or mobile) and no_cta:
        findings.append(
            Finding(
                codigo="sin_llamado_accion",
                titulo="Al entrar no hay un botón claro para contactar",
                detalle="La primera pantalla no invita a escribir, llamar ni cotizar.",
                gravedad="media",
                parametro=7,
                captura=desktop.screenshot if desktop else None,
            )
        )
    if facts is not None and not facts.tel_links:
        findings.append(
            Finding(
                codigo="sin_telefono_click",
                titulo="No hay un teléfono para llamar con un toque",
                detalle="En el celular, el cliente no puede llamar directo desde la web.",
                gravedad="baja",
                parametro=7,
            )
        )

    # --- Celular (parámetro 6) ---
    if mobile and mobile.zoomed_out_on_mobile:
        evidence = [f"En una pantalla de 390 px la página se dibuja con {mobile.layout_width} px de ancho."]
        if facts is not None and not facts.viewport:
            evidence.append("Falta la etiqueta <meta name='viewport'>.")
        findings.append(
            Finding(
                codigo="no_adaptada_celular",
                titulo="La web no está adaptada al celular: se ve en miniatura",
                detalle="El cliente tiene que hacer zoom para leer y tocar los botones.",
                evidencia=evidence,
                gravedad="alta",
                parametro=6,
                captura=mobile.screenshot,
            )
        )
    elif mobile and mobile.horizontal_overflow:
        findings.append(
            Finding(
                codigo="se_sale_celular",
                titulo="En el celular, la página se sale de la pantalla hacia el lado",
                detalle="Al deslizar, la página se mueve hacia los lados y se ve rota.",
                evidencia=[f"Ancho del contenido: {mobile.scroll_width} px en una pantalla de {mobile.layout_width} px."],
                gravedad="media",
                parametro=6,
                captura=mobile.screenshot_full,
            )
        )

    # --- Velocidad (parámetro 5) ---
    speed = speed_score(desktop.load_ms if desktop else None, collection.psi_mobile_score)
    if speed is not None and speed.puntaje <= 4:
        seconds = (desktop.load_ms or 0) / 1000 if desktop else None
        findings.append(
            Finding(
                codigo="lenta",
                titulo=f"La web tarda {seconds:.1f} s en cargar".replace(".", ",") if seconds else "La web es lenta",
                detalle="Más de 3 segundos hace que muchos visitantes se vayan antes de verla.",
                evidencia=[speed.justificacion] + ([f"{desktop.requests} archivos, {desktop.weight_kb} KB"] if desktop else []),
                gravedad="alta" if speed.puntaje <= 2 else "media",
                parametro=5,
            )
        )

    # --- Confianza (parámetro 8) ---
    if verification.final_url and verification.final_url.startswith("http://"):
        findings.append(
            Finding(
                codigo="sin_https",
                titulo="La web no tiene el candado de seguridad (HTTPS)",
                detalle="Los navegadores la marcan como 'No seguro', lo que espanta a los clientes.",
                evidencia=[verification.final_url],
                gravedad="media",
                parametro=8,
            )
        )
    kinds = {signal.kind for signal in (facts.trust_signals if facts else [])}
    if facts is not None and not kinds & {"testimonios", "reseñas"}:
        findings.append(
            Finding(
                codigo="sin_testimonios",
                titulo="No muestra opiniones ni reseñas de clientes",
                detalle="Nada en la web le confirma al visitante que otros clientes quedaron conformes.",
                gravedad="media",
                parametro=8,
            )
        )

    # --- Enlaces e imágenes (parámetros 9 y 3) ---
    if collection.broken_links:
        findings.append(
            Finding(
                codigo="enlaces_rotos",
                titulo=f"{len(collection.broken_links)} enlace(s) de la web llevan a páginas que no existen",
                detalle="El cliente hace clic y llega a un error.",
                evidencia=[f"{b.url} — {b.reason}" for b in collection.broken_links[:10]],
                gravedad="media",
                parametro=9,
            )
        )
        link_errors.extend(f"Enlace roto: {b.url}" for b in collection.broken_links)
    if collection.broken_images:
        findings.append(
            Finding(
                codigo="imagenes_rotas",
                titulo=f"{len(collection.broken_images)} imagen(es) de la web no cargan",
                detalle="Se ven cuadros vacíos o íconos de imagen rota.",
                evidencia=[f"{b.url} — {b.reason}" for b in collection.broken_images[:10]],
                gravedad="media",
                parametro=3,
            )
        )

    # --- Vigencia (parámetro 10) ---
    if facts is not None and facts.copyright_years:
        year = max(facts.copyright_years)
        if year <= today.year - 2:
            findings.append(
                Finding(
                    codigo="copyright_antiguo",
                    titulo=f"El pie de página muestra el año {year}: la web se ve desactualizada",
                    detalle="Da la impresión de que nadie la mantiene.",
                    evidencia=[f"Año del copyright: {year}"],
                    gravedad="media",
                    parametro=10,
                )
            )

    # --- Plantilla (parámetro 2) ---
    templates = [hint.name for hint in (facts.builders if facts else []) if hint.name in _TEMPLATE_BUILDERS]
    if templates:
        findings.append(
            Finding(
                codigo="plantilla",
                titulo=f"Hecha con un constructor de plantillas ({templates[0]})",
                detalle="Suele verse genérica y parecida a otras webs.",
                evidencia=[f"{hint.name}: {hint.evidence}" for hint in facts.builders if hint.name in templates],
                gravedad="baja",
                parametro=2,
            )
        )

    # --- SEO local y checklist (sección 8.3) ---
    seo = _seo_local(collection, business, text, city)
    missing_seo = [item.detalle for item in seo.values() if item.ok is False]
    covers_area = seo["cobertura"].ok
    if covers_area is False:
        findings.append(
            Finding(
                codigo="sin_cobertura",
                titulo="No dice en qué comunas o zonas atiende",
                detalle="Quien busca el servicio en su comuna no sabe si lo atienden.",
                gravedad="media",
            )
        )
    if len(missing_seo) >= 3:
        findings.append(
            Finding(
                codigo="seo_local_debil",
                titulo="A Google le falta información para mostrarla en búsquedas locales",
                detalle="Le faltan " + str(len(missing_seo)) + " elementos de SEO local.",
                evidencia=missing_seo,
                gravedad="media",
            )
        )

    return FindingsReport(
        hallazgos=_sorted(findings),
        seo_local=seo,
        checklist=_checklist(collection, text, seo, web_whatsapp=bool(web_whatsapp)),
        link_errors=link_errors,
    )


def _seo_local(collection: WebCollection, business: Business | None, text: str, city: str) -> dict[str, CheckItem]:
    facts = collection.facts
    if facts is None:
        return {}
    title = fold_accents(facts.title or "").lower()
    places = [fold_accents(c).lower() for c in facts.communes_mentioned]
    if city:
        places.append(city)
    title_has_place = any(place and place in title for place in places)
    name_words = [w for w in fold_accents(business.business_name).lower().split() if len(w) >= 4][:2] if business else []
    name_visible = bool(name_words) and all(word in text for word in name_words)
    covers_area = bool(facts.communes_mentioned) or (bool(city) and city in text)
    return {
        "titulo_con_ciudad": CheckItem(
            ok=title_has_place,
            detalle=f"El título de la página {'menciona' if title_has_place else 'no menciona'} la ciudad o comuna: “{facts.title or '(sin título)'}”",
        ),
        "h1_unico": CheckItem(
            ok=len(facts.h1) == 1,
            detalle=f"Tiene {len(facts.h1)} título(s) principal(es) (H1)" + (f": “{facts.h1[0]}”" if facts.h1 else ""),
        ),
        "meta_descripcion": CheckItem(
            ok=bool(facts.meta_description),
            detalle="Tiene descripción para Google" if facts.meta_description else "No tiene descripción para Google (meta description)",
        ),
        "cobertura": CheckItem(
            ok=covers_area,
            detalle=("Menciona: " + ", ".join(facts.communes_mentioned[:8])) if facts.communes_mentioned else (
                "Menciona su ciudad" if covers_area else "No menciona comunas ni zona de cobertura"
            ),
        ),
        "nombre_direccion_telefono": CheckItem(
            ok=name_visible and bool(facts.phones) and covers_area,
            detalle="Nombre, zona y teléfono visibles" if name_visible and facts.phones and covers_area else "Falta mostrar juntos el nombre, la dirección o zona y el teléfono",
        ),
        "datos_estructurados": CheckItem(
            ok=facts.local_business is not None,
            detalle="Tiene datos estructurados de negocio local (LocalBusiness)" if facts.local_business else "No tiene datos estructurados de negocio local (LocalBusiness)",
        ),
        "mapa": CheckItem(
            ok=facts.has_map_embed or bool(facts.maps_links),
            detalle="Tiene mapa o enlace a Google Maps" if facts.has_map_embed or facts.maps_links else "No tiene mapa ni enlace a su ficha de Google Maps",
        ),
    }


def _checklist(collection: WebCollection, text: str, seo: dict[str, CheckItem], *, web_whatsapp: bool) -> dict[str, CheckItem]:
    facts = collection.facts
    if facts is None:
        return {}
    kinds = {signal.kind for signal in facts.trust_signals}
    menu = fold_accents(" ".join([*facts.menu_items, *facts.h2])).lower()

    def item(ok: bool, yes: str, no: str) -> CheckItem:
        return CheckItem(ok=ok, detalle=yes if ok else no)

    return {
        "servicios": item(bool(_SERVICES.search(menu) or _SERVICES.search(text)), "Muestra sus servicios", "No hay una sección clara de servicios"),
        "cobertura": seo.get("cobertura") or CheckItem(ok=False, detalle="No menciona su zona"),
        "telefono": item(bool(facts.phones or facts.tel_links), "Muestra teléfono", "No muestra teléfono"),
        "whatsapp": item(web_whatsapp, "Tiene WhatsApp en la web", "No tiene WhatsApp en la web"),
        "horarios": item(bool(_HOURS.search(text)), "Indica horarios", "No indica horarios de atención"),
        "diferenciadores": CheckItem(ok=None, detalle="Lo evalúa la IA (qué lo hace distinto de la competencia)"),
        "testimonios": item("testimonios" in kinds, "Tiene testimonios", "No tiene testimonios"),
        "resenas": item("reseñas" in kinds, "Menciona reseñas o calificaciones", "No muestra reseñas"),
        "confianza": item(
            bool(kinds & {"certificaciones", "garantía", "experiencia"}),
            "Muestra señales de confianza (experiencia, certificaciones o garantía)",
            "No muestra experiencia, certificaciones ni garantías",
        ),
    }
