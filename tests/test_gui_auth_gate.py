"""
Gate de sesión de la GUI (main_gui.Api) — cierra el bypass de autenticación
que existía con Eel/pywebview (ver docs/PLAN_MEJORAS_V4.5.md §3.1), y
"cambiar código en caliente" (§3.2).
"""
import security


def test_primera_vez_queda_autenticada_sin_pasar_por_verificar_clave(sesion_gui_aislada):
    api = sesion_gui_aislada.main_gui.Api()

    assert api._codigo_nuevo is not None, "primera corrida: se genera un código nuevo"
    assert api._autenticado is True, \
        "no tiene sentido pedir loguearse con un código que se acaba de mostrar en pantalla"

    res = api.cargar_config()
    assert "error" not in res, "cargar_config funciona sin haber llamado verificar_clave"


def test_segunda_instancia_arranca_sin_autenticar_y_rechaza_metodos_sensibles(sesion_gui_aislada):
    api1 = sesion_gui_aislada.main_gui.Api()  # genera admin.hash (primera vez)
    api2 = sesion_gui_aislada.main_gui.Api()  # admin.hash ya existe

    assert api2._codigo_nuevo is None
    assert api2._autenticado is False

    rechazo = {"ok": False, "error": "no_autenticado"}
    assert api2.cargar_config() == rechazo
    assert api2.guardar_config({"instalaciones": []}) == rechazo
    assert api2.toggle_monitoreo(True) == rechazo
    assert api2.cambiar_codigo_gui() == rechazo
    assert api2.enviar_ahora_gui({}) == rechazo


def test_estado_acceso_gui_no_requiere_sesion(sesion_gui_aislada):
    api = sesion_gui_aislada.main_gui.Api()
    estado = api.estado_acceso_gui()
    assert "primera_vez" in estado
    assert "agent_version" in estado, "la versión del agente viaja para mostrarla en el header"


def test_login_incorrecto_no_autentica_y_bloquea_tras_varios_intentos(sesion_gui_aislada):
    sesion_gui_aislada.main_gui.Api()  # deja creado admin.hash
    api = sesion_gui_aislada.main_gui.Api()  # segunda instancia, sin autenticar

    r_malo = api.verificar_clave("codigo-incorrecto-a-proposito")
    assert r_malo["ok"] is False
    assert api._autenticado is False
    assert api.cargar_config() == {"ok": False, "error": "no_autenticado"}


def test_cambiar_codigo_en_caliente_actualiza_la_sesion_actual(sesion_gui_aislada):
    api = sesion_gui_aislada.main_gui.Api()  # primera vez -> ya autenticada
    assert api._autenticado is True

    res = api.cambiar_codigo_gui()
    assert res["ok"] is True
    assert res["codigo"]

    verif = api.verificar_clave(res["codigo"])
    assert verif["ok"] is True, "el código nuevo debe verificar en la MISMA sesión, sin reiniciar la GUI"


def test_regenerar_codigo_acceso_invalida_el_codigo_anterior(sesion_gui_aislada):
    codigo1 = security.regenerar_codigo_acceso()
    hash_guardado = open(security.ADMIN_HASH_FILE).read().strip()
    assert security.verificar_codigo_acceso(codigo1, hash_guardado)

    codigo2 = security.regenerar_codigo_acceso()
    assert codigo1 != codigo2
    hash_guardado_2 = open(security.ADMIN_HASH_FILE).read().strip()
    assert not security.verificar_codigo_acceso(codigo1, hash_guardado_2)
