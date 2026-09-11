"""
TecnoMonitor Agent — Servicio de Windows nativo (v4.5.0)

Cambios respecto de v4.3:
  - Deja de ser un `while True` lanzado por tarea programada ONLOGON.
    Ahora es un servicio real registrado en el SCM: arranca con el sistema
    (sin necesidad de que alguien inicie sesión), sobrevive al logoff y
    Windows lo reinicia solo si el proceso muere.
  - El sleep entre ciclos es un WaitForSingleObject sobre el stop event,
    así el apagado de Windows no espera hasta 5 minutos ni corta un ciclo
    de recolección a la mitad.
  - El candado por socket en 127.0.0.1:64999 se reemplaza por un named
    mutex global. El socket dejaba el puerto en TIME_WAIT después de un
    taskkill /F y la instancia siguiente salía en silencio con exit(0).

Cambios de la 4.4.1 (post-validación en laboratorio):
  - activity.log rota a los 5 MB y conserva 5 archivos. Antes crecía sin
    límite y nadie lo truncaba.
  - El timestamp incluye la fecha. Con sólo la hora era imposible ubicar
    una caída de hace tres días.
  - Detección de agentes v4.3 sobrevivientes: si el candado por socket de
    la versión vieja sigue tomado, hay dos agentes reportando al mismo
    servidor y pisándose el checkpoint. El servicio lo registra y aborta
    en vez de duplicar telemetría en silencio.

Cambios de la 4.5.0:
  - schema_version del envelope pasa a "4.5" (agent_logic.py): activa del
    lado servidor la exigencia de header Authorization: Bearer <token>
    validado contra hospital_id. El agente ya lo envía en cada ciclo sin
    cambios de código — requiere que auth_token esté cargado y sea válido
    en la configuración de cada hospital antes de desplegar este build.
  - KPIs de RIS/PACS/usuarios pueden extraerse vía ElasticSearch en vez de
    SQL Server directo (elastic.enabled_ris_metrics, coexiste con
    enabled_sql) — ver docs/ELK_RIS_METRICS.md.

Comandos (requiere privilegios de administrador):
    TecnoMonitorService.exe --startup auto install
    TecnoMonitorService.exe start
    TecnoMonitorService.exe stop
    TecnoMonitorService.exe remove
"""

import sys
import os

# --- FIX CRÍTICO PARA MODO --noconsole ---
# En un servicio no hay consola adjunta: sys.stdout/stderr son None y
# cualquier print() de una librería de terceros levantaría AttributeError.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")
# -----------------------------------------

import time
import json
import socket
import logging
import traceback
from logging.handlers import RotatingFileHandler

import win32event
import win32service
import win32serviceutil
import win32api
import winerror
import servicemanager
import win32timezone  # noqa: F401  — import explícito: PyInstaller no lo detecta solo

import security
import agent_logic
from agent_logic import ejecutar_ciclo_agente

# ---------------------------------------------------------------------------
# RUTAS
# ---------------------------------------------------------------------------
# El SCM arranca los servicios con el CWD en C:\Windows\System32.
# Nos movemos al directorio del ejecutable para que las rutas relativas
# (rules.json y demás) sigan resolviendo igual que antes.
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

try:
    os.chdir(BASE_DIR)
except Exception:
    pass

DATA_DIR    = security.get_app_data_path()
CONFIG_FILE = os.path.join(DATA_DIR, "monitor_config.json")
LOG_FILE    = os.path.join(DATA_DIR, "activity.log")

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------
# v4.4.1: rotación + fecha completa.
#
# Rotación: 5 MB por archivo, 5 archivos de historia (activity.log,
# activity.log.1 ... activity.log.5). Un hospital con interval_minutes=5
# genera del orden de 15 MB al mes, así que esto cubre cómodamente un
# trimestre de historia con techo de 30 MB.
#
# Fecha: el datefmt anterior era sólo '%H:%M:%S'. Para diagnosticar una
# caída de hace tres días quedaban horas sueltas sin saber de qué día eran.
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS   = 5


