# Changelog (reconstruido)

Este historial se reconstruyó a partir de los comentarios dejados en el propio código
(`agent_logic.py`, `headless_service.py`, `service_control.py`, `web/script.js`,
`TecnoMonitor.iss`) y del historial de commits del repositorio. No reemplaza al historial de
Git — para el detalle línea por línea de cada cambio, `git log`/`git blame` son la fuente
autoritativa; esto es un resumen narrativo pensado para entender *por qué* el sistema quedó
como está.

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

## Nota sobre numeración de versión

El instalador (`TecnoMonitor.iss`) declara `AppVersion=4.4.1 Sentinel`. El envelope que el
agente envía en cada ciclo declara `agent_version: "4.4.0"` (un valor distinto, hardcodeado en
`agent_logic.ejecutar_ciclo_agente`) y `schema_version: "4.3"` — ver
[BUILD.md](./BUILD.md#versionado). Al documentar o depurar por versión, conviene confirmar a
cuál de los tres números se refiere cada fuente (instalador, envelope, o comentarios del
código).
