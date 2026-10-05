# Vortexia Prospector — manual de uso

[![Descargar para Windows](https://img.shields.io/badge/Descargar_para_Windows-Vortexia_Prospector-0078D6?style=for-the-badge&logo=windows&logoColor=white)](https://github.com/rznavarro/buscadorpro/raw/main/descargas/VortexiaProspector-Setup.exe)

Encuentra negocios locales en Google Maps que **tienen página web** y un **WhatsApp** (o un
celular para probar), revisa su web y te dice qué problemas tiene, para que les ofrezcas un
rediseño. Todo corre en tu computador: nada se publica ni se envía a nadie. El programa
**nunca escribe a los negocios**: tú decides a quién contactar.

---

## 0. Instalar (solo la primera vez)

Necesitas Windows 10 u 11 (64 bits) con internet. No pide permisos de administrador.

1. Pulsa el botón **Descargar para Windows** de arriba. Se descarga `VortexiaProspector-Setup.exe`.
2. Ábrelo con doble clic.
   - Si Windows muestra "Windows protegió su PC", pulsa **Más información** → **Ejecutar de
     todas formas** (pasa con los programas que no compraron un certificado de firma).
3. Pulsa **Siguiente** e **Instalar**. Se abre una ventana negra que descarga Python, las
   librerías y el navegador del programa: la primera vez tarda varios minutos y se cierra sola.
4. Al final, deja marcado **Abrir Vortexia Prospector** y pulsa **Finalizar**.

Queda instalado como cualquier programa: lo encuentras en el **menú Inicio** (escribe
"Vortexia") y en el **Escritorio**, con su ícono. Se instala en
`%LOCALAPPDATA%\Programs\Vortexia Prospector` (en este manual, "la carpeta del programa").

**Actualizar a una versión nueva:** descarga el instalador nuevo y ábrelo; se instala encima.
Tus leads, contactados y configuración (`data` y `.env`) no se tocan.

**Desinstalar:** Configuración de Windows → Aplicaciones → Vortexia Prospector → Desinstalar.
Tu carpeta `data` (leads y contactados) se conserva, por si lo vuelves a instalar.

> ¿Prefieres la versión portátil (sin instalador)? Descarga
> [VortexiaProspector-Windows.zip](https://github.com/rznavarro/buscadorpro/raw/main/descargas/VortexiaProspector-Windows.zip),
> descomprímelo fuera de OneDrive y haz doble clic en **Instalar Vortexia Prospector.bat**.

---

## 1. Abrir el programa

**Búscalo en el menú Inicio (escribe "Vortexia") o haz doble clic en su ícono del Escritorio.**

- Se abre en **su propia ventana**, como una aplicación.
- Para cerrarlo, **cierra esa ventana**: el programa se cierra solo.
- Si lo abres cuando ya estaba abierto, solo se abre otra ventana (no pasa nada malo).
- Si prefieres usarlo en tu navegador, también está en `http://127.0.0.1:8000` mientras está abierto.

---

## 2. Buscar leads (el uso diario)

1. Escribe qué buscas, como lo buscarías en Google Maps: `cerrajeros en Rancagua`,
   `gasfíters en Ñuñoa`, `peluquerías en Miami`. Sirve cualquier rubro y cualquier ciudad o país.
2. Elige cuántos **leads nuevos** quieres (por defecto 20) y pulsa **Buscar leads**.
3. Se abre una ventana de Chromium con Google Maps. **No la cierres** (puedes minimizarla).
4. Arriba ves el avance en vivo. Puedes pulsar **■ Detener** cuando quieras: lo encontrado
   hasta ese momento queda guardado.

**Qué cuenta como lead:** un negocio con **web propia que abre** y con **WhatsApp
verificado** o un **celular** para probar.

**Qué se salta solo** (y por qué lo verás en el registro de la búsqueda):

| Se salta | Por qué |
|---|---|
| Sin web, solo Facebook/Instagram, o con la web caída | Solo te interesan negocios con web que funciona |
| Ya revisado otro día | Nunca se repite un negocio |
| Repetido | Mismo WhatsApp, teléfono, web o red social que un lead anterior |
| Ya contactado | Su número está en tu lista de "Ya contactados" |

Si la búsqueda se acaba antes de juntar los leads que pediste, la página te avisa: prueba
otra búsqueda (otra comuna u otro rubro) para completar el día.

> **Si Google bloquea** (aparece "Google bloqueó la búsqueda"): el programa se detiene solo
> y **nunca** intenta saltarse el captcha. Espera un rato (una hora o más) antes de volver a buscar.

---

## 3. La lista de leads del día

Cada día tiene su lista. Arriba a la derecha ("Días") puedes ver los días anteriores.

**WhatsApp de cada lead:**

| Lo que ves | Qué significa |
|---|---|
| ✓ Existe · confirmado por ti | Tú abriste el chat y existe |
| Verificado · confianza alta | El negocio publica ese WhatsApp en 2 lugares, o coincide con su teléfono de Maps |
| Verificado · confianza media | El negocio lo publica en un solo lugar |
| Por probar · es un celular | No publica WhatsApp, pero su teléfono es un celular: pruébalo |

**Botones:**

- **Abrir WhatsApp / Probar WhatsApp**: abre el chat con ese número en WhatsApp (no envía nada).
- **✓ Existe**: el número tiene WhatsApp. Queda confirmado por ti.
- **✗ No existe**: el lead se descarta y no vuelve a aparecer. **↶ Deshacer** lo revierte.
- **Copiar**: copia el teléfono.
- **Ver informe →**: abre el informe completo del negocio (ver sección 4).

**Filtros** (se pueden combinar): con WhatsApp · celular por probar · web mala · rating > 4,5 ·
más de 100 reseñas · WhatsApp + web mala · grandes oportunidades. Si en el día hiciste varias
búsquedas, también puedes ver cada búsqueda por separado.

**⬇ Exportar CSV** descarga lo que estás viendo (con los filtros puestos) para abrirlo en Excel.

**↻ Revisar N webs pendientes**: aparece si alguna web quedó sin revisar a fondo (por ejemplo,
si pulsaste Detener). La revisa en segundo plano.

**Repetidos ocultos** (al final de la lista): los negocios que se saltaron por repetidos, con el
motivo. Si alguno no es realmente el mismo negocio, pulsa **No es repetido** y vuelve a la lista.

---

## 4. El informe de cada negocio

Haz clic en el nombre del negocio (o en **Ver informe →**). Arriba está el resumen: web,
WhatsApp, Google Maps, rating y reseñas, y el puntaje de la web.

| Sección | Qué te dice |
|---|---|
| **Principales problemas** | Lo que está mal en su web, en palabras de cliente. Cada uno con su **gravedad** (alta, media, baja) y su evidencia técnica (plegada). Estos se detectan **sin IA**, midiendo la web |
| **Oportunidades / Puntos fuertes** | Los redacta la IA (por ahora apagada, ver sección 6) |
| **Capturas** | Cómo se ve su web en computador y en celular, primera pantalla y página completa |
| **Rúbrica de Vortexia** | Los 10 parámetros de tu rúbrica. Velocidad y enlaces los mide el programa; los otros 8 los puntúa la IA |
| **SEO local / Checklist** | Qué le falta para aparecer en Google y qué información no muestra (✓ tiene, ✗ falta, ? lo decide la IA) |
| **WhatsApp encontrados** | Cada número que publica, dónde estaba y **el enlace exacto** como prueba |
| **De dónde salió cada dato** | Si un dato vino de Maps, de su web o lo confirmaste tú |

**↻ Re-analizar web**: vuelve a revisar la web ahora (tarda entre 20 segundos y 1 minuto, y la
página se actualiza sola). Úsalo si el negocio cambió su web o si la revisión anterior falló.
Una web ya revisada no se vuelve a revisar sola antes de 30 días.

---

## 5. Ya contactados

En **Ya contactados** (arriba a la derecha) está la lista de negocios que ya contactaste por
tu cuenta. **Ningún negocio con esos números aparece como lead.**

Para agregar más: copia las filas de tu planilla (nombre, teléfono, estado, hora, fecha) y
pégalas en el cuadro. Puedes pegar la planilla completa todas las veces que quieras: los
números que ya estaban solo se actualizan. Las líneas sin un número válido no se guardan y
quedan en el cuadro para que las corrijas.

---

## 6. Activar la IA (cuando quieras invertir)

Hoy la IA está **apagada** y no cuesta nada: ves los problemas que el programa mide solo.
Con la IA, cada web además recibe un **puntaje de 10 a 100** según tu rúbrica, y
**oportunidades** y **puntos fuertes** redactados para tu mensaje de venta.

Para activarla:

1. Crea una clave en <https://console.anthropic.com> (pide un medio de pago).
2. Abre el archivo `.env` de la carpeta del programa (ver sección 7) y pega la clave en
   `ANTHROPIC_API_KEY=` (empieza con `sk-ant-`).
3. Cierra y vuelve a abrir el programa.

Costo aproximado: unos US$0,15 por web con `claude-opus-5-5`, o la mitad con `claude-sonnet-5-5`
(se cambia en `LLM_MODEL`). Solo se gasta al revisar webs nuevas o al pulsar Re-analizar.

---

## 7. Configuración (archivo .env)

El programa funciona sin configurar nada. Si quieres cambiar algo:

1. En la carpeta del programa, abre el archivo `.env` con el Bloc de notas (el instalador lo
   crea; si no está, copia `.env.example` y renombra la copia como `.env`).
2. Cambia lo que necesites y guarda.
3. Cierra y vuelve a abrir el programa.

Lo más útil:

| Opción | Para qué | Valor actual |
|---|---|---|
| `ANTHROPIC_API_KEY` | Activa la IA (vacía = apagada, costo $0) | vacía |
| `SEARCH_LIMIT_DEFAULT` | Cuántos leads se piden por defecto | 20 |
| `DEFAULT_REGION` | País para leer teléfonos sin código (CL Chile, AR Argentina…) | CL |
| `DELAY_MIN_SECONDS` / `DELAY_MAX_SECONDS` | Pausas en Google Maps. Súbelas si Google bloquea seguido | 2 / 6 |
| `HEADLESS` | `false` = se ve la ventana de Maps. **Déjalo en false**: sin ventana, Google oculta el número de reseñas | false |
| `WEB_RETRIES` | Reintentos si una web falla un momento | 1 |
| `REANALYZE_AFTER_DAYS` | Días antes de volver a revisar una web | 30 |

El archivo `.env` es privado: nunca lo compartas (ahí va tu clave de IA).

---

## 8. Si algo falla

1. **Mira el registro**: en la página, arriba a la derecha, **Registro**. Ahí está todo lo que
   hizo el programa hoy, con la hora. Las líneas en rojo son errores, con su detalle técnico.
   Sácale una captura y envíasela a quien te ayude con el programa.
2. Problemas comunes:

| Lo que ves | Qué hacer |
|---|---|
| "Google bloqueó la búsqueda" | Esperar una hora o más. Si pasa seguido, subir las pausas (sección 7) |
| "Se cerró la ventana del navegador" | No cierres la ventana de Chromium mientras busca. Vuelve a buscar: lo encontrado quedó guardado |
| "Parece que no hay conexión a internet" | Revisa tu internet y vuelve a buscar |
| "Falta instalar el navegador del programa" | Vuelve a ejecutar el instalador (sección 0): se instala encima sin borrar tus datos |
| Un aviso de Windows dice que no pudo abrirse | Ciérralo, espera unos segundos y vuelve a abrirlo. Si se repite, abre el registro del día en `data\logs` (sección 9) |
| No se abre nada | Vuelve a ejecutar el instalador. Si sigue igual, abre **Abrir Vortexia Prospector.bat** (en la carpeta del programa): muestra el error en una ventana negra |
| Un negocio quedó sin revisar | Pulsa **Re-analizar web** en su informe, o **Revisar webs pendientes** en la lista |

Las webs que fallan un momento (no responden, error del servidor) se reintentan solas una
vez. Una web que tarda demasiado (más de 2 minutos) se corta y la búsqueda sigue con la siguiente.

---

## 9. Tus datos y copias de seguridad

Todo está en la carpeta `data`, dentro de la carpeta del programa (instalado:
`%LOCALAPPDATA%\Programs\Vortexia Prospector\data`; pega esa ruta en la barra del Explorador de archivos):

- `prospector.db`: tus leads, búsquedas, contactados y análisis.
- `screenshots`: las capturas de las webs (unos 0,7 MB por web).
- `logs`: el registro de cada día (se guardan los últimos 30 días).

**Copia de seguridad:** con el programa cerrado, copia la carpeta `data` completa a otro lugar
(un pendrive o Google Drive). Para restaurarla, vuelve a ponerla en su lugar.

---

## 10. Cómo comprobar que los datos son reales

El programa nunca inventa datos: si algo no se puede confirmar, queda vacío. Para revisarlo tú
mismo en un negocio:

1. En su informe, pulsa **Abrir en Maps** y compara el nombre, el teléfono, la web, el rating y
   las reseñas.
2. Pulsa **Abrir web** y busca el botón de WhatsApp: en **WhatsApp encontrados** está el enlace
   exacto y en qué parte de la web estaba (botón flotante, menú, pie de página…).
3. Revisa uno o dos **problemas**: abre su evidencia técnica y compruébalo en la web (por
   ejemplo, el año del pie de página o si falta el botón de WhatsApp en el celular).

---

## 11. Pasar el programa a otro computador

1. En el computador nuevo, instálalo con el botón **Descargar para Windows** (sección 0).
2. Con el programa cerrado en ambos computadores, copia la carpeta `data` del computador
   antiguo dentro de la carpeta del programa del nuevo (reemplazando la que hay). Así te
   llevas tus leads, contactados, capturas y registros.

---

## 12. Comandos (para usos avanzados)

Todos se escriben en PowerShell dentro de la carpeta del programa:

| Comando | Qué hace |
|---|---|
| `uv run python -m app.cli serve` | Abre la página (lo mismo que el acceso directo) |
| `uv run python -m app.cli run "cerrajeros en Rancagua" --limit 10` | Busca leads sin abrir la página |
| `uv run python -m app.cli analyze https://ejemplo.cl` | Revisa una web suelta y muestra sus problemas |
| `uv run python -m app.cli facts https://ejemplo.cl` | Muestra todo lo que se mide de una web |
| `uv run python -m app.cli import-contactados archivo.txt` | Agrega contactados desde un archivo de texto |
| `uv run pytest` | Revisa que el programa funcione bien (pruebas automáticas) |
