@echo off
setlocal enabledelayedexpansion

TITLE TecnoMonitor - SRE Automated Build System (v4.3 Sentinel)
COLOR 0B

echo ===================================================================
echo    TECNOIMAGEN MEDICAL IT - Compilacion TecnoMonitor v4.3 Sentinel
echo ===================================================================
echo.

:: 1. Limpieza de artefactos previos
echo [1/4] Limpiando directorios de compilacion anteriores...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist Output rmdir /s /q Output
echo       Limpieza completada con exito.

:: 2. Compilación del Servicio Headless (Background Engine)
echo.
echo [2/4] Compilando TecnoMonitorService.exe (Motor de Segundo Plano)...
pyinstaller --noconfirm --noconsole --onefile ^
    --name "TecnoMonitorService" ^
    --add-data "rules.json;." ^
    headless_service.py

if errorlevel 1 goto ERROR_EXIT
echo       TecnoMonitorService.exe generado correctamente en dist\

:: 3. Compilación de la Interfaz Gráfica con Privilegios de Admin (--uac-admin)
echo.
echo [3/4] Compilando TecnoMonitorConfig.exe (Panel de Control GUI con UAC)...
pyinstaller --noconfirm --windowed --onefile --uac-admin ^
    --name "TecnoMonitorConfig" ^
    --icon "logo.ico" ^
    --add-data "web;web" ^
    --add-data "rules.json;." ^
    main_gui.py

if errorlevel 1 goto ERROR_EXIT
echo       TecnoMonitorConfig.exe generado correctamente en dist\

:: 4. Búsqueda automática de Inno Setup (ISCC.exe) y generación del Instalador
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
"%ISCC_PATH%" TecnoMonitor.iss

if errorlevel 1 goto ERROR_EXIT

echo.
echo ===================================================================
echo   COMPILACION Y EMPAQUETADO COMPLETADOS CON EXITO (v4.3 Sentinel)
echo   Artefacto listo en: Output\TecnoMonitor_v4.3_Sentinel_Setup.exe
echo ===================================================================
pause
exit /b 0

:SUCCESS_NO_ISS
echo.
echo ===================================================================
echo   COMPILACION DE BINARIOS EXITOSA (Sin instalador Inno Setup)
echo   Binarios ubicados en: dist\
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