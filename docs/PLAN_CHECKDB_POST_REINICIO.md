# Plan — Chequeo de integridad de bases SQL Server tras un reinicio (`sql_integrity`)

**Estado — 2026-09-21: implementado (F1–F4). Falta la validación en P03 y el build (F5).**
Se entrega en el agente **4.5.2** (el instalador 4.5.1 ya está en P03 y no cambia). Lo verificado hasta
acá es con simulación (125 tests del agente, sin un SQL Server ni un Logstash reales); el pipeline
de Logstash y sus consultas T-SQL **no se probaron contra un SQL Server real**: ver §7.

## 1. Objetivo y alcance

Cuando la VM que aloja el SQL Server (o el propio servicio SQL) se reinicia —típicamente por un
**corte de energía abrupto**—, ejecutar un `DBCC CHECKDB` sobre las bases de Extensa para detectar
corrupción o bases con problemas, y reportar el resultado al servidor central.

Es una consulta costosa (minutos u horas según el tamaño de las bases), por eso **solo corre ante
un reinicio**, nunca de forma periódica.

- **Incluido ahora:** recolección en el agente por los dos caminos habituales del proyecto,
  contrato de datos e **ingesta** en el servidor.
- **Fuera de alcance por ahora:** visualización en el dashboard y alertas del servidor (se
  definen después, cuando ya lleguen datos reales).

## 2. Decisiones tomadas

| # | Tema | Decisión |
|---|---|---|
| 1 | Dónde corre Logstash | Hoy en la misma VM que el SQL, **pero no siempre**: el diseño no puede depender de eso. |
| 2 | Tipo de chequeo | Configurable: **completo** (como la consulta manual) o **`PHYSICAL_ONLY`** (liviano, sin chequeos lógicos). |
| 3 | Credencial | La misma (`sql.user`). No se agrega una credencial dedicada. |
| 4 | Camino principal | **Elastic (Logstash)**. El SQL directo es la **excepción**, para hospitales sin Elastic. Si ambos están activos, gana Elastic (mismo criterio que autoenrute DICOM y KPIs de RIS). |
| 5 | Bases | Las 26 de la consulta manual por defecto, **editables** (pueden existir más). Las que no existan en un hospital se ignoran. |
| 6 | Qué cuenta como reinicio | El **arranque del servicio SQL** (`create_date` de `tempdb`), lo que cubre tanto el reinicio de la VM como el del servicio. Pensado para cortes de energía. |
| 7 | Versión | **4.5.2**. No 4.6: `schema_version` sale de `major.minor` de `VERSION`, y el servidor trata un `"4.6"` desconocido como formato legacy V2, corrompiendo los datos en silencio (ver [PLAN_MEJORAS_V4.5.md §2](./PLAN_MEJORAS_V4.5.md)). |

## 3. Hallazgos que condicionan el diseño

1. **Duración.** Un CHECKDB de 26 bases puede tardar horas; el ciclo del agente es corto y
   síncrono. En modo *tarea programada* cada ciclo es un proceso nuevo (`--run-once`) que termina y
   mata cualquier hilo. El camino SQL directo necesita un **proceso trabajador separado** con
   estado en disco (mismo mecanismo que los checkpoints de KPIs).
2. **Detección de reinicio.** Se compara el arranque de SQL con el último valor visto. Se toma de la
   `create_date` de `tempdb` (se recrea en cada inicio del servicio y no pide permisos especiales, a
   diferencia de `sys.dm_os_sys_info`).
   - **Primera vez** (instalación o actualización del agente): solo se registra la **línea base**;
     no se dispara ningún chequeo, para no lanzar una carga enorme al actualizar.
   - Tras el arranque se espera un tiempo (`settle_minutes`) y se verifica que las bases estén
     `ONLINE`. Una base que no lo esté ya es un hallazgo en sí misma (`NOT_ONLINE`).
3. **Logstash no puede depender del cajón "al reinicio".** El cajón `elk/ext_al_reinicio-all-sito.bat`
   (vacío hoy) sirve solo si Logstash corre en la misma VM que el SQL, y el trigger "al iniciar el
   equipo" puede dispararse antes de que SQL esté disponible (sin reintento). El pipeline decide
   **del lado SQL**: consulta `sqlserver_start_time` y solo corre el CHECKDB si es posterior al
   último valor procesado (`:sql_last_value` del plugin JDBC). Funciona esté donde esté Logstash y
   **reintenta solo** si SQL todavía no estaba listo tras un corte de energía.
