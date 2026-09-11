"""
Control de la tarea programada TecnoMonitorAgent_Task (modo Tarea, v4.5.0).

Alternativa a service_control.py (que habla con el SCM) para hospitales que
eligieron, en el instalador, no registrar un servicio de Windows — por
ejemplo porque una política de seguridad bloquea la creación de servicios
nuevos (un vector de persistencia clásico) aunque sí permita tareas
programadas simples.

Se usa la API COM del Programador de Tareas (Schedule.Service), no
`schtasks.exe`: mismo criterio que ya aplica service_control.py frente a
`sc.exe` — no depender de parsear texto que cambia según el idioma del
Windows del hospital.

Diseño deliberado, distinto al de la tarea programada de v4.3 (ver
headless_service.py y docs/ARQUITECTURA.md): la tarea NO lanza un proceso de
larga duración. Cada disparo corre `TecnoMonitorService.exe --run-once`, que
hace un solo ciclo y sale — es el propio Programador de Tareas quien la
repite cada `interval_minutes` vía el trigger de repetición, no un `while
True` propio (eso era justamente lo que le daba problemas a la tarea de
v4.3: un candado por socket que quedaba en TIME_WAIT).

La tarea corre con el grupo "Usuarios" como principal (no un usuario
nombrado ni SYSTEM): se dispara con el logon de quien sea que esté usando
el equipo en ese momento, con privilegios estándar — no "privilegios más
altos". Eso es lo que hace que se pueda registrar sin la misma exigencia de
privilegios que un servicio, a costa de detenerse si nadie tiene sesión
iniciada.

⚠️ NOTA HONESTA: los valores de los enums de la API COM de abajo (LogonType,
RunLevel, MultipleInstances, tipo de trigger) están escritos según la
documentación pública de Microsoft, pero este módulo no se probó contra un
Windows real desde este entorno de desarrollo (Linux). Antes de confiar en
él en producción, validar cada función contra el Programador de Tareas real
— en particular MultipleInstances (marcado abajo) es el valor del que menos
seguro estoy.
"""

import win32com.client
import pywintypes

TASK_NAME   = "TecnoMonitorAgent_Task"
TASK_FOLDER = "\\"  # raíz del Programador de Tareas

# --- Constantes de la API COM del Task Scheduler 2.0 ---
# No vienen como enums en win32com; son los valores numéricos documentados
# por Microsoft (ITaskDefinition, IPrincipal, ITrigger, IRegistrationInfo).
_TASK_TRIGGER_LOGON    = 9   # TASK_TRIGGER_LOGON
_TASK_ACTION_EXEC      = 0   # TASK_ACTION_EXEC
_TASK_LOGON_GROUP      = 4   # TASK_LOGON_GROUP — corre con la sesión del grupo, sin password guardada
_TASK_RUNLEVEL_LUA     = 0   # TASK_RUNLEVEL_LUA — estándar, NO "privilegios más altos"
_TASK_CREATE_OR_UPDATE = 6   # TASK_CREATE_OR_UPDATE
_TASK_INSTANCES_IGNORE_NEW = 2  # ⚠️ validar: si ya hay una corriendo, no lanzar otra

# Grupo "Usuarios" — la tarea corre con quien esté logueado en ese momento,
# sin atarse a la cuenta de quien instaló.
_GRUPO_USUARIOS = "BUILTIN\\Users"


def _conectar():
    scheduler = win32com.client.Dispatch('Schedule.Service')
    scheduler.Connect()
    return scheduler


def _obtener_carpeta(scheduler):
    return scheduler.GetFolder(TASK_FOLDER)


def _obtener_tarea(scheduler=None):
    """Devuelve la tarea registrada, o None si no existe."""
    scheduler = scheduler or _conectar()
    try:
        return _obtener_carpeta(scheduler).GetTask(TASK_NAME)
    except pywintypes.com_error:
        return None


def esta_instalado() -> bool:
    try:
        return _obtener_tarea() is not None
    except Exception:
        return False


