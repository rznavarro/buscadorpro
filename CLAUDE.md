# VORTEXIA PROSPECTOR — Metaprompt del proyecto

> Este archivo es la especificación maestra del proyecto. Claude Code lo lee al inicio de cada sesión.
> Guárdalo en la raíz de la carpeta del proyecto con el nombre `CLAUDE.md`.

---

## 0. Tu rol y cómo trabajamos

Eres el **arquitecto y desarrollador principal** de Vortexia Prospector. Joaquín (fundador de Vortexia) no va a programar a mano: tú construyes, él prueba y aprueba.

Reglas de trabajo:

1. **Trabaja por fases** (sección 12). No empieces una fase sin que la anterior esté aprobada por Joaquín.
2. Al terminar cada fase, entrega siempre:
   - Qué construiste (en español simple, sin jerga innecesaria).
   - El **comando exacto** para probarlo, listo para copiar y pegar.
   - Qué debería ver Joaquín si funciona.
   - Qué queda pendiente o qué riesgo detectaste.
   Luego **detente y espera su OK**.
3. La primera vez, pregunta si usa Windows (PowerShell), macOS o Linux, y da los comandos para ese sistema.
4. **Antes de escribir código que use Browser Use, lee su documentación actual** (docs.browser-use.com y el repositorio oficial) y fija la versión exacta en `pyproject.toml`. Su API cambia entre versiones: no asumas nombres de clases o parámetros de memoria.
5. Escribe tests para toda la lógica que no depende del navegador. Una fase no está terminada si los tests no pasan.
6. Si algo de este documento es ambiguo o contradictorio, pregunta antes de decidir. Si tomas una decisión de arquitectura, anótala en la sección "Estado del proyecto" al final de este archivo.
7. Mantén actualizada la sección **Estado del proyecto** al cerrar cada fase.

---

## 1. Qué estamos construyendo

No es otro scraper genérico. Es un **agente de investigación comercial para Vortexia** que responde a:

> "Encuentra negocios de este tipo en esta zona y dime cuáles tienen una presencia web deficiente y cómo puedo mejorarla."

Entrada: una búsqueda como `cerrajeros en Rancagua` o `gasfíters en Rancagua`.

Salida: lista de prospectos con datos de contacto, WhatsApp verificado, análisis de su web, score y oportunidades concretas para Vortexia.

Flujo de la V1:

```
BUSCAR → EXTRAER → VERIFICAR → ANALIZAR → GUARDAR
```

Vortexia vende creación y rediseño de sitios web. Las "oportunidades" que detecte el sistema deben ser cosas que un rediseño web resuelve.

---

## 2. Reglas no negociables

1. **Precisión > cantidad.** Nunca inventar WhatsApp, teléfonos, webs, servicios, datos de contacto ni problemas. Si algo no se puede confirmar, el campo queda vacío y su estado dice `NO CONFIRMADO`.
2. **Todo dato guarda su fuente y su evidencia.** Ejemplo: WhatsApp encontrado en `web` → evidencia: el `href` exacto donde apareció.
3. **La IA solo razona; el código extrae.** Teléfonos, URLs, enlaces de WhatsApp, redes sociales, title, H1, etc. se extraen con parsers y regex, nunca con un LLM.
4. **Cada problema que detecte la IA debe venir con evidencia observable** (un texto, un elemento, una captura). Problema sin evidencia = se descarta.
5. **La V1 no contacta a nadie.** Nada de enviar mensajes, abrir chats automáticamente ni iniciar sesión en WhatsApp.
6. **Ritmo humano y sin evasión.** Delays aleatorios, una sola sesión de navegador para Maps, límite de resultados configurable. Si Google muestra captcha o "tráfico inusual", **detén la búsqueda, márcala como `BLOQUEADO` y avisa en la interfaz. Nunca intentes resolver o saltarte un captcha.**
7. **Modular.** Cada pieza (fuente de Maps, navegador, modelo de IA, base de datos, interfaz) vive detrás de una interfaz para poder cambiarla después.
8. **Secretos en `.env`**, nunca en el código ni en git. Entrega un `.env.example`.

Nota de diseño: el scraping automatizado de Google Maps va contra los términos de uso de Google y puede provocar bloqueos. Por eso la fuente de Maps es una interfaz (`MapsSource`): la implementación por defecto usa Browser Use con volúmenes bajos de uso interno, y debe ser fácil añadir después una implementación con la API oficial de Google Places sin tocar el resto del sistema.

---

## 3. Stack

- **Python 3.12** con **uv** para entorno y dependencias.
- **browser-use** (versión fijada) como motor de navegación.
- **Playwright** (vía Browser Use o directo, por CDP) para leer el DOM, capturas y mediciones.
- **httpx** para descargar webs simples, **selectolax** (o BeautifulSoup + lxml) para parsear HTML.
- **phonenumbers** para normalizar teléfonos.
- **pydantic v2** para modelos y salidas estructuradas.
- **SQLite** con **SQLModel** (o sqlite3 con una capa de repositorio propia).
- **FastAPI + Jinja2 + HTMX** para la interfaz (sin build de JavaScript). Si algún día hace falta tooling de JS, usar **pnpm**, nunca npm.
- **SDK de Anthropic** para el análisis; el modelo se configura en `.env` y queda detrás de una interfaz `LLMClient`.
- **pytest** para tests.
- Opcional: **PageSpeed Insights API** (clave en `.env`) para medir rendimiento móvil.

Nada de Celery, Redis, Docker ni servicios externos en la V1. Todo corre local con un solo comando.

---

## 4. Arquitectura

```
USUARIO → Interfaz (FastAPI) → Pipeline
                                  │
              ┌───────────────────┼────────────────────┐
              ↓                   ↓                    ↓
        MapsSource          WebCollector            Extractores
     (Browser Use navega,  (httpx / Playwright:    (Python puro:
      Python lee el DOM)    HTML, capturas,         teléfonos, WhatsApp,
              │             tiempos)                redes, SEO, hechos)
              └───────────────────┬────────────────────┘
                                  ↓
                     Analizador IA (rúbrica fija)
                                  ↓
                       Scorer + Informe (Python)
                                  ↓
                              SQLite
                                  ↓
                              Interfaz
```

División de responsabilidades:

| Capa | Hace | No hace |
|---|---|---|
| Browser Use | Abrir navegador, buscar en Maps, hacer scroll en resultados, abrir fichas, expandir horarios, hacer clic, navegar webs dinámicas | Guardar datos, lógica de negocio, extraer lo que un parser puede leer |
| Python | Leer el DOM, parsear HTML, normalizar teléfonos, detectar `wa.me` y redes, validar URLs, base de datos, filtros, orden, exportar, interfaz | Juzgar diseño o calidad |
| IA | Evaluar diseño, conversión, confianza; detectar problemas, oportunidades y puntos fuertes con evidencia | Extraer teléfonos, URLs o cualquier dato que el código pueda leer |

**Principio clave para Maps y webs:** Browser Use lleva el navegador hasta la página correcta; luego Python extrae del DOM **todos los `href` y textos visibles en bruto** y los clasifica. Usa el agente LLM de Browser Use solo cuando la interacción no es predecible (por ejemplo, un panel que cambia de estructura).

Estructura de carpetas sugerida:

```
vortexia-prospector/
  app/
    config.py
    models.py               # pydantic: SearchRun, Business, WebsiteAnalysis…
    db/                     # esquema, migraciones simples, repositorio
    sources/
      base.py               # interfaz MapsSource
      browser_use_maps.py   # implementación por defecto
    browser/
      engine.py             # envoltorio de Browser Use + Playwright
    extract/
      phones.py
      whatsapp.py
      socials.py
      links.py              # desenvolver /url?q=, linktree, acortadores
      html_facts.py         # title, H1, meta, viewport, formularios, etc.
    analyze/
      collector.py          # captura desktop/móvil + hechos + texto visible
      rubric.yaml           # rúbrica fija (ver sección 8)
      llm.py                # interfaz LLMClient + implementación Anthropic
      scorer.py
      report.py
    pipeline.py
    cli.py
    web/                    # rutas FastAPI + plantillas Jinja/HTMX
  data/                     # sqlite + capturas (en .gitignore)
  tests/
    fixtures/               # HTML de ejemplo para tests
  .env.example
  pyproject.toml
  README.md
```

---

## 5. Pipeline paso a paso

1. Joaquín escribe la búsqueda y un límite (por defecto 20). Se crea un `SearchRun` con estado `EN_CURSO`.
2. `MapsSource.search(query, limit)` abre Maps (idioma español, región configurable), hace scroll en la lista hasta llegar al límite o al final, y devuelve las URLs de cada ficha con su posición.
3. Para cada ficha, `MapsSource.get_details(url)` abre el panel, expande horarios si hace falta y devuelve datos en bruto: textos visibles + **todos los enlaces del panel**. Python normaliza y clasifica.
4. **Deduplicar** por `place_id` o URL canónica de Maps. Si el negocio ya existe y su web se analizó hace menos de N días (configurable, por defecto 30), no se re-analiza; solo se vincula a esta búsqueda.
5. Clasificar el "sitio web" que entrega Maps:
   - Desenvolver redirecciones de Google (`/url?q=`).
   - Si es Instagram o Facebook → no tiene web propia; guardar como red social.
   - Si es `wa.me` u otro enlace de WhatsApp → es su WhatsApp, no su web.
   - Si es Linktree o similar → abrirlo y extraer sus enlaces (ahí suele estar el WhatsApp).
