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
Tarea Programada de Windows "TecnoMonitor_KPIs_Negocio" (cada 1 hora)
   │  dispara ext_kpis_negocio-all-sito.bat
   ▼
ext_kpis_negocio-all-sito.bat
   │  CALL logstash.bat -f ext_ris_metrics.conf     (corre, termina)
   │  CALL logstash.bat -f ext_pacs_metrics.conf    (corre, termina)
   │  CALL logstash.bat -f ext_users_metrics.conf   (corre, termina)
   ▼
SQL Server del hospital
   │  (mismas tablas/columnas que SQL_QUERY en agent_logic.py, ventana = última hora cerrada)
   ▼
Logstash — upsert idempotente (document_id determinístico por hora+clave), un proceso
           independiente por CALL — no se mezclan datos entre los tres
   ▼
ElasticSearch — 3 índices nuevos: ext_ris_metrics_hourly, ext_pacs_metrics_hourly,
                ext_users_metrics_hourly
   ▼
agent_logic.extraer_metricas_ris_elastic()  ←  reemplaza a extraer_metricas_sql()
   (mismo checkpoint .sql_checkpoint, mismo application_metrics de salida)
```

**Agrupados en una sola Tarea Programada** (`ext_kpis_negocio-all-sito.bat`, con un `CALL
logstash.bat` secuencial por `.conf` — mismo patrón que ya usan los `.bat` "-all-" existentes
del hospital, ej. `ext_cardiocath-all-sito.bat`), porque los tres comparten la misma cadencia
(cada 1 hora). Cada `CALL` sigue siendo un proceso Logstash independiente y autocontenido — no
hay mezcla de datos entre índices, solo se ejecutan uno atrás del otro. Al sumar una medición
nueva con esta misma cadencia horaria, agregar otro bloque `CALL`/`timeout` a este mismo `.bat`
en vez de crear una tarea nueva — la cantidad de tareas depende de cuántas **cadencias**
distintas necesites, no de cuántos `.conf` tengas. `ext_dicom_queues` queda con su propio `.bat`
y tarea aparte porque necesita una cadencia más agresiva (cada 5 min, no cada 1 hora).

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
- `ext_dicom_queues.conf`: el SELECT está confirmado contra una instalación real ya en
  producción en otro hospital (mismas columnas que espera `agent_logic.get_dicom_routing_queues`:
  `idrule`, `fromnode_key/nickname/hostname`, `tonode_key/nickname/hostname`,
  `pending_instances`). Adaptado acá para seguir el mismo patrón sin `schedule =>` que los otros
  tres, aunque la referencia original corría como proceso persistente con cron interno
  (`*/5 * * * *`) — si tu Logstash sí es un daemon persistente, agregar de vuelta
  `schedule => "*/5 * * * *"` al `.conf` y no usar el `.bat` de acá. Solo tiene un `output`
  (índice de estado actual) — no incluye el índice histórico que menciona la guía original para
  Kibana, ya que el agente no lo lee y no está en el alcance de este ciclo.
- `ext_dicom_queues-sito.bat`: calco del patrón `CALL ...\logstash.bat -f ...conf` +
  `timeout /t 30 /nobreak` de un `.conf` individual, ya usado por varios pipelines del hospital.
  Necesita su propia Tarea Programada, cadencia cada 5 min.
- `ext_kpis_negocio-all-sito.bat`: agrupa `ext_ris_metrics.conf`, `ext_pacs_metrics.conf` y
  `ext_users_metrics.conf` en un solo `.bat` (tres `CALL` secuenciales, cada uno un proceso
  Logstash independiente) — mismo patrón que los `.bat` "-all-" existentes del hospital
  (`ext_cardiocath-all-sito.bat` y similares). Una sola Tarea Programada para los tres, cadencia
  cada 1 hora. Para sumar una medición nueva con esta misma cadencia, agregar otro bloque
  `CALL`/`timeout` acá en vez de crear una tarea nueva.

Placeholders a completar antes de usar: `<PASSWORD>` (contraseña del usuario SQL, la misma que
ya usan los demás `.conf` de ese servidor) y `<ELASTIC_HOST>` (IP del cluster Elastic, la misma
que ya usan los demás `.conf`). El host SQL (`SRVDB-ESTENSA` en el hospital piloto) y el usuario
(`sa`) están tomados de un `.conf` existente real — confirmar que coincidan con el servidor del
hospital que estés configurando, pueden variar de un sitio a otro.

**Estado de validación (hospital piloto, `2026-09-11`):** `ext_ris_metrics.conf` corrió contra
`SRVDB-ESTENSA` real sin errores de conexión ni de sintaxis SQL — el JDBC input ejecutó la
query completa (ver troubleshooting arriba para el camino recorrido hasta llegar a esto: JVM +
Elasticsearch). Falta confirmar en Kibana Dev Tools que el documento resultante en
`ext_ris_metrics_hourly` tiene la forma exacta esperada (ver mapping arriba), y repetir la
misma prueba para `ext_pacs_metrics.conf` y `ext_users_metrics.conf` — este último es el que
más conviene revisar de cerca por el `STRING_AGG`. `ext_dicom_queues.conf` está confirmado en
su SQL (viene de una instalación real ya en producción en otro hospital), pendiente la misma
prueba de punta a punta en este sitio.

## Troubleshooting — problemas reales encontrados en el hospital piloto

Esta sección documenta, en el orden en que aparecieron, los problemas reales que aparecieron al
poner esto en marcha por primera vez — **ninguno de los dos terminó siendo un bug de nuestros
`.conf`**, los dos eran problemas preexistentes del servidor. Quedan acá para que el próximo
hospital no tenga que redescubrirlos desde cero.

**Orden de diagnóstico recomendado, de atrás para adelante:** la próxima vez, antes de tocar
ningún `.conf`, conviene primero confirmar que Elasticsearch responde
(`http://<host>:<puerto>` en un navegador) y recién después probar Logstash — así se evita
diagnosticar Logstash por un rato largo cuando el problema de fondo está en el otro extremo,
como pasó acá.

