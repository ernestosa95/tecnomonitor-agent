import sys
import os

# --- FIX CRÍTICO PARA MODO --noconsole ---
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")
# -----------------------------------------

import eel
import json
import time
import security
import agent_logic
import service_control   # v4.4: control del servicio vía SCM (reemplaza schtasks/taskkill)

DATA_DIR    = security.get_app_data_path()
CONFIG_FILE = os.path.join(DATA_DIR, "monitor_config.json")
LOG_FILE    = os.path.join(DATA_DIR, "activity.log")

# ---------------------------------------------------------------------------
# Código de acceso a la GUI — v4.5: código único por instalación (ver
# security.py), ya no un hash fijo compartido entre todos los hospitales.
# Se resuelve una sola vez al arrancar el proceso; si es la primera vez (o el
# admin.hash fue borrado a propósito para resetear el acceso), _CODIGO_NUEVO
# guarda el texto plano para que la GUI se lo muestre una única vez.
# ---------------------------------------------------------------------------
_ADMIN_HASH, _CODIGO_NUEVO = security.obtener_o_generar_hash_admin()

# Lockout de intentos fallidos — vive en Python (no solo en el frontend, que
# es trivialmente saltable llamando la función expuesta directo). Simple:
# tras 5 fallos consecutivos, bloquea 60s; se resetea al acertar o al vencer.
_INTENTOS_FALLIDOS = 0
_BLOQUEADO_HASTA   = 0.0
_MAX_INTENTOS      = 5
_LOCKOUT_SEGUNDOS  = 60


def resource_path(relative_path):
    base_path = getattr(sys, '_MEIPASS', os.path.abspath("."))
    return os.path.join(base_path, relative_path)


eel.init(resource_path('web'))


# ---------------------------------------------------------------------------
# AUTENTICACIÓN
# ---------------------------------------------------------------------------
@eel.expose
def estado_acceso_gui():
    """
    El frontend la llama al cargar la página, antes de que el admin escriba
    nada. Si _CODIGO_NUEVO está poblado, es la única vez que se muestra.
    """
    return {"primera_vez": _CODIGO_NUEVO is not None, "codigo": _CODIGO_NUEVO}


@eel.expose
def verificar_clave(clave_ingresada: str) -> dict:
    """El frontend envía el código; Python compara el hash. Nunca viaja el código guardado."""
    global _INTENTOS_FALLIDOS, _BLOQUEADO_HASTA

    ahora = time.time()
    if ahora < _BLOQUEADO_HASTA:
        return {"ok": False, "bloqueado": True, "segundos_restantes": int(_BLOQUEADO_HASTA - ahora) + 1}

    if security.verificar_codigo_acceso(clave_ingresada, _ADMIN_HASH):
        _INTENTOS_FALLIDOS = 0
        return {"ok": True, "bloqueado": False, "segundos_restantes": 0}

    _INTENTOS_FALLIDOS += 1
    if _INTENTOS_FALLIDOS >= _MAX_INTENTOS:
        _BLOQUEADO_HASTA   = ahora + _LOCKOUT_SEGUNDOS
        _INTENTOS_FALLIDOS = 0
        return {"ok": False, "bloqueado": True, "segundos_restantes": _LOCKOUT_SEGUNDOS}

    return {"ok": False, "bloqueado": False, "segundos_restantes": 0}


# ---------------------------------------------------------------------------
# CONFIGURACIÓN
# ---------------------------------------------------------------------------
def _desencriptar_config(data: dict) -> dict:
    """Desencripta todas las credenciales de un dict de configuración."""
    if data.get("auth_token"):
        data["auth_token"] = security.desencriptar(data["auth_token"])

    if isinstance(data.get("proxmox"), dict):
        px = data["proxmox"]
        if px.get("pass"):
            px["pass"] = security.desencriptar(px["pass"])

    if isinstance(data.get("idrac"), dict):
        if data["idrac"].get("pass"):
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

    # --- NUEVO v4.3: Desencriptar ElasticSearch ---
    if isinstance(data.get("elastic"), dict) and data["elastic"].get("pass"):
        data["elastic"]["pass"] = security.desencriptar(data["elastic"]["pass"])

    return data


@eel.expose
def cargar_config():
    if not os.path.exists(CONFIG_FILE):
        return {}
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return _desencriptar_config(data)
    except Exception as e:
        return {"_error": str(e)}


