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
| 3 | Bypass de autenticación local de la GUI (Eel expone funciones antes del login) | Cualquier proceso local puede leer credenciales descifradas | Medio | Seguridad |
| 4 | Contraseña de admin hardcodeada e igual en todos los hospitales | Compromiso de una instalación compromete todas | Bajo–Medio | Seguridad |
| 5 | Preparar el agente para auth obligatoria en `schema_version 4.5` del lado servidor | Bloqueante para poder subir de versión de esquema sin romper ingesta | Medio | Contrato/Seguridad |
| 6 | Un ítem de `application_metrics` mal formado tira abajo **todo el reporte** (incluye infraestructura) | Pérdida de telemetría de infraestructura por un problema de datos de negocio | Medio | Robustez/Contrato |
| 7 | ElasticSearch por HTTP plano + `verify=False` generalizado | Credenciales e integridad de datos expuestas en la LAN | Medio | Seguridad |
| 8 | Sin tope de memoria/tiempo en paginación de logs de Elastic | Ciclo puede colgarse o crecer sin límite tras una caída larga | Bajo–Medio | Robustez |
| 9 | Hilos WMI que exceden el timeout de 90s quedan huérfanos | Fuga de hilos/objetos COM en entornos con equipos lentos | Medio | Robustez |
| 10 | Permisos de `secret.key`/`monitor_config.json` sin endurecer | Cualquier usuario local con acceso a `ProgramData` puede descifrar credenciales | Bajo | Seguridad |

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

### 1.3 🟠 Reporte "todo o nada": un dato de negocio malo tumba la telemetría de infraestructura

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

---

## 2. Preparación para autenticación obligatoria (`schema_version 4.5`)

