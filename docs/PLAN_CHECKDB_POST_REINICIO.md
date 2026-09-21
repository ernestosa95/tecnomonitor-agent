# Plan — Chequeo de integridad de bases SQL Server tras un reinicio (`sql_integrity`)

**Estado — 2026-09-21: en ejecución.** Decisiones tomadas; F1 (contrato + ingesta del servidor) en
curso. Se entrega en el agente **4.5.2** (el instalador 4.5.1 ya está en P03 y no cambia).

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
| 6 | Qué cuenta como reinicio | El **arranque del servicio SQL** (`sqlserver_start_time`), lo que cubre tanto el reinicio de la VM como el del servicio. Pensado para cortes de energía. |
| 7 | Versión | **4.5.2**. No 4.6: `schema_version` sale de `major.minor` de `VERSION`, y el servidor trata un `"4.6"` desconocido como formato legacy V2, corrompiendo los datos en silencio (ver [PLAN_MEJORAS_V4.5.md §2](./PLAN_MEJORAS_V4.5.md)). |

## 3. Hallazgos que condicionan el diseño

1. **Duración.** Un CHECKDB de 26 bases puede tardar horas; el ciclo del agente es corto y
   síncrono. En modo *tarea programada* cada ciclo es un proceso nuevo (`--run-once`) que termina y
   mata cualquier hilo. El camino SQL directo necesita un **proceso trabajador separado** con
   estado en disco (mismo mecanismo que los checkpoints de KPIs).
2. **Detección de reinicio.** Se compara `sqlserver_start_time` (`sys.dm_os_sys_info`; alternativa
   sin permisos especiales: `create_date` de `tempdb`) con el último valor visto.
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

- `elk/ext_checkdb.conf` + `elk/ext_checkdb.sql` (`statement_filepath`), sin `schedule`, como los
  demás: un ciclo por invocación. Se invoca desde `elk/ext_checkdb-all-sito.bat` (cajón propio).
- `use_column_value => true` con `tracking_column => "sqlserver_start_time"`. El `.sql`:
  1. Lee `sqlserver_start_time`.
  2. Si `:sql_last_value` es el valor inicial → devuelve una única fila `BASELINE` (siembra el valor,
     no corre CHECKDB).
  3. Si el arranque es posterior al último procesado **y** pasó `settle_minutes` → corre el CHECKDB
     base por base y devuelve una fila por base.
  4. Si no → no devuelve filas (el valor no avanza, y se reintenta en la próxima invocación).
- Índice `ext_checkdb`, `document_id` = `<db>_<sqlserver_start_time>` (idempotente ante reintentos).
  El `output` descarta las filas `BASELINE`.

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
| **F1** | Contrato (agente y servidor) + **ingesta en el servidor** + pruebas. | En curso |
| **F2** | **Camino Elastic (principal):** `ext_checkdb.conf/.sql`, `.bat` propio, lector del agente (`collection_meta`, envío una vez por reinicio), botón de test. | Pendiente |
| **F3** | **Camino SQL directo (excepción):** `sql_integrity.py`, estado, detector, trabajador, tests con `pyodbc` simulado. | Pendiente |
| **F4** | **GUI y docs:** toggles en las tarjetas SQL y Elastic, tipo de chequeo, lista de bases, test que verifica permisos; `CONFIGURACION`, `MODULOS`, `CHANGELOG`. | Pendiente |
| **F5** | **Validación en P03 y build 4.5.2.** Simular un reinicio sin reiniciar la VM (borrando la línea base), con 1 o 2 bases chicas y `PHYSICAL_ONLY`; después un reinicio real planificado. Ajustar `VERSION` a 4.5.2 y el test que fija la versión. | Pendiente |

No hay un SQL Server real en el entorno de desarrollo: los tests son con simulación, y lo
verdaderamente probado sale de F5.

## 6. Riesgos y puntos a validar en P03

- **Logstash:** formato y zona horaria de `:sql_last_value` frente a `sqlserver_start_time`
  (`jdbc_default_timezone`); que el Programador de tareas no lance una segunda instancia mientras la
  primera sigue corriendo; que el driver JDBC no corte una sentencia de horas.
- **Impacto en producción:** el CHECKDB completo compite por I/O y CPU con el RIS/PACS. Usar
  `PHYSICAL_ONLY` si se ve afectación; evaluar `MAXDOP` si hace falta acotarlo.
- **SQL lento tras un corte:** la recuperación de bases puede tardar; por eso la espera con
  reintento, y `NOT_ONLINE` como resultado válido si no se recuperan a tiempo.
- **Credencial ampliada:** verificar que la activación en un hospital no exponga más de lo debido.