@eel.expose
def guardar_config(config: dict):
    try:
        # Encriptar credenciales antes de persistir
        if config.get("auth_token"):
            config["auth_token"] = security.encriptar(config["auth_token"])

        if isinstance(config.get("proxmox"), dict) and config["proxmox"].get("pass"):
            config["proxmox"]["pass"] = security.encriptar(config["proxmox"]["pass"])

        if isinstance(config.get("idrac"), dict) and config["idrac"].get("pass"):
            config["idrac"]["pass"] = security.encriptar(config["idrac"]["pass"])

        if isinstance(config.get("sql"), dict) and config["sql"].get("pass"):
            config["sql"]["pass"] = security.encriptar(config["sql"]["pass"])

        if isinstance(config.get("vms"), list):
            for vm in config["vms"]:
                if isinstance(vm, dict) and vm.get("pass"):
                    vm["pass"] = security.encriptar(vm["pass"])

        # --- NUEVO: Encriptar Mirth Connect ---
        if isinstance(config.get("mirth_servers"), list):
            for m in config["mirth_servers"]:
                if isinstance(m, dict) and m.get("pass"):
                    m["pass"] = security.encriptar(m["pass"])

        # --- NUEVO v4.3: Encriptar ElasticSearch ---
        if isinstance(config.get("elastic"), dict) and config["elastic"].get("pass"):
            config["elastic"]["pass"] = security.encriptar(config["elastic"]["pass"])

        # Escritura atómica del JSON
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
        os.replace(tmp, CONFIG_FILE)

        # --- v4.4: Reinicio ordenado para aplicar cambios ---
        # Antes se hacía toggle_monitoreo(False) seguido de toggle_monitoreo(True),
        # que era un taskkill /F + un arranque inmediato. El proceso viejo podía
        # seguir liberando recursos cuando el nuevo intentaba tomar el candado, y
        # el agente quedaba apagado sin aviso. Ahora esperamos la confirmación
        # del SCM antes de volver a levantarlo.
        res = service_control.reiniciar()
        if not res.get("success"):
            return {
                "success": True,
                "warning": True,
                "msg": ("La configuración se guardó correctamente, pero el servicio "
                        f"no pudo reiniciarse: {res.get('msg')}"),
            }

        return {"success": True}

    except Exception as e:
        return {"success": False, "msg": str(e)}


# ---------------------------------------------------------------------------
# CONTROL DEL SERVICIO
# ---------------------------------------------------------------------------
# v4.4: el agente es un servicio de Windows (TecnoMonitorAgent). Toda la
# interacción pasa por el SCM en vez de schtasks/taskkill, así que ya no
# dependemos de que haya una sesión interactiva abierta ni de parsear la
# salida de `sc` (que cambia según el idioma del Windows del hospital).
# ---------------------------------------------------------------------------
@eel.expose
def toggle_monitoreo(activar: bool):
    return service_control.iniciar() if activar else service_control.detener()


@eel.expose
def check_service_status():
    """True sólo si el SCM reporta el servicio en ejecución."""
    return service_control.esta_corriendo()


@eel.expose
def estado_servicio_detallado():
    """
    Estado textual para el badge de la GUI.

    Distingue 'Detenido' de 'No instalado', que con la detección por psutil de
    la v4.3 se veían igual y mandaban al técnico a buscar el problema donde no
    estaba.
    """
    return {
        "estado":    service_control.estado_legible(),
        "instalado": service_control.esta_instalado(),
        "corriendo": service_control.esta_corriendo(),
    }


# ---------------------------------------------------------------------------
# LOG
# ---------------------------------------------------------------------------
@eel.expose
def limpiar_log():
    try:
        with open(LOG_FILE, 'w', encoding='utf-8') as f:
            f.write("--- Log limpiado por el administrador ---\n")
        return True
    except Exception:
        return False


@eel.expose
def leer_log_delta(posicion_anterior: int):
    if not os.path.exists(LOG_FILE):
        return {"content": "", "pos": 0}
    try:
        with open(LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
            f.seek(0, 2)
            tamano_total = f.tell()
            if posicion_anterior > tamano_total:
                posicion_anterior = 0
            f.seek(posicion_anterior)
            contenido      = f.read()
            nueva_posicion = f.tell()
        return {"content": contenido, "pos": nueva_posicion}
    except Exception as e:
        return {"content": f"[Error leyendo log: {e}]\n", "pos": posicion_anterior}


# ---------------------------------------------------------------------------
# TESTS DE CONEXIÓN
# ---------------------------------------------------------------------------
@eel.expose
def test_proxmox_gui(data):
    return agent_logic.test_connection_proxmox(data)


@eel.expose
def test_vmware_gui(data):
    return agent_logic.test_connection_vmware(data)


@eel.expose
def test_idrac_gui(data):
    return agent_logic.test_connection_idrac(data)


@eel.expose
def test_vm_gui(data):
    return agent_logic.test_connection_vm_wmi(data)


# --- NUEVO: Test Mirth Connect ---
@eel.expose
def test_mirth_gui(data):
    return agent_logic.test_connection_mirth(data)


@eel.expose
def probar_conexion_central(url: str):
    import requests as req
    try:
        r = req.get(url, timeout=5, verify=False)
        return {"success": True, "code": r.status_code}
    except Exception as e:
        return {"success": False, "msg": str(e)}


@eel.expose
def reset_historial_sql():
    try:
        agent_logic.reset_checkpoint()
        return True
    except Exception:
        return False


@eel.expose
def test_ssl_gui(data):
    return agent_logic.test_ssl_gui(data)


# --- NUEVO v4.3: Test ElasticSearch ---
@eel.expose
def test_elastic_gui(data):
    return agent_logic.test_connection_elastic(data)

@eel.expose
def test_dicom_index_gui(data):
    return agent_logic.test_connection_dicom_index(data)


# ---------------------------------------------------------------------------
# ARRANQUE
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    try:
        eel.start('index.html', size=(1100, 900), port=0)
    except Exception as e:
        try:
            with open(LOG_FILE, 'a', encoding='utf-8') as f:
                f.write(f"--- CRASH GUI: {e} ---\n")
        except Exception:
            pass