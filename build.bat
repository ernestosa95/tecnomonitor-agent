@echo off
setlocal enabledelayedexpansion

:: v4.6: version leida de un unico archivo (VERSION), en vez de estar
:: hardcodeada en build.bat/TecnoMonitor.iss/agent_logic.py por separado
:: (ver docs/PLAN_MEJORAS_V4.5.md §6). Cambiar la version de ahora en mas es
:: editar ese archivo solo, nada mas.
set /p AGENT_VERSION=<VERSION

TITLE TecnoMonitor - SRE Automated Build System (v%AGENT_VERSION%)
COLOR 0B

echo ===================================================================
echo    TECNOIMAGEN MEDICAL IT - Compilacion TecnoMonitor v%AGENT_VERSION%
echo ===================================================================
echo.

:: 0. Verificacion de dependencias nuevas de la v4.4
echo [0/4] Verificando pywin32...
python -c "import win32serviceutil" 2>nul
if errorlevel 1 (
    echo       [FALLO] Falta pywin32. Instalalo con:
    echo               pip install pywin32
    echo               python Scripts\pywin32_postinstall.py -install
    pause
    exit /b 1
)
echo       pywin32 presente.

:: Checksum de rules.json (ver docs/PLAN_MEJORAS_V4.5.md §3.6) -- se
:: regenera en cada build para que siempre corresponda al rules.json que se
:: esta empaquetando en este momento, no a una version vieja del archivo.
:: agent_logic.py lo valida al cargar las reglas (ver _rules_json_integro);
:: si no se genera este paso, el agente simplemente omite el chequeo.
powershell -NoProfile -Command "(Get-FileHash 'rules.json' -Algorithm SHA256).Hash" > rules.json.sha256
echo       Checksum de rules.json generado.

:: 1. Limpieza de artefactos previos
echo.
echo [1/4] Limpiando directorios de compilacion anteriores...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist Output rmdir /s /q Output
echo       Limpieza completada con exito.

:: 2. Compilacion del Servicio de Windows
::
::    IMPORTANTE: --onedir, NO --onefile.
::    El bootloader de onefile se re-lanza como proceso hijo; el SCM registra
::    el PID del padre y pierde el canal de control, con lo cual el servicio
::    no responde a stop/shutdown y Windows lo mata por timeout.
::
::    --hidden-import win32timezone: pywin32 lo carga de forma dinamica y el
::    analizador estatico de PyInstaller no lo ve. Sin el, el servicio arranca
::    y muere al primer uso de fechas.
echo.
echo [2/4] Compilando TecnoMonitorService (Servicio de Windows, modo onedir)...
:: v4.6: paramiko (monitoreo SSH de equipos Linux, ver
:: docs/PLAN_MEJORAS_V4.5.md §9.2) depende de cryptography, que ya se usa en
:: el proyecto (security.py) y suele empaquetar bien; se deja el
:: --hidden-import de paramiko igual por si PyInstaller no detecta alguno de
:: sus submodulos internos (paramiko.py3compat, etc.) en el primer build real.
pyinstaller --noconfirm --noconsole --onedir ^
    --name "TecnoMonitorService" ^
    --hidden-import win32timezone ^
    --hidden-import win32serviceutil ^
    --hidden-import win32service ^
    --hidden-import win32event ^
    --hidden-import servicemanager ^
    --hidden-import win32com.client ^
    --hidden-import paramiko ^
    --add-data "rules.json;." ^
    --add-data "rules.json.sha256;." ^
    --add-data "VERSION;." ^
    headless_service.py

if errorlevel 1 goto ERROR_EXIT
echo       dist\TecnoMonitorService\TecnoMonitorService.exe generado correctamente

:: 3. Compilacion de la Interfaz Grafica con Privilegios de Admin (--uac-admin)
::    La GUI ahora habla con el SCM via pywin32, no con schtasks/taskkill.
echo.
echo [3/4] Compilando TecnoMonitorConfig.exe (Panel de Control GUI con UAC)...
:: v4.6: GUI migrada de Eel a pywebview (ver docs/PLAN_MEJORAS_V4.5.md §9.1).
:: pywebview elige en tiempo de ejecucion el backend de Windows disponible
:: (edgechromium/WebView2 si esta el runtime instalado, si no cae a winforms);
:: PyInstaller no detecta esa carga dinamica, de ahi los --hidden-import.
:: TODO: confirmar en el primer build real en Windows si hace falta agregar
:: --collect-all pywebview o empaquetar el WebView2 Loader por separado.
pyinstaller --noconfirm --windowed --onefile --uac-admin ^
    --name "TecnoMonitorConfig" ^
    --icon "logo.ico" ^
    --hidden-import win32serviceutil ^
    --hidden-import win32service ^
    --hidden-import paramiko ^
    --hidden-import webview.platforms.edgechromium ^
    --hidden-import webview.platforms.winforms ^
    --hidden-import clr ^
    --add-data "web;web" ^
    --add-data "rules.json;." ^
    --add-data "rules.json.sha256;." ^
    --add-data "VERSION;." ^
    main_gui.py

if errorlevel 1 goto ERROR_EXIT
echo       TecnoMonitorConfig.exe generado correctamente en dist\

:: 4. Busqueda automatica de Inno Setup (ISCC.exe) y generacion del Instalador
echo.
echo [4/4] Buscando Inno Setup y empaquetando instalador final...

set "ISCC_PATH="
if exist "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" set "ISCC_PATH=C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if exist "C:\Program Files\Inno Setup 6\ISCC.exe" set "ISCC_PATH=C:\Program Files\Inno Setup 6\ISCC.exe"
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC_PATH=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"

if "%ISCC_PATH%"=="" (
    echo [ADVERTENCIA] No se encontro ISCC.exe en las rutas estandar.
    goto SUCCESS_NO_ISS
)

echo       Usando compilador Inno Setup en: "%ISCC_PATH%"
"%ISCC_PATH%" /DMyAppVersion=%AGENT_VERSION% TecnoMonitor.iss

if errorlevel 1 goto ERROR_EXIT

echo.
echo ===================================================================
echo   COMPILACION Y EMPAQUETADO COMPLETADOS CON EXITO (v%AGENT_VERSION%)
echo   Artefacto listo en: Output\TecnoMonitor_v%AGENT_VERSION%_Setup.exe
echo ===================================================================
pause
exit /b 0

:SUCCESS_NO_ISS
echo.
echo ===================================================================
echo   COMPILACION DE BINARIOS EXITOSA (Sin instalador Inno Setup)
echo   Servicio : dist\TecnoMonitorService\
echo   GUI      : dist\TecnoMonitorConfig.exe
echo ===================================================================
pause
exit /b 0

:ERROR_EXIT
echo.
echo ===================================================================
echo   [FALLO] El proceso de compilacion se interrumpio por errores.
echo ===================================================================
pause
exit /b 1