4. **Un cajón propio.** Un CHECKDB de horas dentro de `ext_tiempo_real-all-sito.bat` bloquearía
   todos los pipelines de 5 minutos (colas DICOM, etc.), que quedarían con datos obsoletos. El
   pipeline de integridad va en **su propio `.bat` y su propia tarea programada** (cada 15 min
   alcanza: solo decide si hay que correr).
5. **Permisos.** `DBCC CHECKDB` exige `sysadmin` o `db_owner` en cada base, más de lo que hoy
   necesita el agente para los KPIs. Como la credencial es la misma, hay que **ampliarla** donde se
   active; el botón de test de la GUI debe verificarlo.
6. **La consulta manual tiene tres fallas** que no conviene arrastrar:
   - `#DBCC_OUTPUT` se crea dentro del bucle y no se borra si el `DBCC` falla: el resto de las bases
     se reporta como `ERROR` con un mensaje engañoso ("ya existe el objeto").
   - `Detalle VARCHAR(500)` con `ERROR_MESSAGE()` (hasta 2048 caracteres): el `INSERT` dentro del
     `CATCH` puede abortar todo el lote.
   - Solo dice "encontró errores": sin cantidad ni mensajes.
7. **Compatibilidad de despliegue.** El servidor descarta sin error las claves de
   `software_monitoring` que no conoce: un agente nuevo no rompe a un servidor viejo (los datos se
   pierden hasta que el servidor los procese). Igual, el servidor se actualiza primero.
8. **Espacio en disco.** `CHECKDB` crea internamente un snapshot en el mismo volumen y usa `tempdb`;
   con poco espacio libre, sobre todo tras un corte de energía, puede fallar.

## 4. Diseño

### 4.1 Caminos

| | Elastic (principal) | SQL directo (excepción) |
|---|---|---|
| Habilita | `enabled_elastic` + `elastic.enabled_checkdb` | `enabled_sql` + `sql.enabled_checkdb` |
| Quién ejecuta el CHECKDB | Logstash (`elk/ext_checkdb.conf` + `.sql`) | Proceso trabajador del agente |
| Quién decide que hubo reinicio | El pipeline, en SQL (`sqlserver_start_time` vs. `:sql_last_value`) | El agente, comparando contra su estado en disco |
| Tipo de chequeo y lista de bases | En el `.sql` del pipeline (editable) | `sql.checkdb_type`, `sql.checkdb_databases` |
| Qué hace el agente | Lee el índice y reenvía el último resultado | Detecta, lanza el trabajador y reenvía el resultado |

### 4.2 Pipeline de Logstash (camino Elastic)

Archivos en `elk/`: `ext_checkdb.conf`, `ext_checkdb.sql` y `ext_checkdb-all-sito.bat`.

- **Sin `schedule =>`**, como los demás: un ciclo por invocación. Se invoca desde su **propio cajón**
  (`ext_checkdb-all-sito.bat`, tarea programada cada 15 min con "no iniciar una instancia nueva" si ya
  se está ejecutando) y con su **propio `--path.data`**, porque dos Logstash simultáneos con el mismo
  data dir se bloquean entre sí y este cajón puede encimarse con los de 5 minutos.
- **Seguimiento numérico** (`use_column_value`, `tracking_column => "sqlserver_start_epoch"`,
  `tracking_column_type => "numeric"`, `last_run_metadata_path` **propio**): el arranque de SQL en
  segundos desde 1970. Es numérico y sale del propio SQL, así que no depende de zonas horarias ni de
  en qué VM corra Logstash. El `last_run_metadata_path` por defecto es un único archivo por usuario,
  compartido por todos los pipelines jdbc; como este sí usa seguimiento, compartirlo pisaría a otro.
- `ext_checkdb.sql` (`statement_filepath`), editable por hospital (tipo, esperas, lista de bases):
  1. Si `sql_last_value` es 0 (primera corrida) → una fila `BASELINE`, que **no se indexa**: solo siembra
     el valor, no chequea nada.
  2. Si el arranque es posterior al último procesado, pasó `EsperaMin` y las bases están `ONLINE` (o
     venció `EsperaMaxMin`) → corre el CHECKDB base por base (cursor T-SQL) y devuelve una fila por base.
  3. Si no → **0 filas** (siempre devuelve un result set), el valor no avanza y se reintenta en la próxima
     invocación.
- Índice `ext_checkdb`, `document_id` = `<db>_<sqlserver_start_epoch>` (idempotente ante reintentos).
- El `.sql` **no puede usar dos puntos** salvo en el marcador de `sql_last_value` (Logstash sustituye
  marcadores antes de enviar el texto a SQL Server); lo aclara el encabezado del archivo.

### 4.3 Camino SQL directo

