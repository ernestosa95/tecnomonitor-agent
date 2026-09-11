# KPIs de RIS vía ElasticSearch (Logstash → Elastic → Agente)

Migra la extracción de KPIs de negocio (`ris`/`pacs`/`users`) del camino directo a SQL Server
(`extraer_metricas_sql`) a un pipeline de Logstash — el mismo patrón que ya usa el agente para
el autoenrute DICOM (`get_dicom_routing_queues`, ver [MODULOS.md](./MODULOS.md)). Basado en la
guía técnica de colas DICOM que ya está en producción en el hospital.

> Los archivos de referencia (`.conf`, `.bat`) viven en `/elk` en la raíz del repo. **No se
> despliegan por el instalador del agente** — son para aplicar a mano en el servidor de
> Logstash de cada hospital.

> **Corrección de campo (confirmada en el primer hospital piloto, ver
> [CHANGELOG.md](./CHANGELOG.md)):** el diseño original de este documento asumía que la
> instancia de Logstash del hospital corre como un proceso de larga duración con `pipelines.yml`
> y `schedule =>` interno. La convención real observada — al menos en Extensa/Estensa, a juzgar
> por los ~29 pipelines ya existentes en un hospital piloto (`ext_sm1report-sito-vw.conf` y
> similares) — es distinta: **cada pipeline es un `.bat` propio que llama a Logstash con `CALL`,
> sin `schedule =>` en el `.conf`, y una Tarea Programada de Windows dedicada por `.bat` es la
> que dispara la periodicidad.** El `jdbc input` sin `schedule =>` corre la query una sola vez y
> Logstash termina solo; el `.bat` sigue con un `timeout /t 30 /nobreak` después del `CALL`. Los
> `.conf`/`.bat` de `/elk` ya reflejan este patrón — si tu Logstash sí corre como daemon
> persistente con `pipelines.yml`, hay que volver a agregar `schedule =>` a cada `.conf`.

## Por qué no es un simple "gauge" como las colas DICOM

Las colas de autoenrute son un valor "ahora mismo" (`pending_instances`): no hace falta
checkpoint, cualquier corrida de Logstash refleja el estado actual y punto. Los KPIs de RIS son
**contadores acumulados por bloque de tiempo** (cuántos estudios se admitieron/ejecutaron/etc.
entre las 02:00 y las 10:00, por ejemplo) — el agente necesita contar cada bloque **una sola
vez**, y poder recuperar bloques históricos completos si estuvo caído. Por eso el diseño acá es
distinto al de las colas DICOM en un punto importante: en vez de "última foto conocida", Logstash
publica **buckets horarios idempotentes**, y el agente reconstruye sus bloques (del tamaño que
tenga configurado, vía `ris_executions_per_day`) sumando los buckets de Elastic que caen dentro
del rango — con el mismo checkpoint/backfill que ya usa hoy contra SQL Server directo
(`.sql_checkpoint`, sin cambios).

## La trampa de `usuarios_unicos`

`usuarios_unicos` es un `COUNT(DISTINCT User_GUID)` — **no es una suma**. Si Logstash publicara
"cuántos usuarios únicos hubo esta hora" y el agente sumara horas para reconstruir un bloque de
8hs, un técnico logueado a las 08:15 y a las 14:30 se contaría dos veces. Por eso
`ext_users_metrics.conf` no publica un conteo: publica el **array de `User_GUID` distintos de
esa hora, por rol** (`user_guids`). El agente hace la **unión** de esos arrays a través de todas
las horas del bloque y recién ahí cuenta — matemáticamente igual al `COUNT(DISTINCT...)` que
hace hoy SQL Server sobre el bloque completo, sin importar en cuántas horas se reparta.

## Arquitectura