6. Si hay web propia: descargarla con httpx; si el HTML viene casi vacío (sitio armado con JavaScript), renderizarla con Playwright. Visitar como máximo 2–3 páginas internas relevantes (contacto, servicios, nosotros) para buscar WhatsApp, comunas y servicios.
7. Tomar capturas: escritorio (1440 px) y móvil (390 px), primera pantalla y página completa con altura limitada.
8. Armar el paquete de evidencia → el LLM evalúa la rúbrica → el scorer suma → se genera el informe.
9. Guardar todo y actualizar los contadores del `SearchRun` (la interfaz los consulta en vivo).

Cada negocio se procesa de forma independiente: si uno falla, se guarda el error en su registro y la búsqueda continúa. Las webs se pueden analizar en paralelo con concurrencia limitada (por defecto 3); Maps siempre en secuencia.

---

## 6. Base de datos (SQLite)

**searches**
`id, query, limit, status (EN_CURSO / TERMINADA / FALLIDA / DETENIDA / BLOQUEADO), phase, businesses_found, websites_found, whatsapp_verified, opportunities_found, error, created_at, finished_at`

**businesses**
`id, place_id (único, puede ser nulo), google_maps_url (único), business_name, category, phone_raw, phone_e164, whatsapp_url, whatsapp_status, whatsapp_source, whatsapp_evidence, whatsapp_candidates (JSON), whatsapp_multiple_numbers (bool), website, website_status (OK / CAIDO / SIN_WEB / SOLO_REDES / BLOQUEADO), instagram, facebook, address, city, commune (comuna / barrio / partido según el país), opening_hours (JSON por día), rating, review_count, services (JSON), description, field_sources (JSON: de dónde salió cada dato), first_seen_at, updated_at`

**search_results**
`search_id, business_id, position` (posición en los resultados de Maps)

**website_analyses** (un negocio puede tener varios análisis en el tiempo)
`id, business_id, url_analyzed, final_url, analyzed_at, rubric_version, model_name, website_score, rubric_scores (JSON: los 10 parámetros con puntaje, justificación y evidencia), facts (JSON: hechos deterministas), seo_local (JSON), info_checklist (JSON), main_problems (JSON), opportunities (JSON), strengths (JSON), opportunity_level (ALTA / MEDIA / BAJA), screenshots (rutas), load_time_ms, psi_mobile_score, raw_llm_output (JSON), error`

Todos los campos de la lista original de Joaquín deben existir y poder filtrarse: nombre, categoría, teléfono, URL de WhatsApp, estado de WhatsApp, web, URL de Maps, Instagram, Facebook, dirección, ciudad, comuna, horarios, rating, reseñas, servicios, descripción, fecha de extracción, score, análisis, problemas y oportunidades.

**Servicios y descripción:** solo lo que aparece literalmente en Maps o en la web. La IA puede agrupar o resumir, nunca agregar servicios que no estén escritos.

---

## 7. WhatsApp (prioridad máxima)

Objetivo: que Joaquín pueda hacer clic en **ABRIR WHATSAPP** y se abra directamente el contacto correcto.

### Estados

- `VERIFICADO` → se encontró un enlace explícito de WhatsApp del negocio (en Maps, en su web oficial, o en un Linktree/red enlazada desde su web o su ficha).
- `NO CONFIRMADO` → solo hay un teléfono, aunque sea móvil. **No se construye un `wa.me` a partir de un teléfono.**
- `NO ENCONTRADO` → no hay enlace ni teléfono.

**Cambio decidido por Joaquín (2026-10-04): "Probar WhatsApp" y confirmación manual.**
- Si un negocio no publica WhatsApp pero tiene un **celular**, la interfaz muestra un botón **Probar WhatsApp**. El botón abre `wa.me/<celular>` para que Joaquín lo pruebe a mano.
- Ese enlace de prueba **no se guarda** como `whatsapp_url` y el negocio sigue como `NO CONFIRMADO`.
- Si Joaquín marca **Existe**, pasa a `VERIFICADO`, con la fuente "confirmado por Joaquín" y la fecha.
- Si marca **No existe**, el lead se **descarta**: desaparece de su lista y no vuelve a aparecer en búsquedas futuras.
- Esta misma confirmación manual sirve también para los WhatsApp que el negocio sí publicó.

### Orden de búsqueda

1. Enlaces del panel de Google Maps (si no aparece en la vista de escritorio, reintentar el panel con emulación móvil; configurable).
2. Web oficial: home + páginas internas de contacto.
3. Enlaces de Linktree o similares que estén enlazados desde la ficha o la web.
4. Otras fuentes públicas enlazadas directamente por el propio negocio.

### Qué detectar (regex sobre el HTML completo, incluidos atributos y scripts)

- `wa.me/<número>`
- `api.whatsapp.com/send?phone=<número>`
- `web.whatsapp.com/send?phone=<número>`
- `whatsapp://send?phone=<número>`
- Acortadores como `wa.link/...` → seguir la redirección con httpx; solo es `VERIFICADO` si termina en un enlace de WhatsApp válido.
- Botones flotantes y plugins de WhatsApp: el número suele estar en `onclick`, en atributos `data-*` o en un JSON de configuración dentro de un `<script>`.
- **Ignorar** `chat.whatsapp.com/...` (son invitaciones a grupos, no el contacto del negocio).

### Normalización

- Usar `phonenumbers` con región por defecto configurable (`CL` por defecto; también debe funcionar con `AR`).
- Guardar siempre el formato canónico: `https://wa.me/<código país + número, solo dígitos>`. Ejemplo Chile: `https://wa.me/569XXXXXXXX`.
- Argentina: los móviles en WhatsApp van como `549` + código de área **sin el 15**. Cubrir con tests.
- Guardar el enlace original tal cual en `whatsapp_evidence`.
- Si aparecen varios números distintos: guardarlos todos en `whatsapp_candidates`, elegir el principal por prioridad (Maps > botón flotante o header de la web > footer) y marcar `whatsapp_multiple_numbers = true` para revisión manual.

---

## 8. Análisis de la web

El análisis tiene dos capas: **hechos** (código) y **juicio** (IA). El score usa la rúbrica fija de Vortexia.

### 8.1 Hechos deterministas (Python + Playwright)

