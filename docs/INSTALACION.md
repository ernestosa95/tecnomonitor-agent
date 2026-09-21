# Instalación

## Requisitos

- Windows con permisos de administrador (el instalador exige `PrivilegesRequired=admin`).
- Arquitectura x64 (`ArchitecturesInstallIn64BitMode=x64`).
- Para el módulo VMware: `pyVmomi` debe estar disponible en el entorno de ejecución del agente
  (se detecta en runtime; si falta, el módulo reporta el error de forma controlada en vez de
  romper el ciclo completo).
- Conectividad saliente HTTPS hacia `central_url` (servidor central) y hacia cada integración
  habilitada (Proxmox/vCenter, iDRAC, SQL Server, Mirth, ElasticSearch, URLs SSL a monitorear).

## Qué instala `TecnoMonitor_v<versión>_Setup.exe` (el nombre sale de `/VERSION`)

El instalador está definido en [`TecnoMonitor.iss`](../TecnoMonitor.iss) (Inno Setup). Pasos,
en orden:

1. **`InitializeSetup`** (antes de copiar archivos) — "cirugía" de instalaciones previas:
   - Detiene y elimina el servicio `TecnoMonitorAgent` si ya existe.
   - Elimina la tarea programada `TecnoMonitorAgent_Task` si existe (una instalación 4.5+
     previa en modo tarea) y `TecnoMonitor_AutoStart` si existe (rastro de v4.3).
   - Mata procesos huérfanos `TecnoMonitorService.exe` / `TecnoMonitorConfig.exe`.
