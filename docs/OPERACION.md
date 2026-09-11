# Operación y troubleshooting

## Control del servicio

El servicio de Windows se llama `TecnoMonitorAgent`. Puede controlarse desde:

- La GUI (`TecnoMonitorConfig.exe`), botón de Iniciar/Detener Monitoreo — internamente llama a
  `service_control.iniciar()` / `.detener()`, que usan `win32serviceutil` contra el SCM (no
  `sc.exe` ni `schtasks`, para no depender de parsear texto que cambia según el idioma del
  Windows instalado).
- Directamente por línea de comandos (como administrador), útil si la GUI no arranca:
  ```
  sc query TecnoMonitorAgent
  sc start TecnoMonitorAgent
  sc stop TecnoMonitorAgent
  ```
- Reinstalación/registro manual del servicio (requiere estar en la carpeta `service\` del
  ejecutable instalado):
  ```
  TecnoMonitorService.exe --startup auto install
  TecnoMonitorService.exe start
  TecnoMonitorService.exe stop
  TecnoMonitorService.exe remove
  ```

### Tiempos de espera esperables

- **Arrancar** el servicio puede tardar hasta 30 s (`TIMEOUT_START` en `service_control.py`)
  antes de que la GUI reporte éxito/fallo.
- **Detenerlo** puede tardar hasta 60 s (`TIMEOUT_STOP`): el servicio espera a terminar el
  ciclo de recolección en curso (WMI/iDRAC/SQL/Elastic pueden tardar) antes de salir, en vez de
  cortarlo a la mitad. Si un ciclo está atascado en una llamada de red sin timeout corto, el
  stop puede demorar más de lo esperado.
- Al **guardar la configuración** desde la GUI, se dispara un `reiniciar()` (stop + start
  secuencial, esperando confirmación de cada paso) y el botón de monitoreo queda bloqueado
  visualmente por 30 s (`bloquearBotonMonitoreo` en `script.js`) para dar tiempo a que el
  nuevo proceso termine de levantar.

## Interpretar mensajes de error comunes de la GUI

| Mensaje | Causa | Acción |
|---|---|---|
| "Requiere privilegios de Administrador" | La GUI no se abrió con UAC elevado, o el usuario no tiene permisos sobre el SCM | Cerrar y volver a abrir con "Ejecutar como administrador" |
| "El servicio TecnoMonitorAgent no está instalado..." | Instalación incompleta, o se ejecutó `remove` sin volver a `install` | Reinstalar con el setup, o correr `TecnoMonitorService.exe --startup auto install` como admin |
| "El servicio ya está en ejecución" / "no estaba en ejecución" | Carrera con otra acción simultánea (poco común) | Ignorar si el estado final es el esperado; refrescar el badge de estado |

## Logs

- Archivo: `%PROGRAMDATA%\TecnoMonitor\activity.log`, visible en vivo desde la GUI (polling
  cada 2 s vía `leer_log_delta`, que lee solo el delta desde la última posición leída — no
  relee el archivo completo en cada refresco).
- Rotación: 5 MB por archivo, hasta 5 archivos de historia (`activity.log`, `.1`… `.5`) — techo
  de ~30 MB, suficiente para varios meses de operación con `interval_minutes=5`.
- Formato: `[YYYY-MM-DD HH:MM:SS] mensaje`.
- Botón "Limpiar consola" en la GUI trunca el archivo y escribe una marca
  (`--- Log limpiado por el administrador ---`); no rota ni conserva lo anterior.
- Si `activity.log` no se puede abrir (permisos, disco lleno), el servicio cae a
  `logging.basicConfig` como fallback — y si ni eso funciona, el arranque/caída del servicio
  igual queda registrado en el **Visor de Eventos de Windows** (aplicación → origen
  "TecnoMonitor"), vía `servicemanager.LogInfoMsg/LogErrorMsg`. Es la única traza disponible si
  el servicio muere antes de poder escribir a `ProgramData`.

### Qué buscar en el log ante un problema

- `🚨 Se detectó un agente v4.3 todavía en ejecución` → hay una tarea programada
  `TecnoMonitor_AutoStart` o un proceso viejo compitiendo. Ver sección de migración en
  [INSTALACION.md](./INSTALACION.md).
- `🚨 Ya hay una instancia del agente corriendo` → el mutex global ya estaba tomado; confirmar
  que no haya un segundo proceso del servicio corriendo a mano.
- `⏱️ Timeout WMI (<ip>): no respondió en 90s` → el equipo no está caído (pasó el chequeo de
  puerto 135) pero WMI no contestó a tiempo; frecuente con equipos sobrecargados o con el
  servicio WMI degradado.
- `❌ Autoenrute DICOM: índice '<index>' desactualizado` → el pipeline de Logstash que alimenta
  el índice de autoenrute está caído o atrasado; no es un problema del agente.
- `⚠️ El usuario no tiene permiso de lectura sobre '<index>' (HTTP 403)` → usuario de Elastic sin
  rol sobre el índice de autoenrute (puede tener permiso sobre los logs y no sobre este índice
  — por eso existe el test dedicado, ver [MODULOS.md](./MODULOS.md)).

## Checkpoints

| Archivo | Qué controla | Cómo resetearlo |
|---|---|---|
| `.sql_checkpoint` | Hasta dónde se extrajeron los KPIs de negocio | Botón "Resetear historial SQL" en la GUI (`reset_historial_sql`), o borrar el archivo manualmente con el servicio detenido |
| `.elastic_checkpoint` | Hasta qué timestamp se procesaron logs de Elastic | Borrar el archivo manualmente con el servicio detenido (no tiene botón dedicado en la GUI) |

**Resetear el checkpoint SQL fuerza un backfill histórico completo** desde
`sql.historical_start_date` en el próximo ciclo, procesando un bloque (`24/executions_per_day`
horas) por ciclo hasta alcanzar el presente — puede tardar horas si el rango histórico es
grande. La GUI ya advierte esto en el diálogo de confirmación.

Ambos checkpoints se escriben de forma atómica (`.tmp` + `os.replace`) y solo después de que el
POST al servidor central confirme éxito — ver [ENVELOPE_API.md](./ENVELOPE_API.md).

## Diagnóstico de disco (`debug_disk.py`)

Script standalone (no se empaqueta ni se instala) para validar manualmente, en la consola,
cómo se comporta `psutil.disk_io_counters` en el equipo — mide operaciones y tiempo activo de
`PhysicalDrive0` en ventanas de 1 s y calcula latencia promedio. Útil para contrastar contra lo
que reporta el módulo WMI de latencia de disco cuando se sospecha un valor inconsistente.
Requiere Ctrl+C para detenerlo.

## Preguntas frecuentes de operación

**¿Puedo editar `monitor_config.json` a mano?**
No es el flujo soportado: las contraseñas se guardan cifradas con una clave (`secret.key`)
específica de ese equipo, así que escribirlas en texto plano requeriría luego pasar por la GUI
para que se recifren igual. Además, guardar desde la GUI siempre reinicia el servicio; un
editado a mano no lo hace, y el servicio seguiría con la configuración vieja en memoria hasta
el próximo reinicio manual.

**¿Qué pasa si el servidor central está caído por varias horas?**
Los módulos de estado (WMI, iDRAC, Proxmox, Mirth, SSL) simplemente no logran enviar ese ciclo
y no hay pérdida real, porque son snapshots del estado *actual* — el próximo ciclo exitoso
refleja igual el estado vigente. Los módulos con checkpoint (SQL, Elastic) tampoco pierden
datos: como el checkpoint no avanza sin confirmación de envío, retoman exactamente donde
quedaron en cuanto el servidor vuelve a estar disponible.
