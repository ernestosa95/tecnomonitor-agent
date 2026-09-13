"""
Autoenrute DICOM directo a SQL Server (v4.6) — restaura el camino que existía
hasta v4.3, como alternativa a la variante vía ElasticSearch, mismo patrón
que los KPIs de negocio (extraer_metricas_sql / extraer_metricas_ris_elastic).

Corre en CADA ciclo (interval_minutes global), no en el bloque de extracción
de KPIs -- sin checkpoint ni ventana, es una foto del estado actual de las
reglas. Ver docs/PLAN_MEJORAS_V4.5.md.
"""
from unittest import mock

import agent_logic


class _FakeCursor:
    def __init__(self, filas):
        self._filas = filas

    def execute(self, query):
        self._query_ejecutada = query

    def fetchall(self):
        return self._filas


class _FakeConnection:
    def __init__(self, filas):
        self._filas = filas

    def cursor(self):
        return _FakeCursor(self._filas)

    def close(self):
        pass


def test_obtener_dicom_routing_sql_mapea_filas_al_mismo_formato_que_elastic():
    filas = [
        ("R001", "pacs01", "PACS Principal", "10.0.0.5", "cloud01", "Nube Central", "cloud.tecnoimagen.com.ar", 42),
        ("R002", "pacs02", "PACS Secundario", "10.0.0.6", "cloud01", "Nube Central", "cloud.tecnoimagen.com.ar", 0),
    ]
    with mock.patch.object(agent_logic.pyodbc, "connect", return_value=_FakeConnection(filas)):
        rutas, status, errores = agent_logic.obtener_dicom_routing_sql(
            {"host": "SRVDB", "user": "sa", "pass": "x"}
        )

    assert status == "ok"
    assert errores == 0
    assert len(rutas) == 2
    assert rutas[0]["id_rule"] == "R001"
    assert rutas[0]["from_node"] == {"key": "pacs01", "nickname": "PACS Principal", "hostname": "10.0.0.5"}
    assert rutas[0]["to_node"]["key"] == "cloud01"
    assert rutas[0]["pending_instances"] == 42
    assert rutas[0]["snapshot_age_minutes"] == 0.0, "sin lag de pipeline que medir: el dato es siempre en vivo"


def test_obtener_dicom_routing_sql_sin_filas_es_empty_no_error():
    with mock.patch.object(agent_logic.pyodbc, "connect", return_value=_FakeConnection([])):
        rutas, status, errores = agent_logic.obtener_dicom_routing_sql({"host": "SRVDB", "user": "sa", "pass": "x"})
    assert rutas == []
    assert status == "empty"


def test_obtener_dicom_routing_sql_sin_host_es_error():
    rutas, status, errores = agent_logic.obtener_dicom_routing_sql({})
    assert status == "error"
    assert errores == 1


def test_obtener_dicom_routing_sql_falla_de_conexion_es_error_no_crashea():
    with mock.patch.object(agent_logic.pyodbc, "connect", side_effect=agent_logic.pyodbc.Error("no conecta")):
        rutas, status, errores = agent_logic.obtener_dicom_routing_sql({"host": "SRVDB", "user": "sa", "pass": "x"})
    assert status == "error"
    assert rutas == []


# --- _dicom_routing_habilitado: prioridad Elastic sobre SQL, igual que los KPIs ---

def test_dicom_routing_habilitado_por_sql_si_elastic_esta_apagado():
    config = {"enabled_sql": True, "sql": {"host": "SRVDB", "enabled_dicom_routing": True}}
    assert agent_logic._dicom_routing_habilitado(config) is True


def test_dicom_routing_habilitado_falso_si_ningun_camino_esta_activo():
    config = {"enabled_sql": True, "sql": {"host": "SRVDB", "enabled_dicom_routing": False}}
    assert agent_logic._dicom_routing_habilitado(config) is False


def test_dicom_routing_habilitado_requiere_el_modulo_maestro_ademas_del_sub_toggle():
    # enabled_sql=False (la tarjeta SQL está apagada) aunque el sub-toggle diga True
    config = {"enabled_sql": False, "sql": {"host": "SRVDB", "enabled_dicom_routing": True}}
    assert agent_logic._dicom_routing_habilitado(config) is False


# --- Despacho real en ejecutar_ciclo_agente: Elastic gana si ambos están activos ---

def _config_base():
    return {
        "hospital_id": "H1", "auth_token": "tok", "central_url": "https://x",
        "enabled_sql": True,
        "sql": {"host": "SRVDB", "user": "sa", "pass": "x", "enabled_dicom_routing": True},
        "enabled_elastic": True,
        "elastic": {"host": "127.0.0.1", "enabled_dicom_routing": True},
    }


def test_ejecutar_ciclo_agente_usa_elastic_si_ambos_estan_configurados():
    config = _config_base()
    with mock.patch.object(agent_logic, "get_dicom_routing_queues", return_value=([{"id_rule": "VIA-ELASTIC"}], "ok", 0)) as mock_elastic, \
         mock.patch.object(agent_logic, "obtener_dicom_routing_sql") as mock_sql, \
         mock.patch.object(agent_logic, "recolectar_logs_elastic", return_value={"events": [], "meta": {"scan_time": "x", "new_alerts": 0}}), \
         mock.patch.object(agent_logic.requests, "post", side_effect=agent_logic.requests.exceptions.ConnectionError("x")):
        res = agent_logic.ejecutar_ciclo_agente(config)

    mock_elastic.assert_called_once()
    mock_sql.assert_not_called()


def test_ejecutar_ciclo_agente_usa_sql_si_elastic_no_tiene_el_sub_toggle_activo():
    config = _config_base()
    config["elastic"]["enabled_dicom_routing"] = False

    with mock.patch.object(agent_logic, "get_dicom_routing_queues") as mock_elastic, \
         mock.patch.object(agent_logic, "obtener_dicom_routing_sql", return_value=([{"id_rule": "VIA-SQL"}], "ok", 0)) as mock_sql, \
         mock.patch.object(agent_logic.requests, "post", side_effect=agent_logic.requests.exceptions.ConnectionError("x")):
        agent_logic.ejecutar_ciclo_agente(config)

    mock_elastic.assert_not_called()
    mock_sql.assert_called_once()


def test_ejecutar_ciclo_agente_no_llama_a_ninguno_si_ambos_apagados():
    config = _config_base()
    config["sql"]["enabled_dicom_routing"] = False
    config["elastic"]["enabled_dicom_routing"] = False

    with mock.patch.object(agent_logic, "get_dicom_routing_queues") as mock_elastic, \
         mock.patch.object(agent_logic, "obtener_dicom_routing_sql") as mock_sql, \
         mock.patch.object(agent_logic.requests, "post", side_effect=agent_logic.requests.exceptions.ConnectionError("x")):
        agent_logic.ejecutar_ciclo_agente(config)

    mock_elastic.assert_not_called()
    mock_sql.assert_not_called()