def _configurar_logging():
    """
    Monta el handler rotativo. Si falla (permisos, disco lleno), cae a un
    logging básico: preferimos un log pobre antes que un servicio que no
    arranca porque no pudo abrir su archivo.
    """
    formato = logging.Formatter(
        '[%(asctime)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # Limpiamos handlers previos por si basicConfig corrió en algún import.
    for h in list(root.handlers):
        root.removeHandler(h)

    try:
        handler = RotatingFileHandler(
            LOG_FILE,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUPS,
            encoding='utf-8',
            delay=True,   # no abre el archivo hasta el primer write
        )
        handler.setFormatter(formato)
        root.addHandler(handler)
    except Exception:
        logging.basicConfig(
            filename=LOG_FILE,
            level=logging.INFO,
            format='[%(asctime)s] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S',
            filemode='a',
            encoding='utf-8',
        )


_configurar_logging()


def log(msg: str):
    """Wrapper de logging: nunca falla, nunca silencia la causa real."""
    try:
        logging.info(msg)
    except Exception as log_err:
        try:
            with open(LOG_FILE, 'a', encoding='utf-8', errors='replace') as f:
                f.write(f"[LOG_ERROR] {log_err} | Mensaje original: {msg}\n")
        except Exception:
            pass


def log_evento_windows(msg: str, error: bool = False):
    """
    Escribe también en el Visor de Eventos de Windows.

    Es la única traza visible si el servicio muere antes de poder abrir
    activity.log (por ejemplo, si ProgramData no es escribible).
    """
    try:
        if error:
            servicemanager.LogErrorMsg(f"TecnoMonitor: {msg}")
        else:
            servicemanager.LogInfoMsg(f"TecnoMonitor: {msg}")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# CANDADO ANTI-CLONES (named mutex global)
# ---------------------------------------------------------------------------
# El SCM ya garantiza una sola instancia del servicio, pero el mutex evita
# que alguien lance el .exe a mano en paralelo y duplique los reportes.
_mutex_handle = None
MUTEX_NAME = "Global\\TecnoMonitorAgent_SingleInstance"

# Puerto del candado de la v4.3. No lo usamos: sólo lo sondeamos para
# detectar agentes viejos que hayan sobrevivido a la migración.
PUERTO_CANDADO_LEGACY = 64999


def obtener_candado() -> bool:
    global _mutex_handle
    try:
        _mutex_handle = win32event.CreateMutex(None, True, MUTEX_NAME)
        if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
            _mutex_handle = None
            return False
        return True
    except Exception as e:
        # Si el mutex falla por permisos, preferimos seguir corriendo
        # antes que dejar el hospital sin monitoreo.
        log(f"⚠️ No se pudo crear el mutex ({e}). Se continúa sin candado.")
        return True


def liberar_candado():
    global _mutex_handle
    if _mutex_handle:
        try:
            win32event.ReleaseMutex(_mutex_handle)
            win32api.CloseHandle(_mutex_handle)
        except Exception:
            pass
        _mutex_handle = None


def detectar_agente_legacy() -> bool:
    """
    True si hay un agente v4.3 todavía vivo en este equipo.

    Los candados de las dos versiones son mecanismos distintos (socket vs
    mutex) y no se ven entre sí, así que durante la migración pueden convivir
    dos agentes: ambos reportan al mismo servidor y ambos mueven el mismo
    checkpoint de ElasticSearch en ProgramData, con lo cual se pierden
    documentos o se duplican.

    Sondeamos el puerto que usaba la v4.3: si está ocupado, hay un agente
    viejo corriendo.
    """
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.0)
        s.bind(('127.0.0.1', PUERTO_CANDADO_LEGACY))
        return False       # pudimos bindear: no hay nadie
    except socket.error:
        return True        # ocupado: hay un agente v4.3 vivo
    except Exception:
        return False       # ante la duda, no bloqueamos el arranque
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# CARGA DE CONFIGURACIÓN
# ---------------------------------------------------------------------------
def cargar_config_segura():
    if not os.path.exists(CONFIG_FILE):
        return None
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)

        if data.get("auth_token"):
            data["auth_token"] = security.desencriptar(data["auth_token"])

        if isinstance(data.get("proxmox"), dict) and data["proxmox"].get("pass"):
            data["proxmox"]["pass"] = security.desencriptar(data["proxmox"]["pass"])

        if isinstance(data.get("idrac"), dict) and data["idrac"].get("pass"):
            data["idrac"]["pass"] = security.desencriptar(data["idrac"]["pass"])

        if isinstance(data.get("sql"), dict) and data["sql"].get("pass"):
            data["sql"]["pass"] = security.desencriptar(data["sql"]["pass"])

        if isinstance(data.get("vms"), list):
            for vm in data["vms"]:
                if isinstance(vm, dict) and vm.get("pass"):
                    vm["pass"] = security.desencriptar(vm["pass"])

        if isinstance(data.get("mirth_servers"), list):
            for m in data["mirth_servers"]:
                if isinstance(m, dict) and m.get("pass"):
                    m["pass"] = security.desencriptar(m["pass"])

        if isinstance(data.get("elastic"), dict) and data["elastic"].get("pass"):
            data["elastic"]["pass"] = security.desencriptar(data["elastic"]["pass"])

        return data

    except json.JSONDecodeError as e:
        log(f"❌ monitor_config.json malformado: {e}")
    except Exception as e:
        log(f"⚠️ Error al cargar configuración: {e}")

    return None


