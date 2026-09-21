# Módulos de recolección

Todos los módulos se orquestan desde `agent_logic.ejecutar_ciclo_agente()`. Cada uno es
independiente: si un módulo falla, se registra el error en `collection_meta.<modulo>` y el
ciclo continúa con el resto (no aborta el envío completo por un módulo caído).

Los dos módulos que leen datos de la base del hospital (SQL Server y autoenrute DICOM, más
abajo) siguen un principio de diseño explícito: **SQL directo y ElasticSearch conviven siempre
como caminos alternativos, nunca uno reemplaza al otro** — ver
[ARQUITECTURA.md](./ARQUITECTURA.md#principio-de-diseño-sql-directo--elasticsearch-siempre-los-dos-caminos).

## Proxmox / VMware

**Habilitación:** `enabled_proxmox` + `proxmox.*` · **Función:** `obtener_physical_layer` (Proxmox) / `obtener_vmware_layer` (VMware)

- **Proxmox:** autentica contra `/api2/json/access/ticket` (ticket + CSRF token), luego consulta
  `/api2/json/nodes/<node>/status` para CPU, RAM total/usada y uptime del nodo. No recolecta
  lista de VMs individuales en este modo (sí en VMware).
- **VMware:** usa `pyVmomi` (`SmartConnect`) contra vCenter o un ESXi directo, con verificación
  TLS deshabilitada (`ssl.CERT_NONE`). Recolecta:
  - Info y telemetría del host físico (modelo, vendor, uptime, % CPU, % RAM).
  - Lista completa de VMs con estado (Online/Offline según `powerState`), % CPU, RAM
    total/usada. Un error al leer una VM puntual se loguea y se omite esa VM sin abortar el
    resto del listado.
  - Si `pyVmomi` no está instalado, el módulo reporta el error de forma controlada
    (`test_connection_vmware` y `obtener_vmware_layer` capturan `ImportError` explícitamente).

`test_connection_proxmox` / `test_connection_vmware` son los que respaldan el botón "Test
conexión" de la GUI; para VMware primero valida que el puerto 443 esté alcanzable antes de
intentar el login (evita esperar el timeout completo de `pyVmomi` contra un host caído).

## iDRAC (Dell) — sensores y storage

**Habilitación:** `enabled_idrac` + `idrac.*` · **Funciones:** `obtener_sensors_idrac`, `obtener_storage_fisico_v3`

Se consulta la API Redfish del iDRAC (`verify=False`, HTTPS sin validar certificado):

- **Sensores** (`/Chassis/System.Embedded.1/Thermal` y `.../Power`): temperaturas (°C),
  velocidad de fans (RPM), consumo eléctrico actual y por fuente.
- **Storage RAID** (`/Systems/System.Embedded.1/Storage`), recolectado **en paralelo** para no
  acercarse al timeout global del ciclo en servidores con múltiples arreglos:
  1. Lista controladoras RAID (`ThreadPoolExecutor(max_workers=5)`).
  2. Para cada controladora, obtiene URLs de volúmenes lógicos y discos físicos (síncrono).
  3. Resuelve el detalle de todos los volúmenes y discos en paralelo
     (`ThreadPoolExecutor(max_workers=10)`).
  - Resultado: `controllers[]` (nombre, salud, modelo), `logical_volumes[]` (nombre, nivel
    RAID, tamaño, salud), `physical_drives[]` (slot, modelo, tamaño, tipo de medio, salud), y
    un flag `collection_complete` que indica si el recorrido llegó hasta el final sin cortarse
    por error.

## WMI/SSH — VMs, workstations y equipos médicos (Windows o Linux)

**Habilitación:** `enabled_vms` + `vms[]` · **Función:** `obtener_vm_data`, que despacha según
`vms[].os` a `_recolectar_wmi_interno` (Windows, default si el campo no está) o a
`_recolectar_ssh_interno` (Linux, desde v4.6 — ver
[PLAN_MEJORAS_V4.5.md §9.2](./PLAN_MEJORAS_V4.5.md#92-monitoreo-de-equipos-linux-en-vms-hoy-solo-wmiwindows)).
Ambos caminos producen el mismo `vm_obj` de salida (ver
[ENVELOPE_API.md#virtual_layer](./ENVELOPE_API.md#virtual_layer)).

### Windows (WMI)

Por cada equipo de la lista, en paralelo (`ThreadPoolExecutor(max_workers=5)`):

1. Chequeo previo de puerto TCP 135 (RPC endpoint mapper) — si está cerrado, se marca
   `state_reason = "port_closed"` sin intentar la conexión WMI completa (falla rápido).
2. Conexión WMI (`impersonation_level=Impersonate`, `authentication_level=Pktprivacy`) para:
   - Hostname real (usado como `id` si no se configuró un nombre manual).
   - CPU: promedio de `LoadPercentage` de todos los procesadores lógicos.
   - RAM: total/usada/uso % a partir de `Win32_OperatingSystem`.
   - Uptime: `LastBootUpTime` parseado desde formato WMI.
   - **Latencia de disco:** 3 muestras de `AvgDisksecPerTransfer` con 5 s de espera entre
     muestras (promedio de valores > 0), clasificada en `OK` (≤20 ms), `Warning` (20–50 ms) o
     `Critical` (>50 ms) por unidad lógica.
   - Espacio en disco por unidad lógica (`Win32_LogicalDisk(DriveType=3)`, solo discos fijos).
   - **Servicios monitoreados:** de la lista configurada en `servicios`, cruza
     `Win32_Service` con `Win32_PerfFormattedData_PerfProc_Process` (por PID) para reportar
     estado, % CPU, RAM, threads y handles del proceso asociado a cada servicio.
3. Todo el paso 2 corre en un hilo dedicado con **timeout duro de 90 s** (`obtener_vm_data`);
   si no responde a tiempo, se reporta `state = "Offline"`, `state_reason = "wmi_timeout"` sin
   bloquear el resto del ciclo.

`state_reason` distingue explícitamente: `ok`, `port_closed`, `wmi_error` (con detalle en
`collection_error`), `wmi_timeout` — para que el servidor central pueda diferenciar "equipo
apagado" de "problema de credenciales/red" de "WMI colgado".

### Linux (SSH)

Mismo paralelismo y mismo timeout duro de 90 s que el camino WMI (`obtener_vm_data` es el mismo
wrapper para ambos). Por cada equipo con `os: "linux"`:

1. Chequeo previo de puerto TCP 22 — si está cerrado, `state_reason = "port_closed"`, igual que
   WMI con el 135.
2. Conexión SSH (`paramiko.SSHClient`, `AutoAddPolicy` — no valida host key, mismo nivel de
   riesgo que el `verify=False` ya usado para iDRAC/Elastic/central) para correr, en su mayoría
   en una sola ida y vuelta por dato:
   - Hostname real (`hostname`), usado como `id` si no se configuró un nombre manual.
   - RAM: `/proc/meminfo` (`MemTotal`/`MemAvailable`).
   - Uptime: `/proc/uptime`.
   - **CPU:** dos lecturas de `/proc/stat` con ~1 s de espera entre medio, delta de
     `idle`/`total` — mismo patrón de muestreo antes/después que ya usa
     `obtener_salud_red_pasiva` para medir tráfico de red.
   - Disco: `df -P -B1`, excluyendo `tmpfs`/`devtmpfs`/`overlay`/`squashfs`. **No se calcula
     latencia de disco** (`storage[].performance`) — mapear punto de montaje a dispositivo real
     en LVM/RAID de forma confiable agrega complejidad para un dato que nadie pidió todavía.
   - **Servicios monitoreados:** por cada unidad de `servicios` (nombres `systemd`, ej.
     `postgresql`), `systemctl show --property=ActiveState,SubState,MainPID`; si hay `MainPID`,
     `ps -o %cpu,rss,nlwp` para CPU/RAM/threads, y la cantidad de file descriptors abiertos
     (`/proc/<pid>/fd`) como aproximación de `handles` (no es el mismo concepto que en Windows,
     mismo propósito práctico: detectar una fuga de recursos).
3. `state_reason` distingue `ok`, `port_closed`, `ssh_error` (con detalle en
   `collection_error`), `ssh_timeout` — mismo criterio que el camino WMI, con sus propios
   valores para no confundir un problema de SSH con uno de WMI en el mismo log.

## SQL Server (KPIs de negocio)

**Habilitación:** `enabled_sql` + `sql.*` · **Función:** `extraer_metricas_sql`

> **v4.5 — variante vía Elastic:** un hospital puede migrar este módulo para que ya no conecte
> directo a SQL Server, leyendo en cambio de índices que un pipeline de Logstash alimenta cada
> hora (mismo patrón que el autoenrute DICOM más abajo). Habilitación:
> `elastic.enabled_ris_metrics` · Función: `extraer_metricas_ris_elastic`. Ambos caminos
> comparten el mismo checkpoint y producen el mismo `application_metrics` — ver
> [ELK_RIS_METRICS.md](./ELK_RIS_METRICS.md) para la arquitectura completa y
> [CONFIGURACION.md](./CONFIGURACION.md#kpis-de-ris-vía-elastic--coexistencia-con-enabled_sql-v45)
> para la coexistencia de flags.

Extrae KPIs operativos de las bases `ExtensaRadio`, `ExtensaPACS` y `SL_UserAndConfig` mediante
una única consulta con tres sub-`SELECT ... FOR JSON PATH` (ver `SQL_QUERY` en `agent_logic.py`):

- **`ris`** (por equipo/AET/modalidad, ventana de tiempo): totales de estudios, citados,
  admitidos, ejecutados, con imagen disponible, en borrador, definitivos (aprobados) y
  suspendidos. Las columnas `[CreatedOn/PlanningDate/AdmissionDate/ExecutionDate/ReportDate/
  ApprovalDate/ModifiedOn]` se evalúan cada una contra la ventana con su propio filtro,
  agrupando por `[Description], [AETitle], [DICOMModalityCode]`.
- **`pacs`**: estudios distintos almacenados por AET/modalidad en la ventana, filtrando
  borrados lógicos (`DELETED IS NULL OR DELETED = '0'`).
- **`users`**: usuarios únicos e inicios de sesión por rol, a partir de `UserAuditHistory`
  (`AuditText = 'User Logon OK'`).

Todas las consultas usan `WITH (NOLOCK)` (lectura sucia) para no interferir con el sistema
transaccional del RIS/PACS en producción.

### Checkpointing y backfill

- El checkpoint (`.sql_checkpoint`) guarda el **fin** del último bloque extraído con éxito.
- El tamaño del bloque es `24 / executions_per_day` horas (ej. 8 h con 3 ejecuciones/día).
- Si no hay checkpoint previo:
  - Con `historical_start_date` configurada → arranca el **backfill histórico** desde esa
    fecha, procesando un bloque por ciclo hasta alcanzar el presente.
  - Sin esa fecha → arranca desde el inicio del día actual.
- Si el bloque a extraer todavía no terminó (`target_end_time > ahora`), la función devuelve
  `None` y ese ciclo simplemente no incluye `application_metrics` — no es un error.
- **El checkpoint solo se persiste cuando el POST al servidor central confirma éxito**
  (`ejecutar_ciclo_agente`, tras `r.raise_for_status()`). Si el envío falla, el mismo bloque se
  vuelve a extraer y reintentar en el próximo ciclo — no hay pérdida de datos de negocio por
  una caída de red transitoria.

## Mirth Connect

**Habilitación:** `enabled_mirth` + `mirth_servers[]` · **Función:** `mirth_collector.recolectar_mirth`

Por cada servidor Mirth configurado (secuencial, no paralelo):

1. Login contra `/api/users/_login` (form-encoded), con headers `X-Requested-With: OpenAPI`
   requeridos por Mirth para no rechazar la request como potencial CSRF.
2. `/api/channels/statistics` → mensajes recibidos/enviados por canal.
3. `/api/channels/statuses` → estado (`RUNNING`/`STOPPED`/etc.), mensajes en cola y errores
   acumulados por canal, cruzando por `channelId` con las estadísticas del paso 2.
4. Logout (`/api/users/_logout`), best-effort.

Si un servidor falla (credenciales, red, Mirth caído), se reporta un canal sintético
`SYSTEM_ERROR` con el detalle del error para ese alias, sin afectar a los demás servidores
Mirth configurados.

## Certificados SSL

**Habilitación:** `enabled_ssl` + `ssl_urls[]` · **Función:** `obtener_certificados_ssl`

Por cada URL: conecta por TCP al host:puerto (default 443), hace el handshake TLS **sin
validar la cadena de confianza** (`verify_mode = ssl.CERT_NONE` — se lee el certificado aunque
esté vencido o sea autofirmado, a propósito, porque el objetivo es *auditar* su vigencia, no
confiar en él para una conexión de negocio), y parsea el certificado en formato DER con
`cryptography.x509`.

Clasificación semafórica por días restantes hasta `not_valid_after`:

| Días restantes | Estado |
|---|---|
| `< 0` | `CRITICAL` (vencido) |
| `0 – 6` | `CRITICAL` |
| `7 – 29` | `WARNING` |
| `≥ 30` | `OK` |
| Error de conexión/parseo | `ERROR` (con `last_error` truncado a 100 caracteres) |

## ElasticSearch — logs de Suitestensa

**Habilitación:** `enabled_elastic` + `elastic.*` · **Función:** `recolectar_logs_elastic`

1. Carga y **precompila** las reglas de `rules.json` (ver [REGLAS_LOGS.md](./REGLAS_LOGS.md)).
2. Lee el checkpoint (`.elastic_checkpoint`); si no existe, arranca desde
   `now-<interval_minutes>m` (usa el intervalo global del agente, no un lookback fijo por
   regla).
3. Consulta el índice (`index_pattern`, default `se-es-logging-*`) filtrando
   `level.keyword IN [Error, Fatal, Critical]` y `@timestamp > checkpoint`, paginando con
   `search_after` en lotes de 1000 documentos hasta agotar resultados.
4. Para cada log, intenta matchear contra las reglas compiladas (por `service_target` y
   regex); si no matchea ninguna, se clasifica como `UNKNOWN-ERR-99`.
5. Los eventos se agrupan por `rule_id` (conteo, primera/última ocurrencia, servicios
   afectados) — lo que se envía al servidor central es solo `{rule_id, count}` por regla
   disparada en la ventana, no el detalle de cada log individual.
6. Los patrones `UNKNOWN-ERR-99` (primeros 100 caracteres del mensaje como clave) se acumulan
   localmente en `unknowns_lab.json`, con evidencia completa, para que un técnico pueda
   revisarlos en el equipo y eventualmente convertirlos en una regla nueva de `rules.json`.
7. El checkpoint se actualiza al timestamp del log más reciente visto, y —al igual que en
   SQL— **solo se persiste tras la confirmación de envío exitoso**.

## Autoenrute DICOM (SQL directo o vía ElasticSearch)

**Habilitación:** `enabled_sql` + `sql.enabled_dicom_routing` (directo, v4.6) **o**
`enabled_elastic` + `elastic.enabled_dicom_routing` (Elastic gana si ambos están activos) ·
**Funciones:** `obtener_dicom_routing_sql` / `get_dicom_routing_queues`, despachadas desde
`ejecutar_ciclo_agente` §5.5 — mismo criterio de prioridad que los KPIs de negocio (ver
[SQL Server (KPIs de negocio)](#sql-server-kpis-de-negocio) más abajo: si ambos caminos están
configurados, gana la variante Elastic).

Corre en **cada ciclo** del intervalo global (a diferencia de los KPIs de negocio, que respetan
su propia ventana de `executions_per_day`) — es una foto del estado actual de las reglas, no
una serie histórica con checkpoint.

### Directo a SQL Server (`obtener_dicom_routing_sql`, v4.6)

Restaura el camino que existía hasta v4.3 (entre v4.4 y v4.5 solo existía vía Elastic — ver
[CONFIGURACION.md](./CONFIGURACION.md#autoenrute-dicom-dos-caminos-independientes-historia-del-flag)
para el porqué). Misma query confirmada en producción que usa `elk/ext_dicom_queues.conf` para
poblar el índice de Elastic (`DICOMAUTOROUTINGRULES` + `DICOMCLIENT` + `DICOMAUTOROUTINGQUEUE`
de `ExtensaPACS`), pero sin el pipeline de Logstash intermedio: el dato es siempre en vivo.
`snapshot_age_minutes` viaja en `0.0` en cada regla — no hay lag que medir.

### Vía ElasticSearch (`get_dicom_routing_queues`)

Lee un índice de Elasticsearch
(`dicom_index`, default `ext_dicom_queues`) que un pipeline de Logstash actualiza cada 5
minutos con upsert por `IDRULE` — es una "foto" del último ciclo del pipeline, no un stream de
eventos.

Punto clave de robustez: si el pipeline de Logstash se cae, el índice **no da error** — sigue
devolviendo los últimos documentos conocidos como si fueran actuales. Por eso cada documento
se valida por antigüedad usando su `@timestamp` (leído vía `docvalue_fields` en formato
`epoch_millis`, para no depender de cómo el pipeline serializó la fecha ni de la zona horaria
del servidor Elastic):

- Documentos más viejos que `dicom_max_age_minutes` (default 15) se descartan.
- Si **todos** los documentos recuperados están vencidos, la función devuelve una lista vacía
  a propósito con `status = "stale"` — se prefiere un hueco visible en el gráfico del servidor
  central antes que una línea plana con datos viejos que aparente normalidad.
- `status = "empty"` si el índice no devolvió documentos en absoluto.
- `status = "error"` ante fallo de conexión/HTTP.
- `status = "ok"` (o `"stale"` con conteo de descartados si hubo alguno) en el caso normal.

Existe además `test_connection_dicom_index`, un test específico (separado del test genérico de
Elastic) porque un usuario válido para leer los logs de Suitestensa puede no tener permiso
sobre el índice de autoenrute — ese `403` es difícil de diagnosticar en producción sin un test
dedicado que lo señale explícitamente.

## Integridad de bases SQL (`DBCC CHECKDB` tras un reinicio)

**Habilitación:** `enabled_elastic` + `elastic.enabled_checkdb` (principal, vía Logstash) **o**
`enabled_sql` + `sql.enabled_checkdb` (directo, la excepción para hospitales sin Elastic). Si ambos
están activos gana Elastic — mismo criterio que autoenrute DICOM y los KPIs de RIS. ·
**Código:** `sql_integrity.py` (despachado desde `ejecutar_ciclo_agente` §5.6). Plan, decisiones y
riesgos: [PLAN_CHECKDB_POST_REINICIO.md](./PLAN_CHECKDB_POST_REINICIO.md).

Un `DBCC CHECKDB` de las bases de Extensa es una consulta **costosa** (minutos u horas), por eso solo
corre ante un **reinicio del servicio SQL Server** — típicamente un corte de energía abrupto — y el
resultado se manda **una sola vez por reinicio** en `software_monitoring.sql_integrity` (contrato en
[CONTRATO_AGENTE.md §7ter](./CONTRATO_AGENTE.md)). Nunca corre de forma periódica.

Un reinicio se detecta por el arranque de SQL (`create_date` de `tempdb`, que se recrea en cada
inicio del servicio y no pide permisos especiales), así que cubre tanto el reinicio de la VM como el
del propio servicio.

### Vía Elastic (`recolectar_elastic`) — camino principal

Logstash ejecuta el chequeo y el agente solo lee: el pipeline `elk/ext_checkdb.conf` +
`elk/ext_checkdb.sql` corre cada 15 minutos en su **propio cajón** (`ext_checkdb-all-sito.bat`, con su
`--path.data`) y decide **del lado SQL** si hubo un reinicio nuevo comparando el arranque de SQL con el
último procesado (`sqlserver_start_epoch`, columna de seguimiento numérica de Logstash). Así funciona
esté donde esté Logstash y reintenta solo si SQL tarda en levantar tras un corte. La primera corrida
solo siembra el valor (fila `BASELINE`, no indexada): **no chequea nada al instalar el pipeline**.

El agente lee `elastic.checkdb_index` (default `ext_checkdb`), toma los documentos del **arranque más
reciente** y los reenvía una vez (`last_sent_epoch` en su estado). Un índice inexistente es `empty`
(todavía nunca hubo un reinicio chequeado), no un error. El tipo de chequeo (`full`/`physical_only`) y la
lista de bases se editan en `ext_checkdb.sql`.

### Directo a SQL (`recolectar_sql`) — la excepción

El agente detecta el reinicio y lanza un **proceso trabajador** desacoplado, porque el chequeo dura más
que un ciclo y en modo *tarea programada* cada ciclo es un proceso efímero (`--run-once`) que mataría
cualquier hilo.

| Estado (`.sql_integrity_state_<hospital>`) | Qué pasa |
|---|---|
| Sin línea base | La primera vez (instalación o actualización) solo se registra el arranque actual. **No se chequea.** |
| `pending` | Reinicio detectado. Se espera `checkdb_settle_minutes` y que las bases estén `ONLINE` (hasta `checkdb_max_wait_minutes`). |
| `running` | Se lanza `TecnoMonitorService.exe --sql-integrity-worker <hospital>`, que chequea base por base. Si muere se relanza (hasta 4 lanzamientos); agotados, se informa lo hecho y las bases faltantes como `ERROR`. Si SQL se reinicia otra vez, se descarta la corrida y se arranca la nueva. |
| `done` | Resultado listo: se envía en el siguiente reporte y se marca `sent` **solo tras un POST exitoso**. |

Hay un único escritor por archivo, para no necesitar locks: el estado lo escribe solo el agente y los
resultados (`.sql_integrity_results_<hospital>`) solo el trabajador. Cada resultado se guarda apenas se
tiene, así que el trabajador retoma desde la primera base que falte. El trabajador escribe en su propio
log (`sql_integrity_worker.log`, rotado a 1 MB), no en `activity.log`.

La consulta se hace **por base desde Python** (`DBCC CHECKDB ([base]) WITH NO_INFOMSGS, ALL_ERRORMSGS,
TABLERESULTS [, PHYSICAL_ONLY]`), sin las tres fallas de la consulta manual: sin tablas temporales que
se filtren entre bases, sin columnas que trunquen mensajes largos, y con cantidad de errores (se toma del
resumen `CHECKDB found N allocation errors and M consistency errors`) y los primeros mensajes.

### Estados y `collection_meta`

`collection_meta.sql_integrity.status`: `ok` (al día, o resultado nuevo en este reporte) · `pending`
(esperando que SQL se asiente) · `running` (chequeo en curso, con `done`/`total`) · `empty` (Elastic sin
resultados todavía) · `error` · `disabled`. El estado por base es `OK` · `ERROR` · `NOT_ONLINE`.

Los botones de test de la GUI (`test_conexion_sql`, `test_conexion_indice`) verifican, respectivamente,
la conexión + que las bases existan/estén ONLINE + que el usuario sea `sysadmin` o `db_owner`, y que se
pueda leer el índice.