El contrato (§2bis) es explícito: a partir de `schema_version: "4.5"`, el servidor va a exigir
`Authorization: Bearer <token>` único por hospital, y va a rechazar (401) si falta, no existe,
o no corresponde al `hospital_id` declarado — **sin decir cuál de los tres motivos fue**. Y
remarca: *"un agente en versión 4.5 no debe enviarse a producción contra este servidor"* hasta
que el servidor confirme que el cambio ya está desplegado.

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
4. **Manejo explícito del 401** en `ejecutar_ciclo_agente`: hoy cualquier `raise_for_status()`
   fallido cae al mismo bloque genérico `{"status": "Error", "error": str(e)}`. Vale la pena
   loguear el 401 de forma distinguible ("token rechazado o hospital_id no coincide — revisar
   configuración") para que un técnico en el sitio no confunda esto con una caída de red.
5. **Plan de rollback**: si tras bumpear a `4.5` algo falla, poder volver a `4.3` sin reinstalar
   (ej. un flag de configuración interno, no expuesto en la GUI, para forzar el
   `schema_version` a enviar durante la transición) da un camino de salida rápido sin depender
   de un nuevo build.

Este punto no es "código a escribir" tanto como **gobierno de release**: la parte técnica ya
está resuelta (envío del Bearer token), lo que falta es el proceso de corte coordinado.

---

## 3. Seguridad

Retomado de la revisión previa ([SEGURIDAD.md](./SEGURIDAD.md)), con la autenticación
servidor↔agente ya cubierta en la sección 2. Prioridad sugerida para v4.5:

### 3.1 Bypass de autenticación local de la GUI (alto)
El overlay de "Acceso Restringido" es solo visual; las funciones `@eel.expose` quedan
disponibles por WebSocket desde que arranca el proceso, independientemente del login. Mitigar
con una bandera de sesión server-side (Python) que las funciones sensibles verifiquen antes de
ejecutar cualquier acción (`cargar_config`, `guardar_config`, `toggle_monitoreo`, etc.), seteada
recién cuando `verificar_clave` devuelve `True`.

### 3.2 Contraseña de admin hardcodeada y compartida (alto)
Mover a algo generado por instalación (ej. derivado de una semilla única guardada en
`ProgramData` al instalar, mostrada una vez en el instalador) en vez de un literal en el código
fuente idéntico en todos los binarios distribuidos. Agregar backoff/bloqueo tras N intentos
fallidos consecutivos.

### 3.3 ElasticSearch por HTTP plano (medio-alto)
Migrar las URLs de `agent_logic.py` (`recolectar_logs_elastic`, `get_dicom_routing_queues`,
`test_connection_dicom_index`) de `http://` a `https://` cuando el clúster de Elastic lo
soporte, o al menos loguear una advertencia explícita si `enabled_elastic` está activo y el
host no está sirviendo HTTPS, para que quede visible en `activity.log` en vez de ser un riesgo
silencioso.

### 3.4 `verify=False` generalizado (medio)
Evaluar, por integración, si tiene sentido ofrecer una opción de CA propia/pinning en vez de
desactivar la validación por completo siempre — particularmente para el envío al servidor
central, que es el canal que transporta el envelope completo de cada hospital.

### 3.5 Endurecer permisos de `ProgramData\TecnoMonitor` (bajo esfuerzo, alto valor)
Agregar en el instalador (`TecnoMonitor.iss`) o en `security.get_app_data_path()` una ACL
explícita que restrinja `secret.key` y `monitor_config.json` a `SYSTEM` + `Administradores`,
en vez de heredar los permisos por defecto de `ProgramData`.

### 3.6 Integridad de `rules.json` (bajo)
Sin verificación de integridad hoy. Si se justifica por el modelo de amenaza del despliegue
(equipos compartidos, acceso físico no controlado), evaluar una firma simple o checksum
verificado por el servicio al cargar el archivo.

---

## 4. Robustez

Retomado de la revisión previa, más el hallazgo nuevo de la sección 1.3:

### 4.1 Reporte "todo o nada" ante datos de negocio inválidos — ver §1.3 (alto, nuevo)

### 4.2 Paginación de Elastic sin tope (medio)
`recolectar_logs_elastic` pagina con `search_after` sin límite de iteraciones ni de tiempo de
reloj. Agregar un máximo de páginas o un timeout de pared que corte y continúe en el próximo
ciclo (el checkpoint no habría avanzado, así que no se pierde nada, solo se pospone).

### 4.3 Hilos WMI huérfanos tras timeout (medio)
`obtener_vm_data` abandona el hilo si no responde en 90s, pero no lo termina — la sesión
COM/WMI sigue viva en segundo plano. Con equipos intermitentemente lentos esto puede acumular
hilos/objetos COM ciclo tras ciclo. Evaluar un mecanismo de cancelación más agresivo o, como
mínimo, telemetría interna (contador de hilos vivos) para detectar acumulación antes de que
se vuelva un problema de memoria.

### 4.4 `except Exception: pass` sin registro alguno (bajo)
Varios puntos (`save_checkpoint`, `reset_checkpoint`, guardado de checkpoint Elastic) silencian
errores sin dejar ni un log de debug. Agregar al menos un `log_func` best-effort en esos catch,
para poder diagnosticar por qué un checkpoint dejó de avanzar sin tener que instrumentar el
código en el momento del incidente.

### 4.5 GUI dependiente de CDN externo (bajo)
Bootstrap/Font Awesome se cargan desde CDN; en redes hospitalarias sin salida a internet la GUI
funciona pero se ve sin estilos. Empaquetar los assets localmente elimina esta dependencia.

---

## 5. Performance

### 5.1 Query SQL con `OR` sobre columnas de fecha distintas (medio)
`SQL_QUERY` en `agent_logic.py` filtra con `OR` sobre 7 columnas de fecha distintas, lo que
típicamente impide el uso eficiente de índices por columna en tablas grandes. Si el tiempo de
extracción se vuelve un problema medible en algún hospital con volumen alto, evaluar reescribir
como `UNION` de sub-consultas indexadas por cada fecha, coordinando con quien administra el
Extensa RIS/PACS de cada sitio los índices disponibles.

### 5.2 Concurrencia alta contra iDRAC (bajo-medio)
`obtener_storage_fisico_v3` lanza hasta 10 requests HTTPS paralelas contra el mismo iDRAC. Los
BMC de Dell suelen tener límites bajos de sesiones concurrentes; bajar a 3-4 *workers* es más
conservador y reduce el riesgo de 503/timeouts intermitentes en RAID grandes.

---

## 6. Deuda técnica / higiene de versión

- **Consolidar el versionado**: hoy conviven `AppVersion=4.4.1` (instalador),
  `agent_version="4.4.0"` (envelope) y `schema_version="4.3"` (envelope), cada uno hardcodeado
  en un lugar distinto (ver [BUILD.md](./BUILD.md#versionado)). Para v4.5, considerar una
  única fuente de verdad (ej. un archivo `VERSION` leído tanto por `build.bat`/`.iss` como por
  `agent_logic.py`) para evitar que se repita la situación de una versión reportando otra.
- **Retirar o actualizar `Compiler.txt`**: contiene comandos de PyInstaller desactualizados
  (sin `--onedir`, con `--hidden-import=proxmoxer` que ya no aplica) que podrían inducir a
  compilar un build de servicio inestable si alguien los usa por error en vez de `build.bat`.
- **`requirements.txt` ausente**: las dependencias se infieren hoy de los imports (ver
  [BUILD.md](./BUILD.md)). Formalizarlo reduce el riesgo de builds no reproducibles al preparar
  el entorno de compilación de v4.5.

---

## 7. Secuencia sugerida para v4.5

No es un compromiso de fechas, es un orden de dependencias e impacto:

1. **Fixes de paridad con el contrato** (§1.1, §1.2) — bajo esfuerzo, reactivan alertas críticas
   ya diseñadas del lado servidor. Se pueden liberar incluso antes del resto de v4.5 si el
   proceso de release lo permite, dado que no dependen de ningún cambio de `schema_version`.
2. **Validación local antes de adjuntar `application_metrics`** (§1.3) — mitiga el riesgo de
   perder telemetría completa por un dato de negocio malo, independiente de todo lo demás.
3. **Coordinación con el equipo de servidor** sobre auth obligatoria (§2) y sobre los supuestos
   de cadencia de Mirth/KPI (§1.5) — son bloqueantes de proceso, no de código, así que conviene
   arrancarlos en paralelo a los puntos 1 y 2, no después.
4. **Bump controlado a `schema_version: "4.5"`** — solo una vez confirmado por el equipo de
   servidor que la validación de token está desplegada, con el flag de rollback listo (§2,
   punto 5).
5. **Seguridad de la GUI local** (§3.1, §3.2) — no depende de nada del servidor, se puede hacer
   en paralelo a todo lo anterior.
6. **Resto de robustez/performance/seguridad** (§3.3–§3.6, §4, §5) — según capacidad, no son
   bloqueantes para el corte de versión pero conviene no acumularlos indefinidamente.
7. **Higiene de versión** (§6) — antes de compilar el primer build oficial de v4.5, para no
   heredar la misma inconsistencia de números de versión.

## 8. Checklist de validación antes de liberar v4.5

- [ ] Confirmar en un iDRAC real que `physical_layer.storage_layer` (ya renombrado) llega al
      servidor y que el dashboard efectivamente muestra estado de RAID.
- [ ] Forzar una falla real de fan/PSU en un servidor de laboratorio (o simular la respuesta de
      Redfish) y confirmar que la alerta `FAN_<name>`/`PSU_<name>` dispara con el fix de §1.2.
- [ ] Provocar un dato de negocio inválido a propósito (ej. un valor `NULL` no cubierto) y
      confirmar que ya no tumba el reporte completo, sino que se maneja según lo definido en
      §1.3.
- [ ] Confirmar con el equipo de servidor, por escrito, que la validación de token para
      `schema_version 4.5` está desplegada en producción antes de que cualquier hospital reciba
      el build que la activa.
- [ ] Probar el flujo de rollback de `schema_version` (§2, punto 5) en laboratorio antes de
      necesitarlo en producción.
- [ ] Revisar que ningún hospital tenga `auth_token` vacío o de prueba antes de activar la
      exigencia de token del lado servidor.
