# Changelog (reconstruido)

Este historial se reconstruyó a partir de los comentarios dejados en el propio código
(`agent_logic.py`, `headless_service.py`, `service_control.py`, `web/script.js`,
`TecnoMonitor.iss`) y del historial de commits del repositorio. No reemplaza al historial de
Git — para el detalle línea por línea de cada cambio, `git log`/`git blame` son la fuente
autoritativa; esto es un resumen narrativo pensado para entender *por qué* el sistema quedó
como está.

## v4.5.0

- **`schema_version` del envelope pasa a `"4.5"`** (antes `"4.3"`), alineado por primera vez con
  `agent_version` (`"4.5.0"`) y con `AppVersion` del instalador (`4.5.0`). Este cambio de
  `schema_version` no es cosmético: activa del lado servidor la exigencia de header
  `Authorization: Bearer <token>` validado contra `hospital_id` — el agente ya enviaba ese header
  en cada ciclo sin cambios de código, pero requiere que cada hospital tenga un `auth_token` real
  cargado antes de desplegar este build (ver [BUILD.md](./BUILD.md#versionado)).
- **Corrección de dos bugs de paridad con el contrato del servidor**, encontrados cruzando el
  envelope del agente contra la especificación real de `alerts_engine`:
  - `physical_layer.storage` se renombra a `physical_layer.storage_layer` — la clave vieja nunca
    coincidía con lo que el servidor esperaba, así que las alertas de RAID/discos físicos nunca
    se evaluaban, aunque el agente sí recolectaba bien los datos de iDRAC.
  - Los sensores de iDRAC (temperaturas, fans, fuentes de alimentación) dejan de reportar
    `status: "OK"` hardcodeado y pasan a leer `Status.Health` real de Redfish — antes, las
    alertas `FAN_<name>`/`PSU_<name>` no podían dispararse nunca, sin importar el estado real
    del hardware.
- **Validación defensiva de `application_metrics`** antes de adjuntarlo al envelope
  (`extraer_metricas_sql`/`extraer_metricas_ris_elastic`): normaliza a lista vacía las
  sub-listas que SQL Server devuelve como `NULL` cuando una sub-consulta no matchea filas, y
  valida la forma mínima que exige el contrato — si algo no calza, se omite el bloque y se
  reintenta en el próximo ciclo en vez de dejar que el servidor rechace el reporte completo
  (perdiendo también la telemetría de infraestructura que viajaba en el mismo POST).
- **Código de acceso a la GUI único por instalación**, generado al azar y mostrado una sola vez,
  en reemplazo del hash fijo (`sha256("TM4dm1n")`) que era idéntico en todas las instalaciones
  distribuidas. Se agrega además un lockout de 5 intentos fallidos consecutivos.
- **Nuevo camino para KPIs de RIS/PACS/usuarios vía ElasticSearch**
  (`elastic.enabled_ris_metrics`), alternativo al SQL Server directo existente, siguiendo el
  mismo patrón que ya usaba el autoenrute DICOM: un pipeline de Logstash pre-agrega buckets
  horarios idempotentes y el agente los suma para reconstruir sus bloques, preservando el mismo
  checkpoint/backfill de siempre. Convive con `enabled_sql` (no lo reemplaza) — es una migración
  hospital por hospital. Incluye soporte completo en la GUI (sub-ítem dentro de la tarjeta de
  ElasticSearch) y los `.conf` de Logstash de referencia en `elk/`. Ver
  [ELK_RIS_METRICS.md](./ELK_RIS_METRICS.md).

Detalle completo de todo lo anterior en [PLAN_MEJORAS_V4.5.md](./PLAN_MEJORAS_V4.5.md).

## v4.4.1 "Sentinel" (post-validación en laboratorio)

- `activity.log` pasa a rotar a los 5 MB, conservando 5 archivos de historia. Antes crecía sin
  límite y nadie lo truncaba.
- El timestamp del log pasa a incluir la fecha completa (antes era solo `%H:%M:%S`), porque con
  solo la hora era imposible ubicar en qué día había ocurrido una caída de hace varios días.
- Se agrega detección de agentes v4.3 sobrevivientes: si el candado por socket de la versión
  vieja sigue tomado al arrancar, el servicio lo registra y **aborta el arranque** en vez de
  dejar correr dos agentes en paralelo (que duplicarían telemetría y se pisarían el checkpoint
  de ElasticSearch).

## v4.4 — Servicio de Windows + ElasticSearch

- **Cambio de mecanismo de ejecución:** de una tarea programada (`ONLOGON`, "TecnoMonitor_AutoStart")
  a un servicio de Windows real (`TecnoMonitorAgent`) gestionado por el SCM. Motivación: la
  tarea programada dejaba de correr si nadie iniciaba sesión en el servidor; el servicio arranca
  con el sistema, sobrevive al logoff, y Windows lo reinicia solo si el proceso muere (política
  de *failure actions* configurada por el instalador).
- El candado anti-duplicados pasa de un socket en `127.0.0.1:64999` a un **mutex con nombre**
  (`Global\TecnoMonitorAgent_SingleInstance`). El socket dejaba el puerto en `TIME_WAIT` después
  de un `taskkill /F`, y la instancia siguiente salía en silencio con `exit(0)` sin dejar rastro
  claro del motivo.
- El sleep entre ciclos deja de ser `time.sleep()` y pasa a ser un `WaitForSingleObject` sobre un
  evento de stop — el apagado de Windows ya no tiene que esperar hasta el intervalo completo
  configurado (podía ser de varios minutos) ni corta un ciclo de recolección a la mitad.
- El control del servicio desde la GUI deja de usar `schtasks`/`taskkill` y pasa a hablar
  directo con el SCM vía `pywin32` (`service_control.py`), evitando depender de parsear la
  salida de `sc.exe` (que cambia según el idioma del Windows del hospital).
- **Guardar configuración** pasa de "matar y relanzar" (`taskkill /F` + arranque inmediato, con
  posible carrera entre el proceso viejo liberando recursos y el nuevo tomando el candado) a un
  reinicio ordenado que espera la confirmación del SCM en cada paso (`service_control.reiniciar`).
- **Nuevo módulo ElasticSearch:**
  - Logs de Suitestensa clasificados contra `rules.json`, con checkpoint propio
    (`.elastic_checkpoint`) y laboratorio local de patrones desconocidos (`unknowns_lab.json`).
  - Autoenrute DICOM migra de lectura directa a SQL Server a lectura de un índice de Elastic
    alimentado por Logstash, con detección de "pipeline caído" por antigüedad de documento (en
    vez de asumir que el índice siempre refleja el estado actual).
  - El flag de autoenrute se mueve de `sql.enabled_dicom_routing` a
    `elastic.enabled_dicom_routing`, con fallback de compatibilidad hacia la ubicación vieja
    para no romper agentes ya desplegados con la config anterior (ver
    [CONFIGURACION.md](./CONFIGURACION.md)).
- La GUI (`TecnoMonitorConfig.exe`) pasa a requerir UAC (`--uac-admin`) porque ahora habla con
  el SCM directamente.
- Nueva función de estado detallado (`estado_servicio_detallado`) para distinguir "Detenido" de
  "No instalado" en el badge de la GUI — con la detección anterior basada en `psutil` ambos
  casos se veían igual y mandaban al técnico a buscar el problema en el lugar equivocado.

## v4.3 y anteriores (según referencias en el código)

Reconstruido solo a partir de menciones retrospectivas en comentarios de versiones
posteriores — no hay una fuente única para el detalle completo de estas versiones:

- Mecanismo de ejecución: `while True` lanzado por una tarea programada `ONLOGON` llamada
  `TecnoMonitor_AutoStart`.
- Candado anti-duplicados: socket local en el puerto `64999`.
- Control desde la GUI: `schtasks /Run` para arrancar y `taskkill /F` para detener — sin espera
  de confirmación entre ambos pasos.
- El flag de autoenrute DICOM vivía en `sql.enabled_dicom_routing`, y el autoenrute se leía
  directo de SQL Server (antes de existir el módulo de ElasticSearch).
- Versiones incrementales previas agregaron, en algún punto entre v4.0 y v4.3: monitoreo de
  Mirth Connect (`enabled_mirth`, comentado como "NUEVO v4.1" en `main_gui.py`), certificados
  SSL (`enabled_ssl`, "NUEVO v4.2"), y el propio módulo de logs/ElasticSearch base
  ("NUEVO v4.3").

## Nota sobre numeración de versión (histórica, resuelta en v4.5.0)

Hasta v4.4.1, el instalador (`TecnoMonitor.iss`) declaraba `AppVersion=4.4.1 Sentinel` mientras
el envelope declaraba `agent_version: "4.4.0"` (un valor distinto) y `schema_version: "4.3"` —
tres números de versión sin relación entre sí. Desde v4.5.0 los tres están alineados (ver
[BUILD.md](./BUILD.md#versionado)); esta nota queda como registro de que, en versiones
anteriores a esta, no se podía asumir que "la versión" fuera un único número.