### 1. `JAVA_HOME` externo rompe Logstash

Si el servidor tiene una JDK moderna (ej. 19) instalada y seteada como `JAVA_HOME` a nivel
sistema (para otra aplicación, sin relación con Logstash), **todos** los pipelines de Logstash
fallan al arrancar la JVM — no solo los nuevos, los ~29 ya existentes también, porque el
problema está antes de que Logstash llegue a leer ningún `.conf`. Dos síntomas en cadena, según
qué tan lejos llegue el arranque:

1. `Unrecognized VM option 'UseConcMarkSweepGC'` — el `config/jvm.options` de Logstash trae
   flags del recolector de basura CMS, eliminado de Java desde la versión 14. Fix: comentar
   (`#`) las tres líneas de `## GC configuration` en `jvm.options` (`-XX:+UseConcMarkSweepGC`,
   `-XX:CMSInitiatingOccupancyFraction=75`, `-XX:+UseCMSInitiatingOccupancyOnly`) y dejar que la
   JVM use su recolector por defecto (G1GC en JDK moderno).
2. Con eso resuelto, puede aparecer `InaccessibleObjectException` (ej. sobre
   `java.security.MessageDigest`) — JRuby (que usa Logstash por dentro) necesita acceso
   reflectivo a módulos internos del JDK que las versiones modernas restringen por defecto.
   Parchear esto agregando `--add-opens` de a uno por error que aparezca es un pozo sin fondo.

**La solución de raíz para ambos** es no usar esa JDK externa: Logstash trae su propia JDK
empaquetada, compatible con la versión de JRuby que usa internamente. Los `.bat` de `/elk` ya
incluyen `set JAVA_HOME=` antes de invocar Logstash, precisamente para esto — no toca la
variable a nivel sistema (por si algo más en el servidor sí depende de esa JDK), solo la limpia
para la invocación puntual de Logstash. Si los ~29 pipelines existentes del hospital no tienen
esta línea en sus `.bat`, probablemente estén fallando en silencio también — vale la pena
avisarle a quien administra ese servidor.

**Ojo con la sesión de consola al probar a mano:** `set JAVA_HOME=` solo vale para la ventana de
cmd donde se escribe — una consola nueva vuelve a heredar el `JAVA_HOME` del sistema. Para
probar el fix real, ejecutar el `.bat` de `/elk` (que ya lo limpia por su cuenta) en vez de
llamar a `logstash.bat` directo con `set JAVA_HOME=` tipeado a mano en cada sesión nueva.

### 2. Elasticsearch no arranca — Error 1067, carpeta temporal inaccesible

Un problema completamente distinto y sin relación con Logstash/Java: el servicio de Windows
"Elasticsearch 7.10.2" fallaba con **Error 1067: El proceso terminó inesperadamente** — mensaje
genérico que Windows da cuando el proceso se cae solo, sin indicar la causa real.

La causa real vive en un log que **no** es el principal de Elasticsearch, sino el de stderr del
wrapper del servicio (Commons Daemon/procrun):
```
<carpeta de instalación de Elasticsearch>\logs\elasticsearch-service-x64-stderr.<fecha>.log
```
Ahí apareció:
```
ERROR: Temporary file directory [C:\Users\<cuenta de servicio>\AppData\Local\Temp\elasticsearch] does not exist or is not accessible
```
El servicio corre bajo una cuenta de usuario cuya carpeta temporal (`AppData\Local\Temp\elasticsearch`)
no existía. **Fix aplicado:** crear esa carpeta a mano (`AppData\Local\Temp\elasticsearch`, y
`Temp` también si no existía) bajo el perfil de esa cuenta, y reiniciar el servicio.

**Nota para el futuro (no aplicada todavía, evaluar si vuelve a pasar):** depender de la carpeta
`Temp` de un perfil de usuario puntual es frágil — una limpieza de temporales, una política de
grupo, o un reseteo de perfil pueden volver a borrarla. La alternativa más robusta es fijar la
variable `ES_TMPDIR` a una carpeta dedicada dentro de la propia instalación de Elasticsearch
(ej. `<ES_HOME>\temp`), en vez de depender del `AppData` de una cuenta de servicio.

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
3. **Crear dos Tareas Programadas** (no cuatro — ver "Arquitectura" arriba): una para
   `ext_dicom_queues-sito.bat` (cada 5 min) y otra para `ext_kpis_negocio-all-sito.bat` (cada 1
   hora, dispara los tres pipelines de KPIs en secuencia). Lo más seguro es exportar (`.xml`)
   una tarea ya existente del hospital para ese `.bat` individual, importarla, y solo cambiarle
   el nombre, la ruta del `.bat` y el desencadenador de repetición — así se hereda automáticamente
   la configuración de usuario/privilegios/reintentos ya validada en ese sitio, sin adivinarla.
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