- Módulo nuevo (`sql_integrity.py`) para no seguir engordando `agent_logic.py`.
- Estado en `ProgramData\TecnoMonitor\.sql_integrity_state_<hospital>` (mismo esquema que los
  checkpoints): `baseline → idle → pending → running → done (sin enviar) → enviado`.
- Cada ciclo consulta `sqlserver_start_time`. Si cambió y hay línea base → `pending`; tras
  `settle_minutes` y con las bases `ONLINE` (hasta un máximo de espera) → lanza el trabajador.
- **Trabajador:** subproceso desacoplado (`--sql-integrity-worker <hospital>`), con mutex de
  instancia única. Chequea base por base, guardando cada resultado en el estado. Si detecta otro
  reinicio a mitad de camino, descarta la corrida y arranca la del nuevo arranque.
- Consulta endurecida, por base y desde Python: `DBCC CHECKDB ([db]) WITH NO_INFOMSGS,
  ALL_ERRORMSGS, TABLERESULTS [, PHYSICAL_ONLY]`. Se captura la **cantidad de errores** y los
  primeros mensajes (truncados). Sin tablas temporales que se filtren ni columnas que trunquen.
- El resultado se envía **una vez** y se marca como enviado solo tras un POST exitoso (igual que el
  checkpoint de KPIs). Mientras corre, `collection_meta.sql_integrity.status = "running"`.

### 4.4 Contrato de datos

`software_monitoring.sql_integrity` (clave nueva, opcional; se manda una vez por reinicio):

```json
"sql_integrity": {
  "sqlserver_start_time": "2026-09-21T08:14:03",
  "check_type": "full",
  "source": "elastic",
  "databases": [
    { "db": "ExtensaRadio", "status": "OK",    "error_count": 0, "detail": "",
      "duration_s": 412, "checked_at": "2026-09-21T09:41:10" },
    { "db": "ExtensaPACS",  "status": "ERROR", "error_count": 3, "detail": "Msg 8939 ... (truncado)",
      "duration_s": 95,  "checked_at": "2026-09-21T09:43:02" }
  ]
}
```

- `status`: `OK` · `ERROR` (el CHECKDB encontró errores o no pudo ejecutarse) · `NOT_ONLINE`
  (la base no estaba `ONLINE`; `detail` trae el `state_desc`).
- `check_type`: `full` | `physical_only`. `source`: `elastic` | `sql`.
- Todas las fechas en **hora local del hospital, sin zona** (igual que `envelope.timestamp`).
- `collection_meta.sql_integrity`: `{ "enabled": bool, "status": "ok|running|pending|error|disabled" }`.

### 4.5 Configuración nueva

| Clave | Default | Descripción |
|---|---|---|
| `sql.enabled_checkdb` | `false` | Habilita el camino SQL directo (excepción). |
| `sql.checkdb_type` | `"full"` | `"full"` o `"physical_only"`. |
| `sql.checkdb_databases` | las 26 de Extensa | Lista editable; las inexistentes se ignoran. |
| `sql.checkdb_settle_minutes` | `10` | Espera tras el arranque de SQL antes de chequear. |
| `elastic.enabled_checkdb` | `false` | Habilita la lectura del índice. |
| `elastic.checkdb_index` | `"ext_checkdb"` | Índice que publica el pipeline. |

### 4.6 Servidor

Ingesta ahora, sin cambios de esquema: filas en `software_monitoring` con
`app_name = 'sql_integrity'`, `component_id` = base, `status_value` = estado,
`metric_value` = cantidad de errores, `timestamp` = `checked_at` de esa base y el resto en
`extra_data`. Idempotente por `(hospital, app_name, base, timestamp)`. La pestaña Software y las
alertas se resuelven después; los otros consumidores de `software_monitoring` filtran por
`app_name`, así que no se ven afectados.

## 5. Fases

| Fase | Qué | Estado |
|---|---|---|
| **F1** | Contrato (agente y servidor) + **ingesta en el servidor** + pruebas. | ✅ Hecha |
| **F2** | **Camino Elastic (principal):** `elk/ext_checkdb.*`, lector del agente, `collection_meta`, envío una vez por reinicio, botón de test. | ✅ Hecha (Logstash sin probar contra un entorno real) |
| **F3** | **Camino SQL directo (excepción):** `sql_integrity.py`, estado, detector, trabajador, tests con `pyodbc` simulado. | ✅ Hecha (sin probar contra un SQL Server real) |
| **F4** | **GUI y docs:** tarjetas en SQL y Elastic, tipo de chequeo, lista de bases, tests de permisos y de índice; `CONFIGURACION`, `MODULOS`, `OPERACION`, `CHANGELOG`. | ✅ Hecha |
| **F5** | **Validación en P03 y build 4.5.2.** `VERSION` ya está en 4.5.2. Falta compilar el instalador en Windows y validar (ver §7). | ⏳ Pendiente |

