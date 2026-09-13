"""
Sub-toggle independiente para los logs de Suitestensa dentro de la tarjeta
Elastic (elastic.enabled_logs) — antes, tener Elastic activo implicaba
siempre leer logs, sin forma de desactivarlo por separado. Retrocompatible:
"ausente o true" = activo, solo un false explícito lo desactiva.
"""
from unittest import mock

import agent_logic


def test_logs_habilitado_por_defecto_si_la_clave_no_esta_presente():
    config = {"enabled_elastic": True, "elastic": {"host": "127.0.0.1"}}
    assert agent_logic._logs_suitestensa_habilitado(config) is True


def test_logs_deshabilitado_con_false_explicito():
    config = {"enabled_elastic": True, "elastic": {"host": "127.0.0.1", "enabled_logs": False}}
    assert agent_logic._logs_suitestensa_habilitado(config) is False


def test_logs_habilitado_con_true_explicito():
    config = {"enabled_elastic": True, "elastic": {"host": "127.0.0.1", "enabled_logs": True}}
    assert agent_logic._logs_suitestensa_habilitado(config) is True


def test_logs_deshabilitado_si_el_modulo_elastic_esta_apagado():
    config = {"enabled_elastic": False, "elastic": {"host": "127.0.0.1", "enabled_logs": True}}
    assert agent_logic._logs_suitestensa_habilitado(config) is False


def test_logs_deshabilitado_sin_host():
    config = {"enabled_elastic": True, "elastic": {"enabled_logs": True}}
    assert agent_logic._logs_suitestensa_habilitado(config) is False


def test_ejecutar_ciclo_agente_no_llama_recolectar_logs_si_enabled_logs_es_false():
    config = {
        "hospital_id": "H1", "auth_token": "tok", "central_url": "https://x",
        "enabled_elastic": True,
        "elastic": {"host": "127.0.0.1", "enabled_logs": False},
    }
    with mock.patch.object(agent_logic, "recolectar_logs_elastic") as mock_logs, \
         mock.patch.object(agent_logic.requests, "post",
                            side_effect=agent_logic.requests.exceptions.ConnectionError("x")):
        agent_logic.ejecutar_ciclo_agente(config)
    mock_logs.assert_not_called()


def test_ejecutar_ciclo_agente_llama_recolectar_logs_si_enabled_logs_ausente():
    config = {
        "hospital_id": "H1", "auth_token": "tok", "central_url": "https://x",
        "enabled_elastic": True,
        "elastic": {"host": "127.0.0.1"},  # config vieja, sin la clave -- retrocompatible
    }
    with mock.patch.object(agent_logic, "recolectar_logs_elastic",
                            return_value={"events": [], "meta": {"scan_time": "x", "new_alerts": 0}}) as mock_logs, \
         mock.patch.object(agent_logic.requests, "post",
                            side_effect=agent_logic.requests.exceptions.ConnectionError("x")):
        agent_logic.ejecutar_ciclo_agente(config)
    mock_logs.assert_called_once()
