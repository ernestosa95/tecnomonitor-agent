# Arquitectura

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
   para configurar el agente y para arrancar/detener/reiniciar el servicio a través del SCM
   de Windows. No recolecta ni envía telemetría.
2. **`TecnoMonitorService.exe`** (servicio de Windows, nombre de servicio `TecnoMonitorAgent`):
   corre en segundo plano de forma permanente, independientemente de si hay una sesión de
   usuario abierta o si la GUI está corriendo. Es el que hace el trabajo real.

Esta separación es intencional (ver [CHANGELOG.md](./CHANGELOG.md), migración v4.3→v4.4):
antes el "agente" era un `while True` lanzado por una tarea programada `ONLOGON`, que dejaba
de funcionar si nadie iniciaba sesión en el servidor. Convertirlo en un servicio real resuelve
ese problema de raíz.

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
| `activity.log` (+ `.1`…`.5`) | Log rotativo de actividad (5 MB × 5 archivos) | Servicio |
| `.sql_checkpoint` | Marca de tiempo hasta donde ya se extrajo del SQL de negocio | Servicio, tras confirmar el envío |
| `.elastic_checkpoint` | Marca de tiempo del último log de Suitestensa procesado | Servicio, tras confirmar el envío |
| `unknowns_lab.json` | Patrones de error no reconocidos por `rules.json`, agrupados localmente | Servicio |

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