- Estado HTTP, URL final, HTTPS, redirecciones.
- Tiempo de carga (DOMContentLoaded y load), número de requests, peso aproximado. PageSpeed móvil si hay clave.
- `title`, meta description, H1 (cantidad y texto), H2, `lang`, meta viewport, favicon, canonical.
- Etiquetas Open Graph (cómo se ve el enlace al compartirlo por WhatsApp).
- Datos estructurados `LocalBusiness` (JSON-LD).
- Teléfono visible, enlaces `tel:`, enlaces de WhatsApp, botón flotante, formularios, `mailto:`.
- Redes sociales enlazadas.
- Menciones de ciudad y comunas (lista configurable en `data/comunas.json`; empezar con Región Metropolitana y O'Higgins).
- Año del copyright y otras señales de antigüedad.
- Enlaces internos rotos (revisar hasta N enlaces) e imágenes rotas.
- Móvil: scroll horizontal no deseado, viewport, si hay CTA o WhatsApp visible en la primera pantalla.
- Constructor o plantilla detectable (meta generator, rutas de assets): Wix, WordPress + tema, Elementor, Shopify, PrestaShop, Tokko, BuscadorProp, Houzez, Lovable, etc.
- Señales de confianza en el texto: testimonios, reseñas, años de experiencia, certificaciones (por ejemplo SEC), garantías.

### 8.2 Rúbrica fija de Vortexia (score sobre 100)

Esta rúbrica es de Joaquín y es **fija**. Guárdala en `app/analyze/rubric.yaml` con un número de versión. **No la modifiques, no cambies sus parámetros ni sus pesos sin autorización explícita de Joaquín.**

10 parámetros, cada uno de 1 a 10. Score total = suma (10–100).

| # | Parámetro | Quién lo evalúa |
|---|---|---|
| 1 | Primera impresión / Hero | IA (captura de primera pantalla, escritorio y móvil) |
| 2 | Identidad visual propia (vs. plantilla genérica) | IA + detección de plantilla |
| 3 | Evidencia / portafolio de trabajo real | IA + hechos (galerías, fotos reales vs. stock) |
| 4 | Navegación y claridad de menú | IA + hechos (ítems de menú, menú móvil) |
| 5 | Velocidad de carga | **Código** (tabla fija, ver abajo) |
| 6 | Responsive / mobile | Hechos + IA (captura móvil) |
| 7 | Llamado a la acción (CTA) | Hechos + IA (CTA visible, WhatsApp visible, CTA repetido) |
| 8 | Prueba social / confianza | Hechos + IA |
| 9 | Coherencia y funcionalidad de enlaces | **Código** (tabla fija, ver abajo) |
| 10 | Actualización / vigencia del contenido | Hechos + IA |

Para los parámetros que evalúa la IA, define en `rubric.yaml` descriptores anclados por tramo (1–3, 4–6, 7–8, 9–10) para que el puntaje sea consistente. Ejemplo para el parámetro 1:

- 1–3: en 5 segundos no se entiende qué hace el negocio ni dónde atiende.
- 4–6: se entiende, pero se ve genérico o desordenado.
- 7–8: claro y atractivo.
- 9–10: claro, atractivo, propuesta de valor evidente y CTA inmediato.

Tablas fijas por código (configurables en `rubric.yaml`):

- **Velocidad:** con PageSpeed móvil → `round(psi / 10)`, mínimo 1. Sin PageSpeed, por tiempo de carga medido: < 1,5 s = 10 · < 2,5 s = 8 · < 4 s = 6 · < 6 s = 4 · < 9 s = 2 · resto = 1. Marcar la medición local como referencial.
- **Enlaces:** 0 errores = 10 · 1 = 8 · 2–3 = 6 · 4–6 = 4 · 7 o más = 2.

Consistencia:

- Temperatura 0, salida con esquema JSON estricto, prompt versionado.
- Los parámetros calculados por código nunca los sobrescribe la IA.
- Guardar `rubric_version` y `model_name` en cada análisis.
- Criterio de aceptación: analizar la misma web dos veces no puede variar más de 5 puntos.

### 8.3 Secciones que no suman al score

**SEO local** (sí / no + detalle): title con servicio + ciudad, H1 único y descriptivo, meta description, comunas o zona de cobertura mencionadas, nombre + dirección + teléfono visibles, datos estructurados `LocalBusiness`, mapa embebido o enlace a la ficha de Google.

**Checklist de información** (sí / no): servicios, comunas/cobertura, teléfono, WhatsApp, horarios, diferenciadores, testimonios, reseñas, elementos de confianza.

### 8.4 Qué entrega la IA

Recibe: capturas (escritorio y móvil), hechos en JSON, texto visible recortado y la rúbrica. Devuelve JSON con:

- Puntaje + justificación + evidencia para cada parámetro que le toca.
- `main_problems`: 3 a 5, ordenados por impacto. Cada uno con título, impacto en el negocio, parámetro de la rúbrica relacionado y evidencia.
- `opportunities`: 3 a 6, cada una algo que un rediseño de Vortexia resuelve (rediseño visual, CTA de WhatsApp, SEO local, testimonios, mejor presentación de servicios, galería de trabajos reales…).
- `strengths`: 1 a 3 puntos positivos reales del sitio (servirán para los mensajes personalizados de la V2).

**Lenguaje:** los problemas y oportunidades se escriben en términos prácticos de negocio (qué siente o qué no encuentra el cliente que entra a la web), nada de código, líneas ni fragmentos. La evidencia técnica va en un campo aparte que la interfaz muestra plegado.

### 8.5 Nivel de oportunidad (código, umbrales configurables)

- **ALTA:** score < 50 y rating ≥ 4,3 y ≥ 20 reseñas; o negocio sin web propia con buen rating y reseñas.
- **MEDIA:** score 50–69.
- **BAJA:** score ≥ 70.

---

## 9. Detalles de Google Maps

- Abrir Maps en español y con región configurable (Chile por defecto).
- Límite de resultados por búsqueda: por defecto 20, máximo configurable.
- Delays aleatorios entre acciones (por defecto 2–6 s). Una sola sesión de navegador para Maps.
- Extraer de cada ficha: nombre, categoría, teléfono, web, dirección, horarios, rating, número de reseñas, descripción/"acerca de", servicios si aparecen, todos los enlaces del panel.
- Ciudad y comuna: parsear desde la dirección con Python; si no se puede, dejar vacío.
- Detección de bloqueo (captcha, "tráfico inusual", página vacía repetida): detener, marcar `BLOQUEADO`, avisar. Nunca intentar resolverlo.

---

## 10. Interfaz

Simple, oscura, legible. Corre en `localhost`, se abre con un solo comando.

**Inicio (`/`)**
- Título VORTEXIA PROSPECTOR.
- Campo de búsqueda + límite + botón **ANALIZAR**.
- Lista de búsquedas anteriores.

**Búsqueda (`/search/{id}`)**
- Progreso en vivo (HTMX, cada 2 s): fase actual y botón **Detener**.
- Contadores: negocios encontrados · webs encontradas · WhatsApp verificados · oportunidades detectadas.
- Tabla ordenable: Negocio | Web | WhatsApp | Rating | Reseñas | Score | Oportunidad. Score con color (rojo < 50, ámbar 50–69, verde ≥ 70).
- Filtros rápidos: con WhatsApp · con web · sin web · score < 50 · rating > 4,5 · más de 100 reseñas · WhatsApp + web mala · grandes oportunidades.
- Botón **Exportar CSV** (respeta los filtros activos).

**Informe del negocio (`/business/{id}`)**

```
CERRAJERÍA XYZ
Web: xyz.cl                          [Abrir web]
WhatsApp: https://wa.me/569XXXXXXXX  [ABRIR WHATSAPP]   (solo si es VERIFICADO)
Google Maps: [Abrir en Maps]
Rating: 4.7 · Reseñas: 183
WEB SCORE: 42/100

PRINCIPALES PROBLEMAS
1. …
OPORTUNIDADES
1. …
PUNTOS FUERTES
1. …
```

Además: capturas escritorio/móvil, desglose de los 10 parámetros, SEO local, checklist de información, fuente de cada dato, evidencia técnica plegada y botón **Re-analizar web**.

Si el WhatsApp está `NO CONFIRMADO`, mostrar el teléfono con un botón para copiarlo, sin botón de WhatsApp.

---

## 11. Línea de comandos (para probar cada fase)

```
uv run python -m app.cli maps "cerrajeros en Rancagua" --limit 5
uv run python -m app.cli facts https://ejemplo.cl
uv run python -m app.cli analyze https://ejemplo.cl
uv run python -m app.cli run "cerrajeros en Rancagua" --limit 10
uv run python -m app.cli serve
```

---

## 12. Fases de construcción

> **Cambio de plan decidido por Joaquín (2026-10-04).** Lo que él quiere usar todos los días es: **20 leads locales nuevos de Google Maps por día**, ordenados en una página de su computador, con un botón para descartar el lead si el WhatsApp no existe.
>
> **Qué cambia:**
> - **Búsquedas libres:** cualquier rubro y cualquier ciudad o país (ej. "peluquerías en Miami"). No hay una lista fija de rubros.
> - **Lead:** negocio con **web propia que abre** y con WhatsApp VERIFICADO, o con un celular para "Probar WhatsApp" (sección 7). Sin web, solo redes o con la web caída = descartado (decidido por Joaquín el 2026-10-04: "solo nos interesan los negocios con página web").
> - **Cuándo se buscan:** con un botón, cuando Joaquín quiera (no automático).
>
> **Orden de fases desde la 4:**
> - **Fase 4 — Leads diarios (nueva):** página local con buscador → 20 leads nuevos que no se repitan con días anteriores → verificación de web y WhatsApp (Fase 3) → lista del día ordenada → botones Abrir/Probar WhatsApp, Existe, No existe (descartar) → exportar CSV. Toma partes de las antiguas Fases 5 y 6.
> - **Fase 5 — Análisis IA + score** (antes Fase 4): se suma a la lista de leads.
> - **Fase 6 — Pipeline e interfaz completos** (lo que quede de las antiguas 5 y 6: informe por negocio, filtros, caché de análisis, re-analizar).
> - **Fase 7 — Endurecimiento** (sin cambios).
>
> Las descripciones de abajo son las originales; donde no coincidan, manda este cambio de plan.

**Fase 0 — Preparación**
Estructura del proyecto, `uv`, `pyproject.toml`, `.env.example`, `.gitignore` (`data/`, `.env`), `git init`. Revisar la documentación actual de Browser Use y fijar versión. Prueba mínima: abrir el navegador en una página y cerrarlo.
✅ Aceptación: `uv run pytest` corre y la prueba mínima abre el navegador.

**Fase 1 — Núcleo sin navegador**
Modelos, esquema SQLite, repositorio, `phones.py`, `whatsapp.py`, `socials.py`, `links.py`, `html_facts.py`. Fixtures HTML con: `wa.me`, `api.whatsapp.com`, `whatsapp://`, `wa.link`, número dentro de un JSON de plugin, `chat.whatsapp.com` (debe ignorarse), números chilenos y argentinos, `/url?q=` de Google, página tipo Linktree.
✅ Aceptación: tests en verde, al menos 20 casos de WhatsApp cubiertos.

**Fase 2 — Google Maps**
`MapsSource` + implementación con Browser Use. Comando `maps` que imprime JSON.
✅ Aceptación: 5 negocios reales con datos que Joaquín revisa a mano contra Maps; deduplicación funcionando; bloqueo detectado y reportado.

**Fase 3 — Recolector web**
Descarga/render, hechos, capturas, WhatsApp desde la web, enlaces rotos, tiempos de carga. Comando `facts`.
✅ Aceptación: funciona sobre 3 webs reales (una mala, una buena, una hecha con plantilla).

**Fase 4 — Análisis IA + score**
`rubric.yaml`, prompt versionado, esquema JSON, scorer, informe. Comando `analyze`.
✅ Aceptación: misma web dos veces → diferencia ≤ 5 puntos; cada problema con evidencia; nada inventado.

**Fase 5 — Pipeline completo**
Orquestación de punta a punta, errores aislados por negocio, caché de análisis (30 días), contadores del `SearchRun`. Comando `run`.
✅ Aceptación: `run "cerrajeros en Rancagua" --limit 10` termina y deja todo en SQLite.

**Fase 6 — Interfaz**
Las tres pantallas de la sección 10, filtros, exportar CSV, botón Detener, re-analizar.
✅ Aceptación: Joaquín hace una búsqueda completa solo desde el navegador.

**Fase 7 — Endurecimiento**
Logs legibles, reintentos con límite, timeouts, manejo de webs caídas, `README.md` de uso escrito para Joaquín (cómo instalar, configurar `.env`, ejecutar y leer los informes).
✅ Aceptación: revisión manual de 10 negocios sin ningún dato inventado.

---

## 13. Fuera de alcance en la V1 (no construir)

Envío de mensajes, generación de DMs, CRM, seguimientos, métricas, login, multiusuario, deploy en la nube, sincronización con Airtable, funciones "por si acaso".

Hoja de ruta para después (solo para que la arquitectura no lo bloquee):

- **V2:** generar un DM personalizado usando datos reales del informe (`strengths`, `main_problems`, nombre del negocio). Nada de mensajes genéricos.
- **V3:** abrir WhatsApp con el mensaje prellenado para que Joaquín lo revise y lo envíe manualmente.
- **V4:** CRM, seguimientos, métricas. Posible sincronización con su base de Airtable de prospección (tabla Leads).

---

## 14. Definición de "V1 terminada"

Joaquín escribe `gasfíters en Rancagua`, pulsa ANALIZAR y obtiene, sin tocar código:

- La lista de negocios relevantes con sus datos.
- WhatsApp `VERIFICADO` solo cuando hay evidencia, con botón para abrirlo.
- Análisis de cada web con score según la rúbrica de Vortexia.
- Problemas, oportunidades y puntos fuertes por negocio.
- Filtros y exportación CSV.
- Cero datos inventados.

---

## Estado del proyecto

_(Claude Code actualiza esta sección al cerrar cada fase.)_

- **Fase actual:** **V1 terminada.** Fases 0–7 aprobadas por Joaquín (la 7, el 2026-10-04). Lo siguiente, solo si Joaquín lo pide, es la hoja de ruta de la sección 13 (V2: mensajes personalizados), activar la IA cuando decida invertir, o ajustes según el uso diario.
- **Versión de Browser Use fijada:** `browser-use==0.13.10` (revisada el 2026-10-04). Python 3.12 vía uv 0.12.23. Chromium de Playwright revisión 1243 (Chrome 153).
- **Sistema operativo de Joaquín:** Windows 10 Pro, PowerShell. El proyecto vive en `C:\Users\RzNavarro\proyectos\buscadorpro` (fuera de OneDrive, para evitar bloqueos de archivos al sincronizar `.venv`, SQLite y capturas).
- **Decisiones tomadas:**
  - Browser Use 0.13 ya no usa Playwright por dentro: controla Chromium por CDP. `app/browser/engine.py` (`BrowserEngine`) arranca el `Browser` de Browser Use y conecta Playwright al mismo Chromium con `connect_over_cdp`. El resto del sistema solo usa `BrowserEngine`.
  - API confirmada leyendo el código instalado: `Browser` es un alias de `BrowserSession`; se usan `start()`, `kill()` (cierra Chromium aunque haya `keep_alive`) y la propiedad `cdp_url`. `stop()` no mata el proceso.
  - Por defecto se usa el Chromium que trae Playwright (`playwright.chromium.executable_path`). Browser Use 0.13.10 busca `chrome-win` y no reconoce `chrome-win64`, así que caería en el Chrome del sistema. `BROWSER_EXECUTABLE_PATH` en `.env` permite cambiarlo.
  - Se excluye el argumento por defecto `--extensions-on-chrome-urls`: con Chromium 153+ impide que se abra el puerto CDP y el arranque se cuelga 30 s (encontrado por bisección).
  - Desactivados en Browser Use: telemetría (`ANONYMIZED_TELEMETRY=false`), extensiones automáticas (uBlock, cookies, ClearURLs: alterarían cómo se ve cada web) y `captcha_solver` (regla 6). También su configuración propia de logs: solo se ven advertencias y errores.
  - `BrowserEngine` borra al cerrar las carpetas temporales (perfil y descargas) que Browser Use deja en `%TEMP%` en cada ejecución.
  - Todas las dependencias del stack se instalaron en la Fase 0 para detectar choques temprano. No hubo ninguno; `uv.lock` fija las versiones.
  - Tests que abren un navegador real llevan el marcador `browser` y no corren con `uv run pytest` a secas: se corren con `uv run pytest -m browser`.
  - CLI con `argparse`. Comando de prueba de la Fase 0: `browser-check`.
  - Referencia de Browser Use: Joaquín descargó el repo oficial (rama `main`) en `OneDrive\Escritorio\buscadorpro\browser-use-main`. Es 0.13.10 con 34 archivos de cambios menores sin publicar, que no tocan lo que usamos. Se usa solo como documentación (`skills/open-source`, `examples`); la dependencia sigue siendo la versión publicada en PyPI.
  - **Fase 1 — datos:** las tablas SQLModel viven en `app/models.py`. `app/db/schema.py` crea las tablas y lleva una tabla `schema_version` con un diccionario `MIGRATIONS` para cambios futuros. `app/db/repository.py` es la única puerta a SQLite.
  - **Deduplicación:** la columna `place_id` guarda nuestra clave del lugar (`place_id:ChIJ…`, `ftid:0x…:0x…` o `cid:…`), sacada de la URL de Maps. `google_maps_url` se guarda canónica, sin coordenadas de la vista ni parámetros de sesión. `upsert_business` solo sobrescribe los campos que se pasaron explícitamente (`exclude_unset`), así una actualización parcial no borra datos.
  - **WhatsApp — reglas decididas:**
    - Enlace sin código de país (`wa.me/987654321`): se completa con la región por defecto y se agrega una nota en `note`, porque tal como está en la web ese botón no abre el chat correcto. Esto es útil como argumento de venta.
    - Argentina: `phonenumbers` convierte el "15" a 549 + área, y también se deja nota. Un fijo argentino sin 9 se mantiene tal cual (WhatsApp Business puede usar fijos).
    - `wa.me/message/…` cuenta como VERIFICADO (abre el chat del negocio), pero sin número. Siempre queda por debajo de cualquier enlace con número.
    - Un número de plugin solo cuenta si la palabra "whatsapp" o el nombre del plugin aparece a menos de 300 caracteres. El `telephone` de los datos estructurados (JSON-LD) nunca cuenta. Los enlaces dentro de comentarios HTML no cuentan.
    - Ubicación: flotante > footer > header/menú > cuerpo (el footer gana al header). Prioridad para elegir el principal: Maps > web (flotante o header > cuerpo > footer) > Linktree > otras.
    - Acortadores (wa.link, walink.co): se siguen solo las redirecciones (`Location` o `meta refresh`) hasta encontrar un enlace de WhatsApp con número. Nunca se visita WhatsApp. Si no llegan a WhatsApp, quedan en `ignored`.
    - `floating_button` registra que existe un botón flotante aunque su número ya aparezca en otro lado.
  - **Comunas:** `data/comunas.json` (52 de la RM y 33 de O'Higgins) se versiona como excepción en `.gitignore`. Las comunas que también son palabras comunes (Colina, Navidad, Independencia…) solo cuentan escritas con mayúscula inicial y con contexto en la misma línea ("comuna", "sector", "cobertura", "atendemos", u otra comuna no ambigua). Las demás se buscan sin distinguir mayúsculas ni tildes.
  - `html_facts.py` solo extrae lo que está en el HTML. Tiempos, capturas, enlaces e imágenes rotas, scroll horizontal y PageSpeed se miden en la Fase 3.
  - **Fase 2 — Maps: quién hace qué.** Browser Use abre y mantiene la sesión de Chromium. Las acciones predecibles (abrir la búsqueda, scroll con la rueda del mouse, abrir cada ficha, clic en la pestaña "Información") se hacen con Playwright sobre ese mismo Chromium. Motivo: la API directa de Browser Use ("Actor") está marcada como *legacy* en su documentación (`skills/open-source/references/actor.md`), y su ejemplo oficial `examples/browser/playwright_integration.py` usa Playwright para acciones precisas. El agente con IA de Browser Use no se usa en esta fase: no hizo falta.
  - **Lectura de Maps** (`app/sources/maps_parser.py`, Python puro y probado con HTML real guardado en `tests/fixtures/maps/`): solo atributos estables (`data-item-id`, `aria-label`, `role`), nunca las clases internas de Google.
    - Rating, reseñas y categoría se leen solo del encabezado.
    - Los enlaces y el WhatsApp de Maps se leen solo de las regiones "Información de …" y "Acciones para …". Las reseñas y "Otras personas también buscan" quedan fuera: traen datos de terceros.
    - El horario sale de las etiquetas "lunes, …, Copiar el horario", sin tener que hacer clic.
  - **Clave de deduplicación:** las URLs de la lista traen el `place_id` oficial (`!19sChIJ…`), que tiene prioridad sobre el ftid.
  - **Ciudad y comuna:** se sacan de la dirección. Se descartan la calle, el código postal, la región y las partes con números; la comuna se busca en `data/comunas.json`.
  - **"Sitio web" de Maps** (`MapsPlace.to_business`):
    - web propia → `website` con estado vacío (se verifica en la Fase 3);
    - red social → se guarda en `instagram`/`facebook` y el estado queda SOLO_REDES;
    - Linktree → se guarda en `website` con SOLO_REDES, para abrirlo después;
    - wa.me → WhatsApp VERIFICADO con fuente maps y SIN_WEB;
    - sin enlace → SIN_WEB.
    - Lo que Maps mostraba tal cual queda en `field_sources["website_maps"]`.
  - **Actualizaciones:** `to_business` solo pasa los campos con valor. El estado de WhatsApp solo se pasa si no es NO ENCONTRADO, así una ficha que esta vez no mostró el teléfono no borra lo que ya estaba guardado.
  - **Bloqueos:** página `/sorry/` de Google, o los textos "tráfico inusual", "unusual traffic", "no soy un robot" o "captcha". El texto se revisa solo cuando no apareció el contenido esperado, para que un nombre de negocio o una reseña no se confundan con un bloqueo. Dos fichas vacías seguidas también cuentan como bloqueo. En todos los casos se lanza `MapsBlockedError`, la búsqueda queda BLOQUEADO con su motivo y se conserva lo guardado hasta ese momento. Si Google pide aceptar cookies, se pulsa "Rechazar todo" (no es un captcha).
  - **Vista móvil (sección 7.1):** implementada en `BrowserEngine.new_mobile_page()`, con emulación CDP sobre el contexto normal (un contexto nuevo de Playwright se colgaba). Queda apagada por defecto (`MAPS_MOBILE_RETRY=false`) porque en la prueba la versión móvil web de Maps mostró el aviso "Abrir aplicación" y solo el botón "Llamar": menos enlaces que en escritorio, no más. **Joaquín confirmó dejarla apagada (2026-10-04).**
  - **Pipeline:** `app/pipeline.py` tiene el paso de Maps (`run_maps_step`): crea el `SearchRun`, guarda cada negocio con `upsert`, aísla los errores de cada ficha y actualiza los contadores. El comando `maps` lo usa, imprime JSON por stdout y el avance por stderr. Código de salida: 0 si terminó, 2 si hubo bloqueo.
  - **Prueba real** (2026-10-04): "cerrajeros en Rancagua" con 5 negocios tardó 33 s, sin bloqueos. Aparecieron todos los casos: web propia, Facebook como web, sin web, y wa.me como web (JT Keys → VERIFICADO). La segunda búsqueda reconoció los negocios repetidos. Después se borró `data/prospector.db` para que Joaquín empiece limpio.
  - **Prioridad n.º 1 de Joaquín (2026-10-04):** de cada negocio, tener la **URL de su web** y un **WhatsApp que realmente exista**. Decisión aprobada: **nivel de confianza automático + confirmación manual**.
    - **Confianza automática** (se implementa en las Fases 3 y 5, al combinar Maps y web):
      - **ALTA:** el mismo número aparece en 2 fuentes independientes (Maps, web, Linktree), o el WhatsApp publicado coincide con el teléfono de la ficha de Maps.
      - **MEDIA:** aparece en una sola fuente.
      - **Botón roto:** se marca aparte cuando el enlace publicado no funciona (sin código de país, acortador caído).
    - **Confirmación manual** (Fase 6): después de pulsar ABRIR WHATSAPP, Joaquín marca "Existe" o "No existe". Se guarda con fecha (campos nuevos en `businesses`, agregados con una migración).
    - **No se verifica iniciando sesión en WhatsApp** (regla 5). Prueba del 2026-10-04: la página pública `wa.me/<número>` es idéntica (99,75 %; solo cambian códigos internos al azar) para un número real y uno inventado, así que no sirve para saber si el número existe.
    - **Web confirmada** (Fase 3): comprobar que la web abre, guardar la URL final y marcarla OK o CAIDO.
  - **Fase 3 — recolector web** (`app/analyze/collector.py`; las reglas puras están en `app/analyze/web_status.py`):
    - **`verify(url)`**, rápido (httpx con los certificados de Windows vía `truststore`). Revisa:
      - **Estado de la web:** OK, CAIDO o BLOQUEADO, con un motivo en palabras simples. Cuenta como CAIDO: sin DNS, timeout, error HTTP, certificado SSL inválido (lo confirma el navegador real), dominio a la venta, suspendido o estacionado, página por defecto del hosting, "en construcción" (solo si la página casi no tiene texto), error de base de datos de WordPress o "Index of /". Es BLOQUEADO cuando hay una protección anti-bots que tampoco deja pasar al navegador. Si la web redirige a Facebook o Instagram, queda SOLO_REDES.
      - **URL final**, después de las redirecciones.
      - **WhatsApp** en el inicio, en hasta `WEB_MAX_INTERNAL_PAGES` páginas internas (contacto > servicios > nosotros) y en los Linktree enlazados. Se siguen los acortadores.
      - **Navegador:** solo se usa si la web se arma con JavaScript (menos de 200 caracteres de texto), tiene protección anti-bots o un problema de certificado.
      - **Linktree como "sitio web":** si enlaza a una web propia, se verifica esa web y se suman los WhatsApp de ambas.
    - **`collect(url)`**, completo: lo anterior más el navegador real (sin ventana).
      - Tiempos de carga, número de requests y peso aproximado.
      - Capturas de escritorio (1440×900) y celular (390×844): primera pantalla y página completa, con alto máximo `SCREENSHOT_MAX_HEIGHT`. Se guardan en `data/screenshots/<dominio>/<fecha>/`.
      - Primera pantalla: si se ve el WhatsApp o el teléfono, y qué botones de acción hay.
      - **Botones falsos:** se ven como botón ("Llamar", "Cotiza"), tienen fondo propio y tamaño de botón, pero no son enlace. Ejemplo real verificado en el HTML: el "Llamar" de cerrajeriamultiservice.cl es un `h2` sin enlace.
      - En celular: scroll horizontal, medido sobre `clientWidth`, y página "alejada" cuando falta la etiqueta `viewport`.
      - Enlaces e imágenes rotas (HEAD y luego GET). Se excluyen los enlaces técnicos (`wp-admin`, `wp-json`, `cdn-cgi`, `imunify-bot-check`); este último es una trampa para robots.
      - PageSpeed si hay `PAGESPEED_API_KEY`. Sin clave, la velocidad local se marca como "referencial".
    - **Confianza del WhatsApp** (`choose_whatsapp`): ALTA o MEDIA, con el motivo en `confidence_reason` (se guarda en `field_sources["whatsapp_confidence"]`).
    - **Botón roto:** un enlace sin código de país, un número inválido o un acortador caído. Queda en `whatsapp_broken_button` y su evidencia en `field_sources["whatsapp_broken_web"]`.
    - **Migración v2:** columnas `whatsapp_confidence` y `whatsapp_broken_button`. Una base de la Fase 2 se actualiza sola, sin perder datos.
    - **WhatsApp combinado entre fuentes** (`pipeline.whatsapp_changes`): siempre se recalcula con todas las fuentes (lo guardado de otras fuentes más lo nuevo). Volver a correr `maps` ya no borra el WhatsApp de la web; `field_sources` se suma en vez de reemplazarse.
    - **Paso de verificación** (`run_verify_step`, comando `verify <id de búsqueda>`): revisa la web de cada negocio con concurrencia `WEB_CONCURRENCY`.
      - Si la web abre, `website` pasa a la URL final y la URL de Maps queda en `field_sources["website_maps"]`.
      - Completa Instagram y Facebook desde la web si Maps no los tenía.
      - Los negocios sin web igual reciben la confianza de su WhatsApp de Maps.
    - **Prueba real** (2026-10-04), `maps` + `verify` con 5 cerrajeros de Rancagua:
      - CGC Fenix (web hecha con Manus): web OK y WhatsApp VERIFICADO con confianza **ALTA** (el WhatsApp flotante de la web coincide con el teléfono de Maps).
      - Multiservice (WordPress + Elementor): web OK y WhatsApp con confianza MEDIA. Tiene el botón falso "Llamar".
      - Los otros 3 (solo Facebook o sin web): NO CONFIRMADO.
      - Un dominio inexistente sale CAIDO en 1 s.
      - Después se borraron los datos de prueba.
  - **Fase 4 nueva — leads diarios** (decidido el 2026-10-04; ver el cambio de plan en las secciones 7 y 12):
    - **Página local** (`app/web/app.py`, comando `serve`, FastAPI + Jinja2 + HTMX 2.0.4 guardado en `app/web/static/`, sin depender de internet):
      - buscador libre más la cantidad de leads (por defecto `SEARCH_LIMIT_DEFAULT`);
      - avance en vivo cada 2 s, con el botón Detener;
      - lista "Leads del día" y enlaces a los días anteriores;
      - botones Abrir / Probar WhatsApp, ✓ Existe, ✗ No existe y ↶ Deshacer;
      - exportar a CSV.
      - Se busca **una búsqueda a la vez**, en segundo plano (`JobManager`).
    - **Búsqueda de leads** (`pipeline.run_leads_job`):
      - recorre la lista de Maps de a poco (`MapsSource.iter_search`), con la lista en una pestaña y las fichas en otra de la misma sesión;
      - **se salta sin abrir** todo negocio ya guardado (incluidos los descartados y los que no fueron lead);
      - revisa la web y el WhatsApp de cada uno (Fase 3);
      - para al llegar a la meta, al final de la lista, con Detener o ante un bloqueo de Google. Lo encontrado queda guardado.
      - Si la búsqueda se agota antes de la meta, la página lo dice para que Joaquín pruebe otra búsqueda.
    - **Lead** = no descartado y con WhatsApp VERIFICADO o con un celular probable (`is_probable_mobile`).
      - Chile: 9 dígitos que empiezan con 9, porque `phonenumbers` no distingue fijos de celulares.
      - Otros países: tipo MOBILE o FIXED_LINE_OR_MOBILE.
    - **Cualquier país:** el país de cada negocio sale de su teléfono (`region_for_phone`) y se usa al revisar su web. Así un `wa.me/3055551234` de Miami queda como +1.
      - Las direcciones de otros países se leen bien (se descartan nombres de país y estado + código postal).
    - **Confirmación manual** (`app/leads.py`):
      - **✓ Existe** agrega un candidato con fuente `manual` (la más alta: queda VERIFICADO con confianza ALTA y `whatsapp_check=EXISTE`).
      - **✗ No existe** marca `whatsapp_check=NO_EXISTE` y `discarded_at`: el lead desaparece y no vuelve a aparecer.
      - **↶ Deshacer** revierte cualquiera de las dos.
      - El enlace "Probar WhatsApp" (`wa.me/<celular>`) nunca se guarda como `whatsapp_url`.
    - **Orden por defecto:** confirmados → verificados con confianza ALTA → MEDIA → por probar; dentro de cada grupo, más reseñas primero. También se puede ordenar por "sin web o caída primero", reseñas, rating o nombre.
    - **Días:** los días se cuentan en la hora local del computador (las fechas se guardan en UTC).
    - **CSV:** con ";" y BOM, para que Excel en español lo abra con tildes y columnas separadas.
    - **Migración v3:** columnas `whatsapp_check`, `whatsapp_checked_at`, `discarded_at` y `discard_reason` en `businesses`, y `leads_found` en `searches`.
    - **Ventana cerrada a mano:** si se cierra la pestaña de fichas, se abre otra sola. Si se cierra el navegador completo, la búsqueda queda FALLIDA con un mensaje simple ("No cierres la ventana de Chromium mientras busca") y lo encontrado queda guardado.
    - **Prueba real** (2026-10-04) desde la página, "cerrajeros en Rancagua" con 5 leads:
      - Primer intento: 3 leads y luego falló porque se cerró el navegador de Maps. Probablemente se cerró a mano la ventana; no se pudo reproducir.
      - Segundo intento: **5/5 leads en 35 s**, con los 3 ya vistos saltados sin abrir.
      - Botones Existe, No existe y Deshacer probados con clics en un navegador real.
      - Se borraron los datos de prueba.
  - **Fase 5 — análisis de webs** (decidido por Joaquín el 2026-10-04):
    - **IA lista pero APAGADA:** por ahora no quiere gastar dinero. Sin `ANTHROPIC_API_KEY` en `.env`, `build_llm_client()` devuelve None y no se llama a la IA (costo $0). Para activarla basta con poner la clave: el análisis corre automático al buscar leads.
    - **Mientras tanto, hallazgos automáticos gratis** (`app/analyze/findings.py`):
      - Problemas medidos sin IA, en lenguaje de negocio, con evidencia técnica aparte, gravedad (alta, media o baja) y el parámetro de la rúbrica relacionado.
      - Detecta: botón falso, WhatsApp roto, sin WhatsApp en la web, WhatsApp escondido en celular, sin llamado a la acción, sin teléfono con un toque, no adaptada al celular, se sale de la pantalla, lenta, sin HTTPS, sin testimonios ni reseñas, enlaces e imágenes rotas, copyright de hace 2 años o más, plantilla (solo constructores tipo Wix, Manus o Lovable; WordPress solo no cuenta), sin cobertura, SEO local débil (3 o más ítems faltantes).
      - Negocios sin web que revisar: "No tiene web propia", "Usa Facebook o Instagram…" y "Su web no funciona" (`status_findings`).
      - También calcula el SEO local y el checklist de información de la sección 8.3. "Diferenciadores" lo decide solo la IA.
    - **Revisión automática al buscar** (`pipeline.review_websites`): después de Maps, revisa con `collect` las webs que abren de los leads nuevos (concurrencia `WEB_CONCURRENCY`). Guarda un `WebsiteAnalysis` con hechos, hallazgos, SEO, checklist, capturas y los puntajes de código; si hay IA, suma su juicio. Respeta el botón Detener y corre aunque Maps haya terminado con bloqueo, porque las webs no pasan por Google.
    - **Rúbrica** (`app/analyze/rubric.yaml`, versión "2026-10-04.1"):
      - Los 10 parámetros de Joaquín sin cambios: 5 y 9 por código, 8 por IA.
      - **Los descriptores por tramo de los 8 parámetros de IA los redacté yo: pendiente de que Joaquín los revise.**
      - **Definición de "errores" del parámetro 9** (enlaces): enlaces internos rotos + botones de WhatsApp rotos + botones falsos. Pendiente de confirmar con Joaquín.
      - Para la velocidad se usa el tiempo de carga en escritorio (la primera visita, sin caché).
    - **Puntaje** (`scorer.py`):
      - Total = suma de los 10 parámetros, y solo existe si la IA puntuó sus 8 con evidencia.
      - Nivel de oportunidad según la sección 8.5. **Decisión:** un score < 50 sin el rating ni las reseñas de ALTA queda en MEDIA. Un negocio sin web propia queda en ALTA con buena reputación y en MEDIA sin ella.
    - **IA** (`llm.py`, `PROMPT_VERSION` "2026-10-04.1", modelo por defecto `claude-opus-5-5`, esfuerzo `medium`; ambos se cambian en `.env`):
      - La interfaz es `LLMClient` y la implementación es `AnthropicLLMClient`.
      - **SDK:** browser-use fija `anthropic==0.76.0`, que no trae `output_config.format` ni `fallbacks` como parámetros con nombre. Se envían por `extra_body` (salida en JSON con esquema estricto, esfuerzo y `fallbacks: "default"` con la beta `server-side-fallback-2026-07-01`) y la respuesta se lee como JSON crudo.
      - **Sin temperatura:** los modelos actuales la rechazan. La consistencia sale de los tramos, el esquema fijo y los parámetros de código.
      - El system prompt con la rúbrica va marcado para caché.
      - **Regla 4 en código** (`clean_analysis`): se descarta todo puntaje, problema, oportunidad o punto fuerte sin evidencia, y todo parámetro repetido o de código.
      - **Probado solo con respuestas simuladas** (`httpx.MockTransport`): no hay clave para probar con la API real.
    - **Página:**
      - En cada lead, los 3 problemas más graves a la vista y el resto plegado, con detalle, evidencia y botones a las 4 capturas (servidas en `/capturas`; solo esa carpeta, nunca la base de datos).
      - Puntaje con color y nivel de oportunidad (con IA); oportunidad también para los negocios sin web.
      - Aviso de "IA apagada" y nuevo orden "más problemas primero".
      - El CSV agrega problemas, puntaje y oportunidad.
    - **Comandos nuevos:** `analyze <url>` y `run "<búsqueda>" --limit N`.
    - **Migración v4:** columna `website_analyses.findings`.
    - **Prueba real** (2026-10-04), "cerrajeros en Rancagua" con 5 leads desde la página:
      - 5/5 leads en 53 s; luego se revisaron solas las 2 webs que abren: Multiservice con 7 problemas (incluido el botón falso "Llamar") y CGC Fenix con 5.
      - El detalle y las capturas se vieron bien en un navegador real.
      - Se borraron los datos de prueba.
  - **No repetir leads** (pedido de Joaquín, 2026-10-04; `app/dedupe.py`):
    - **Huellas** (tabla `fingerprints`): de cada lead (WhatsApp verificado o celular) y de cada descartado se guardan su **número** (teléfono, WhatsApp y todos los candidatos), su **web** y sus **redes** (Instagram/Facebook). Un negocio nuevo que comparte cualquier huella con uno anterior o con un contactado es **REPETIDO**: queda guardado con `duplicate_of_id` y `duplicate_reason` (en palabras simples), no cuenta como lead y no se muestra. Los negocios que no fueron lead (por ejemplo, solo fijo) no dejan huella: una sucursal con WhatsApp sí puede ser lead.
    - **Números:** se comparan en forma canónica (`canonical_number`). México: el celular antiguo `+52 1 …` = `+52 …` (`normalize_phone` ahora lo acepta; el enlace `wa.me/521…` se conserva tal cual porque abre el chat). Argentina: `549…` (WhatsApp) = `54…` (como aparece a veces en Maps).
    - **Webs** (`site_key`): dominio sin "www", así las sucursales de una misma web cuentan como repetidas. En plataformas compartidas se agrega la ruta: Linktree y similares, Google Sites, wixsite, acortadores y plataformas de reservas o directorios (Booksy, Fresha, AgendaPro, Doctoralia, MercadoLibre, PedidosYa…), para no confundir dos salones distintos de Booksy. `wa.link/…` y `wa.me/message/…` sin número usan el enlace como huella.
    - **Tres controles en la búsqueda** (`run_leads_job`): (1) con la **tarjeta de la lista** (web y teléfono que muestra, leídos por `parse_feed`), sin abrir la ficha; (2) con la **ficha**, antes de revisar la web; (3) **después de revisar la web**, porque ahí suele aparecer el WhatsApp. Al empezar, `ensure_fingerprints` guarda las huellas de los leads antiguos que no las tenían. Contador nuevo "repetidos o ya contactados" y el motivo en el registro.
    - **Ya contactados** (tabla `contacted_leads`, página `/contactados`, comando `import-contactados <archivo>`): Joaquín pega las filas de su planilla (nombre, teléfono, estado, hora, fecha, "checked"). Se lee con tabulaciones; si no las hay, se busca el número dentro del texto. Las líneas sin número válido no se guardan y vuelven al cuadro para corregirlas. Pegar la lista de nuevo no duplica: actualiza el estado. Al importar, los leads ya guardados con esos números se ocultan (`flag_contacted`).
    - **Repetidos ocultos:** en la lista del día, una sección plegada muestra cada repetido con su motivo y el botón **"No es repetido"** (`unmark_duplicate`), por si la deduplicación se equivocó.
    - **Migración v5:** columnas `duplicate_of_id` y `duplicate_reason` en `businesses`; las tablas nuevas las crea `create_all`.
    - **Lista real importada** (2026-10-04): 142 filas de la planilla de Joaquín → **140 contactados** (2 números repetidos dentro de la lista), 0 líneas inválidas. Quedaron en `data/prospector.db`, que todavía no existía porque las pruebas anteriores habían borrado sus datos.
    - **Prueba real** (2026-10-04, sobre una copia de la base): "vidrierias en providencia", 3 leads. Un negocio se saltó por tener el mismo número que uno de la lista de contactados, y el motivo mostró su nombre, estado y fecha. Los otros 3 fueron leads nuevos.
  - **Fase 6 — pipeline e interfaz completos** (2026-10-04):
    - **Informe del negocio** (`/negocio/{id}`, datos en `app/business_report.py`). Muestra el resumen de la sección 10: web con [Abrir web], WhatsApp con [ABRIR WHATSAPP] solo si está VERIFICADO (si no, teléfono con botón Copiar y, si es celular, Probar WhatsApp), Maps, rating, web score con color y oportunidad. Además:
      - problemas (los de la IA y los medidos sin IA, con su evidencia plegada), oportunidades y puntos fuertes (si la IA está apagada, lo dice);
      - capturas de escritorio y celular, con enlace a la página completa;
      - los 10 parámetros de la rúbrica (quién evalúa cada uno, puntaje, justificación y evidencia);
      - SEO local y checklist de información;
      - datos de la ficha de Maps (ciudad, comuna, descripción, servicios, horario);
      - todos los WhatsApp encontrados con la evidencia exacta, y de dónde salió cada dato (`field_sources`, en palabras simples);
      - evidencia técnica plegada (y el JSON completo de lo medido) e historial de revisiones;
      - botones Existe, No existe y Deshacer; aviso si es repetido o descartado.
      - Se llega desde el nombre del lead en la lista ("Ver informe →") y desde los repetidos ocultos.
    - **Re-analizar web** (`ReviewManager` en `app/web/app.py`): corre en segundo plano, de a una web por vez, con su propio navegador sin ventana. El informe muestra "Revisando…" y se recarga solo al terminar. Si falla, muestra el motivo. `review_website(refresh_business=True)` también actualiza el negocio: si la web se cayó o volvió, su URL final y el WhatsApp que publica.
    - **"Revisar N webs pendientes"** en la lista del día: revisa las webs que abren pero quedaron sin análisis (por ejemplo, si se pulsó Detener). Muestra el avance y la lista se actualiza sola.
    - **Filtros rápidos** (`FILTERS` en `app/leads.py`, se combinan entre sí): con WhatsApp, celular por probar, con web, sin web (incluye solo redes y web caída), web mala, rating > 4,5, más de 100 reseñas, WhatsApp + web mala y grandes oportunidades. **Decisión:** "web mala" = puntaje < 50 con IA; sin IA, una web que abre con al menos un problema grave detectado, porque sin IA no hay puntaje.
    - **Búsquedas del día:** si en un día hubo varias búsquedas, se puede filtrar por cada una. Esto reemplaza la pantalla `/search/{id}` de la sección 10: la lista del día ya muestra el avance en vivo, el botón Detener, los contadores y la tabla.
    - Los filtros, la búsqueda elegida y el orden quedan en la URL. Los respetan la recarga automática de la lista y **Exportar CSV**.
    - **Caché de análisis:** una web con un análisis exitoso de hace menos de `REANALYZE_AFTER_DAYS` (30) días no se vuelve a revisar al buscar. Re-analizar siempre revisa de nuevo.
    - **Errores reales encontrados y corregidos en la prueba en vivo:**
      - Al revisar 3 webs en paralelo, 2 fallaban con "El navegador no está iniciado": las tres abrían el navegador interno a la vez. Ahora se abre una sola vez, con un candado (`WebCollector._browser`). Esto afectaba a la búsqueda de leads desde la Fase 5 cuando ninguna web había necesitado navegador antes.
      - Un repetido detectado después de revisar su web igual se revisaba a fondo: `review_websites` ahora vuelve a leer cada negocio antes de revisarlo.
      - Re-analizar una web caída fallaba al calcular los hallazgos: ahora solo informa "Su web no funciona", sin inventar otros problemas.
      - En la búsqueda real de Joaquín ("cerrajeros en santiago"), la web de "ADA Cerrajero" era un Google Sites privado que redirige al inicio de sesión de Google, y se analizó esa página como si fuera la del negocio. Ahora una web que termina en una página de inicio de sesión (Google, Microsoft, Wix; `login_wall_reason`) queda CAÍDA con el motivo "La web no es pública…". Sus 2 análisis antiguos siguen guardados hasta que Joaquín pulse Re-analizar.
    - **Prueba real** (2026-10-04, sobre la copia de la base de la búsqueda de vidrierías, en un navegador de verdad): "Revisar 2 webs pendientes" tardó 47 s; el filtro "Con web" dejó 2 de 3 leads; el informe de Vidriería Alustyl mostró todas las secciones; "Re-analizar" tardó 22 s y la página se recargó sola, sin errores de JavaScript.
  - **Solo negocios con web que abre** (decisión de Joaquín, 2026-10-04):
    - `is_lead` exige `website_status == OK`. Joaquín eligió **descartar también las webs caídas** (incluidas las privadas que piden iniciar sesión). Las bloqueadas por anti-bots tampoco cuentan, porque no se puede confirmar que abren.
    - **Se salta sin abrir la ficha** todo resultado cuya tarjeta de la lista no muestra "Sitio web", o cuyo "sitio" es Facebook, Instagram, WhatsApp o Google (`card_has_website`; `MapsListing.card_seen` indica si la tarjeta se leyó). Un Linktree sí se abre, porque puede llevar a una web propia. **Verificado en vivo:** en 15 resultados de "cerrajeros en ñuñoa", la tarjeta y la ficha coincidieron en los 15; 7 no tenían web.
    - Si la ficha no tiene web, o su web resulta caída o solo redes, se salta con el motivo en el registro. Contador nuevo: "sin web o con web caída (saltados)".
    - Se quitaron el filtro "Con web", el filtro "Sin web" y el orden "Sin web primero". El contador de cada día cuenta los leads con la regla vigente (`days_with_searches`).
    - Las huellas de no repetir solo se guardan para leads con web y para los descartados.
    - **"No es repetido"** de un negocio cuya web no se había revisado (porque se marcó repetido antes) revisa su web en segundo plano; si abre, entra a la lista.
    - **Prueba real** (2026-10-04, sobre una copia de la base): "cerrajeros en ñuñoa", 3 leads. Los 3 tienen web que abre; 7 sin web se saltaron sin abrir; 2 repetidos de la lista de contactados se detectaron desde la tarjeta, sin abrir la ficha; las 3 webs se revisaron a fondo sin errores.
    - **Base real de Joaquín:** antes de corregirla se respaldó en `data/prospector.respaldo-2026-10-04.db`. ADA Cerrajero (Google Sites privado) se volvió a revisar y quedó CAÍDA, con su web original de Maps restaurada. Con la regla nueva, la lista de hoy pasó de 21 a 11 leads, todos con web que abre.
  - **Acceso directo:** `Abrir Vortexia Prospector.bat` en la carpeta del proyecto, y un acceso directo "Vortexia Prospector" en el Escritorio de Joaquín. Hace `cd` a la carpeta del proyecto y ejecuta `serve`, así ya no depende de la carpeta en que esté abierta la terminal: la de VS Code abre en `OneDrive\Escritorio\buscadorpro`, donde ya no está el programa. Probado: el servidor responde.
  - **Fase 7 — endurecimiento** (2026-10-04):
    - **Registro del día** (`app/logs.py`): `data/logs/prospector-AAAA-MM-DD.log`, con hora, nivel y mensaje en español; se guardan 30 días. Ahí van todos los mensajes de búsqueda (`LeadsProgress.say`) y el detalle técnico de cada error (`log.exception`). En la pantalla solo se ve el motivo en palabras simples. La página **Registro** (`/registro`) muestra el de hoy, lo más nuevo arriba y con errores y advertencias destacados. Lo activa `cli.main`; los tests no escriben registros.
    - **Reintentos con límite** (`WEB_RETRIES=1`, `WEB_RETRY_PAUSE_SECONDS=3`, `MAPS_RETRIES=1`):
      - **Webs:** se reintenta lo que puede ser pasajero (no respondió, conexión cortada, errores 429, 500, 502, 503, 504 y 520–524). No se reintenta un dominio que no existe ni un 404. Si sigue fallando, el motivo dice "(se intentó 2 veces)".
      - **Enlaces rotos:** un enlace solo cuenta como roto si sigue fallando después de reintentar.
      - **Revisión a fondo:** si falla (por ejemplo, se cae el navegador), se reintenta una vez; si se cortó por tiempo, no.
      - **Maps:** una ficha que no carga o una búsqueda que no responde se reintentan una vez. La ficha vacía cuenta para la detección de bloqueo solo en el último intento, así que el reintento no se confunde con un bloqueo. **Ante un captcha o bloqueo nunca se reintenta** (regla 6, con test).
    - **Tiempos máximos por web:** `WEB_VERIFY_MAX_SECONDS=120` para comprobar si abre y buscar su WhatsApp, y `WEB_REVIEW_MAX_SECONDS=300` para la revisión a fondo. Si una web no termina, se corta con un motivo claro y la búsqueda sigue.
    - **Errores en palabras simples** (`friendly_error`): sin internet, falta el navegador (`playwright install`), el navegador no pudo abrirse, ventana cerrada, revisión cortada por tiempo.
    - **Abrir dos veces:** `serve` revisa si el puerto ya está en uso; si el programa ya está abierto, abre esa página en vez de fallar (doble clic repetido en el acceso directo).
    - **Vista limitada de Google Maps:** sin ventana (`HEADLESS=true`), Google muestra una "vista limitada" **sin el número de reseñas**. Se probó el 2026-10-04: la misma ficha mostró "(336)" con ventana y nada sin ventana. El programa no inventa el dato: queda vacío y no borra el ya guardado. Si pasa, avisa una vez en el registro. `HEADLESS=false` (el valor por defecto) es necesario; quedó documentado en `.env.example` y en el README.
    - **Título del hallazgo de copyright:** decía "El pie de página dice © 2022", pero esa web dice "2022 Todos los derechos reservados", sin ©. Ahora dice "El pie de página muestra el año 2022". Se corrigieron los 3 análisis guardados; antes se respaldó en `data/prospector.respaldo-2026-10-04b.db`.
    - **README.md** para Joaquín: abrir, buscar, leer la lista y el informe, ya contactados, activar la IA, `.env`, qué hacer si algo falla, datos y respaldos, cómo comprobar que los datos son reales, instalar en otro computador y comandos.
    - **Auditoría de aceptación** ("cero datos inventados", 2026-10-04): se tomaron los 10 primeros leads reales de Joaquín ("cerrajeros en santiago") y se compararon con las fuentes en vivo, con la ventana de Maps visible. Se compararon nombre, teléfono, web en Maps, dirección, rating, reseñas, que la web abra y que cada WhatsApp guardado aparezca literalmente (su evidencia) en la fuente: **80 datos, 0 diferencias**. Los problemas que se pueden comprobar en el texto (copyright y sin HTTPS) se confirmaron en vivo. Falta la revisión manual de Joaquín, que es el criterio formal; la guía está en la sección 10 del README.
    - **Prueba real final** (2026-10-04, copia de la base, con ventana): "gasfiter en providencia", 3 leads. 3 sin web se saltaron sin abrir; 2 repetidos de la lista de contactados; 3 webs revisadas; las reseñas se guardaron. El registro del día quedó escrito y legible.
  - **Publicación en GitHub** (2026-10-04): repositorio **público** <https://github.com/rznavarro/buscadorpro>, rama `main`, etiqueta `v1.0.0`.
    - **Nunca se suben** `data/` (base de datos, contactados, capturas, registros y respaldos) ni `.env`. Antes del primer commit, los números y nombres reales de contactados que había en ejemplos, tests y estas notas se reemplazaron por datos inventados. Los tests usan páginas públicas de Google Maps de 3 cerrajerías de Rancagua.
    - **Descarga para Windows:** el botón del README descarga `descargas/VortexiaProspector-Windows.zip`, que es un archivo del propio repositorio. Se arma con `tools/armar-descarga.ps1` (`git archive` del último commit, sin `tests/`, `tools/`, `descargas/` ni `CLAUDE.md`; los `.bat` van con CRLF). **Después de cada cambio hay que rearmarlo y subirlo.** Trae `Instalar Vortexia Prospector.bat`, que instala uv (con winget o, si falla, el instalador oficial), ejecuta `uv sync` e instala Chromium, crea `.env` a partir de `.env.example` y el acceso directo del Escritorio. `Abrir Vortexia Prospector.bat` usa su propia carpeta (`%~dp0`). Probado en una carpeta nueva: instala, abre la página y empieza sin datos.
    - **Instalador de Windows (como cualquier programa)** (2026-10-05): `descargas/VortexiaProspector-Setup.exe` (unos 2,2 MB), hecho con **Inno Setup 6** (`installer/VortexiaProspector.iss`) y fabricado con `uv run python tools/armar-instalador.py`, que copia los archivos de git sin los de desarrollo, se detiene si se colara algo de `data/` o `.env` y compila. El botón del README apunta a este `.exe`; el zip portátil queda como alternativa.
      - Instala en `%LOCALAPPDATA%\Programs\Vortexia Prospector` sin pedir permisos de administrador y solo en Windows de 64 bits.
      - Crea accesos directos con ícono en el menú Inicio y, si se marca, en el Escritorio. Apuntan a `.venv\Scripts\pythonw.exe -m app.desktop`.
      - Después de copiar los archivos ejecuta `Instalar Vortexia Prospector.bat --desde-instalador` (uv, `uv sync --no-dev` y Chromium) en una ventana visible, comprueba que quedó Python y, si no, avisa.
      - Al desinstalar: cierra el programa si está abierto (`[UninstallRun]`), borra `app/` y `.venv`, y **conserva `data/` y `.env`**.
      - Probado instalando y desinstalando en modo silencioso. Una carpeta de prueba con una ruta muy larga dejó restos (rutas de más de 260 caracteres); en la ubicación real, la ruta más larga es de 197 caracteres.
    - **Lanzador de escritorio** (`app/desktop.py`): arranca el servidor sin consola (pythonw; stdout y stderr van a `data/logs/consola.log` o a devnull) y abre una ventana propia con Edge en modo aplicación (`--app`, perfil propio en `data/ventana`; si no hay Edge, prueba Chrome y, si tampoco, el navegador predeterminado). Al cerrar esa ventana se apaga el servidor. Si el puerto ya está en uso, solo abre otra ventana. Los errores se muestran en un cuadro de mensaje de Windows. Probado: la ventana se abrió con el título "Vortexia Prospector" y, al cerrarla, el programa se cerró solo.
    - **Ícono:** `tools/crear-icono.py` genera `app/web/static/vortexia.ico` y `vortexia.png` (una V blanca sobre el morado de la página); también se usa como favicon. La versión del proyecto quedó en 1.0.0.
    - **GitHub Actions no funciona:** la cuenta de Joaquín está bloqueada por un problema de facturación ("account is locked due to a billing issue"). Por eso el zip va dentro del repositorio y no como Release armado por un workflow. Si Joaquín lo resuelve, se puede volver a un workflow que publique Releases.
- **Pendientes / riesgos:**
  - **Volumen de Maps:** con "solo con web", hacen falta más resultados de Maps para juntar 20 leads. Los sin web se saltan sin abrir, pero la lista igual se recorre más lejos. `max_listings` está en 120 resultados por búsqueda. Si Google empieza a bloquear, subir las pausas (`DELAY_MIN_SECONDS` y `DELAY_MAX_SECONDS`).
  - **Webs caídas por un rato:** un negocio cuya web estaba caída al revisarla (incluso después del reintento) queda guardado como no lead y no se vuelve a revisar en otra búsqueda. Si se quisiera, en el futuro se podrían volver a revisar las webs caídas cada cierto tiempo.
  - **Re-analizar y búsqueda a la vez:** se pueden usar al mismo tiempo, cada uno con su navegador. No se probó con muchas revisiones en paralelo a una búsqueda larga; si el computador se pone lento, conviene esperar a que termine la búsqueda.
  - **Historial de revisiones:** cada Re-analizar guarda un análisis nuevo con sus capturas (unos 0,7 MB). Hoy no se borra nada.
  - **Repetidos — tarjeta de la lista:** el teléfono de la tarjeta se lee con la región por defecto (CL). Un número extranjero sin "+" no se reconoce ahí, pero se detecta igual al abrir la ficha (control 2). En una prueba real, un repetido no se detectó con la tarjeta, sino al abrir la ficha (no se revisó por qué: puede que la tarjeta no mostrara el teléfono).
  - **Repetidos — plataformas compartidas:** la lista de plataformas de reservas y directorios es fija. Si aparece otra donde dos negocios distintos comparten dominio, se marcarían como repetidos por error; Joaquín lo ve en "repetidos ocultos" y puede pulsar "No es repetido". Hay que agregar esa plataforma a `_SHARED_HOSTS`.
  - **Criterio de aceptación de la IA** (la misma web dos veces no puede variar más de 5 puntos): no se puede medir sin clave. Medirlo apenas Joaquín active la IA.
  - **Espacio en disco:** las capturas ocupan unos 0,7 MB por web (4 JPG). Con unas 10 webs al día son unos 2,5 GB al año. Más adelante convendría borrar las capturas viejas.
  - **"Probar WhatsApp":** para otros países, `is_probable_mobile` acepta números que podrían ser fijos (EE. UU. no los distingue). Joaquín los descarta con "No existe".
  - **Botones falsos:** la detección es una heurística (fondo propio + tamaño de botón + no es enlace ni tiene cursor de clic). Se muestra con la captura de la primera pantalla como evidencia, para que Joaquín la confirme antes de usarla en un mensaje.
  - **Pestaña "Información"** (descripción y servicios): no apareció en ninguna ficha de cerrajeros, así que el lector por secciones (`parse_about`) está probado solo con HTML sintético. Verificarlo cuando aparezca una ficha real que la tenga.
  - **Fase 5:** si se vuelve a correr `maps`, `website` vuelve a la URL de Maps hasta el siguiente `verify` (por ejemplo, un Linktree en vez de la web propia que se encontró dentro). El pipeline completo siempre corre `maps` y después `verify`, así que se corrige solo.
  - browser-use fija versiones exactas de `anthropic` (0.76.0) y `pydantic` (2.13.5). El `LLMClient` envía los parámetros nuevos por `extra_body`, pero **nunca se probó contra la API real** (no hay clave): probarlo el día que Joaquín la active.
  - La API de Browser Use cambia entre versiones. Los parches de arriba son específicos de la 0.13.10: revisarlos si se actualiza.