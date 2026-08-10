[Setup]
; --- Metadatos de la Aplicación ---
AppName=TecnoMonitor Agent
AppVersion=4.3.1 Sentinel
AppPublisher=Medical IT (Soporte Técnico)
AppCopyright=Copyright (C) 2026

; Oculta la pantallita de "Bienvenido" para que sea una instalación más rápida
DisableWelcomePage=yes

; --- Rutas de Instalación ---
; {pf} es C:\Program Files (x86) o C:\Program Files dependiendo de la arquitectura
DefaultDirName={pf}\TecnoMonitor
DefaultGroupName=TecnoMonitor
OutputDir=Output
OutputBaseFilename=TecnoMonitor_v4.3.1_Sentinel_Setup

; --- Iconos y Permisos ---
SetupIconFile=logo.ico
Compression=lzma2
SolidCompression=yes
; Fuerza a que el instalador pida permisos de Administrador (Escudo de Windows)
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64

[Tasks]
Name: "desktopicon"; Description: "Crear un acceso directo en el Escritorio"; GroupDescription: "Accesos directos adicionales:"

[InstallDelete]
; Borra los ejecutables viejos ANTES de copiar los nuevos para evitar errores de archivo bloqueado
Type: files; Name: "{app}\TecnoMonitorService.exe"
Type: files; Name: "{app}\TecnoMonitorConfig.exe"

[Files]
; Asumimos que PyInstaller dejó tus archivos finales dentro de la carpeta "dist"
Source: "dist\TecnoMonitorService.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\TecnoMonitorConfig.exe"; DestDir: "{app}"; Flags: ignoreversion
; Llevamos el logo para usarlo en los accesos directos
Source: "logo.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; Crea accesos directos en el menú inicio y el escritorio para la GUI de configuración
Name: "{group}\TecnoMonitor Config"; Filename: "{app}\TecnoMonitorConfig.exe"; IconFilename: "{app}\logo.ico"
Name: "{commondesktop}\TecnoMonitor Config"; Filename: "{app}\TecnoMonitorConfig.exe"; Tasks: desktopicon; IconFilename: "{app}\logo.ico"

[Run]
; 1. Crea la tarea programada maestra (Ejecuta como SYSTEM y con el nivel más alto de privilegios al iniciar sesión)
Filename: "schtasks.exe"; Parameters: "/Create /F /TN ""TecnoMonitor_AutoStart"" /TR ""\""{app}\TecnoMonitorService.exe\"""" /SC ONLOGON /RU ""SYSTEM"" /RL HIGHEST"; Flags: runhidden

; 2. Abre la interfaz gráfica automáticamente cuando termina de instalarse
Filename: "{app}\TecnoMonitorConfig.exe"; Description: "Abrir configuración de TecnoMonitor ahora"; Flags: postinstall nowait shellexec

[UninstallRun]
; 1. Al desinstalar, mata los procesos de fondo para que Windows deje borrar los archivos
Filename: "taskkill.exe"; Parameters: "/F /IM TecnoMonitorService.exe /T"; Flags: runhidden; RunOnceId: "KillService"
Filename: "taskkill.exe"; Parameters: "/F /IM TecnoMonitorConfig.exe /T"; Flags: runhidden; RunOnceId: "KillConfig"

; 2. Elimina la tarea programada de Windows para no dejar basura en el servidor
Filename: "schtasks.exe"; Parameters: "/Delete /TN ""TecnoMonitor_AutoStart"" /F"; Flags: runhidden; RunOnceId: "DelTask"

[Code]
// --- CIRUGÍA PREVIA A LA INSTALACIÓN ---
// Esta función se ejecuta en el milisegundo 1, apenas el usuario hace doble clic en el instalador.
// Se encarga de aniquilar a la versión vieja antes de que Windows bloquee los archivos.
function InitializeSetup(): Boolean;
var
  ResultCode: Integer;
begin
  Exec('cmd.exe', '/c taskkill /F /IM TecnoMonitorService.exe /T', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec('cmd.exe', '/c taskkill /F /IM TecnoMonitorConfig.exe /T', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec('cmd.exe', '/c schtasks /Delete /TN "TecnoMonitor_AutoStart" /F', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  
  Result := True;
end;