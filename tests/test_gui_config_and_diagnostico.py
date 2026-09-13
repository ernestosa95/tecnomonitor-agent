"""
main_gui.Api: guardado/carga de config (round-trip con cifrado real) y
"Enviar ahora" (envío manual inmediato de un perfil, sin esperar el
intervalo global ni pasar por el checkpoint de KPIs de negocio).

No cubre la parte de UI/DOM (eso requiere un motor de render real — se
validó a mano con pywebview/QtWebEngine, ver la conversación de diseño).
Esto prueba la capa de la que depende esa UI: los métodos de Api.
"""
from unittest import mock

import security


def test_guardar_y_cargar_config_hacen_roundtrip_con_cifrado_real(sesion_gui_aislada):
    api = sesion_gui_aislada.main_gui.Api()  # primera vez -> autenticada

    config = {
        "config_version": 2,
        "interval_minutes": 5,
        "instalaciones": [
            {
                "hospital_id": "H42",
                "auth_token": "token-plano-h42",
                "central_url": "https://tecnomonitor.tecnoimagen.com.ar/v1/hospital-status",
                "enabled": True,
                "enabled_idrac": True,
                "idrac": {"ip": "192.168.1.50", "user": "root", "pass": "calvin"},
            }
        ],
    }

    res = api.guardar_config(config)
    assert res["success"] is True

    with open(sesion_gui_aislada.config_file, "r", encoding="utf-8") as f:
        crudo = f.read()
    assert "calvin" not in crudo, "la contraseña no debe quedar en texto plano en el archivo"
    assert "token-plano-h42" not in crudo

    recargada = api.cargar_config()
    perfil = recargada["instalaciones"][0]
    assert perfil["idrac"]["pass"] == "calvin", "cargar_config debe devolver todo ya desencriptado"
    assert perfil["auth_token"] == "token-plano-h42"


def test_cargar_config_sin_archivo_devuelve_estructura_vacia_valida(sesion_gui_aislada):
    api = sesion_gui_aislada.main_gui.Api()
    res = api.cargar_config()
    assert res["instalaciones"] == []
    assert res["interval_minutes"] == 5


def test_enviar_ahora_gui_delega_en_ejecutar_ciclo_agente(sesion_gui_aislada):
    api = sesion_gui_aislada.main_gui.Api()
    perfil = {"hospital_id": "H1", "central_url": "https://x", "auth_token": "t"}

    with mock.patch("main_gui.agent_logic.ejecutar_ciclo_agente",
                    return_value={"status": "OK", "timestamp": "10:00:00"}) as mock_ciclo:
        res = api.enviar_ahora_gui(perfil)

    mock_ciclo.assert_called_once_with(perfil)
    assert res["status"] == "OK"


def test_enviar_ahora_gui_no_crashea_si_ejecutar_ciclo_agente_explota(sesion_gui_aislada):
    api = sesion_gui_aislada.main_gui.Api()
    with mock.patch("main_gui.agent_logic.ejecutar_ciclo_agente", side_effect=RuntimeError("boom")):
        res = api.enviar_ahora_gui({"hospital_id": "H1"})
    assert res["status"] == "Error"
    assert "boom" in res["error"]
