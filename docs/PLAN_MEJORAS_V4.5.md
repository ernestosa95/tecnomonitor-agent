# Plan de mejoras — TecnoMonitor Agent v4.5

Este plan es un documento de planificación, no una implementación. No se tocó código para
producirlo. Se construyó cruzando tres fuentes:

1. El código actual del agente (`agent_logic.py`, `main_gui.py`, `security.py`,
   `headless_service.py`, `service_control.py`, `mirth_collector.py`, `web/`).
2. La revisión previa de robustez/performance/seguridad (ver [SEGURIDAD.md](./SEGURIDAD.md)
   y el resto de `/docs`).
3. El **contrato de ingesta del servidor** (`82f8acf1-10contratoingestaagente.md`, provisto por
   el usuario, `2026-09-10`) — qué campos exige, qué hace con cada uno, y qué detector de
   `alerts_engine` lo consume.

El cruce con el contrato del servidor cambia la prioridad de varias cosas: aparecen **dos
defectos funcionales confirmados en el código actual** — no hipótesis, están verificados línea
por línea — que hacen que categorías enteras de alertas críticas (ventiladores, fuentes de
alimentación, RAID) nunca se disparen hoy, sin que nada en los logs del agente lo delate. Van
primero en la lista porque tienen impacto clínico/operativo directo y el fix es acotado.

---

## 0. Resumen ejecutivo — qué priorizar primero

| # | Hallazgo | Impacto | Esfuerzo | Categoría |
|---|---|---|---|---|
| 1 | ✅ `physical_layer.storage` debería ser `physical_layer.storage_layer` | Alertas RAID (`LOGICAL_VOLUME`/discos físicos) **nunca se evalúan** hoy | Bajo | Corrección/Contrato — **resuelto en agente, ver §1.1** |
| 2 | ✅ Sensores iDRAC (temp/fans/PSU) reportan `status: "OK"` hardcodeado | Alertas `FAN_<name>` y `PSU_<name>` **nunca pueden disparar** (siempre ven "OK") | Bajo | Corrección/Contrato — **resuelto en agente, ver §1.2** |
| 3 | ✅ Bypass de autenticación local de la GUI (funciones expuestas antes del login) | Cualquier proceso local podía leer credenciales descifradas | Medio | Seguridad — **resuelto en la migración a pywebview, ver §3.1** |
| 4 | ✅ Contraseña de admin hardcodeada e igual en todos los hospitales | Compromiso de una instalación compromete todas | Bajo–Medio | Seguridad — **resuelto, ver §3.2** |
| 5 | Preparar el agente para auth obligatoria en `schema_version 4.5` del lado servidor | Bloqueante para poder subir de versión de esquema sin romper ingesta | Medio | Contrato/Seguridad |
| 6 | ✅ Un ítem de `application_metrics` mal formado tira abajo **todo el reporte** (incluye infraestructura) | Pérdida de telemetría de infraestructura por un problema de datos de negocio | Medio | Robustez/Contrato — **resuelto (mitigación local), ver §1.3** |
| 7 | ✅ ElasticSearch por HTTP plano (opcional ahora) + `verify=False` generalizado | Credenciales e integridad de datos expuestas en la LAN | Medio | Seguridad — **HTTPS opcional resuelto, ver §3.3; `verify=False` de fondo sigue en §3.4** |
| 8 | ✅ Sin tope de memoria/tiempo en paginación de logs de Elastic | Ciclo puede colgarse o crecer sin límite tras una caída larga | Bajo–Medio | Robustez — **resuelto, ver §4.2** |
| 9 | ✅ Hilos WMI que exceden el timeout de 90s quedan huérfanos | Fuga de hilos/objetos COM en entornos con equipos lentos | Medio | Robustez — **telemetría resuelta (no cancelación), ver §4.3** |
| 10 | ✅ Permisos de `secret.key`/`monitor_config.json` sin endurecer | Cualquier usuario local con acceso a `ProgramData` puede descifrar credenciales | Bajo | Seguridad — **resuelto, ver §3.5** |

El resto del documento desarrolla cada punto y agrega los de menor prioridad.

---

## 1. Corrección funcional — paridad agente ↔ contrato del servidor

Esta sección es nueva respecto de la revisión anterior: solo se pudo detectar comparando línea
por línea el JSON que arma `agent_logic.py` contra lo que el contrato dice que
`alerts_engine` efectivamente lee. Son bugs de hoy, no de diseño de v4.5, pero conviene
resolverlos como parte del mismo release porque tocan el mismo archivo/lógica.

### 1.1 ✅ RESUELTO — `physical_layer.storage` vs `physical_layer.storage_layer`

**Estado:** corregido en el agente. Pendiente confirmar del lado servidor que el dashboard ya
muestra estado de RAID con esta clave (ver checklist, §8).

**Era así antes del fix** (`agent_logic.py:1455`):
```python
reporte["physical_layer"]["storage"] = obtener_storage_fisico_v3(idrac_cfg, log_callback)
```

El contrato (§4.3) especifica que el motor de alertas busca la clave
**`physical_layer.storage_layer`** para evaluar `logical_volumes[].status` y
`physical_drives[].status`. Como el agente escribe `storage` (sin `_layer`), esa clave nunca
existe donde el servidor la busca: **las alertas de RAID/discos físicos están muertas en
producción**, aunque el agente sí está recolectando correctamente los datos de iDRAC
(`obtener_storage_fisico_v3` funciona bien — el problema es solo el nombre de la clave en el
envelope final).

- **La forma interna del objeto ya coincide** con lo que espera el contrato
  (`logical_volumes[].{name,status}`, `physical_drives[].{slot,status}` están presentes, con
  campos extra que el contrato ignora sin problema al no tener schema Pydantic estricto para
  este bloque).
- **Fix aplicado:** se renombró la clave a `storage_layer` en `ejecutar_ciclo_agente`, sin
  período de transición con doble clave (decisión tomada: el nombre viejo `storage` nunca fue
  leído por el servidor, así que no hay consumidor que perder). `obtener_storage_fisico_v3()`
  no cambió — la forma interna del objeto ya coincidía con lo que espera el contrato.

### 1.2 ✅ RESUELTO — Sensores iDRAC con `status` hardcodeado en `"OK"`

**Estado:** corregido en el agente. Pendiente validar contra un iDRAC real con una fan/PSU
degradada para confirmar que la alerta efectivamente dispara (ver checklist, §8).

**Era así antes del fix** (`agent_logic.py:696-721`, función `obtener_sensors_idrac`):
```python
for t in d.get("Temperatures", []):
    sensors["temperatures"].append({..., "status": "OK"})   # <- literal, no lee Redfish
for f in d.get("Fans", []):
    sensors["fans"].append({..., "status": "OK"})           # <- literal
for ps in d.get("PowerSupplies", []):
    sensors["power"]["supplies"].append({..., "status": "OK"})  # <- literal
```

