"""
Control del servicio TecnoMonitorAgent desde la GUI.

Reemplaza el par schtasks /Run + taskkill /F de la v4.3, que era un arranque
sin garantías y un kill forzado (con el riesgo de dejar un ciclo de envío a
medio camino y el puerto del candado en TIME_WAIT).

Se habla con el SCM vía pywin32 y no con `sc.exe`, para no depender de
parsear texto que cambia según el idioma del Windows del hospital.

v4.5.0: el agente ahora puede instalarse como servicio de Windows (lo de
siempre, este módulo) o como tarea programada liviana, sin privilegios de
servicio (ver task_control.py y docs/ARQUITECTURA.md) — para hospitales cuya
política de seguridad bloquea la creación de servicios nuevos. Este módulo
lee el modo elegido en la instalación (`install_mode.txt`, escrito por
TecnoMonitor.iss) y despacha a la implementación correspondiente en cada
función pública, para que main_gui.py no tenga que saber cuál de las dos
está corriendo.
"""

import os

import win32service
import win32serviceutil
import pywintypes

import security
import task_control

SERVICE_NAME = "TecnoMonitorAgent"

# Cuánto esperamos a que el servicio confirme el cambio de estado.
# El stop puede tardar: el agente termina el ciclo de recolección en curso
# antes de salir, en vez de cortarlo por la mitad.
TIMEOUT_START = 30
TIMEOUT_STOP  = 60

_MODE_FILE = os.path.join(security.get_app_data_path(), "install_mode.txt")


def _modo_instalado() -> str:
    """
    "service" o "task", según lo que haya escrito el instalador.

    Si el archivo no existe (instalaciones de antes de v4.5.0, que nunca lo
    escribieron), se asume "service" — es el único modo que existía.
    """
    try:
        with open(_MODE_FILE, "r", encoding="utf-8") as f:
            modo = f.read().strip().lower()
            if modo in ("service", "task"):
                return modo
    except Exception:
        pass
    return "service"


def _mensaje_error(e: pywintypes.error) -> str:
    codigo = e.winerror
    if codigo == 5:
        return "Requiere privilegios de Administrador."
    if codigo == 1060:
        return ("El servicio TecnoMonitorAgent no está instalado. "
                "Reinstalá TecnoMonitor o ejecutá 'TecnoMonitorService.exe install' como admin.")
    if codigo == 1056:
        return "El servicio ya está en ejecución."
    if codigo == 1062:
        return "El servicio no estaba en ejecución."
    return f"{e.strerror} (código {codigo})"


def _servicio_esta_instalado() -> bool:
    try:
        win32serviceutil.QueryServiceStatus(SERVICE_NAME)
        return True
    except pywintypes.error:
        return False


def _servicio_esta_corriendo() -> bool:
    """True sólo si el SCM reporta SERVICE_RUNNING."""
    try:
        estado = win32serviceutil.QueryServiceStatus(SERVICE_NAME)[1]
        return estado == win32service.SERVICE_RUNNING
    except pywintypes.error:
        return False
    except Exception:
        return False


def _servicio_estado_legible() -> str:
    """Estado para mostrar en el badge de la GUI."""
    try:
        estado = win32serviceutil.QueryServiceStatus(SERVICE_NAME)[1]
    except pywintypes.error as e:
        if e.winerror == 1060:
            return "No instalado"
        return "Desconocido"
    except Exception:
        return "Desconocido"

    return {
        win32service.SERVICE_STOPPED:          "Detenido",
        win32service.SERVICE_START_PENDING:    "Iniciando...",
        win32service.SERVICE_STOP_PENDING:     "Deteniendo...",
        win32service.SERVICE_RUNNING:          "En ejecución",
        win32service.SERVICE_CONTINUE_PENDING: "Reanudando...",
        win32service.SERVICE_PAUSE_PENDING:    "Pausando...",
        win32service.SERVICE_PAUSED:           "En pausa",
    }.get(estado, "Desconocido")


def _servicio_iniciar() -> dict:
    try:
        if _servicio_esta_corriendo():
            return {"success": True, "msg": "El servicio ya estaba corriendo."}
        win32serviceutil.StartService(SERVICE_NAME)
        win32serviceutil.WaitForServiceStatus(
            SERVICE_NAME, win32service.SERVICE_RUNNING, TIMEOUT_START
        )
        return {"success": True}
    except pywintypes.error as e:
        if e.winerror == 1056:
            return {"success": True, "msg": "El servicio ya estaba corriendo."}
        return {"success": False, "msg": _mensaje_error(e)}
    except Exception as e:
        return {"success": False, "msg": str(e)}


def _servicio_detener() -> dict:
    try:
        win32serviceutil.StopService(SERVICE_NAME)
        win32serviceutil.WaitForServiceStatus(
            SERVICE_NAME, win32service.SERVICE_STOPPED, TIMEOUT_STOP
        )
        return {"success": True}
    except pywintypes.error as e:
        if e.winerror == 1062:
            return {"success": True, "msg": "El servicio ya estaba detenido."}
        return {"success": False, "msg": _mensaje_error(e)}
    except Exception as e:
        return {"success": False, "msg": str(e)}


# ---------------------------------------------------------------------------
# API PÚBLICA — despacha según el modo instalado (servicio o tarea)
# ---------------------------------------------------------------------------
def esta_instalado() -> bool:
    if _modo_instalado() == "task":
        return task_control.esta_instalado()
    return _servicio_esta_instalado()


def esta_corriendo() -> bool:
    if _modo_instalado() == "task":
        return task_control.esta_corriendo()
    return _servicio_esta_corriendo()


def estado_legible() -> str:
    if _modo_instalado() == "task":
        return task_control.estado_legible()
    return _servicio_estado_legible()


def iniciar() -> dict:
    if _modo_instalado() == "task":
        return task_control.iniciar()
    return _servicio_iniciar()


def detener() -> dict:
    if _modo_instalado() == "task":
        return task_control.detener()
    return _servicio_detener()


def reiniciar(interval_minutes=5) -> dict:
    """
    Aplica la configuración recién guardada.

    Modo servicio: detiene y vuelve a levantar el servicio (como siempre —
    a diferencia de la v4.3, el arranque espera a que el stop se confirme,
    así que no hay carrera entre matar el proceso y volver a lanzarlo).

    Modo tarea: no hace falta un stop/start — cada invocación `--run-once`
    ya relee la config del disco por su cuenta. Lo único que vive fuera del
    proceso es el intervalo de repetición del propio trigger de la tarea,
    así que se reconfigura ese trigger con el `interval_minutes` recién
    guardado (ver task_control.actualizar_intervalo) y se confirma que la
    tarea siga habilitada.
    """
    if _modo_instalado() == "task":
        res = task_control.actualizar_intervalo(interval_minutes)
        if not res.get("success"):
            return res
        return task_control.iniciar()

    res_stop = detener()
    if not res_stop.get("success"):
        return res_stop
    return iniciar()