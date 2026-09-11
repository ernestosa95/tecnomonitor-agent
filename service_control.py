"""
Control del servicio TecnoMonitorAgent desde la GUI.

Reemplaza el par schtasks /Run + taskkill /F de la v4.3, que era un arranque
sin garantías y un kill forzado (con el riesgo de dejar un ciclo de envío a
medio camino y el puerto del candado en TIME_WAIT).

Se habla con el SCM vía pywin32 y no con `sc.exe`, para no depender de
parsear texto que cambia según el idioma del Windows del hospital.
"""

import win32service
import win32serviceutil
import pywintypes

SERVICE_NAME = "TecnoMonitorAgent"

# Cuánto esperamos a que el servicio confirme el cambio de estado.
# El stop puede tardar: el agente termina el ciclo de recolección en curso
# antes de salir, en vez de cortarlo por la mitad.
TIMEOUT_START = 30
TIMEOUT_STOP  = 60


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


def esta_instalado() -> bool:
    try:
        win32serviceutil.QueryServiceStatus(SERVICE_NAME)
        return True
    except pywintypes.error:
        return False


def esta_corriendo() -> bool:
    """True sólo si el SCM reporta SERVICE_RUNNING."""
    try:
        estado = win32serviceutil.QueryServiceStatus(SERVICE_NAME)[1]
        return estado == win32service.SERVICE_RUNNING
    except pywintypes.error:
        return False
    except Exception:
        return False


def estado_legible() -> str:
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


def iniciar() -> dict:
    try:
        if esta_corriendo():
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


def detener() -> dict:
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


def reiniciar() -> dict:
    """
    Detiene y vuelve a levantar el servicio para que tome la config nueva.

    A diferencia de la v4.3, el arranque espera a que el stop se confirme:
    ya no hay carrera entre matar el proceso y volver a lanzarlo.
    """
    res_stop = detener()
    if not res_stop.get("success"):
        return res_stop
    return iniciar()