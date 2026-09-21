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

**Estrategia: agrupar por cadencia, no por dominio.** La cantidad de `.bat`/Tareas Programadas
depende de cuántas **cadencias** distintas necesitás, no de cuántos `.conf` tengas — sumar una
medición nueva con una cadencia ya existente es agregar un bloque `CALL`/`timeout` al `.bat` de
ese cajón, no crear una tarea nueva. Tres cajones definidos hoy (el contenido de cada uno se
termina de definir por separado, ver [MODULOS.md](./MODULOS.md) y este mismo documento a medida
que se agreguen más `.conf`):

| Cajón | `.bat` | Disparador de la tarea | Qué vive ahí hoy |
|---|---|---|---|
| Tiempo real | `ext_tiempo_real-all-sito.bat` | Repetir cada 5 min | `ext_dicom_queues.conf` |
| Métricas de negocio | `ext_kpis_negocio-all-sito.bat` | Repetir cada 1 hora | `ext_ris_metrics.conf`, `ext_pacs_metrics.conf`, `ext_users_metrics.conf` |
| Al reinicio | `ext_al_reinicio-all-sito.bat` | Al iniciar el equipo (sin repetición) | *(pendiente de definir)* |

Cada `CALL` dentro de un `.bat` sigue siendo un proceso Logstash independiente y autocontenido
— no hay mezcla de datos entre índices, solo se ejecutan uno atrás del otro dentro de la misma
corrida de la tarea. Mismo patrón que ya usan los `.bat` "-all-" existentes del hospital (ej.
`ext_cardiocath-all-sito.bat`).

El cajón "al reinicio" es para datos que solo tiene sentido recalcular cuando la VM/servidor
arranca de nuevo (no una serie de tiempo continua) — el disparador en el Programador de Tareas
es **"Al iniciar el equipo"**, sin desencadenador de repetición.

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
| `user_guids` | array de string | `User_GUID` **distintos** logueados esa hora para ese rol — **no** un conteo (ver arriba). ⚠️ En la práctica puede llegar como **string suelto** (no envuelto en `[...]`) cuando hubo un único GUID esa hora para ese rol — `STRING_AGG` no deja coma que partir y el `mutate.split` de `ext_users_metrics.conf` no siempre lo deja como lista de un elemento. El agente lo tolera desde `4.5.1` (`_normalizar_user_guids` en `agent_logic.py`, corre antes de validar); antes de ese fix el bloque horario entero quedaba rechazado y el checkpoint nunca avanzaba (visto en producción, hospital P03, 2026-09-18). No se tocó el `.conf` — arreglarlo ahí requeriría redesplegar Logstash en cada hospital afectado. |

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
- `ext_tiempo_real-all-sito.bat`: cajón de cadencia "cada 5 min" — hoy solo llama a
  `ext_dicom_queues.conf`, pero está pensado como "-all-" para sumar ahí cualquier otra medición
  futura con esta misma cadencia (ver tabla de cajones arriba).
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

**Estado de validación (hospital piloto, `2026-09-11`):**

- ✅ `ext_ris_metrics.conf` — confirmado de punta a punta: conectó a `SRVDB-ESTENSA`, ejecutó la
  query completa sin errores, conectó a Elasticsearch, indexó y el documento resultante en
  `ext_ris_metrics_hourly` se verificó con la forma esperada (ver mapping arriba). Corrido a
  mano con `CALL logstash.bat -f ext_ris_metrics.conf` — todavía no vía el `.bat`/Tarea
  Programada real.
- ⏳ `ext_pacs_metrics.conf` / `ext_users_metrics.conf` — mismo diseño y mismo entorno ya
  validado con `ext_ris_metrics.conf`, pero **no probados individualmente todavía**. Repetir la
  misma prueba manual antes de confiar en ellos — `ext_users_metrics.conf` es el que más
  conviene revisar de cerca por el `STRING_AGG`.
- ⏳ `ext_dicom_queues.conf` — SQL confirmado en otra instalación real (otro hospital), pero
  **sin probar todavía en este sitio**.

**Segundo hospital, ya con Tareas Programadas reales (`2026-09-16`):** los 3 puntos ⏳ de
arriba quedaron confirmados acá — `ext_pacs_metrics.conf`, `ext_users_metrics.conf` (incluido
el `STRING_AGG`) y `ext_dicom_queues.conf` corrieron sin ningún `ERROR` en el log de Logstash,
tanto a mano como disparados por `TecnoMonitor_Tiempo_Real`/`TecnoMonitor_KPIs_Negocio` reales.
Los índices de RIS y usuarios no aparecían al principio (`404` al testear desde la GUI) — se
confirmó con una query directa en SQL Server que la hora evaluada tenía **0 filas reales**, no
un problema del pipeline: Elastic no crea un índice hasta que Logstash escribe su primer
documento. Ese mismo caso reveló un bug real en el agente (`_buscar_bucket_horario` trataba ese
`404` como error de conexión en vez de "0 documentos", ver [CHANGELOG.md](./CHANGELOG.md)) —
corregido. Detalle completo de los dos problemas encontrados (uno del Programador de Tareas, uno
del agente) en la sección de troubleshooting más abajo.

