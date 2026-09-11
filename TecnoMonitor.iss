[Setup]
; --- Metadatos de la Aplicación ---
AppName=TecnoMonitor Agent
AppVersion=4.5.0
AppPublisher=Medical IT (Soporte Técnico)
AppCopyright=Copyright (C) 2026

DisableWelcomePage=yes

; --- Rutas de Instalación ---
DefaultDirName={pf}\TecnoMonitor
DefaultGroupName=TecnoMonitor
OutputDir=Output
OutputBaseFilename=TecnoMonitor_v4.5.0_Setup

; --- Iconos y Permisos ---
SetupIconFile=logo.ico
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64

[Tasks]
Name: "desktopicon"; Description: "Crear un acceso directo en el Escritorio"; GroupDescription: "Accesos directos adicionales:"

[InstallDelete]
; v4.3 dejaba el servicio suelto en la raíz de {app}. Ahora vive en {app}\service.
Type: files;          Name: "{app}\TecnoMonitorService.exe"
Type: files;          Name: "{app}\TecnoMonitorConfig.exe"
; Limpieza de la carpeta del servicio para no mezclar DLLs de versiones distintas
Type: filesandordirs; Name: "{app}\service"

[Files]
; El SERVICIO se compila en modo --onedir (carpeta, no onefile).
; Motivo: el bootloader de --onefile lanza un proceso hijo y el SCM se queda
; con el PID del padre; los controles de stop/shutdown se pierden y Windows
; termina matando el servicio por timeout. Con --onedir el .exe que registra
; el SCM es el mismo que corre el bucle.
Source: "dist\TecnoMonitorService\*"; DestDir: "{app}\service"; Flags: ignoreversion recursesubdirs createallsubdirs

; La GUI sigue siendo un único .exe portable.
Source: "dist\TecnoMonitorConfig.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "logo.ico";                    DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\TecnoMonitor Config"; Filename: "{app}\TecnoMonitorConfig.exe"; IconFilename: "{app}\logo.ico"
Name: "{commondesktop}\TecnoMonitor Config"; Filename: "{app}\TecnoMonitorConfig.exe"; Tasks: desktopicon; IconFilename: "{app}\logo.ico"

[Run]
; 1. Registrar el servicio con arranque automático.
;    Corre como LocalSystem por defecto: arranca con el equipo, sin depender
;    de que alguien inicie sesión, y sobrevive al logoff.
Filename: "{app}\service\TecnoMonitorService.exe"; Parameters: "--startup auto install"; Flags: runhidden waituntilterminated; StatusMsg: "Registrando el servicio TecnoMonitor..."

; 2. Arranque retrasado: evita competir por I/O y red con el resto de los
;    servicios del hospital durante el boot.
Filename: "sc.exe"; Parameters: "config TecnoMonitorAgent start= delayed-auto"; Flags: runhidden waituntilterminated

; 3. Recuperación automática ante caída del proceso.
;    Reinicia al minuto en los tres primeros fallos; el contador se resetea
;    a las 24 h. Esto es lo que la tarea programada nunca tuvo.
Filename: "sc.exe"; Parameters: "failure TecnoMonitorAgent reset= 86400 actions= restart/60000/restart/60000/restart/60000"; Flags: runhidden waituntilterminated
Filename: "sc.exe"; Parameters: "failureflag TecnoMonitorAgent 1"; Flags: runhidden waituntilterminated

; 4. Levantarlo ya, sin esperar al próximo reinicio.
Filename: "sc.exe"; Parameters: "start TecnoMonitorAgent"; Flags: runhidden waituntilterminated; StatusMsg: "Iniciando el servicio TecnoMonitor..."

; 5. Abrir la GUI al terminar.
Filename: "{app}\TecnoMonitorConfig.exe"; Description: "Abrir configuración de TecnoMonitor ahora"; Flags: postinstall nowait shellexec

[UninstallRun]
Filename: "sc.exe";       Parameters: "stop TecnoMonitorAgent"; Flags: runhidden waituntilterminated; RunOnceId: "StopSvc"
Filename: "{app}\service\TecnoMonitorService.exe"; Parameters: "remove"; Flags: runhidden waituntilterminated; RunOnceId: "RemoveSvc"
Filename: "taskkill.exe"; Parameters: "/F /IM TecnoMonitorConfig.exe /T"; Flags: runhidden; RunOnceId: "KillConfig"
; Por si quedó la tarea programada de una instalación 4.3 previa
Filename: "schtasks.exe"; Parameters: "/Delete /TN ""TecnoMonitor_AutoStart"" /F"; Flags: runhidden; RunOnceId: "DelTask"

[Code]
// --- CIRUGÍA PREVIA A LA INSTALACIÓN ---
// Se ejecuta apenas el usuario hace doble clic. Desmonta la versión anterior
// (sea 4.3 con tarea programada, o 4.4 con servicio) antes de que Windows
// bloquee los archivos que estamos por sobrescribir.
function InitializeSetup(): Boolean;
var
  ResultCode: Integer;
begin
  // 1. Servicio 4.4, si existe
  Exec('cmd.exe', '/c sc stop TecnoMonitorAgent', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(3000);
  Exec('cmd.exe', '/c sc delete TecnoMonitorAgent', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(1000);

  // 2. MIGRACIÓN DESDE v4.3: la tarea programada ONLOGON es exactamente el
  //    bug que estamos corrigiendo. Si sobrevive, compite con el servicio.
  Exec('cmd.exe', '/c schtasks /Delete /TN "TecnoMonitor_AutoStart" /F', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);

  // 3. Procesos huérfanos
  Exec('cmd.exe', '/c taskkill /F /IM TecnoMonitorService.exe /T', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec('cmd.exe', '/c taskkill /F /IM TecnoMonitorConfig.exe /T', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);

  Result := True;
end;
