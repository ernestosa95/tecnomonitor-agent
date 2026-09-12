import sys
import os

# --- FIX CRÍTICO PARA MODO --noconsole ---
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")
# -----------------------------------------

import functools
import json
import time
import webview
import security
import agent_logic
import service_control   # v4.4: control del servicio vía SCM (reemplaza schtasks/taskkill)

DATA_DIR    = security.get_app_data_path()
CONFIG_FILE = os.path.join(DATA_DIR, "monitor_config.json")
LOG_FILE    = os.path.join(DATA_DIR, "activity.log")


def resource_path(relative_path):
    base_path = getattr(sys, '_MEIPASS', os.path.abspath("."))
    return os.path.join(base_path, relative_path)


# ---------------------------------------------------------------------------
# GATE DE SESIÓN (v4.6, ver docs/PLAN_MEJORAS_V4.5.md §3.1)
#
# Con Eel, cualquier @eel.expose quedaba invocable por WebSocket desde que
# arrancaba el proceso, sin depender del overlay de login (que era solo
# visual). pywebview tiene el mismo problema de fondo: todo método público de
# la clase pasada como js_api es invocable desde JS apenas la ventana carga.
# Este decorador cierra ese hueco: cada método sensible verifica una bandera
# de sesión en Python (no en el frontend, que es trivialmente saltable) antes
# de ejecutar cualquier acción, seteada recién cuando verificar_clave()
# confirma el código correcto.
# ---------------------------------------------------------------------------
def _requiere_sesion(func):
    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        if not self._autenticado:
            return {"ok": False, "error": "no_autenticado"}
        return func(self, *args, **kwargs)
    return wrapper