Los tests son con simulación (125 en total, 39 propios de este módulo, más una prueba de la GUI en un
navegador con la API simulada); lo verdaderamente probado contra SQL Server y Logstash sale de F5.

## 6. Riesgos y puntos a validar en P03

- **T-SQL sin probar contra SQL Server real** (no hay ninguno en el entorno de desarrollo, ni un
  validador de T-SQL): el script de `ext_checkdb.sql` y los tests de la GUI que consultan `sys.databases`
  pueden tener un error de sintaxis o de compatibilidad de versión. Se prueba a mano en SSMS primero
  (instrucciones en el encabezado del `.sql`).
- **Logstash:** que el marcador de `sql_last_value` se sustituya como numérico dentro del archivo; que el
  Programador de tareas no lance una segunda instancia mientras la primera sigue corriendo; que el driver
  JDBC no corte una sentencia de horas.
- **Pérdida de un resultado si Elastic está caído justo al terminar un CHECKDB largo:** el seguimiento
  avanza al terminar la consulta, antes de que el output confirme la escritura. Mitigación posible: cola
  persistente (`queue.type: persisted`) en el logstash.yml de este pipeline.
- **Impacto en producción:** el CHECKDB completo compite por I/O y CPU con el RIS/PACS. Usar
  `PHYSICAL_ONLY` si se ve afectación; evaluar `MAXDOP` si hace falta acotarlo.
- **Espacio libre:** el CHECKDB crea un snapshot interno en el mismo volumen y usa `tempdb`; con poco espacio,
  sobre todo tras un corte, puede fallar (sale como `ERROR` de esa base con el mensaje).
- **SQL lento tras un corte:** la recuperación de bases puede tardar; por eso la espera con reintento y
  `NOT_ONLINE` como resultado válido si no se recuperan a tiempo.
- **Credencial ampliada:** `sysadmin`/`db_owner` para `DBCC CHECKDB`; verificar que la activación en un
  hospital no exponga más de lo debido.
- **Trabajador desacoplado (SQL directo):** si el servicio se detiene mientras el trabajador corre, el
  trabajador puede seguir vivo y el agente lo detecta por PID + hora de creación; a validar en Windows real.

## 7. Guía de validación en P03 (F5)

**Antes de tocar el hospital**
1. Compilar el instalador 4.5.2 en Windows (`build.bat`) y actualizar encima de 4.5.1 (la
   configuración se conserva). Con el módulo apagado no cambia nada respecto de 4.5.1.
2. Actualizar y reiniciar el **servidor** primero (ingesta de `sql_integrity`, más el tope de 2 MB).

**Camino Elastic (principal)**
1. En SSMS, probar `elk/ext_checkdb.sql` a mano: reemplazar el marcador de `sql_last_value` por `0`
   (debe devolver la fila `BASELINE`) y por `1` con `@SoloFisico = 1`, `@EsperaMin = 0` y 1–2 bases chicas en
   `@Bases` (debe devolver una fila por base).
2. Instalar `ext_checkdb.conf`, `ext_checkdb.sql` y `ext_checkdb-all-sito.bat` en el servidor ELK; crear la
   tarea programada (cada 15 min, sin instancias en paralelo). La primera corrida solo siembra el valor.
3. Simular un reinicio sin reiniciar la VM: detener la tarea, poner un valor **menor** en el archivo
   `.ext_checkdb_last_run` (por ejemplo `--- 1`) y dejar 1–2 bases chicas y `PHYSICAL_ONLY` en el `.sql`.
4. En la GUI del agente: activar "Integridad de bases (CHECKDB)" en la tarjeta de Elastic y probar el botón
   de test. En el próximo ciclo debe viajar `software_monitoring.sql_integrity` y aparecer filas
   `app_name = 'sql_integrity'` en `software_monitoring` del servidor.
5. Después, con las 26 bases y el tipo elegido, un **reinicio real planificado** de SQL Server.

**Camino SQL directo (solo si el hospital no tiene Elastic)**: ver el procedimiento de prueba de
[OPERACION.md](./OPERACION.md#integridad-de-bases-sql-v452) (editar `baseline_boot` para simular el reinicio).

**Reversa:** desactivar el módulo en la GUI (o instalar 4.5.1) no deja nada colgado; el archivo de estado se
puede borrar y el pipeline de Logstash se quita desactivando su tarea.