def instalar(exe_path: str, interval_minutes: int) -> dict:
    """
    Crea (o actualiza si ya existe) la tarea programada.

    exe_path: ruta absoluta al TecnoMonitorService.exe instalado.
    interval_minutes: cada cuánto se repite al principio — ver
    actualizar_intervalo() para cambiarlo después sin recrear la tarea.
    """
    try:
        scheduler = _conectar()
        task_def = scheduler.NewTask(0)

        task_def.RegistrationInfo.Description = (
            "TecnoMonitor Agent - recolecta telemetria de infraestructura, "
            "KPIs clinicos y estado de integraciones (modo tarea programada)."
        )
        task_def.Settings.Enabled = True
        task_def.Settings.StartWhenAvailable = True
        task_def.Settings.DisallowStartIfOnBatteries = False
        task_def.Settings.StopIfGoingOnBatteries = False
        task_def.Settings.MultipleInstances = _TASK_INSTANCES_IGNORE_NEW

        trigger = task_def.Triggers.Create(_TASK_TRIGGER_LOGON)
        trigger.Id = "LogonTrigger"
        trigger.UserId = ""  # vacío = dispara con el logon de cualquier usuario
        trigger.Repetition.Interval = f"PT{int(interval_minutes)}M"
        trigger.Repetition.Duration = ""  # vacío = repetir indefinidamente

        action = task_def.Actions.Create(_TASK_ACTION_EXEC)
        action.Path = exe_path
        action.Arguments = "--run-once"

        task_def.Principal.GroupId   = _GRUPO_USUARIOS
        task_def.Principal.LogonType = _TASK_LOGON_GROUP
        task_def.Principal.RunLevel  = _TASK_RUNLEVEL_LUA

        folder = _obtener_carpeta(scheduler)
        folder.RegisterTaskDefinition(
            TASK_NAME, task_def, _TASK_CREATE_OR_UPDATE,
            "", "", _TASK_LOGON_GROUP,
        )
        return {"success": True}
    except Exception as e:
        return {"success": False, "msg": str(e)}


def desinstalar() -> dict:
    try:
        scheduler = _conectar()
        folder = _obtener_carpeta(scheduler)
        try:
            folder.DeleteTask(TASK_NAME, 0)
        except pywintypes.com_error:
            pass  # ya no existía — no es un error
        return {"success": True}
    except Exception as e:
        return {"success": False, "msg": str(e)}


def iniciar() -> dict:
    """Habilita la tarea y dispara una corrida inmediata (feedback rápido)."""
    tarea = _obtener_tarea()
    if tarea is None:
        return {"success": False, "msg": "La tarea programada no está instalada."}
    try:
        tarea.Enabled = True
        tarea.Run(None)
        return {"success": True}
    except Exception as e:
        return {"success": False, "msg": str(e)}


def detener() -> dict:
    """
    Deshabilita la tarea (no se vuelve a disparar). Un ciclo ya en curso, si
    lo hay, sigue hasta terminar — son ciclos cortos por diseño, no hace
    falta un stop "duro" como en el servicio.
    """
    tarea = _obtener_tarea()
    if tarea is None:
        return {"success": False, "msg": "La tarea programada no está instalada."}
    try:
        tarea.Enabled = False
        return {"success": True}
    except Exception as e:
        return {"success": False, "msg": str(e)}


def esta_corriendo() -> bool:
    """
    "Corriendo" acá significa "habilitada/programada", no "hay un proceso
    vivo ahora mismo": un ciclo de tarea es efímero por diseño, así que ese
    es el equivalente correcto a "el monitoreo está activo" en este modo.
    """
    tarea = _obtener_tarea()
    if tarea is None:
        return False
    try:
        return bool(tarea.Enabled)
    except Exception:
        return False


def estado_legible() -> str:
    """Estado para mostrar en el badge de la GUI."""
    tarea = _obtener_tarea()
    if tarea is None:
        return "No instalado"
    try:
        if not tarea.Enabled:
            return "Tarea deshabilitada"
        ultimo_resultado = tarea.LastTaskResult
        if ultimo_resultado not in (0, None):
            return f"Tarea activa (último ciclo con error: 0x{ultimo_resultado:X})"
        return "Tarea activa"
    except Exception:
        return "Desconocido"


def actualizar_intervalo(interval_minutes: int) -> dict:
    """
    Reconfigura cada cuánto se repite, sin recrear la tarea entera.

    Se llama desde service_control.reiniciar() cada vez que se guarda un
    interval_minutes nuevo estando en modo tarea — acá vive el intervalo
    real: el proceso --run-once no tiene noción de "cada cuánto", eso lo
    decide únicamente el trigger de la tarea.
    """
    try:
        scheduler = _conectar()
        tarea = _obtener_tarea(scheduler)
        if tarea is None:
            return {"success": False, "msg": "La tarea programada no está instalada."}

        definicion = tarea.Definition
        for trigger in definicion.Triggers:
            trigger.Repetition.Interval = f"PT{int(interval_minutes)}M"

        folder = _obtener_carpeta(scheduler)
        folder.RegisterTaskDefinition(
            TASK_NAME, definicion, _TASK_CREATE_OR_UPDATE,
            "", "", _TASK_LOGON_GROUP,
        )
        return {"success": True}
    except Exception as e:
        return {"success": False, "msg": str(e)}