2. **Página nueva (v4.5.0): modo de ejecución.** El wizard pregunta entre:
   - **Servicio de Windows** (por defecto) — arranca solo, sobrevive a reinicios y cierres de
     sesión.
   - **Tarea programada** — sin "ejecutar aunque no haya sesión iniciada" ni "privilegios más
     altos"; pensada para hospitales cuya política de seguridad bloquea la creación de
     servicios nuevos. Se detiene al cerrar sesión/reiniciar hasta que alguien vuelva a
     loguearse. El instalador en sí sigue exigiendo admin en ambos casos (`PrivilegesRequired=admin`
     no cambia según la elección) — ver [ARQUITECTURA.md](./ARQUITECTURA.md#modos-de-ejecución-servicio-vs-tarea-programada-v450).
3. Copia archivos a `{Archivos de programa}\TecnoMonitor`:
   - `service\` → build completo `--onedir` de `TecnoMonitorService` (el `.exe` + sus DLLs;
     el mismo binario sirve ambos modos).
   - `TecnoMonitorConfig.exe` → GUI, un único ejecutable portable.
   - `logo.ico`.
4. **Si se eligió Servicio:**
   - Registra el servicio: `TecnoMonitorService.exe --startup auto install`.
   - Configura arranque **retrasado** (`sc config TecnoMonitorAgent start= delayed-auto`) para
     no competir por I/O y red con el resto de los servicios del hospital durante el boot.
   - Configura **recuperación automática ante caídas**: hasta 3 reinicios (al minuto de cada
     falla), con el contador de fallos reseteándose cada 24 h
     (`sc failure ... reset= 86400 actions= restart/60000/restart/60000/restart/60000`).
   - Arranca el servicio inmediatamente (`sc start TecnoMonitorAgent`), sin esperar al próximo
     reinicio del equipo.
5. **Si se eligió Tarea programada:** registra la tarea (`TecnoMonitorService.exe install-task`,
   ver [task_control.py](../task_control.py)) — ya queda habilitada con un intervalo por
   defecto de 5 minutos; la GUI ajusta ese intervalo al valor real la primera vez que se guarda
   la configuración.
6. Escribe `install_mode.txt` (`"service"` o `"task"`) en `%PROGRAMDATA%\TecnoMonitor` — lo lee
   `service_control.py` para saber a quién despachar en tiempo de ejecución.
7. Ofrece abrir la GUI de configuración al terminar (`postinstall`, opcional).
8. Crea accesos directos en el Menú Inicio y, si se marcó la tarea, en el Escritorio.

### Por qué el servicio se compila `--onedir` y no `--onefile`

El bootloader de PyInstaller en modo `--onefile` descomprime y relanza un **proceso hijo**; el
SCM de Windows queda con el PID del proceso padre (el bootloader), que no responde a las
señales de control de servicio. El resultado observado en v4.3 era que Windows terminaba
matando el servicio por timeout al intentar detenerlo. Con `--onedir`, el ejecutable que el SCM
registra es el mismo proceso que corre el bucle, así que sí responde correctamente a
start/stop/shutdown. Ver [BUILD.md](./BUILD.md).

## Desinstalación

`[UninstallRun]` en el `.iss` intenta la limpieza de **ambos modos**, sin importar cuál se usó
— el desinstalador no tiene disponible la elección del wizard de instalación, y borrar algo que
no existe (un servicio o una tarea que nunca se registraron) es un no-op inofensivo:

1. Detiene el servicio (si existe) y lo desregistra (`TecnoMonitorService.exe remove`).
2. Desregistra la tarea programada (si existe) — `TecnoMonitorService.exe remove-task`, más un
   `schtasks /Delete` de respaldo por si el `.exe` ya no estuviera disponible.
3. Mata la GUI si estaba abierta.
4. Limpia cualquier resto de la tarea programada `TecnoMonitor_AutoStart` de v4.3.

**Nota:** la desinstalación no borra `%PROGRAMDATA%\TecnoMonitor` (configuración, logs,
checkpoints, clave de cifrado) — eso queda en el equipo salvo borrado manual. Es una decisión
de diseño razonable (permite reinstalar sin perder configuración/histórico), pero conviene
tenerlo presente si se retira el agente de un equipo por motivos de seguridad.

## Migración desde v4.3

v4.3 usaba una tarea programada (`TecnoMonitor_AutoStart`, disparada `ONLOGON`) y un candado
por socket en `127.0.0.1:64999` para evitar instancias duplicadas. v4.4 migra a un servicio de
Windows real con un mutex con nombre. El instalador y el propio servicio (`detectar_agente_legacy`
en `headless_service.py`) están preparados para detectar y avisar si un agente v4.3 sigue vivo
durante la transición, ya que ambos mecanismos de candado son independientes entre sí y no se
detectan mutuamente — si conviven, duplican telemetría y corrompen los checkpoints compartidos.

## Primera configuración

1. Abrir `TecnoMonitorConfig.exe` (requiere UAC — se compila con `--uac-admin`).
2. La primera vez, la GUI genera un código de acceso único para ese equipo y lo muestra una
   sola vez en el propio overlay — **anotarlo antes de continuar**, no se vuelve a mostrar. En
   aperturas siguientes, se pide ese mismo código (ver [SEGURIDAD.md](./SEGURIDAD.md) para el
   mecanismo completo, la recuperación si se pierde, y sus límites actuales).
3. Completar `Hospital ID`, `Auth Token` y `URL del servidor central`. **El `Auth Token` no se
   inventa acá**: hay que generarlo antes desde el panel de administración del servidor central
   para ese hospital puntual (alta nueva del hospital, o el botón/endpoint de regenerar token si
   ya existe) y pegarlo tal cual — se muestra una sola vez del lado del servidor. Un token que
   no corresponda a ese `Hospital ID` hace que el servidor rechace todos los reportes en
   silencio (ver [CONFIGURACION.md](./CONFIGURACION.md#auth_token--de-dónde-sale-y-por-qué-tiene-que-coincidir-con-hospital_id)).
4. Habilitar y completar cada tarjeta de integración según corresponda (ver
   [CONFIGURACION.md](./CONFIGURACION.md) para el detalle de cada campo).
5. Usar los botones "Test conexión" de cada tarjeta antes de guardar, para validar
   credenciales/conectividad sin esperar al primer ciclo real.
6. Guardar — en modo servicio esto lo reinicia automáticamente; en modo tarea reconfigura el
   intervalo de repetición de la tarea con el valor guardado (ver
   [ARQUITECTURA.md](./ARQUITECTURA.md#modos-de-ejecución-servicio-vs-tarea-programada-v450)).
