"""
Mirth Connect: distilación de topología (origen/destino/routing interno vía
Channel Writer) desde GET /api/channels, cache de esa llamada, saneado de
credenciales, y el fix del bug donde una falla en el logout descartaba
telemetría ya recolectada. Ver docs/CONTRATO_AGENTE.md §7 y el plan de
implementación del mapa de integraciones.
"""
import json
from unittest import mock

import requests

import mirth_collector


# ---------------------------------------------------------------------------
# Fakes de la sesión HTTP de Mirth (login/statistics/statuses/channels/logout)
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, json_data=None, raise_exc=None):
        self._json = json_data if json_data is not None else {}
        self._raise_exc = raise_exc

    def raise_for_status(self):
        if self._raise_exc:
            raise self._raise_exc

    def json(self):
        return self._json


class _FakeSession:
    def __init__(self, statistics=None, statuses=None, channels=None,
                 login_falla=False, logout_falla=False, channels_falla=False):
        self.headers = {}
        self._statistics = statistics if statistics is not None else {}
        self._statuses = statuses if statuses is not None else {}
        self._channels = channels if channels is not None else {}
        self._login_falla = login_falla
        self._logout_falla = logout_falla
        self._channels_falla = channels_falla
        self.logout_calls = 0
        self.channels_calls = 0

    def post(self, url, **kwargs):
        if url.endswith("/_login"):
            if self._login_falla:
                raise requests.exceptions.ConnectionError("login roto")
            return _FakeResponse({})
        if url.endswith("/_logout"):
            self.logout_calls += 1
            if self._logout_falla:
                raise requests.exceptions.ConnectionError("logout roto (timeout)")
            return _FakeResponse({})
        raise AssertionError(f"POST inesperado en el fake: {url}")

    def get(self, url, **kwargs):
        if url.endswith("/statistics"):
            return _FakeResponse(self._statistics)
        if url.endswith("/statuses"):
            return _FakeResponse(self._statuses)
        if url.endswith("/channels"):
            self.channels_calls += 1
            if self._channels_falla:
                raise requests.exceptions.ConnectionError("channels roto")
            return _FakeResponse(self._channels)
        raise AssertionError(f"GET inesperado en el fake: {url}")


def _statuses_ok(channel_id="ch-1", nombre="ADT_A01", estado="RUNNING", queued=5, errores=1):
    return {
        "list": {
            "dashboardStatus": [
                {
                    "channelId": channel_id,
                    "name": nombre,
                    "state": estado,
                    "statistics": {
                        "entry": [
                            {"com.mirth.connect.donkey.model.message.Status": "QUEUED", "long": queued},
                            {"com.mirth.connect.donkey.model.message.Status": "ERROR", "long": errores},
                        ]
                    },
                }
            ]
        }
    }


def _statistics_ok(channel_id="ch-1", received=100, sent=95):
    return {"list": {"channelStatistics": [{"channelId": channel_id, "received": received, "sent": sent}]}}


def _channel_json(cid="ch-1", nombre="ADT_A01", destino_transport="TCP Sender"):
    return {
        "id": cid,
        "name": nombre,
        "revision": 7,
        "sourceConnector": {
            "transportName": "TCP Listener",
            "properties": {"listenerConnectorProperties": {"host": "0.0.0.0", "port": "6661"}},
        },
        "destinationConnectors": {
            "connector": [
                {
                    "metaDataId": 1,
                    "name": "Enviar a RIS",
                    "transportName": destino_transport,
                    "enabled": True,
                    "properties": {"remoteAddress": "10.0.2.10", "remotePort": "6663"}
                        if destino_transport == "TCP Sender"
                        else {"channelId": "ch-2"},
                }
            ]
        },
    }


def _channels_ok(**kwargs):
    return {"list": {"channel": [_channel_json(**kwargs)]}}


def _cfg(alias="Produccion", url="https://mirth.local"):
    return {"alias": alias, "url": url, "user": "u", "pass": "p"}


def setup_function(_):
    # El cache de topología vive a nivel de módulo -- limpiarlo entre tests
    # para que no se filtren resultados de un test a otro.
    mirth_collector._topo_cache.clear()


# ---------------------------------------------------------------------------
# _endpoint_de_conector / _sanear_endpoint: distilación por tipo, sin fugas
# ---------------------------------------------------------------------------

def test_endpoint_listener_arma_host_y_puerto():
    conn = {"transportName": "TCP Listener",
            "properties": {"listenerConnectorProperties": {"host": "0.0.0.0", "port": "6661"}}}
    out = mirth_collector._endpoint_de_conector(conn)
    assert out == {"transport": "TCP Listener", "endpoint": "0.0.0.0:6661",
                    "host": "0.0.0.0", "port": 6661, "target_channel_id": None}


