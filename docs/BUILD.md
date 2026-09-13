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
| `pywebview` | `main_gui.py` | Bridge Python↔JS de la GUI (v4.6, reemplaza a `eel`) |
| `paramiko` | `agent_logic.py` (módulo SSH, equipos Linux) | v4.6 — ver [PLAN_MEJORAS_V4.5.md §9.2](./PLAN_MEJORAS_V4.5.md#92-monitoreo-de-equipos-linux-en-vms-hoy-solo-wmiwindows) |
| `requests`, `urllib3` | prácticamente todos los módulos de red | `urllib3.disable_warnings(InsecureRequestWarning)` global, ver [SEGURIDAD.md](./SEGURIDAD.md) |
| `wmi`, `pythoncom` | `agent_logic.py` (módulo WMI) | Requiere Windows; `pywin32` provee `pythoncom` |
| `pyodbc` | `agent_logic.py` (módulo SQL) | Requiere driver ODBC de SQL Server instalado en el equipo (`ODBC Driver 17` con fallback a `SQL Server`) |
| `psutil` | `agent_logic.py` (red pasiva), `debug_disk.py` | |
| `cryptography` | `security.py` (Fernet), `agent_logic.py` (parseo x509 de certificados) | |
| `pyVmomi` | `agent_logic.py` (módulo VMware) | **Import diferido** dentro de la función — si no está instalado, el módulo lo reporta como error controlado en vez de romper el arranque del agente completo |
| `pywin32` (`win32service`, `win32serviceutil`, `win32event`, `win32api`, `winerror`, `servicemanager`, `win32timezone`) | `headless_service.py`, `service_control.py` | Núcleo de la integración con el SCM de Windows |

Desde v4.6 estas dependencias están formalizadas en `requirements.txt` (con marcadores
`sys_platform == "win32"` para las que no aplican en un entorno de desarrollo no-Windows) — ya
no hace falta inferirlas de los imports a mano. `requirements-dev.txt` agrega `pytest`, solo
para correr la suite de `tests/` (ver más abajo), nunca se empaqueta en el agente.

## Tests

```
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

La suite (`tests/`) corre en cualquier plataforma: `tests/conftest.py` stubea los módulos
Windows-only (`wmi`, `pythoncom`, `pywin32`) antes de importar `agent_logic`/`main_gui`/
`headless_service`, así que no hace falta Windows ni una instalación real de WMI/SQL
Server/iDRAC/Elastic para correrla — todo lo que toca una integración externa está mockeado a
nivel de objeto (`paramiko.SSHClient`, `requests.post`, etc.), no del protocolo de red en sí.
Cubre: migración de `monitor_config.json` y cifrado de credenciales, el gate de sesión de la
GUI, la recolección SSH de equipos Linux, el aislamiento de fallas del ciclo multi-hospital, y
el lote de robustez/seguridad (401, rollback de `schema_version`, HTTPS opcional de Elastic,
integridad de `rules.json`, tope de paginación).

**Lo que la suite NO cubre** (requiere un entorno real, no un mock): el render de la GUI en
WebView2/Windows, y cualquier prueba contra hardware/software real (iDRAC, SQL Server con el
schema de Extensa, un clúster Elastic, un servidor SSH real) — para eso, ver el checklist de
[PLAN_MEJORAS_V4.5.md §8](./PLAN_MEJORAS_V4.5.md#8-checklist-de-validación-antes-de-liberar-v45).

## Versionado

Desde v4.6, la versión vive en un único archivo, `/VERSION` (contenido: `4.5.0`) — `agent_logic.py`
lo lee al importar (`AGENT_VERSION`, con `SCHEMA_VERSION` derivado como major.minor) y
`build.bat` lo pasa a `TecnoMonitor.iss` como macro del preprocesador de Inno Setup
(`/DMyAppVersion=...`). Bumpear la versión es editar `/VERSION` una sola vez, ya no tocar tres
archivos a mano. Hasta v4.4.1 ni siquiera estaban alineados (`AppVersion=4.4.1` convivía con
`agent_version="4.4.0"` y `schema_version="4.3"`) — ver [CHANGELOG.md](./CHANGELOG.md) para el
detalle de esa inconsistencia histórica.

**`schema_version` no es un número de versión cosmético**: el servidor central lo usa para
decidir cómo interpretar el payload y, desde `"4.5"`, para exigir el header
`Authorization: Bearer <token>` validado contra `hospital_id` (ver
[ENVELOPE_API.md](./ENVELOPE_API.md) y el contrato de ingesta del servidor). Antes de compilar
un build con `schema_version` en `"4.5"`, confirmar que:

1. El servidor central ya tiene desplegada esa validación de token (no asumirlo — el propio
   contrato advierte que un agente en 4.5 contra un servidor que todavía no la implementó
   corrompe el payload en silencio, ver [PLAN_MEJORAS_V4.5.md §2](./PLAN_MEJORAS_V4.5.md)).
2. Cada hospital que reciba este build tiene un `auth_token` real cargado en su configuración
   (no vacío ni de prueba) — sin esto, el servidor rechaza el reporte completo con 401.