Redfish sí expone salud real por sensor en `Status.Health` (`t.get("Status", {}).get("Health")`,
igual que ya se hace correctamente para `controllers`, `logical_volumes` y `physical_drives` en
`obtener_storage_fisico_v3`). Acá el valor nunca se lee: cada temperatura, fan y fuente de
alimentación se reporta como `"OK"` sin importar su estado real.

El contrato (§4.2) es explícito: `FAN_<name>` y `PSU_<name>` son alertas **todo-o-nada** —
"cualquier `status` distinto de exactamente `"OK"` dispara CRITICAL". Con el valor
hardcodeado, **esas dos categorías de alerta no pueden dispararse jamás desde este agente**,
sin importar qué tan mal esté un ventilador o una fuente físicamente. Es, en la práctica, un
sensor de hardware crítico que reporta "todo bien" sin haber mirado el dato.

(Nota: la alerta `TEMP_<name>` sí funciona hoy, porque se basa en el `value` numérico —
`ReadingCelsius`, que sí se lee correctamente — contra un umbral configurable, no en el campo
`status`.)

- **Fix aplicado:** ahora se lee `t.get("Status", {}).get("Health", "Unknown")` (mismo patrón ya
  usado en storage) en vez del literal `"OK"`, para `temperatures`, `fans` y `power.supplies`.
  Este cambio solo **activa** una alerta que estaba silenciosamente apagada, no cambia la forma
  de los datos que ya se enviaban. Consecuencia esperada a validar en campo: un sensor sin
  `Status.Health` poblado por el firmware pasa a reportar `"Unknown"` (antes `"OK"` por default),
  lo que el servidor trataría como distinto de `"OK"` y por lo tanto alertable — deseable según
  el criterio del contrato, pero a confirmar contra hardware real (ver checklist, §8).

### 1.3 ✅ RESUELTO (alternativa de bajo esfuerzo) — Reporte "todo o nada": un dato de negocio malo tumba la telemetría de infraestructura

**Estado:** se implementó la alternativa de bajo esfuerzo descripta más abajo (validación local
en `extraer_metricas_sql`), no el desacople en dos `POST` — esa sigue pendiente y requeriría
coordinar con el servidor. Además del riesgo genérico, se identificó y cubrió un caso concreto
y más probable que el resto: si una de las tres sub-consultas (`ris`/`pacs`/`users`) no matchea
ninguna fila, SQL Server devuelve `NULL` para esa columna JSON (no `"[]"`), lo que tras
`json.loads()` deja el campo en `None` en vez de lista vacía — motivo más probable de rechazo
que un tipo de dato exótico. Se normaliza `None` → `[]` y, para el resto de los casos, se agregó
una validación estructural (`_validar_application_metrics`) que revisa los campos exigidos por
el contrato en cada ítem de `ris`/`pacs`/`users`. Si algo no calza, se loguea el detalle exacto
(índice + campo) y se devuelve `None` desde `extraer_metricas_sql` — mismo camino que ya existía
para "sin datos este ciclo": el resto del reporte se envía igual, sin `application_metrics`, y
el checkpoint no avanza (se reintenta el mismo bloque en el próximo ciclo).

El contrato es explícito (§6): si se manda `application_metrics`, **cada ítem de cada lista
exige todos sus campos** vía validación Pydantic estricta, y "falta un campo tira abajo *todo
el reporte*, no solo ese ítem" (HTTP 500 genérico, sin detalle de qué campo falló).

El agente arma un único JSON con `physical_layer` + `virtual_layer` + `software_monitoring` +
`application_metrics` y lo manda en un solo `POST` (`ejecutar_ciclo_agente`). Si por cualquier
motivo la extracción SQL devuelve un valor inesperado que no calza con el schema estricto del
servidor (ej. un cambio de esquema en la base de Extensa que introduce un `NULL` no cubierto
por los `ISNULL(...)`/`CASE...ELSE 0` actuales, o un tipo de dato distinto al esperado), el
`POST` completo rebota con 500 — y con él, **también se pierde en ese ciclo la telemetría de
infraestructura, WMI, sensores, Mirth, SSL, etc.**, que no tenían nada que ver con el problema.

Peor: como el checkpoint SQL solo avanza tras un envío exitoso, si el bloque problemático
persiste (el dato de origen no cambia), **el agente reintenta el mismo bloque roto en cada
ciclo indefinidamente**, bloqueando todo el reporte una y otra vez hasta que alguien
intervenga manualmente.

- **Propuesta para v4.5:** desacoplar el envío de `application_metrics` del resto del envelope
  (dos `POST` independientes, o al menos armar y validar `application_metrics` por separado
  antes de adjuntarlo, descartándolo con un log claro si no pasa una validación local mínima,
  en vez de dejar que tumbe todo el reporte). Requiere alinear con el equipo de servidor si el
  endpoint puede aceptar el envelope sin `application_metrics` como éxito parcial, o si hace
  falta un segundo endpoint.
- Alternativa de menor esfuerzo, sin tocar el servidor: agregar una validación local defensiva
  en `extraer_metricas_sql` (o antes de adjuntar el payload al envelope) que verifique tipos y
  nulabilidad esperada, y si algo no calza, **loguearlo con el detalle exacto del registro
  problemático y omitir `application_metrics` de ese ciclo** en vez de dejar que el servidor lo
  rechace a ciegas. Esto no arregla la pérdida de KPIs de ese ciclo, pero evita perder el resto
  de la telemetría y da visibilidad real de qué dato rompió el contrato (el 500 del servidor no
  dice cuál).

### 1.4 🟡 `hospital_id` sin validación de existencia previa

El contrato (§3) advierte: si `hospital_id` no coincide con uno ya cargado en
`hospitales_metadata` del panel, **el reporte se guarda pero el motor de alertas no evalúa
nada para ese hospital** — sin ningún error visible ni para el agente ni, aparentemente, para
quien mira el dashboard esperando alertas.

Hoy no hay ninguna verificación de este tipo en el agente: es un campo de texto libre en la GUI
(`hosp_id`) sin validación contra el servidor. Un typo al configurar un hospital nuevo produce
una falla **silenciosa y total** del sistema de alertas para ese sitio, indistinguible de "todo
está bien" salvo por la ausencia de datos en el dashboard.