```
Tarea Programada de Windows (una por pipeline, cada 1 hora)
   │  dispara el .bat correspondiente
   ▼
ext_ris_metrics-sito.bat / ext_pacs_metrics-sito.bat / ext_users_metrics-sito.bat
   │  CALL logstash.bat -f <pipeline>.conf   (Logstash corre UNA VEZ y termina solo)
   ▼
SQL Server del hospital
   │  (mismas tablas/columnas que SQL_QUERY en agent_logic.py, ventana = última hora cerrada)
   ▼
Logstash — upsert idempotente (document_id determinístico por hora+clave)
   ▼
ElasticSearch — 3 índices nuevos: ext_ris_metrics_hourly, ext_pacs_metrics_hourly,
                ext_users_metrics_hourly
   ▼
agent_logic.extraer_metricas_ris_elastic()  ←  reemplaza a extraer_metricas_sql()
   (mismo checkpoint .sql_checkpoint, mismo application_metrics de salida)
```

Cada uno de los 3 pipelines es completamente independiente (su propio `.conf`, su propio
`.bat`, su propia Tarea Programada) — no dependen entre sí ni de `ext_dicom_queues`.

## Mapping de los índices nuevos

### `ext_ris_metrics_hourly` (uno por equipo/AET/modalidad/hora)

| Campo | Tipo | Origen |
|---|---|---|
| `equipo`, `aet`, `mod` | string | mismas columnas que `SQL_QUERY.ris` |
| `hour_start` | date | inicio de la hora agregada (siempre una hora ya cerrada) |
| `totales`, `citados`, `admitidos`, `ejecutados`, `con_imagen`, `borradores`, `definitivos`, `suspendidos` | integer | mismos `SUM(CASE...)` que `SQL_QUERY.ris`, acotados a esa hora |

### `ext_pacs_metrics_hourly` (uno por AET/modalidad/hora)

| Campo | Tipo | Origen |
|---|---|---|
| `aet`, `mod` | string | mismas columnas que `SQL_QUERY.pacs` |
| `hour_start` | date | ídem |
| `almacenados` | integer | `COUNT(DISTINCT STUDY_KEY)` acotado a esa hora |

### `ext_users_metrics_hourly` (uno por rol/hora)

| Campo | Tipo | Origen |
|---|---|---|
| `rol` | string | mismo `lsRole.Description` que `SQL_QUERY.users` |
| `hour_start` | date | ídem |
| `inicios_sesion` | integer | `COUNT(a.GUID)` acotado a esa hora — **sí es aditivo**, se suma sin problema |
| `user_guids` | array de string | `User_GUID` **distintos** logueados esa hora para ese rol — **no** un conteo (ver arriba) |

## Los `.conf` y `.bat` (`/elk`)

- `ext_ris_metrics.conf` / `ext_pacs_metrics.conf` / `ext_users_metrics.conf`: `jdbc input`
  **sin `schedule =>`** (ver nota de arriba) — la ventana de 1 hora sale de `GETDATE()` en la
  propia query, no de un cron de Logstash. Reusan la misma lógica `WITH(NOLOCK)`/`CASE`/
  `GROUP BY` de `SQL_QUERY`. El `output` usa un `document_id` determinístico (`fingerprint`
  sobre equipo+aet+mod+hora o aet+mod+hora) para que reprocesar la misma hora sea un upsert, no
  un duplicado — importante porque acá no hay tracking incremental de Logstash sosteniendo el
  estado entre corridas, cada invocación es un proceso nuevo.
- `ext_users_metrics.conf`: además de lo anterior, arma `user_guids_csv` con `STRING_AGG` sobre
  un `SELECT DISTINCT` previo (evita duplicados dentro de la misma hora), y un filtro
  `mutate { split => ... }` lo convierte en array antes de indexar. **Requiere SQL Server
  2017+** por `STRING_AGG` — si el hospital tiene una versión más vieja, reemplazar esa
  sub-consulta por la variante clásica `FOR XML PATH('') + STUFF`.
- `ext_dicom_queues-sito.bat`, `ext_ris_metrics-sito.bat`, `ext_pacs_metrics-sito.bat`,
  `ext_users_metrics-sito.bat`: calco del patrón `CALL ...\logstash.bat -f ...conf` +
  `timeout /t 30 /nobreak` ya usado por los demás pipelines del hospital. Cada uno necesita su
  propia Tarea Programada (ver más abajo) — no hay un `.bat`/tarea compartido entre los tres.

