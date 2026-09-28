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
    def __init__(self, json_data=None, raise_exc=None, content=b""):
        self._json = json_data if json_data is not None else {}
        self._raise_exc = raise_exc
        self.content = content

    def raise_for_status(self):
        if self._raise_exc:
            raise self._raise_exc

    def json(self):
        return self._json


class _FakeSession:
    def __init__(self, statistics=None, statuses=None, channels=None,
                 login_falla=False, logout_falla=False, channels_falla=False,
                 channels_json_500=False, channels_xml=None):
        self.headers = {}
        self._statistics = statistics if statistics is not None else {}
        self._statuses = statuses if statuses is not None else {}
        self._channels = channels if channels is not None else {}
        self._login_falla = login_falla
        self._logout_falla = logout_falla
        self._channels_falla = channels_falla
        # Mirth 4.5.2 de H05: /api/channels da 500 en JSON y 200 en XML.
        self._channels_json_500 = channels_json_500
        self._channels_xml = channels_xml
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
            if (kwargs.get("headers") or {}).get("Accept") == "application/xml":
                return _FakeResponse(content=(self._channels_xml or "<list/>").encode("utf-8"))
            if self._channels_json_500:
                return _FakeResponse(raise_exc=requests.exceptions.HTTPError(
                    "500 Server Error: Server Error for url: https://mirth.local/api/channels"))
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


# El mismo canal que _channel_json(), como lo devuelve Mirth 4.5.2 en XML
# (atributos version/class incluidos, recortado a lo que usa la topología).
_CANAL_XML = """
  <channel version="4.5.2">
    <id>{cid}</id>
    <nextMetaDataId>2</nextMetaDataId>
    <name>{nombre}</name>
    <description></description>
    <revision>7</revision>
    <sourceConnector version="4.5.2">
      <metaDataId>0</metaDataId>
      <name>sourceConnector</name>
      <properties class="com.mirth.connect.connectors.tcp.TcpReceiverProperties" version="4.5.2">
        <pluginProperties/>
        <listenerConnectorProperties version="4.5.2">
          <host>0.0.0.0</host>
          <port>6661</port>
        </listenerConnectorProperties>
      </properties>
      <transportName>TCP Listener</transportName>
      <mode>SOURCE</mode>
      <enabled>true</enabled>
    </sourceConnector>
    <destinationConnectors>
      <connector version="4.5.2">
        <metaDataId>1</metaDataId>
        <name>Enviar a RIS</name>
        <properties class="{clase}" version="4.5.2">
          {props}
        </properties>
        <transportName>{transport}</transportName>
        <mode>DESTINATION</mode>
        <enabled>true</enabled>
      </connector>
    </destinationConnectors>
  </channel>"""


def _channel_xml(cid="ch-1", nombre="ADT_A01", destino_transport="TCP Sender"):
    if destino_transport == "TCP Sender":
        clase = "com.mirth.connect.connectors.tcp.TcpDispatcherProperties"
        props = "<remoteAddress>10.0.2.10</remoteAddress><remotePort>6663</remotePort>"
    else:
        clase = "com.mirth.connect.connectors.vm.VmDispatcherProperties"
        props = "<channelId>ch-2</channelId>"
    return _CANAL_XML.format(cid=cid, nombre=nombre, transport=destino_transport, clase=clase, props=props)


def _channels_xml(*canales):
    return "<list>" + "".join(canales) + "\n</list>"


def _cfg(alias="Produccion", url="https://mirth.local"):
    return {"alias": alias, "url": url, "user": "u", "pass": "p"}


def setup_function(_):
    # El cache de topología vive a nivel de módulo -- limpiarlo entre tests
    # para que no se filtren resultados de un test a otro.
    mirth_collector._topo_cache.clear()
    mirth_collector._avisado_xml.clear()


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


# ---------------------------------------------------------------------------
# Fallback a XML: Mirth 4.5.2 (H05) da 500 en JSON en /api/channels
# ---------------------------------------------------------------------------

def test_topologia_por_xml_es_identica_a_la_de_json():
    s_json = _FakeSession(channels={"list": {"channel": [
        _channel_json(), _channel_json(cid="ch-3", nombre="ORM", destino_transport="Channel Writer")]}})
    por_json = mirth_collector._recolectar_topologia(s_json, "https://mirth.local", "Prod", ["ch-1", "ch-3"])

    mirth_collector._topo_cache.clear()
    s_xml = _FakeSession(channels_json_500=True, channels_xml=_channels_xml(
        _channel_xml(), _channel_xml(cid="ch-3", nombre="ORM", destino_transport="Channel Writer")))
    por_xml = mirth_collector._recolectar_topologia(s_xml, "https://mirth.local", "Prod", ["ch-1", "ch-3"])

    assert por_xml == por_json
    assert por_xml[0]["destinations"][0]["metadata_id"] == 1, "en XML llega como texto: debe salir numérico"
    assert por_xml[1]["destinations"][0]["target_channel_id"] == "ch-2"


def test_topologia_por_xml_con_un_solo_canal():
    s = _FakeSession(channels_json_500=True, channels_xml=_channels_xml(_channel_xml()))
    canales = mirth_collector._recolectar_topologia(s, "https://mirth.local", "Prod", ["ch-1"])
    assert [c["channel_id"] for c in canales] == ["ch-1"]
    assert canales[0]["source"]["endpoint"] == "0.0.0.0:6661"


def test_topologia_por_xml_sin_canales_da_lista_vacia():
    s = _FakeSession(channels_json_500=True, channels_xml="<list/>")
    assert mirth_collector._recolectar_topologia(s, "https://mirth.local", "Prod", []) == []


def test_fallback_xml_se_loguea_una_sola_vez_por_alias():
    logs = []
    s = _FakeSession(channels_json_500=True, channels_xml=_channels_xml(_channel_xml()))
    mirth_collector._recolectar_topologia(s, "https://mirth.local", "Prod", ["ch-1"], logs.append)
    mirth_collector._topo_cache.clear()
    mirth_collector._recolectar_topologia(s, "https://mirth.local", "Prod", ["ch-1"], logs.append)
    assert len(logs) == 1 and "se usa XML" in logs[0]


def test_si_fallan_json_y_xml_el_log_muestra_ambos():
    logs = []
    s = _FakeSession(channels_json_500=True, channels_xml="<list><channel>")  # XML cortado
    resultado = mirth_collector._recolectar_topologia(s, "https://mirth.local", "Prod", ["ch-1"], logs.append)
    assert resultado is None
    assert "JSON: 500" in logs[0] and "XML:" in logs[0]


def test_recolectar_mirth_con_json_500_manda_topologia_por_xml():
    s = _FakeSession(statistics=_statistics_ok(), statuses=_statuses_ok(), channels_json_500=True,
                      channels_xml=_channels_xml(_channel_xml()))
    with mock.patch.object(mirth_collector.requests, "Session", return_value=s):
        resultados, status, errores, topo = mirth_collector.recolectar_mirth([_cfg()])
    assert status == "ok" and errores == 0
    assert topo["Produccion"]["channels"][0]["channel_id"] == "ch-1"