- **Propuesta:** si el servidor central expone (o puede exponer) un endpoint de verificación de
  `hospital_id` válido, agregar esa validación al botón "Test Conexión Central" de la GUI
  (`probar_conexion_central`), para detectar el problema en el momento de configurar, no meses
  después. Si no existe tal endpoint, dejarlo documentado como ítem a coordinar con el equipo
  de servidor — no es resoluble solo del lado agente.

### 1.5 🟡 Supuesto de cadencia entre agente y `alerts_engine`

Dos detectores del servidor asumen una cadencia de reporte que no necesariamente coincide con
`interval_minutes` del agente (default 5 minutos, configurable por hospital):

- **Mirth** (§7.1): el debounce de `STOPPED`/`ERROR`/`PAUSED` es "dos ticks seguidos (~2
  minutos, filtra micro-cortes)" — ese "~2 minutos" solo tiene sentido si el agente reporta
  aproximadamente cada 1 minuto. Con el default real de 5 minutos, "dos ticks" son ~10 minutos
  antes de alertar, no 2. No es un bug del agente, pero es una discrepancia de diseño entre
  ambos lados que conviene resolver explícitamente antes de v4.5 (¿el servidor debería
  parametrizar el debounce por tiempo real en vez de por "ticks"? ¿o el agente debería
  recomendar/forzar un `interval_minutes` menor cuando Mirth está habilitado?).
- **KPI de inactividad RIS/Mamografía** (§6): el detector suma `admitidos` "en la ventana
  configurada" del panel — sin ver esa configuración no se puede confirmar si coincide con el
  tamaño de bloque que arma el agente (`24 / executions_per_day` horas, ej. bloques de 8h con
  3 ejecuciones/día). Si la ventana del servidor es más chica que el bloque del agente, el KPI
  podría evaluarse con datos parcialmente vencidos o duplicados de una ventana a otra.

- **Propuesta:** antes de v4.5, sincronizar con quien mantiene el `alerts_engine` qué cadencia
  asume cada detector, y documentar (acá o en el contrato) la relación esperada entre
  `interval_minutes`/`executions_per_day` del agente y las ventanas de cada alerta.

### 1.6 🟡 `collection_meta` no está descrito en el contrato de ingesta del servidor

Al armar [CONTRATO_AGENTE.md](./CONTRATO_AGENTE.md) (contraparte del contrato de ingesta,
pensado para pasarle al equipo de servidor) se confirmó que el agente manda, en la raíz de
cada reporte, una clave `collection_meta` con el estado (`enabled`/`status`, y campos extra
según el módulo) de cada uno de los 8 módulos — y que `10-contratoingestaagente.md` no la
menciona en ningún lado. Como el envelope no tiene schema Pydantic estricto (`Dict[str, Any]`
según ese mismo contrato), lo más probable es que hoy se guarde sin usarse.

- **Propuesta:** confirmar con el equipo de servidor si `collection_meta` se lee o se ignora
  hoy. Si se ignora, evaluar si conviene empezar a usarla para distinguir "módulo apagado" de
  "módulo activo sin datos" de "módulo con error" en el dashboard — el agente ya manda esa
  distinción hecha, no haría falta inferirla de otra forma del lado servidor.

---

## 2. ✅ HECHO — Preparación para autenticación obligatoria (`schema_version 4.5`)

