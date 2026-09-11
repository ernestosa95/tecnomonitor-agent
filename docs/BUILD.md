# Build y empaquetado

## Pipeline (`build.bat`)

Script Windows (`cmd`) que compila y empaqueta todo en un solo paso. Pasos:

1. **Verifica `pywin32`** (`import win32serviceutil`) — falla rápido con instrucciones claras
   si falta, en vez de dejar que PyInstaller genere un build que recién falla al arrancar el
   servicio.
2. **Limpia artefactos previos**: borra `build\`, `dist\`, `Output\` si existen.
3. **Compila el servicio** (`headless_service.py`) con PyInstaller:
   ```
   pyinstaller --noconfirm --noconsole --onedir ^
       --name "TecnoMonitorService" ^
       --hidden-import win32timezone ^
       --hidden-import win32serviceutil ^
       --hidden-import win32service ^
       --hidden-import win32event ^
       --hidden-import servicemanager ^
       --add-data "rules.json;." ^
       headless_service.py
   ```
   - `--onedir`, no `--onefile` — ver justificación en [ARQUITECTURA.md](./ARQUITECTURA.md#por-qué-el-servicio-se-compila---onedir-y-no---onefile) / [INSTALACION.md](./INSTALACION.md).
   - `--hidden-import win32timezone` es necesario porque `pywin32` lo carga dinámicamente y el
     analizador estático de PyInstaller no lo detecta solo; sin este import explícito, el
     servicio arranca y muere en el primer uso de fechas con zona horaria de Windows.
   - `--add-data "rules.json;."` empaqueta las reglas de clasificación junto al `.exe` del
     servicio (ver [REGLAS_LOGS.md](./REGLAS_LOGS.md)).
4. **Compila la GUI** (`main_gui.py`) con PyInstaller:
   ```
   pyinstaller --noconfirm --windowed --onefile --uac-admin ^
       --name "TecnoMonitorConfig" ^
       --icon "logo.ico" ^
       --hidden-import win32serviceutil ^
       --hidden-import win32service ^
       --add-data "web;web" ^
       --add-data "rules.json;." ^
       main_gui.py
   ```
   - `--onefile` sí es aceptable acá porque la GUI no es un servicio de Windows — no tiene el
     problema de PID que describe el paso anterior.
   - `--uac-admin` fuerza la elevación al abrir la GUI, necesaria porque habla con el SCM.
   - `--add-data "web;web"` empaqueta el frontend (HTML/JS/CSS) dentro del ejecutable; en
     runtime se resuelve con `resource_path()` (`sys._MEIPASS` si está "frozen").
5. **Busca `ISCC.exe`** (compilador de Inno Setup) en las rutas estándar de instalación
   (Program Files, Program Files (x86), o `%LOCALAPPDATA%\Programs`). Si lo encuentra, compila
   `TecnoMonitor.iss` y genera el instalador final en `Output\`. Si no lo encuentra, el script
   termina en éxito parcial ("compilación de binarios exitosa, sin instalador") — deja
   `dist\TecnoMonitorService\` y `dist\TecnoMonitorConfig.exe` listos para empaquetar
   manualmente después.

## `Compiler.txt`

Contiene comandos de PyInstaller **más simples y desactualizados** que los de `build.bat`
(sin `--onedir` para el servicio, sin `--hidden-import win32timezone`/`servicemanager`, con
`--hidden-import=proxmoxer` que ya no aplica porque el módulo Proxmox usa `requests` directo,
no la librería `proxmoxer`). Se mantiene en el repo como nota histórica/borrador, pero
**`build.bat` es la referencia vigente** para compilar — no usar los comandos de
`Compiler.txt` para generar un build de producción, ya que el servicio compilado con
`--onefile` reproduce el problema de PID descripto en [INSTALACION.md](./INSTALACION.md).

## Dependencias Python conocidas (a partir de los imports del código)

| Paquete | Usado por | Notas |
|---|---|---|
| `eel` | `main_gui.py` | Bridge Python↔JS de la GUI |
| `requests`, `urllib3` | prácticamente todos los módulos de red | `urllib3.disable_warnings(InsecureRequestWarning)` global, ver [SEGURIDAD.md](./SEGURIDAD.md) |
| `wmi`, `pythoncom` | `agent_logic.py` (módulo WMI) | Requiere Windows; `pywin32` provee `pythoncom` |
| `pyodbc` | `agent_logic.py` (módulo SQL) | Requiere driver ODBC de SQL Server instalado en el equipo (`ODBC Driver 17` con fallback a `SQL Server`) |
| `psutil` | `agent_logic.py` (red pasiva), `debug_disk.py` | |
| `cryptography` | `security.py` (Fernet), `agent_logic.py` (parseo x509 de certificados) | |
| `pyVmomi` | `agent_logic.py` (módulo VMware) | **Import diferido** dentro de la función — si no está instalado, el módulo lo reporta como error controlado en vez de romper el arranque del agente completo |
| `pywin32` (`win32service`, `win32serviceutil`, `win32event`, `win32api`, `winerror`, `servicemanager`, `win32timezone`) | `headless_service.py`, `service_control.py` | Núcleo de la integración con el SCM de Windows |

No hay un `requirements.txt` en el repositorio: las dependencias se infieren de los imports.
Antes de compilar en un entorno nuevo, instalar manualmente los paquetes de la tabla (más
`pyVmomi` si se va a soportar VMware) y correr `pywin32_postinstall.py -install` según indica
el propio `build.bat` en su verificación inicial.

## Versionado

La versión "visible" (instalador, `AppVersion` en `TecnoMonitor.iss`) es `4.4.1`. El envelope
que el agente envía al servidor central lleva un `agent_version` distinto (`"4.4.0"`,
hardcodeado en `agent_logic.ejecutar_ciclo_agente`) y un `schema_version` fijo (`"4.3"`) — no
hay un único lugar en el código que centralice el número de versión. Ver
[CHANGELOG.md](./CHANGELOG.md) para el detalle de qué cambió en cada versión, reconstruido a
partir de los comentarios dejados en el código.