def test_endpoint_channel_writer_da_target_channel_id_sin_endpoint():
    conn = {"transportName": "Channel Writer", "properties": {"channelId": "9a1b4c"}}
    out = mirth_collector._endpoint_de_conector(conn)
    assert out["target_channel_id"] == "9a1b4c"
    assert out["endpoint"] is None


def test_endpoint_channel_writer_sin_asignar_es_none_no_el_literal():
    conn = {"transportName": "Channel Writer", "properties": {"channelId": "none"}}
    out = mirth_collector._endpoint_de_conector(conn)
    assert out["target_channel_id"] is None


def test_endpoint_database_reader_sanea_credenciales_del_jdbc():
    conn = {"transportName": "Database Reader",
            "properties": {"url": "jdbc:sqlserver://10.0.2.10:1433;databaseName=RIS;user=sa;password=SuperSecreto123"}}
    out = mirth_collector._endpoint_de_conector(conn)
    assert "SuperSecreto123" not in out["endpoint"]
    assert "sa" not in out["endpoint"] or "user=***" in out["endpoint"]
    assert "10.0.2.10" in out["endpoint"]


def test_endpoint_http_sender_sanea_userinfo_en_la_url():
    conn = {"transportName": "HTTP Sender", "properties": {"host": "https://admin:clave@obrasocial.example/api"}}
    out = mirth_collector._endpoint_de_conector(conn)
    assert "clave" not in out["endpoint"]
    assert "admin" not in out["endpoint"]
    assert "obrasocial.example" in out["endpoint"]


def test_endpoint_conector_no_mapeado_no_explota():
    conn = {"transportName": "Algo Custom Raro", "properties": {"cualquierCosa": {"anidado": True}}}
    out = mirth_collector._endpoint_de_conector(conn)
    assert out["transport"] == "Algo Custom Raro"
    assert out["endpoint"] is None


def test_endpoint_con_conector_none_no_explota():
    out = mirth_collector._endpoint_de_conector(None)
    assert out["transport"] is None


# ---------------------------------------------------------------------------
# _distilar_canal
# ---------------------------------------------------------------------------

def test_distilar_canal_arma_source_y_destinations():
    ch = _channel_json()
    d = mirth_collector._distilar_canal(ch)
    assert d["channel_id"] == "ch-1"
    assert d["name"] == "ADT_A01"
    assert d["revision"] == 7
    assert d["source"]["transport"] == "TCP Listener"
    assert d["source"]["endpoint"] == "0.0.0.0:6661"
    assert len(d["destinations"]) == 1
    assert d["destinations"][0]["endpoint"] == "10.0.2.10:6663"
    assert d["destinations"][0]["target_channel_id"] is None


def test_distilar_canal_con_channel_writer_como_destino():
    ch = _channel_json(destino_transport="Channel Writer")
    d = mirth_collector._distilar_canal(ch)
    assert d["destinations"][0]["target_channel_id"] == "ch-2"
    assert d["destinations"][0]["endpoint"] is None


def test_distilar_canal_sin_id_devuelve_none():
    assert mirth_collector._distilar_canal({"name": "sin id"}) is None


# ---------------------------------------------------------------------------
# _recolectar_topologia: cache por TTL y por cambio de set de ids
# ---------------------------------------------------------------------------

def test_recolectar_topologia_usa_cache_dentro_del_ttl_con_mismos_ids():
    s = _FakeSession(channels=_channels_ok())
    r1 = mirth_collector._recolectar_topologia(s, "https://mirth.local", "Prod", ["ch-1"])
    r2 = mirth_collector._recolectar_topologia(s, "https://mirth.local", "Prod", ["ch-1"])
    assert r1 == r2
    assert s.channels_calls == 1, "la segunda llamada debería salir del cache, sin pegarle a Mirth de nuevo"


def test_recolectar_topologia_refresca_si_cambia_el_set_de_ids():
    s = _FakeSession(channels=_channels_ok())
    mirth_collector._recolectar_topologia(s, "https://mirth.local", "Prod", ["ch-1"])
    mirth_collector._recolectar_topologia(s, "https://mirth.local", "Prod", ["ch-1", "ch-nuevo"])
    assert s.channels_calls == 2, "un canal nuevo/borrado debe forzar refresco aunque no pasó el TTL"


def test_recolectar_topologia_si_falla_y_hay_cache_devuelve_la_copia_vieja():
    s_ok = _FakeSession(channels=_channels_ok())
    cache_previo = mirth_collector._recolectar_topologia(s_ok, "https://mirth.local", "Prod", ["ch-1"])

    s_roto = _FakeSession(channels_falla=True)
    # Forzamos que no matchee el cache por TTL para que reintente el fetch y falle
    mirth_collector._topo_cache["Prod"]["fetched_at"] -= mirth_collector.CACHE_TOPO_TTL_SEG + 1
    resultado = mirth_collector._recolectar_topologia(s_roto, "https://mirth.local", "Prod", ["ch-1"])
    assert resultado == cache_previo