**Estado:** `schema_version` ya se bumpeó a `"4.5"` en `agent_logic.py` (junto con `agent_version`
y el `AppVersion` del instalador, ver [CHANGELOG.md](./CHANGELOG.md#v450)). La versión
actualizada del contrato de ingesta (`2026-09-11`) confirma por escrito que el gate de token ya
está **"Implementado y desplegable"** del lado servidor — ya no es solo una confirmación verbal
del usuario, hay un documento fechado que lo respalda. Sigue valiendo la pena releer los puntos
1-5 de abajo antes de distribuir el build a un hospital nuevo (en particular el punto 3:
confirmar que ese hospital tiene un `auth_token` real, generado desde el panel del servidor
para ese hospital específico — ver [CONFIGURACION.md](./CONFIGURACION.md#auth_token--de-dónde-sale-y-por-qué-tiene-que-coincidir-con-hospital_id)).

El contrato (§2bis) es explícito: a partir de `schema_version: "4.5"`, el servidor **exige**
`Authorization: Bearer <token>` único por hospital, y rechaza (401) si falta, no existe, o no
corresponde al `hospital_id` declarado — **sin decir cuál de los tres motivos fue**. El token se
genera desde el panel de administración del servidor (alta del hospital, o
`POST /api/hospitales-metadata/{hid}/regenerar-token` para uno ya existente) — nunca del lado
del agente.

Buenas noticias: el agente **ya envía** `Authorization: Bearer {auth_token}` en cada request
(`agent_logic.py:1573`), con el token guardado cifrado (`security.py`) y configurable por la
GUI. La mecánica de transporte ya existe; lo que falta es el **cambio de gobierno de versión**.

### Riesgos concretos a mitigar antes de bumpear a `"4.5"`

1. **No hardcodear `"4.5"` a ciegas** en `ejecutar_ciclo_agente` como se hizo con `"4.3"` hoy.
   El contrato es tajante: mandar `schema_version: "4.5"` contra un servidor que todavía no
   implementó la validación de token produce, en el mejor caso, un rechazo consistente si el
   servidor ya empezó a exigirlo parcialmente, y en el peor caso (si el servidor cae al branch
   `else` por algún motivo) el payload se interpretaría como **formato legacy V2** y se
   corrompería en silencio (§2 del contrato). El costo de un bump prematuro es alto y el
   síntoma es confuso.
2. **Confirmación explícita del lado servidor** antes de liberar el build de v4.5 al primer
   hospital: el propio contrato dice que la referencia autoritativa de si ya está desplegado es
   el documento `11-plan-auth-ingesta-agente.md` del lado servidor. El plan de release de v4.5
   del agente debe incluir un paso de coordinación explícito con quien mantiene ese servidor,
   no asumir que "ya debe estar listo" porque el agente ya está preparado.
3. **Confirmar que el `auth_token` guardado por cada hospital corresponde al token que el panel
   de administración generó para ese `hospital_id`** — el campo ya existe en la config
   (`auth_token`), pero fue pensado originalmente sin la semántica de "token único emitido por
   el panel, no reusable entre hospitales" que describe el contrato. Antes de exigir el header
   en 4.5, auditar/re-emitir tokens por hospital si hoy hay valores placeholder, compartidos, o
   vacíos en instalaciones ya desplegadas.
4. ✅ **RESUELTO — Manejo explícito del 401** en `ejecutar_ciclo_agente`: se agregó un
   `except requests.exceptions.HTTPError` antes del genérico que, si `status_code == 401`,
   loguea un mensaje distinguible ("token rechazado o no corresponde al hospital_id") y lo
   devuelve como `http_status: 401` en el resultado, en vez de caer en el mismo bloque que una
   caída de red. Cualquier otro código HTTP de error también queda con su `http_status`
   explícito.
5. ✅ **RESUELTO — Plan de rollback**: `SCHEMA_VERSION_OVERRIDE_FILE`
   (`ProgramData\TecnoMonitor\schema_version_override.txt`), no expuesto en la GUI. Si existe,
   `_schema_version_efectiva()` usa su contenido en vez de la versión normal — crear ese archivo
   con `"4.3"` adentro fuerza el rollback sin recompilar; borrarlo vuelve al comportamiento
   normal. `agent_version`/`schema_version` ahora también salen de una única fuente
   (`AGENT_VERSION`/`SCHEMA_VERSION`, leídas de `/VERSION` — ver §6).

Este punto no es "código a escribir" tanto como **gobierno de release**: la parte técnica ya
está resuelta (envío del Bearer token), lo que falta es el proceso de corte coordinado.

---

## 3. Seguridad

Retomado de la revisión previa ([SEGURIDAD.md](./SEGURIDAD.md)), con la autenticación
servidor↔agente ya cubierta en la sección 2. Prioridad sugerida para v4.5:

### 3.1 ✅ RESUELTO — Bypass de autenticación local de la GUI (alto)

**Estado:** resuelto como parte de la migración de Eel a pywebview (v4.6, ver §9.1). Cada
método sensible de la clase `Api` (`main_gui.py`) está decorado con `_requiere_sesion`, que
verifica una bandera `self._autenticado` en Python antes de ejecutar cualquier acción — la
misma clase de problema existía en pywebview (todo método público de `js_api` es invocable
desde JS apenas carga la ventana), así que se aprovechó la reescritura completa de la API
expuesta para cerrarlo. La bandera se pone en `True` recién cuando `verificar_clave` confirma
el código correcto (o, la primera vez que corre el equipo, al generarse el código nuevo — no
tiene sentido pedir loguearse con un código que se acaba de mostrar en pantalla). Verificado con
pruebas automatizadas: un método sensible llamado antes de autenticar devuelve
`{"ok": False, "error": "no_autenticado"}` en vez de ejecutar la acción.

### 3.2 ✅ RESUELTO — Contraseña de admin hardcodeada y compartida (alto)

**Estado:** implementado. `security.py` genera un código único por instalación
(`generar_codigo_acceso`, vía `secrets`) la primera vez que se necesita, persiste solo su hash
SHA-256 (`admin.hash`), y `main_gui.py` lo muestra una única vez en el overlay
(`estado_acceso_gui`). Se agregó además un lockout en Python (no solo JS): 5 intentos fallidos
consecutivos bloquean `verificar_clave` por 60 segundos. Recuperación de acceso: borrar
`admin.hash` regenera un código nuevo — sin clave maestra alternativa (ver
[SEGURIDAD.md](./SEGURIDAD.md#acceso-a-la-gui-main_guipy--webindexhtml)). No incluye una función
de "cambiar código" en caliente. El bypass de Eel/pywebview (§3.1) se resolvió aparte, en la
migración a pywebview. ✅ La función de "cambiar código" en caliente sí se agregó después
(`security.regenerar_codigo_acceso` + botón en la GUI, requiere sesión ya iniciada) — sigue sin
existir una clave maestra alternativa, la única recuperación ante pérdida del código sigue
siendo borrar `admin.hash` a mano.

### 3.3 ✅ RESUELTO — ElasticSearch por HTTP plano (medio-alto)

**Estado:** se agregó un checkbox "Usar HTTPS" en la tarjeta de Elastic de la GUI
(`elastic.use_https`, default `false` — retrocompatible, cero cambio de comportamiento para
configs existentes). `_esquema_elastic()` centraliza la decisión y se usa en los 6 puntos de
`agent_logic.py` que arman una URL de Elastic. Se agregó `verify=False` a esos mismos requests
(mismo criterio ya aceptado para iDRAC/central, necesario porque el caso típico es un
certificado autofirmado del propio clúster interno del hospital, no uno de una CA pública) —
ver §3.4, que sigue como el ítem general de fondo sobre `verify=False`.

### 3.4 `verify=False` generalizado (medio)
Evaluar, por integración, si tiene sentido ofrecer una opción de CA propia/pinning en vez de
desactivar la validación por completo siempre — particularmente para el envío al servidor
central, que es el canal que transporta el envelope completo de cada hospital.

### 3.5 ✅ RESUELTO — Endurecer permisos de `ProgramData\TecnoMonitor` (bajo esfuerzo, alto valor)

**Estado:** `security._endurecer_permisos()` corre `icacls` (SID bien conocido de
Administradores, `S-1-5-32-544`, en vez del nombre localizado que varía por idioma de Windows)
la primera vez que se crea la carpeta, restringiéndola a `SYSTEM` + Administradores. Best-effort
y silencioso: si `icacls` falla o no está disponible, la carpeta sigue funcionando con los
permisos heredados de antes — no bloquea el arranque del agente.

### 3.6 ✅ RESUELTO — Integridad de `rules.json` (bajo)

**Estado:** `_rules_json_integro()` compara el SHA-256 real de `rules.json` contra
`rules.json.sha256` (generado por `build.bat` en cada compilación, con `Get-FileHash`) antes de
cargar las reglas. Retrocompatible: si no existe el archivo de checksum, se omite el chequeo
(builds de antes de este cambio). Si existe y no coincide, se tratan las reglas como no
confiables y se omiten ese ciclo (fail-safe) en vez de usarlas a ciegas, con el motivo logueado.

---

## 4. Robustez

Retomado de la revisión previa, más el hallazgo nuevo de la sección 1.3:

### 4.1 Reporte "todo o nada" ante datos de negocio inválidos — ver §1.3 (alto, nuevo)

### 4.2 ✅ RESUELTO — Paginación de Elastic sin tope (medio)

**Estado:** `recolectar_logs_elastic` corta a los 200 páginas o 60s de pared (lo que ocurra
primero), lo que suceda primero. El checkpoint (`newest_ts`) solo avanza hasta el último
documento efectivamente procesado, así que cortar acá no pierde nada — lo que quede afuera del
corte se retoma en el próximo ciclo, igual que ya pasaba con cualquier otro corte temprano.

### 4.3 ✅ RESUELTO (telemetría, no cancelación) — Hilos WMI/SSH huérfanos tras timeout (medio)

**Estado:** no hay forma segura de cancelar un hilo de Python a mitad de una llamada WMI/SSH
colgada, así que se implementó la alternativa que proponía este mismo ítem: un contador global
(`_hilos_recoleccion_vm_activos`, con lock) de hilos de recolección vivos en simultáneo. Al
cumplirse el timeout de 90s, el log muestra cuántos hilos siguen activos (incluidos huérfanos de
timeouts previos) y, si supera `UMBRAL_ALERTA_HILOS_HUERFANOS` (10), agrega una advertencia
explícita de posible fuga — antes esto solo se habría notado como un problema de memoria/CPU
genérico, sin poder rastrearlo hasta este módulo.

### 4.4 ✅ RESUELTO — `except Exception: pass` sin registro alguno (bajo)

**Estado:** `save_checkpoint`, `get_last_checkpoint`, `reset_checkpoint` y el guardado de
checkpoint de Elastic en `ejecutar_ciclo_agente` ahora aceptan un `log_func` opcional y lo usan
para poder diagnosticar por qué un checkpoint dejó de avanzar sin tener que instrumentar el
código en el momento del incidente.

### 4.5 ✅ RESUELTO — GUI dependiente de CDN externo (bajo)

**Estado:** Bootstrap 5.3.0 y Font Awesome 6.0.0 (CSS + JS + los 8 archivos de `webfonts/`) se
descargaron una vez y quedaron empaquetados en `web/vendor/` — `index.html` los referencia por
ruta relativa en vez de `cdn.jsdelivr.net`/`cdnjs.cloudflare.com`. Como todo `web/` ya se
empaqueta con `--add-data "web;web"`, no hizo falta tocar `build.bat` para esto. Verificado con
la ventana pywebview real: los modales (que dependen del JS de Bootstrap) siguen abriendo y
cerrando igual con los assets locales.

---

## 5. Performance

### 5.1 🟡 Query SQL con `OR` sobre columnas de fecha distintas (medio) — sigue pendiente

`SQL_QUERY` en `agent_logic.py` filtra con `OR` sobre 7 columnas de fecha distintas, lo que
típicamente impide el uso eficiente de índices por columna en tablas grandes. **Deliberadamente
no se tocó en esta pasada**: es la única consulta que toca datos clínicos de producción
(RIS/PACS) directamente, y una reescritura mal probada ahí no se detecta con ningún test que se
pueda correr sin acceso a un SQL Server real con el schema de Extensa — el propio ítem ya pedía
coordinar con quien administra esa base antes de tocarla. Si el tiempo de extracción se vuelve
un problema medible en algún hospital con volumen alto, ahí sí vale la pena evaluar reescribir
como `UNION` de sub-consultas indexadas por cada fecha.

### 5.2 ✅ RESUELTO — Concurrencia alta contra iDRAC (bajo-medio)

**Estado:** bajado de 10 a 4 *workers* en el segundo `ThreadPoolExecutor` de
`obtener_storage_fisico_v3` (volúmenes lógicos + discos físicos). De paso se simplificó el uso
de `ThreadPoolExecutor` en esa función a la versión ya importada a nivel de módulo, en vez de
`concurrent.futures.ThreadPoolExecutor` con un `import concurrent.futures` local redundante.

---

## 6. Deuda técnica / higiene de versión

- **✅ HECHO — Consolidar el versionado**: nuevo archivo `/VERSION` (contenido: `4.5.0`) es la
  única fuente de verdad. `agent_logic.py` lo lee al importar (`AGENT_VERSION`, y
  `SCHEMA_VERSION` derivado como major.minor); `build.bat` lo lee a una variable de entorno y se
  lo pasa a `TecnoMonitor.iss` como macro del preprocesador (`/DMyAppVersion=...`), que a su vez
  lo usa en `AppVersion` y en el nombre del instalador. Cambiar la versión de acá en más es
  editar `/VERSION` una sola vez.
- **✅ RESUELTO — Retirar o actualizar `Compiler.txt`**: reescrito para reflejar los comandos
  reales de `build.bat` (`--onedir` para el servicio, sin `proxmoxer`, con los
  `--hidden-import`/`--add-data` actuales de pywebview/paramiko/VERSION/rules.json.sha256),
  dejando claro que es solo una referencia rápida y que `build.bat` es la fuente de verdad.
- **✅ RESUELTO — `requirements.txt` ausente**: creado (incluye `pywebview`, `paramiko` y el
  resto de las dependencias, con marcadores `sys_platform == "win32"` para las que no aplican en
  un entorno de desarrollo no-Windows).

---

## 7. Secuencia sugerida para v4.5

No es un compromiso de fechas, es un orden de dependencias e impacto:

1. ✅ **Fixes de paridad con el contrato** (§1.1, §1.2) — hecho.
2. ✅ **Validación local antes de adjuntar `application_metrics`** (§1.3) — hecho.
3. **Coordinación con el equipo de servidor** sobre los supuestos de cadencia de Mirth/KPI
   (§1.5) — sigue pendiente, no bloquea nada de lo ya hecho.
4. ✅ **Bump a `schema_version: "4.5"`** — hecho, confirmado por el usuario que el servidor ya
   tiene desplegada la validación de token. No se implementó el flag de rollback interno que
   proponía el punto 5 de §2 (no se pidió) — si algo falla tras desplegar, la vía de reversión
   es recompilar con `schema_version` vuelto a `"4.3"` en `agent_logic.py`.
5. **Seguridad de la GUI local** (§3.1, §3.2) — no depende de nada del servidor. ✅ 3.2 hecho;
   3.1 (bypass de Eel) sigue pendiente.
6. **Resto de robustez/performance/seguridad** (§3.3–§3.6, §4, §5) — según capacidad, no son
   bloqueantes para el corte de versión pero conviene no acumularlos indefinidamente.
7. **Higiene de versión** (§6) — antes de compilar el primer build oficial de v4.5, para no
   heredar la misma inconsistencia de números de versión.

## 8. Checklist de validación antes de liberar v4.5

- [ ] Confirmar en un iDRAC real que `physical_layer.storage_layer` (ya renombrado) llega al
      servidor y que el dashboard efectivamente muestra estado de RAID.
- [ ] Forzar una falla real de fan/PSU en un servidor de laboratorio (o simular la respuesta de
      Redfish) y confirmar que la alerta `FAN_<name>`/`PSU_<name>` dispara con el fix de §1.2.
- [x] `_validar_application_metrics`/normalización probados con casos unitarios (None→[],
      campo faltante, tipo incorrecto, lista con forma equivocada) — ver §1.3. Pendiente:
      confirmar contra una extracción real de SQL Server con datos de un hospital.
- [x] Flujo de primera vez / persistencia / lockout / recuperación de `admin.hash` (§3.2)
      probado de punta a punta simulando reinicios de proceso reales.
- [ ] Confirmar con el equipo de servidor, por escrito, que la validación de token para
      `schema_version 4.5` está desplegada en producción antes de que cualquier hospital reciba
      el build que la activa.
- [ ] Probar el flujo de rollback de `schema_version` (§2, punto 5) en laboratorio antes de
      necesitarlo en producción.
- [ ] Revisar que ningún hospital tenga `auth_token` vacío o de prueba antes de activar la
      exigencia de token del lado servidor.

## 9. Más allá de v4.5 — anotado para diseñar más adelante (no bloquea nada de lo actual)

### 9.1 Un agente, múltiples sistemas monitoreados en la misma red

Hoy la arquitectura asume "un agente = una instalación": el `monitor_config.json` de un
hospital apunta a un único SQL Server/hipervisor/iDRAC como la instalación principal, aunque ya
existe un patrón de **múltiples objetivos remotos** para VMs/workstations (`vms[]`, recolectado
vía WMI sin necesitar un agente instalado en cada máquina — ver
[MODULOS.md](./MODULOS.md#wmi--vms-workstations-y-equipos-médicos-windows)).

Surgió la necesidad de extender esa misma idea a otros sistemas que conviven en la red del
hospital pero no son "la instalación principal" — el caso concreto mencionado es una **cache
DICOM** (un router/cache de imágenes separado del PACS principal). La idea es que un único
agente instalado reporte también sobre estos sistemas adicionales, sin necesitar una instalación
del agente por sistema.

**Decisión de diseño (2026-09-12):** a diferencia de `vms[]` (un array liviano de *targets* WMI
dentro de un mismo envelope), cada sistema adicional se va a monitorear **como si fuera un
hospital independiente**: su propia configuración de conexión (credenciales, host, protocolo),
su propio `hospital_id` y su propio `auth_token` emitido por el panel del servidor, y su propio
envelope/POST — no un objeto más colgado del envelope del hospital principal. La diferencia con
"un hospital = un agente físico" de hoy es solo que **un mismo proceso agente** pasa a ejecutar
el ciclo completo una vez por cada perfil configurado, en vez de una sola vez.

```
                    ┌─────────────────────────────────────────────────────────┐
                    │   Equipo Windows en el hospital (un único agente)        │
                    │                                                         │
                    │   monitor_config.json                                   │
                    │   ┌─────────────────────────────────────────────────┐   │
                    │   │ instalaciones: [                                 │   │
                    │   │   { hospital_id: "HOSP-A",        auth_token: A, │   │
                    │   │     enabled: true,                               │   │
                    │   │     sql_cfg, idrac_cfg, vms[], mirth_cfg, ... },  │   │
                    │   │   { hospital_id: "HOSP-A-DICOM",  auth_token: B, │   │
                    │   │     enabled: true,  sql_cfg (mismo patrón) },    │   │
                    │   │   { hospital_id: "HOSP-B", ...    auth_token: C, │   │
                    │   │     enabled: false }  ← nodo desactivado         │   │
                    │   │ ]  (hasta 3 perfiles, caso realista actual)      │   │
                    │   └─────────────────────────────────────────────────┘   │
                    │             ▲                                          │
                    │             │ lee cada ciclo                            │
                    │   ┌─────────┴─────────┐                                │
                    │   │TecnoMonitorService │                                │
                    │   │(headless_service.py)│                               │
                    │   └─────────┬─────────┘                                │
                    │             │ interval_minutes global, único tick;      │
                    │             │ por cada instalación con enabled=true      │
                    │             │ (secuencial, sin paralelismo entre perfiles):│
                    │             │   try:    ejecutar_ciclo_agente(inst_i)    │
                    │             │   except: log_func(inst_i, e); continue   │
                    │             │                                          │
                    │     ┌───────┼──────────────┬───────────────────┐       │
                    │     ▼       │              ▼                   ▼       │
                    │ [SQL/iDRAC/VMs...]   [SQL (mismo patrón      [saltado:  │
                    │    HOSP-A              que RIS/PACS)          enabled  │
                    │                        HOSP-A-DICOM           =false]  │
                    └──────┬──────────────────────┬───────────────────┴──────┘
                           │                      │             (sin POST —
                           │   HTTPS POST (Authorization:      nodo desactivado)
                           │    Bearer <auth_token_i>)
                           ▼                      ▼
                    ┌─────────────────────────────────────────────────────────┐
                    │              Servidor central TecnoMonitor                │
                    │  (recibe un reporte por cada perfil activo, uno por       │
                    │   hospital_id — HOSP-B simplemente no reporta este ciclo) │
                    └─────────────────────────────────────────────────────────┘
```

Implicancias de esta decisión sobre el código actual:

- **Reutiliza el 100% de la lógica de recolección existente** (`obtener_storage_fisico_v3`,
  `extraer_metricas_sql`, `obtener_vm_data`, `recolectar_logs_elastic`, etc.) sin cambios de
  forma — cada función ya recibe su config como parámetro, solo pasa a invocarse una vez por
  perfil en vez de una vez por ciclo. **No hace falta tocar el contrato con el servidor**: cada
  perfil arma y envía su propio envelope, `alerts_engine` no se entera de que comparten proceso.
- **Aislamiento de fallas por perfil**: si un perfil rompe (ej. credenciales SQL vencidas de la
  cache DICOM), el `try/except` alrededor de cada `ejecutar_ciclo_agente(inst_i)` tiene que
  evitar que tumbe el resto — hoy una excepción no controlada en el bucle mata el ciclo entero
  (ver §"Ciclo de vida del servicio" en [ARQUITECTURA.md](./ARQUITECTURA.md)).
- **Checkpoints por perfil**: `.sql_checkpoint`/`.elastic_checkpoint` hoy son archivos únicos;
  pasan a necesitar sufijo por `hospital_id` (`.sql_checkpoint_<hospital_id>`), porque cada
  perfil avanza su propio watermark de forma independiente.
- **`activity.log` por perfil**: con N ciclos intercalados en el mismo log, cada línea necesita
  un prefijo `[hospital_id]` para poder diagnosticar cuál perfil generó cada entrada.
- **GUI multi-perfil**: `main_gui.py`/`web/` pasan de editar un único formulario a necesitar un
  selector de perfil (agregar/editar/quitar) que muestre, para el perfil activo, las mismas
  pestañas que hoy (SQL, iDRAC, VMs, Mirth, SSL, RIS/Elastic), más un toggle **activo/inactivo**
  por perfil (`enabled`) — es la parte de mayor esfuerzo de UI de todo el cambio.
- **`secret.key` se sigue compartiendo** a nivel de instalación (un solo archivo por equipo
  Windows) — solo cambia la forma del JSON que cifra/descifra, de un objeto plano a un array de
  objetos.

**Decisiones tomadas (2026-09-12), cierran las preguntas que habían quedado abiertas:**

1. **Tecnología de la cache DICOM:** es la misma que ya se usa para RIS/PACS — SQL Server, mismo
   patrón que `sql_cfg`/`extraer_metricas_sql`. No hace falta soportar un tipo de perfil nuevo;
   el perfil de la cache DICOM reusa la misma forma de configuración (host, credenciales, query),
   solo apunta a otra base y viaja bajo su propio `hospital_id`/`auth_token`.
2. **Cadencia:** global — un único `interval_minutes` de servicio, todos los perfiles se recorren
   secuencialmente en cada tick (sin scheduler independiente por perfil). Sí se agrega un flag
   **`enabled` por perfil** (visible como toggle en la GUI) para poder desactivar el monitoreo de
   un nodo puntual sin borrar su configuración — el bucle salta los perfiles con `enabled: false`
   sin intentar el ciclo ni contarlos como error.
3. **Cantidad de perfiles:** hasta 3 en el caso realista actual. No justifica paralelizar entre
   perfiles (`ThreadPoolExecutor` a ese nivel) — secuencial alcanza; el paralelismo interno que ya
   existe por perfil (VMs, iDRAC) no cambia.

### 9.1.1 Migración de `monitor_config.json` existentes (formato plano → `instalaciones[]`)

Hoy hay hospitales reales en producción con `monitor_config.json` en el **formato plano actual**
(un solo objeto con `hospital_id`, `auth_token`, `sql`, `idrac`, `vms[]`, `mirth_servers[]`,
`elastic`, etc. — ver `cargar_config`/`guardar_config` en `main_gui.py` y
`cargar_config_segura` en `headless_service.py`). El cambio a `instalaciones[]` no puede asumir
que alguien va a re-configurar cada hospital a mano desde la GUI: tiene que migrarse solo.

**Estrategia: migración transparente y autocurativa en el primer arranque tras actualizar.**

- **Detección:** si el JSON leído del disco **no tiene la clave `instalaciones`**, es formato
  viejo (plano).
- **Migración en memoria:** envolver el dict plano completo, tal cual viene del disco (todavía
  con las credenciales cifradas, antes de cualquier desencriptado), como el único elemento de
  `instalaciones[]`, agregando `"enabled": true`:
  ```python
  def migrar_config_legacy(data: dict) -> dict:
      if "instalaciones" in data:
          return data  # ya migrado
      return {"instalaciones": [{**data, "enabled": True}]}
  ```
- **Dónde vive:** en `agent_logic.py` (ya lo importan tanto `main_gui.py` como
  `headless_service.py`), para no duplicar la detección en los dos puntos de carga que hoy existen
  por separado — esto también es la oportunidad de unificar `_desencriptar_config`
  (`main_gui.py`) y el bloque de desencriptado inline de `cargar_config_segura`
  (`headless_service.py`), que hoy repiten la misma lista de campos dos veces en dos archivos.
- **Persistencia inmediata (self-healing):** tras migrar en memoria, reescribir
  `monitor_config.json` ya en formato nuevo con el mismo patrón de escritura atómica que usa
  `guardar_config` (`.tmp` + `os.replace`). Así la migración ocurre **una sola vez**, en el primer
  ciclo tras actualizar el binario — no en cada lectura, y no depende de que un admin abra la GUI
  y guarde para que el hospital quede en formato nuevo.
- **Efecto en cascada sobre encriptado/desencriptado:** `_desencriptar_config` y el encriptado en
  `guardar_config` hoy operan sobre campos de nivel superior (`config["sql"]["pass"]`, etc.). Con
  `instalaciones[]`, ese mismo bloque de campos a cifrar/descifrar pasa a repetirse **por cada
  perfil** (`for perfil in config["instalaciones"]: ...`) — es un cambio mecánico (mover el cuerpo
  existente adentro de un loop), no una reescritura de la lógica de cifrado en sí.
- **Riesgo a documentar, no a resolver ahora:** si algún día se hace rollback del binario a una
  versión anterior a este cambio después de que un hospital ya migró, esa versión vieja no
  entiende `instalaciones[]` y no va a poder leer su propia configuración. Mismo tipo de riesgo
  que ya está anotado para el rollback de `schema_version` (§2, punto 5) — vale la pena resolver
  ambos con el mismo mecanismo si se llega a implementar un plan de rollback formal.
- **Versionado a futuro:** para no depender para siempre de "detectar por ausencia de una clave"
  como única señal de migración, conviene agregar un campo explícito `config_version` (ej. `2`)
  al escribir el archivo migrado, para que una migración futura (ej. `instalaciones[]` cambia de
  forma otra vez) tenga una señal clara de qué versión está leyendo en vez de encadenar más
  detecciones heurísticas.

No es un ítem de la lista de v4.5 — con las tres decisiones tomadas, este diseño queda listo para
pasar a implementación cuando se priorice.

### 9.2 Monitoreo de equipos Linux en `vms[]` (hoy solo WMI/Windows)

Hoy `vms[]` asume Windows de punta a punta: `_recolectar_wmi_interno` conecta por WMI/DCOM
(puerto 135) y todo lo que arma (CPU, RAM, uptime, disco, servicios) sale de contadores
específicos de Windows (`Win32_OperatingSystem`, `Win32_PerfFormattedData_PerfDisk_LogicalDisk`,
`Win32_Service`, etc. — ver `agent_logic.py:1379-1509`). Cualquier equipo Linux del hospital
(nodos PACS, servidores ELK/Logstash, routers DICOM en Linux) queda fuera del alcance de este
módulo aunque conviva en la misma red que los equipos Windows ya monitoreados.

**Decisión de diseño (2026-09-12):** agregar **SSH** (vía `paramiko`) como el camino
equivalente a WMI para Linux, siguiendo el mismo patrón operativo que ya tiene `vms[]`: un
target remoto con sus propias credenciales en la config, sin instalar nada en el equipo
destino. Autenticación **solo usuario/contraseña por ahora** (mismos campos `user`/`pass` que
ya existen en la tarjeta de equipo, sin agregar credenciales nuevas a la GUI) — si en el futuro
algún hospital tiene un Linux con acceso por clave únicamente, se agrega como extensión sin
romper este diseño.

#### Config: un campo nuevo, retrocompatible

Cada entrada de `vms[]` suma `"os": "windows" | "linux"`. Si no está presente (todas las
configs ya desplegadas), se asume `"windows"` — **cero migración necesaria**, a diferencia del
cambio de `instalaciones[]` (§9.1.1): acá el campo nuevo tiene un default seguro que preserva el
comportamiento actual sin tocar el archivo.

```json
{ "nombre": "PACSWKS01", "type": "vm", "os": "windows", "ip": "192.168.1.50", "user": "admin", "pass": "...", "servicios": "MSSQLSERVER,Spooler" }
{ "nombre": "elk-01",    "type": "eq", "os": "linux",   "ip": "192.168.1.60", "user": "monitor", "pass": "...", "servicios": "elasticsearch,logstash" }
```

`servicios` cambia de semántica según `os`: nombres de servicio de Windows (`Win32_Service.Name`)
para `"windows"`, nombres de unidad `systemd` para `"linux"` — mismo campo de texto separado por
comas, distinto vocabulario esperado.

#### Recolección: `_recolectar_ssh_interno(vm_info, log_func)`, mismo `vm_obj` de salida

Nueva función en `agent_logic.py`, hermana de `_recolectar_wmi_interno`, produciendo
**exactamente la misma forma** de `vm_obj` (mismo contrato hacia `virtual_layer[]`, ver
[CONTRATO_AGENTE.md §5](./CONTRATO_AGENTE.md)):

- **Puerto:** `verificar_puerto(ip, 22)` en vez de 135 — mismo criterio de `"port_closed"` si
  no responde.
- **Conexión:** `paramiko.SSHClient()` con `AutoAddPolicy` (no valida host key — mismo nivel de
  riesgo ya aceptado hoy para iDRAC/Elastic/central con `verify=False`, no es una regresión de
  postura de seguridad nueva, ver [PLAN_MEJORAS_V4.5.md §3.4](#34-verifyfalse-generalizado-medio)).
- **Hostname/CPU/RAM/uptime:** un solo comando remoto que junta `hostname`, `/proc/meminfo`
  (`MemTotal`/`MemAvailable`) y `/proc/uptime` en una sola ida y vuelta SSH (menos exposición a
  latencia de VPN que varios comandos sueltos). CPU requiere **dos** lecturas de `/proc/stat`
  con ~1s de espera entre medio para calcular el delta de uso — mismo patrón de muestreo
  antes/después que ya usa `obtener_salud_red_pasiva` para medir tráfico de red, no una técnica
  nueva en el código.
- **Disco:** `df -P -B1`, filtrando filesystems que no son discos reales (`tmpfs`, `devtmpfs`,
  `overlay`, `squashfs`) → mapea a `mount_point`/`total_gb`/`free_gb`/`usage_percent`.
- **Servicios:** por cada unidad de `servicios`, `systemctl show <unit> --property=ActiveState,SubState,MainPID`
  (batcheado en un solo comando con un loop de shell para no hacer una ida y vuelta SSH por
  servicio). Si `MainPID` > 0, `ps -o %cpu,rss,nlwp --no-headers -p <pid>` para
  `cpu_percent`/`ram_mb`/`threads`.

#### Dos campos sin equivalente limpio en Linux — decisión: omitir, no forzar una aproximación falsa

- **`storage[].performance` (latencia de disco):** WMI la saca de un contador nativo
  (`Win32_PerfFormattedData_PerfDisk_LogicalDisk`). En Linux, el dato más cercano
  (`/proc/diskstats`, `io_ticks`) es **por dispositivo de bloque**, no por punto de montaje —
  mapear mountpoint→device real (LVM, `device-mapper`, RAID) de forma confiable agrega bastante
  complejidad para un dato que hoy nadie pidió explícitamente. **v1: se omite `performance` en
  las entradas Linux** (el resto de `storage[]` sí viaja completo). Se documenta como hueco
  conocido, no se aproxima con un valor que no significa lo mismo.
- **`vital_signs.handles` (handles de Windows):** concepto específico de Windows, sin
  equivalente directo en Linux. **v1: se reemplaza por la cantidad de file descriptors abiertos**
  (`ls /proc/<pid>/fd | wc -l`) — cumple el mismo propósito práctico (detectar una fuga de
  recursos de un proceso) aunque no sea literalmente lo mismo. Se documenta la diferencia en
  [CONTRATO_AGENTE.md](./CONTRATO_AGENTE.md) para que quede claro que `handles` en una entrada
  Linux no es comparable número a número contra una entrada Windows.

#### Impacto en el contrato de datos (avisar al equipo de servidor)

- **`virtual_layer[].wmi_error` se renombra a `collection_error`** (decisión 2026-09-12): con
  dos mecanismos de recolección posibles, un campo llamado `wmi_error` en una entrada Linux es
  confuso. El contrato de ingesta del servidor confirma que este campo no lo lee ninguna alerta
  hoy (se guarda solo de referencia en `full_json_data`), así que el riesgo del rename es bajo —
  igual hay que avisarlo explícitamente antes de desplegar, no asumir que "no lo lee nadie"
  sin confirmarlo.
- **`virtual_layer[].os` (nuevo campo, `"windows"|"linux"`):** se agrega al `vm_obj` de salida
  para que el servidor pueda eventualmente distinguir el origen sin inferirlo de otra cosa (ej.
  mostrar un ícono distinto en el dashboard, o aplicar umbrales de alerta distintos por SO más
  adelante). No lo consume nada hoy — es agregar información, no romper nada existente (mismo
  criterio que ya aplica al resto de `virtual_layer`, sin schema Pydantic estricto).

#### GUI (`web/index.html`/`script.js`)

- La tarjeta de equipo (`agregarVM`) suma un selector "Sistema Operativo" (Windows/Linux),
  reutilizando los mismos inputs de `user`/`pass`/`servicios` que ya existen — no hay campos
  nuevos, solo un selector que cambia qué significan los que ya están.
  - Placeholder de "Servicios" cambia según el SO seleccionado (ej. `MSSQLSERVER, Spooler` vs.
    `postgresql, logstash`) — mejora de usabilidad, no bloquea nada si no se hace.
- El botón "Test WMI" pasa a ser "Test conexión", que según el SO seleccionado llama a
  `test_vm_gui` (WMI, sin cambios) o a una nueva `test_vm_ssh_gui` → `agent_logic.test_connection_vm_ssh(data)`
  (mismo patrón que `test_connection_vm_wmi`, pero conecta por SSH y devuelve el hostname real).

#### Empaquetado

- `paramiko` se agrega a `requirements.txt`. Es una librería SSH pura-Python (sin dependencias
  nativas de Windows), así que no debería necesitar tanto cuidado como pywebview en
  `build.bat` — a confirmar igual en el primer build real si PyInstaller detecta bien sus
  dependencias transitivas (`cryptography`, `bcrypt`, `pynacl`) o hace falta algún
  `--hidden-import` puntual.

No es un ítem de la lista de v4.5 — queda como diseño de referencia para cuando se priorice
implementarlo.
