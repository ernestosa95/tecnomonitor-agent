# KPIs de RIS vía ElasticSearch (Logstash → Elastic → Agente)

Migra la extracción de KPIs de negocio (`ris`/`pacs`/`users`) del camino directo a SQL Server
(`extraer_metricas_sql`) a un pipeline de Logstash — el mismo patrón que ya usa el agente para
el autoenrute DICOM (`get_dicom_routing_queues`, ver [MODULOS.md](./MODULOS.md)). Basado en la
guía técnica de colas DICOM que ya está en producción en el hospital.

> Los archivos de referencia (`.conf`, `pipelines.yml.ejemplo`) viven en `/elk` en la raíz del
> repo. **No se despliegan por el instalador del agente** — son para aplicar a mano en el
> servidor de Logstash del hospital, como ya se hace con `ext_dicom_queues.conf`.

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
SQL Server (ARHCORDSQLV)
   │  (mismas tablas/columnas que SQL_QUERY en agent_logic.py, ventana = última hora cerrada)
   ▼
Logstash — 3 pipelines nuevos, sumados al pipelines.yml existente junto a ext_dicom_queues
   │  upsert idempotente (document_id determinístico por hora+clave)
   ▼
ElasticSearch — 3 índices nuevos: ext_ris_metrics_hourly, ext_pacs_metrics_hourly,
                ext_users_metrics_hourly
   ▼
agent_logic.extraer_metricas_ris_elastic()  ←  reemplaza a extraer_metricas_sql()
   (mismo checkpoint .sql_checkpoint, mismo application_metrics de salida)
```

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

## Los `.conf` (`/elk`)

- `ext_ris_metrics.conf` / `ext_pacs_metrics.conf`: `jdbc input` con `schedule => "5 * * * *"`
  (a los 5 minutos de cada hora, para dar margen a que la hora anterior termine de cerrar del
  todo), reusando la misma lógica `WITH(NOLOCK)`/`CASE`/`GROUP BY` de `SQL_QUERY`. El `output`
  usa un `document_id` determinístico (`fingerprint` sobre equipo+aet+mod+hora o aet+mod+hora)
  para que reprocesar la misma hora sea un upsert, no un duplicado.
- `ext_users_metrics.conf`: además de lo anterior, arma `user_guids_csv` con `STRING_AGG` sobre
  un `SELECT DISTINCT` previo (evita duplicados dentro de la misma hora), y un filtro
  `mutate { split => ... }` lo convierte en array antes de indexar. **Requiere SQL Server
  2017+** por `STRING_AGG` — si el hospital tiene una versión más vieja, reemplazar esa
  sub-consulta por la variante clásica `FOR XML PATH('') + STUFF`.
- `pipelines.yml.ejemplo`: referencia los tres `.conf` nuevos junto al `ext_dicom_queues.conf`
  existente, para que un solo proceso de Logstash (un solo `.bat`, una sola tarea programada)
  corra los cuatro pipelines.

**Ninguno de los tres `.conf` fue probado contra un SQL Server real** (no hay uno accesible
desde donde se escribió este código) — la lógica de agregación está copiada 1:1 de `SQL_QUERY`,
pero conviene validar la sintaxis exacta (en particular `STRING_AGG` en `ext_users_metrics.conf`)
antes de sumarlo a producción.

## Migrar la instancia de Logstash existente

1. **Probar cada `.conf` nuevo de forma aislada primero**, sin tocar la instancia de producción:
   ```
   logstash.bat -f ext_ris_metrics.conf --config.test_and_exit
   logstash.bat -f ext_ris_metrics.conf --path.data C:\Estensa\ELK\L\data_test_ris
   ```
   Confirmar en Kibana/Dev Tools que `ext_ris_metrics_hourly` recibe documentos con la forma
   esperada antes de seguir.
2. Copiar `pipelines.yml.ejemplo` a la carpeta de config de Logstash como `pipelines.yml`,
   ajustando las rutas de `path.config` al layout real del servidor.
3. Cambiar el `.bat` que hoy arranca con `-f ext_dicom_queues.conf` para que arranque **sin**
   `-f` (Logstash carga `pipelines.yml` automáticamente si no se le pasa un archivo puntual).
4. **Hacer este cambio en una ventana de mantenimiento**, no en caliente: es una migración del
   mecanismo de arranque de un pipeline que ya está en producción (autoenrute DICOM). Confirmar
   después de reiniciar que `ext_dicom_queues` sigue publicando normalmente antes de dar por
   cerrada la migración.

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