# ---------------------------------------------------------------------------
# SERVICIO
# ---------------------------------------------------------------------------
class TecnoMonitorService(win32serviceutil.ServiceFramework):

    _svc_name_         = "TecnoMonitorAgent"
    _svc_display_name_ = "TecnoMonitor Agent"
    _svc_description_  = (
        "Recolecta telemetría de infraestructura, KPIs clínicos y estado de "
        "integraciones, y los reporta al servidor central de TecnoMonitor."
    )

    # PyInstaller: el SCM necesita la ruta real del .exe empaquetado.
    if getattr(sys, 'frozen', False):
        _exe_name_ = sys.executable

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        # Evento manual-reset: lo señaliza SvcStop y lo esperan todos los
        # sleeps del bucle. Reemplaza a time.sleep().
        self.hWaitStop = win32event.CreateEvent(None, 1, 0, None)
        self.detener = False

    # -- Control handlers ---------------------------------------------------

    def SvcStop(self):
        """
        Windows pide detener el servicio (apagado, services.msc, GUI).

        Damos un waitHint amplio porque un ciclo de recolección en curso
        (WMI, iDRAC, SQL, Elastic) puede tardar. El bucle sale apenas
        termina el ciclo actual, sin dejar un reporte a medio enviar.
        """
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, waitHint=120000)
        self.detener = True
        win32event.SetEvent(self.hWaitStop)
        log("🛑 Solicitud de detención recibida. Cerrando tras el ciclo actual...")

    SvcShutdown = SvcStop  # mismo tratamiento ante apagado del sistema

    # -- Utilidades ---------------------------------------------------------

    def _esperar(self, segundos: float) -> bool:
        """
        Duerme `segundos`, pero se despierta al instante si piden detener.
        Devuelve True si hay que seguir, False si hay que salir.
        """
        if segundos <= 0:
            return not self.detener
        rc = win32event.WaitForSingleObject(self.hWaitStop, int(segundos * 1000))
        return rc != win32event.WAIT_OBJECT_0

    # -- Bucle principal ----------------------------------------------------

    def SvcDoRun(self):
        self.ReportServiceStatus(win32service.SERVICE_RUNNING)
        log_evento_windows("Servicio iniciado.")
        log("🚀 TecnoMonitor Service v4.5.0 — Iniciando (modo servicio de Windows)")

        # Migración: agente v4.3 sobreviviente
        if detectar_agente_legacy():
            msg = ("Se detectó un agente v4.3 todavía en ejecución (puerto 64999 "
                   "ocupado). Dos agentes reportando en paralelo duplican telemetría "
                   "y se pisan el checkpoint de ElasticSearch. Eliminá la tarea "
                   "programada 'TecnoMonitor_AutoStart' y matá el proceso viejo antes "
                   "de arrancar el servicio.")
            log(f"🚨 {msg}")
            log_evento_windows(msg, error=True)
            return

        if not obtener_candado():
            log("🚨 Ya hay una instancia del agente corriendo. Saliendo.")
            log_evento_windows("Otra instancia ya está activa. Se aborta el arranque.", error=True)
            return

        log("🔒 Candado adquirido. Servicio activo.")

        try:
            self._bucle()
        except Exception:
            detalle = traceback.format_exc()
            log(f"💥 Excepción no controlada, el servicio termina:\n{detalle}")
            log_evento_windows(f"Terminación por excepción no controlada:\n{detalle}", error=True)
            # Salimos con código distinto de 0 para que las failure actions
            # configuradas en el instalador (sc failure) lo reinicien.
            raise
        finally:
            liberar_candado()
            log("👋 Servicio detenido.\n")
            log_evento_windows("Servicio detenido.")

    def _bucle(self):
        while not self.detener:
            try:
                cfg = cargar_config_segura()

                if not cfg:
                    log("⚠️ Configuración no disponible. Reintentando en 60s...")
                    if not self._esperar(60):
                        break
                    continue

                # --- Módulo SQL (KPIs de negocio): vía Elastic si el hospital ya
                # migró su Logstash (ver elk/), si no vía SQL Server directo. ---
                elastic_cfg = cfg.get("elastic") or {}
                if elastic_cfg.get("enabled_ris_metrics") and elastic_cfg.get("host"):
                    try:
                        sql_data = agent_logic.extraer_metricas_ris_elastic(elastic_cfg, log_func=log)
                        if sql_data:
                            cfg["_sql_data_payload"] = sql_data
                        else:
                            log("ℹ️ RIS/Elastic: bloque futuro o sin datos nuevos, se omite en este ciclo.")
                    except Exception as e:
                        log(f"❌ Error en módulo RIS/Elastic: {e}")
                elif cfg.get("enabled_sql") and cfg.get("sql"):
                    try:
                        sql_data = agent_logic.extraer_metricas_sql(cfg["sql"], log_func=log)
                        if sql_data:
                            cfg["_sql_data_payload"] = sql_data
                        else:
                            log("ℹ️ SQL: bloque futuro o sin datos nuevos, se omite en este ciclo.")
                    except Exception as e:
                        log(f"❌ Error en módulo SQL: {e}")
                else:
                    log("ℹ️ Módulo SQL desactivado.")

                if self.detener:
                    break

                # --- Ciclo principal ---
                res = ejecutar_ciclo_agente(cfg, log_callback=log)

                if isinstance(res, dict):
                    if res.get("status") == "OK":
                        log(f"✅ Ciclo completado y enviado OK — {res.get('timestamp', '')}")
                    else:
                        log(f"❌ Fallo en el envío: {res.get('error', 'Error desconocido')}")
                else:
                    log(f"⚠️ Respuesta inesperada del ciclo: {res}")

                try:
                    minutos = float(cfg.get("interval_minutes", 5))
                except (TypeError, ValueError):
                    minutos = 5.0

                log(f"💤 Durmiendo {minutos:.1f} minutos...\n")
                if not self._esperar(minutos * 60):
                    break

            except Exception as e:
                log(f"💥 Error global del bucle: {e}")
                if not self._esperar(60):
                    break


# ---------------------------------------------------------------------------
# ENTRYPOINT
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    if len(sys.argv) == 1:
        # Sin argumentos: nos invocó el SCM. Arrancamos el dispatcher.
        try:
            servicemanager.Initialize()
            servicemanager.PrepareToHostSingle(TecnoMonitorService)
            servicemanager.StartServiceCtrlDispatcher()
        except Exception as e:
            # Caso típico: alguien hizo doble clic en el .exe.
            log_evento_windows(
                f"No se pudo conectar con el Administrador de servicios ({e}). "
                f"Este ejecutable no se lanza a mano: usá 'install' y 'start'.",
                error=True,
            )
    else:
        # install / remove / start / stop / update / debug
        win32serviceutil.HandleCommandLine(TecnoMonitorService)