Placeholders a completar antes de usar: `<PASSWORD>` (contraseña del usuario SQL, la misma que
ya usan los demás `.conf` de ese servidor) y `<ELASTIC_HOST>` (IP del cluster Elastic, la misma
que ya usan los demás `.conf`). El host SQL (`SRVDB-ESTENSA` en el hospital piloto) y el usuario
(`sa`) están tomados de un `.conf` existente real — confirmar que coincidan con el servidor del
hospital que estés configurando, pueden variar de un sitio a otro.

**Ninguno de los tres `.conf` de KPIs fue probado de punta a punta contra un SQL Server real
todavía** — la lógica de agregación está copiada 1:1 de `SQL_QUERY`, pero conviene validar la
sintaxis exacta (en particular `STRING_AGG` en `ext_users_metrics.conf`) antes de confiar en
los datos que produce.

## Instalar los pipelines nuevos en un hospital

1. **Copiar los `.conf` y `.bat`** de `/elk` a la carpeta de configuración de Logstash del
   hospital (junto a los pipelines existentes, ej. `C:\Estensa\ELK\Configfile\`), completando
   los placeholders de conexión.
2. **Probar cada `.conf` de forma aislada primero**, antes de programar ninguna tarea:
   ```
   logstash.bat -f ext_ris_metrics.conf --config.test_and_exit
   logstash.bat -f ext_ris_metrics.conf --path.data C:\Estensa\ELK\L\data_test_ris
   ```
   Confirmar en Kibana Dev Tools (`GET ext_ris_metrics_hourly/_search`) que aparecen documentos
   con la forma esperada (ver mapping abajo) antes de seguir. Repetir para los otros dos.
3. **Crear una Tarea Programada por cada `.bat`** (cuatro en total si también se instala
   `ext_dicom_queues`), disparada cada 1 hora, apuntando al `.bat` correspondiente — mismo
   criterio que ya usan las tareas existentes de este hospital para los demás pipelines (mirar
   una tarea ya existente en el Programador de Tareas para copiar su configuración exacta:
   usuario, reintentos, etc.).
4. Si vas a instalar `ext_dicom_queues` en un hospital que **nunca lo tuvo**, no hay nada que
   cuidar de romper — es alta nueva, no migración. Si en cambio ya existe un
   `ext_dicom_queues.conf` corriendo como proceso persistente con `schedule =>` interno (el
   patrón que describe la guía original), no lo reemplaces por el `.bat` de acá sin evaluarlo
   antes: cambiaría su mecanismo de ejecución.

## Configuración del agente

Ver [CONFIGURACION.md](./CONFIGURACION.md#elasticsearch-logs--autoenrute--kpis-de-ris-enabled_elastic--elastic)
para el detalle de los campos nuevos bajo `elastic.*`. Resumen: `enabled_ris_metrics` convive
con `enabled_sql` (no lo reemplaza) — un hospital no migrado sigue usando SQL directo sin
ningún cambio; activar `enabled_ris_metrics` en un hospital puntual una vez que su Logstash ya
esté publicando a los tres índices nuevos.

La GUI tiene un sub-ítem dedicado ("KPIs de RIS vía Elastic") dentro de la tarjeta 8
(ElasticSearch), con su propio botón de test que valida lectura sobre los tres índices en un
solo llamado (`test_connection_ris_metrics` en `agent_logic.py`) — mismo patrón que el sub-ítem
de autoenrute DICOM.

## Prueba de no regresión antes de apagar el camino SQL directo en un hospital

Con ambos caminos configurados (`enabled_sql` + `enabled_ris_metrics`, aunque solo corra el de
Elastic por el `elif`), comparar manualmente el resultado de `extraer_metricas_sql` contra
`extraer_metricas_ris_elastic` para el mismo bloque de tiempo (llamando a ambas funciones desde
una consola Python en el propio servidor, sin pasar por el ciclo completo del agente) antes de
confiar en el camino nuevo para un hospital en producción.