def test_recolectar_topologia_sin_cache_y_con_falla_devuelve_none():
    s_roto = _FakeSession(channels_falla=True)
    resultado = mirth_collector._recolectar_topologia(s_roto, "https://mirth.local", "Prod", ["ch-1"])
    assert resultado is None


# ---------------------------------------------------------------------------
# recolectar_mirth: end to end, incluyendo el fix del logout
# ---------------------------------------------------------------------------

def test_recolectar_mirth_arma_channel_id_y_errored_numerico():
    s = _FakeSession(statistics=_statistics_ok(), statuses=_statuses_ok(), channels=_channels_ok())
    with mock.patch.object(mirth_collector.requests, "Session", return_value=s):
        resultados, status, errores, topo = mirth_collector.recolectar_mirth([_cfg()])

    canal = resultados["Produccion"][0]
    assert canal["channel_id"] == "ch-1"
    assert canal["errored"] == 1
    assert canal["last_error"] == "Errores acumulados: 1"
    assert status == "ok"
    assert errores == 0
    assert "Produccion" in topo
    assert topo["Produccion"]["channels"][0]["channel_id"] == "ch-1"


def test_recolectar_mirth_sin_configs_devuelve_disabled():
    resultados, status, errores, topo = mirth_collector.recolectar_mirth([])
    assert resultados == {}
    assert status == "disabled"
    assert topo == {}


def test_recolectar_mirth_login_roto_da_system_error_y_partial():
    s = _FakeSession(login_falla=True)
    with mock.patch.object(mirth_collector.requests, "Session", return_value=s):
        resultados, status, errores, topo = mirth_collector.recolectar_mirth([_cfg()])

    assert resultados["Produccion"][0]["channel"] == "SYSTEM_ERROR"
    assert status == "error"  # único servidor configurado y falló -> error, no partial
    assert errores == 1


def test_recolectar_mirth_logout_roto_no_descarta_canales_ya_recolectados():
    """
    El bug original: login+stats+status+logout vivían en el mismo try, así
    que una excepción en el logout (timeout=5, más corto que el resto)
    tiraba el bloque entero al except genérico y reemplazaba canales_data
    -- ya recolectado con éxito -- por un canal sintético SYSTEM_ERROR.
    """
    s = _FakeSession(statistics=_statistics_ok(), statuses=_statuses_ok(),
                      channels=_channels_ok(), logout_falla=True)
    with mock.patch.object(mirth_collector.requests, "Session", return_value=s):
        resultados, status, errores, topo = mirth_collector.recolectar_mirth([_cfg()])

    canal = resultados["Produccion"][0]
    assert canal["channel"] == "ADT_A01", "el canal real recolectado no debe perderse por una falla en el logout"
    assert canal["channel_id"] == "ch-1"
    assert status == "ok", "una falla de logout, ya aislada, no debe marcar el ciclo como partial/error"
    assert errores == 0
    assert s.logout_calls == 1


def test_recolectar_mirth_topologia_rota_no_afecta_canales_data():
    s = _FakeSession(statistics=_statistics_ok(), statuses=_statuses_ok(), channels_falla=True)
    with mock.patch.object(mirth_collector.requests, "Session", return_value=s):
        resultados, status, errores, topo = mirth_collector.recolectar_mirth([_cfg()])

    assert resultados["Produccion"][0]["channel"] == "ADT_A01"
    assert status == "ok"
    assert "Produccion" not in topo


# ---------------------------------------------------------------------------
# Chequeo explícito de no-fuga de credenciales sobre el payload serializado
# ---------------------------------------------------------------------------

def test_topologia_serializada_no_contiene_credenciales():
    ch = {
        "id": "ch-9", "name": "FACT_ObraSocial", "revision": 1,
        "sourceConnector": {"transportName": "TCP Listener",
                             "properties": {"listenerConnectorProperties": {"host": "0.0.0.0", "port": "6661"}}},
        "destinationConnectors": {"connector": [
            {"metaDataId": 1, "name": "A obra social", "transportName": "HTTP Sender", "enabled": True,
             "properties": {"host": "https://svc:MiClaveSecreta@os.example.com/api"}},
            {"metaDataId": 2, "name": "Backup", "transportName": "Database Writer", "enabled": True,
             "properties": {"url": "jdbc:sqlserver://10.0.9.9;databaseName=BK;user=svc;password=OtraClave"}},
        ]},
    }
    s = _FakeSession(statistics=_statistics_ok(), statuses=_statuses_ok(channel_id="ch-9"),
                      channels={"list": {"channel": [ch]}})
    with mock.patch.object(mirth_collector.requests, "Session", return_value=s):
        _, _, _, topo = mirth_collector.recolectar_mirth([_cfg()])

    serializado = json.dumps(topo)
    assert "MiClaveSecreta" not in serializado
    assert "OtraClave" not in serializado