### Pendiente para retomar

1. ✅ Probar `ext_pacs_metrics.conf`, `ext_users_metrics.conf` y `ext_dicom_queues.conf` a mano —
   hecho en el segundo hospital (`2026-09-16`).
2. ✅ Crear las Tareas Programadas de los cajones ya definidos (`ext_tiempo_real-all-sito.bat`
   cada 5 min, `ext_kpis_negocio-all-sito.bat` cada 1 hora) — hecho. `ext_al_reinicio-all-sito.bat`
   sigue sin tarea propia, pendiente de decidir qué `.conf` va ahí.
3. ✅ Dejar correr al menos un ciclo completo vía la Tarea Programada (no a mano) — confirmado
   para ambas tareas tras corregir la condición de energía (ver troubleshooting §3 abajo).
4. Activar `enabled_ris_metrics` (y `enabled_dicom_routing` si corresponde) en la GUI del agente
   para este hospital, **guardar los cambios del hospital**, y confirmar en `activity.log` que
   el ciclo del agente levanta los datos correctamente (buscar `⚙️ RIS/Elastic: Extrayendo
   bloque regular`) — sigue pendiente, los botones de "Test" ya dieron OK para los 4 índices
   pero falta confirmar que quedó guardado y corriendo en un ciclo real del agente (no solo el
   test manual de conexión).

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

### 3. Las Tareas Programadas corren bien a mano ("Run") pero nunca disparan solas

Encontrado al armar `TecnoMonitor_Tiempo_Real`/`TecnoMonitor_KPIs_Negocio` en un hospital real
(`2026-09-16`), importando una tarea `ext_*-sito-vw.bat` existente como plantilla (ver
"Instalar los pipelines nuevos" más abajo). Síntoma: el trigger se ve bien configurado
(`Daily`, `Repeat task every 5 minutes, for a duration of: Indefinitely`, `Enabled`), corre sin
error cuando se lo dispara a mano con clic derecho → **Run**, pero el **`Next Run Time`** de la
lista principal sigue avanzando solo (cada 5 min, cada 1 hora) sin que aparezca ninguna corrida
nueva en el **History** — ni siquiera un intento fallido logueado. Windows descarta el disparo
en silencio y reprograma el siguiente.

**Causa:** la plantilla usada como base trae, en la pestaña **Conditions**, tildado **"Start the
task only if the computer is on AC power"** — el default de Windows para tareas nuevas, pensado
para notebooks, sin sentido en un servidor. Combinado con **"Run task as soon as possible after
a scheduled start is missed"** destildado (pestaña **Settings**, también default), cualquier
disparo que no pueda cumplir la condición de energía se **pierde sin reintentarlo y sin dejar
rastro** en el Event Log — no aparece ni como corrida exitosa ni como fallida.

**Fix aplicado:** en cada tarea nueva creada a partir de una plantilla existente,
1. **Conditions** → destildar "Start the task only if the computer is on AC power" (y de paso
   "Stop if the computer switches to battery power", que queda irrelevante).
2. **Settings** → tildar "Run task as soon as possible after a scheduled start is missed", como
   red de seguridad adicional por si alguna vez vuelve a chocar con la regla "Do not start a new
   instance" (default de "If the task is already running...").

Confirmado con el `History` de ambas tareas: tras el fix, aparecieron corridas nuevas sin que
nadie tocara "Run". **Revisar esto en cada hospital nuevo** al exportar/importar una tarea
existente como base — no es evidente en la pestaña General/Triggers, hay que entrar
específicamente a Conditions.

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
3. **Crear una Tarea Programada por cajón de cadencia** (ver tabla en "Arquitectura" arriba):
   `ext_tiempo_real-all-sito.bat` (cada 5 min), `ext_kpis_negocio-all-sito.bat` (cada 1 hora),
   y `ext_al_reinicio-all-sito.bat` (al iniciar el equipo) cuando tenga algún `.conf` asignado.
   Lo más seguro es exportar (`.xml`) una tarea ya existente del hospital, importarla, y solo
   cambiarle el nombre, la ruta del `.bat` y el desencadenador — así se hereda automáticamente
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
