# Arquitectura

## Principio de diseño: SQL directo + ElasticSearch, siempre los dos caminos

**Toda métrica que dependa de datos de la base de datos del hospital (Extensa RIS/PACS) se
implementa con dos caminos habilitados, no uno solo:**

1. **Directo a SQL Server** — conexión `pyodbc` del propio agente, sin depender de nada externo
   instalado en el hospital.
2. **Vía ElasticSearch** — un pipeline de Logstash (fuera del agente, ver `elk/`) publica los
   mismos datos a un índice, y el agente los lee de ahí en vez de conectar a SQL directo.

Ninguno de los dos es "el nuevo" que reemplaza al otro — **conviven permanentemente**. Decisión
explícita (2026-09-12): no todos los hospitales van a tener siempre la posibilidad de correr un
clúster de Elastic; el camino directo a SQL tiene que seguir existiendo como opción real, no
como compatibilidad legacy a punto de eliminarse. Cuando ambos caminos están configurados a la
vez para la misma métrica, gana Elastic (mismo criterio en los dos ejemplos ya implementados) —
pero un hospital sin Elastic tiene que poder prender solo el camino SQL y tener la métrica
funcionando igual, sin ninguna pérdida de funcionalidad.

**Precedente que rompió esta regla una vez, y no debería repetirse:** el autoenrute DICOM pasó
por un período (v4.4–v4.5) donde el camino SQL directo se eliminó por completo en vez de
quedar como alternativa, dejando sin ese monitoreo a cualquier hospital sin Elastic. Se
restauró en v4.6 — ver
[PLAN_MEJORAS_V4.5.md §1.7](./PLAN_MEJORAS_V4.5.md#17--resuelto-autoenrute-dicom-había-quedado-con-un-solo-camino-posible-solo-elastic).

**Ejemplos ya implementados de este patrón** (ver [MODULOS.md](./MODULOS.md) para el detalle de
cada uno):

| Métrica | Directo a SQL | Vía Elastic |
|---|---|---|
| KPIs de negocio (RIS/PACS/usuarios) | `extraer_metricas_sql` | `extraer_metricas_ris_elastic` |
| Autoenrute DICOM | `obtener_dicom_routing_sql` | `get_dicom_routing_queues` |

**Al agregar una métrica nueva que lea de la base de datos del hospital, implementar los dos
caminos desde el arranque** (no como una fase 2 a futuro) — reusar el patrón de conexión
`pyodbc` ya establecido en `extraer_metricas_sql`/`obtener_dicom_routing_sql` para el lado SQL,
y coordinar con quien administra el ELK del hospital para el lado Elastic (ver
[ELK_RIS_METRICS.md](./ELK_RIS_METRICS.md)).

## Visión general

```
                    ┌─────────────────────────────────────────┐
                    │   Equipo Windows en el hospital          │
                    │                                          │
   Administrador    │   ┌───────────────────┐                 │
   local (GUI)  ───▶│   │ TecnoMonitorConfig │  Eel + Bootstrap │
                    │   │   (main_gui.py)    │  (web/*.html/js) │
                    │   └─────────┬─────────┘                 │
                    │             │ lee/escribe                │
                    │             ▼                            │
                    │   monitor_config.json (ProgramData)       │
                    │             ▲                            │
                    │             │ lee cada ciclo              │
                    │   ┌─────────┴─────────┐                 │
                    │   │TecnoMonitorService │  Servicio de     │
                    │   │(headless_service.py)│  Windows (SCM)  │
                    │   └─────────┬─────────┘                 │
                    │             │ agent_logic.ejecutar_ciclo_agente()
                    │             │                            │
                    └─────────────┼────────────────────────────┘
                                  │  HTTPS POST (Bearer token)
                                  ▼
                       Servidor central TecnoMonitor
                       (tecnomonitor.tecnoimagen.com.ar)
```

Hay **dos procesos independientes** que comparten el mismo `monitor_config.json` pero no se
comunican directamente entre sí:

1. **`TecnoMonitorConfig.exe`** (GUI): solo se ejecuta cuando un administrador la abre. Sirve
   para configurar el agente y para arrancar/detener/reiniciar la recolección — a través del
   SCM de Windows o del Programador de Tareas, según el modo elegido al instalar (ver abajo).
   No recolecta ni envía telemetría.
2. **`TecnoMonitorService.exe`** (mismo ejecutable, invocado de dos formas distintas según el
   modo — ver la sección siguiente): corre en segundo plano y es el que hace el trabajo real.

Esta separación es intencional (ver [CHANGELOG.md](./CHANGELOG.md), migración v4.3→v4.4):
antes el "agente" era un `while True` lanzado por una tarea programada `ONLOGON`, que dejaba
de funcionar si nadie iniciaba sesión en el servidor. Convertirlo en un servicio real resuelve
ese problema de raíz.

## Modos de ejecución: Servicio vs. Tarea Programada (v4.5.0)

Desde v4.5.0, el instalador pregunta qué mecanismo registrar. Ambos usan el mismo binario
compilado (`TecnoMonitorService.exe`, `--onedir`), invocado de forma distinta:

| | Servicio de Windows (default) | Tarea Programada |
|---|---|---|
| Registro | SCM (`sc create`, vía `win32serviceutil`) | Programador de Tareas (API COM `Schedule.Service`, `task_control.py`) |
| Privilegios para instalar | Administrador (siempre) | Administrador (el instalador entero lo exige igual), pero la tarea en sí queda con permisos estándar, sin "privilegios más altos" |
| Modelo de ejecución | Un proceso de larga duración (`_bucle()`, `while not self.detener`) | Un proceso efímero por ciclo (`--run-once`), repetido por el propio disparador de la tarea |
| Sobrevive a logoff/reinicio | Sí | **No** — se detiene hasta que alguien vuelva a iniciar sesión |
| Recuperación ante crash | `sc failure` (reinicio automático) | Ninguna explícita — el próximo disparo programado simplemente vuelve a intentar |
| Por qué elegirlo | Caso general | Hospitales cuya política de seguridad bloquea la creación de servicios nuevos (vector de persistencia común), aun permitiendo tareas programadas simples |

El modo elegido se persiste en `%PROGRAMDATA%\TecnoMonitor\install_mode.txt` (`"service"` o
`"task"`, escrito por `TecnoMonitor.iss`). `service_control.py` lo lee y despacha cada función
pública (`iniciar`, `detener`, `esta_corriendo`, `estado_legible`, `reiniciar`) a la
implementación correspondiente (la suya propia para servicio, o `task_control.py` para tarea) —
`main_gui.py` no necesita saber cuál de las dos está corriendo. Si el archivo no existe
(instalaciones de antes de v4.5.0), se asume `"service"`.

**Por qué el modo tarea no es simplemente "resucitar la tarea de v4.3"**: aquella lanzaba un
`while True` de larga duración bajo el Programador de Tareas, con un candado por socket que
quedaba en `TIME_WAIT`. El modo tarea de v4.5.0 evita esa clase de problema de raíz: cada
invocación de `--run-once` hace un solo ciclo y sale (ver `ejecutar_un_ciclo()` en
`headless_service.py`, compartida con el modo servicio) — no hay proceso de larga duración cuyo
candado se pueda perder. El mutex existente (`Global\TecnoMonitorAgent_SingleInstance`) se toma
y libera alrededor de ese único ciclo, como defensa adicional junto a la propia protección de
solapamiento del Programador de Tareas.

## Ciclo de vida del servicio (`headless_service.py`)

1. El SCM instancia `TecnoMonitorService` y llama a `SvcDoRun()`.
2. Se verifica que no exista un agente v4.3 "legacy" sobreviviente (sondeo del puerto TCP
   `64999` que usaba el candado de esa versión) — si lo hay, el servicio registra el problema
   en el log y en el Visor de Eventos, y **no arranca**, para evitar que dos agentes reporten
   en paralelo y se pisen los checkpoints.
3. Se toma un mutex global (`Global\TecnoMonitorAgent_SingleInstance`) como segunda barrera
   anti-duplicados (por si alguien ejecuta el `.exe` a mano además del servicio).
4. Entra en el bucle principal (`_bucle`):
   - Carga y desencripta `monitor_config.json`.
   - Si el módulo SQL está habilitado, extrae el bloque de KPIs pendiente (`extraer_metricas_sql`).
   - Llama a `agent_logic.ejecutar_ciclo_agente(cfg)`, que recolecta todos los módulos
     habilitados, arma el envelope y lo envía al servidor central.
   - Duerme `interval_minutes` (por defecto 5) usando un `WaitForSingleObject` sobre un evento
     de stop, en vez de `time.sleep()` — así un `SvcStop` interrumpe la espera al instante en
     vez de esperar hasta el próximo ciclo.
5. Ante `SvcStop`/`SvcShutdown` (apagado de Windows, `services.msc`, botón de la GUI): se marca
   la bandera de detención y se espera a que **termine el ciclo de recolección en curso** antes
   de salir — no se corta un envío a la mitad.
6. Cualquier excepción no controlada en el bucle se loguea y se re-lanza, para que el SCM la
   trate como una caída del proceso y dispare las *failure actions* configuradas por el
   instalador (reinicio automático, ver [INSTALACION.md](./INSTALACION.md)).

## Módulo de configuración (`main_gui.py` + `web/`)

- La GUI usa [Eel](https://github.com/python-eel/Eel): un servidor local que expone funciones
  Python (`@eel.expose`) para que el JavaScript del frontend las invoque como si fueran
  funciones asíncronas normales (`await eel.funcion(args)()`).
- El frontend (`web/index.html`, `web/script.js`) es una única página con Bootstrap 5 que arma
  el formulario de configuración, dispara los botones de "Test conexión" de cada integración,
  y hace polling cada pocos segundos para refrescar el estado del servicio y el log en vivo.
- Al guardar la configuración (`guardar_config`), las contraseñas se cifran con `security.py`
  antes de escribir a disco, y el servicio se reinicia (`service_control.reiniciar()`) para
  que tome los cambios — el agente no relee la configuración en caliente entre ciclos salvo
  reiniciando.

## Persistencia en disco

Todo el estado persistente vive en `%PROGRAMDATA%\TecnoMonitor` (`security.get_app_data_path()`):

| Archivo | Contenido | Escrito por |
|---|---|---|
| `monitor_config.json` | Configuración completa (credenciales cifradas) | GUI (`guardar_config`) |
| `secret.key` | Clave Fernet para cifrar/descifrar credenciales | `security.py` (autogenerada al primer uso) |
| `admin.hash` | Hash SHA-256 del código de acceso a la GUI (v4.5+) | `security.py` (autogenerado al primer uso; borrarlo resetea el acceso) |
| `install_mode.txt` | `"service"` o `"task"` — qué modo de ejecución se eligió al instalar (v4.5+) | `TecnoMonitor.iss` (una sola vez, al instalar); leído por `service_control.py` |
| `activity.log` (+ `.1`…`.5`) | Log rotativo de actividad (5 MB × 5 archivos) | Servicio |
| `.sql_checkpoint` | Marca de tiempo hasta donde ya se extrajo del SQL de negocio | Servicio, tras confirmar el envío |
| `.elastic_checkpoint` | Marca de tiempo del último log de Suitestensa procesado | Servicio, tras confirmar el envío |
| `unknowns_lab.json` | Patrones de error no reconocidos por `rules.json`, agrupados localmente | Servicio |
| `.sql_integrity_state_<hospital>` | Estado del chequeo de integridad de bases SQL (línea base, corrida, enviado) — v4.5.2 | Servicio (un solo escritor) |
| `.sql_integrity_results_<hospital>` | Resultado por base del chequeo directo a SQL — v4.5.2 | Proceso trabajador `--sql-integrity-worker` (un solo escritor) |
| `sql_integrity_worker.log` | Log del proceso trabajador (rotado a 1 MB) — v4.5.2 | Proceso trabajador |

`rules.json` **no** vive en `ProgramData`: se resuelve relativo al ejecutable
(`os.path.dirname(os.path.abspath(__file__))`), y PyInstaller lo empaqueta junto al `.exe`
del servicio (`--add-data "rules.json;."`). Actualizar las reglas implica redistribuir el
archivo junto al agente, no es un dato de configuración por hospital.

Todas las escrituras de archivos de estado (config, checkpoints, clave) se hacen de forma
**atómica**: se escribe a un `.tmp` y se hace `os.replace()` al nombre final, para que un
corte de luz o un crash a mitad de escritura nunca deje el archivo corrupto a medias.

## Concurrencia interna

- La recolección WMI de VMs/workstations/equipos se paraleliza con
  `ThreadPoolExecutor(max_workers=5)` (una tarea por equipo configurado), con un timeout duro
  de 90 s por equipo controlado desde un hilo dedicado (`obtener_vm_data`).
- La recolección de storage RAID de iDRAC (`obtener_storage_fisico_v3`) paraleliza las
  consultas Redfish a controladoras/volúmenes/discos con pools de 5 y 10 hilos, para no
  acercarse al timeout global del ciclo en servidores con muchos arreglos.
- El resto de los módulos (Proxmox, SQL, Mirth, SSL, Elastic) se ejecutan de forma secuencial
  dentro de `ejecutar_ciclo_agente`.

Ver [MODULOS.md](./MODULOS.md) para el detalle de qué recolecta cada uno.