class Api:
    """
    Reemplaza a las funciones @eel.expose sueltas: pywebview expone como API
    de JS los métodos públicos de una instancia (`js_api=Api()` en
    create_window), en vez de decorar funciones de módulo una por una.
    """

    def __init__(self):
        # --- Código de acceso a la GUI (v4.5): código único por
        # instalación (ver security.py), ya no un hash fijo compartido
        # entre todos los hospitales. Se resuelve una sola vez al arrancar
        # el proceso; si es la primera vez (o admin.hash fue borrado a
        # propósito para resetear el acceso), _codigo_nuevo guarda el texto
        # plano para que la GUI se lo muestre una única vez. ---
        self._admin_hash, self._codigo_nuevo = security.obtener_o_generar_hash_admin()

        # Lockout de intentos fallidos — vive en Python, no solo en el
        # frontend. Tras 5 fallos consecutivos, bloquea 60s; se resetea al
        # acertar o al vencer.
        self._intentos_fallidos = 0
        self._bloqueado_hasta   = 0.0
        self._max_intentos      = 5
        self._lockout_segundos  = 60

        # Gate de sesión — ver _requiere_sesion arriba. Si _codigo_nuevo está
        # poblado, es la primera vez que corre este equipo: el código recién
        # se generó y se le muestra una única vez a quien abrió la app, sin
        # pasar por verificar_clave (no hay nada para verificar todavía) —
        # se considera autenticado igual que si lo hubiera tipeado.
        self._autenticado = self._codigo_nuevo is not None

    # -----------------------------------------------------------------
    # AUTENTICACIÓN (exentas del gate de sesión: son el login en sí)
    # -----------------------------------------------------------------
    def estado_acceso_gui(self):
        """
        El frontend la llama al cargar la página, antes de que el admin
        escriba nada. Si _codigo_nuevo está poblado, es la única vez que se
        muestra.
        """
        return {"primera_vez": self._codigo_nuevo is not None, "codigo": self._codigo_nuevo}

    def verificar_clave(self, clave_ingresada: str) -> dict:
        """El frontend envía el código; Python compara el hash. Nunca viaja el código guardado."""
        ahora = time.time()
        if ahora < self._bloqueado_hasta:
            return {"ok": False, "bloqueado": True, "segundos_restantes": int(self._bloqueado_hasta - ahora) + 1}

        if security.verificar_codigo_acceso(clave_ingresada, self._admin_hash):
            self._intentos_fallidos = 0
            self._autenticado = True
            return {"ok": True, "bloqueado": False, "segundos_restantes": 0}

        self._intentos_fallidos += 1
        if self._intentos_fallidos >= self._max_intentos:
            self._bloqueado_hasta   = ahora + self._lockout_segundos
            self._intentos_fallidos = 0
            return {"ok": False, "bloqueado": True, "segundos_restantes": self._lockout_segundos}

        return {"ok": False, "bloqueado": False, "segundos_restantes": 0}

    @_requiere_sesion
    def cambiar_codigo_gui(self):
        """
        "Cambiar código" en caliente (ver docs/PLAN_MEJORAS_V4.5.md §3.2):
        antes la única forma de resetear el acceso era borrar admin.hash a
        mano en el equipo. Requiere sesión ya iniciada (no es el login en
        sí) — actualiza el hash en memoria para que el resto de esta sesión
        siga funcionando sin reiniciar la GUI.
        """
        codigo_nuevo = security.regenerar_codigo_acceso()
        self._admin_hash, _ = security.obtener_o_generar_hash_admin()
        return {"ok": True, "codigo": codigo_nuevo}

    # -----------------------------------------------------------------
    # CONFIGURACIÓN — v4.6: objeto raíz {instalaciones: [...],
    # config_version: 2, interval_minutes: N}. La migración desde el
    # formato plano pre-v4.6 (y su persistencia autocurativa) vive en
    # agent_logic, compartida con headless_service.py — ver
    # docs/PLAN_MEJORAS_V4.5.md §9.1.1.
    # -----------------------------------------------------------------
    @_requiere_sesion
    def cargar_config(self):
        if not os.path.exists(CONFIG_FILE):
            return {"instalaciones": [], "config_version": 2, "interval_minutes": 5}
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            data = agent_logic.migrar_y_persistir_si_hace_falta(data, CONFIG_FILE)
            return agent_logic.desencriptar_config(data)
        except Exception as e:
            return {"_error": str(e)}

    @_requiere_sesion
    def guardar_config(self, config: dict):
        try:
            config = agent_logic.encriptar_config(config)

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
            # v4.5.0: en modo tarea programada, el intervalo recién guardado se
            # necesita acá para reconfigurar el trigger de repetición de la tarea
            # (ver service_control.reiniciar / task_control.actualizar_intervalo).
            # v4.6: interval_minutes ahora es global (raíz), no por hospital.
            res = service_control.reiniciar(config.get("interval_minutes", 5))
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

    # -----------------------------------------------------------------
    # CONTROL DEL SERVICIO
    # -----------------------------------------------------------------
    @_requiere_sesion
    def toggle_monitoreo(self, activar: bool):
        return service_control.iniciar() if activar else service_control.detener()

    @_requiere_sesion
    def check_service_status(self):
        """True sólo si el SCM reporta el servicio en ejecución."""
        return service_control.esta_corriendo()

    @_requiere_sesion
    def estado_servicio_detallado(self):
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

    # -----------------------------------------------------------------
    # LOG
    # -----------------------------------------------------------------
    @_requiere_sesion
    def limpiar_log(self):
        try:
            with open(LOG_FILE, 'w', encoding='utf-8') as f:
                f.write("--- Log limpiado por el administrador ---\n")
            return True
        except Exception:
            return False

    @_requiere_sesion
    def leer_log_delta(self, posicion_anterior: int):
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

    # -----------------------------------------------------------------
    # TESTS DE CONEXIÓN
    # -----------------------------------------------------------------
    @_requiere_sesion
    def test_proxmox_gui(self, data):
        return agent_logic.test_connection_proxmox(data)

    @_requiere_sesion
    def test_vmware_gui(self, data):
        return agent_logic.test_connection_vmware(data)

    @_requiere_sesion
    def test_idrac_gui(self, data):
        return agent_logic.test_connection_idrac(data)

    @_requiere_sesion
    def test_vm_gui(self, data):
        return agent_logic.test_connection_vm_wmi(data)

    @_requiere_sesion
    def test_vm_ssh_gui(self, data):
        return agent_logic.test_connection_vm_ssh(data)

    @_requiere_sesion
    def test_mirth_gui(self, data):
        return agent_logic.test_connection_mirth(data)

    @_requiere_sesion
    def probar_conexion_central(self, url: str):
        import requests as req
        try:
            r = req.get(url, timeout=5, verify=False)
            return {"success": True, "code": r.status_code}
        except Exception as e:
            return {"success": False, "msg": str(e)}

    @_requiere_sesion
    def reset_historial_sql(self, hospital_id: str):
        """v4.6: el checkpoint SQL es por hospital_id (ver agent_logic.reset_checkpoint)."""
        try:
            agent_logic.reset_checkpoint(hospital_id)
            return True
        except Exception:
            return False

    @_requiere_sesion
    def test_ssl_gui(self, data):
        return agent_logic.test_ssl_gui(data)

    @_requiere_sesion
    def test_elastic_gui(self, data):
        return agent_logic.test_connection_elastic(data)

    @_requiere_sesion
    def test_dicom_index_gui(self, data):
        return agent_logic.test_connection_dicom_index(data)

    @_requiere_sesion
    def test_ris_metrics_gui(self, data):
        return agent_logic.test_connection_ris_metrics(data)


# ---------------------------------------------------------------------------
# ARRANQUE
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    try:
        window = webview.create_window(
            'TecnoMonitor',
            resource_path(os.path.join('web', 'index.html')),
            js_api=Api(),
            width=1200,
            height=900,
            min_size=(1000, 700),
        )
        webview.start()
    except Exception as e:
        try:
            with open(LOG_FILE, 'a', encoding='utf-8') as f:
                f.write(f"--- CRASH GUI: {e} ---\n")
        except Exception:
            pass
