"""
Portal paciente: cola de publicación RIS + MPS (REQ-07) -- portal_paciente.py.

No hay un SQL Server ni un Elastic reales en la suite: la conexión y las respuestas se simulan.
Lo probado contra SQL/Logstash reales se valida en el hospital piloto.
"""
from unittest import mock

import pytest
import requests

import agent_logic
import portal_paciente as pp


@pytest.fixture()
def datos(tmp_path, monkeypatch):
    """Aísla los archivos de estado de agent_logic (el ciclo completo escribe checkpoints)."""
    monkeypatch.setattr(agent_logic, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(agent_logic, "SQL_CHECKPOINT_FILE", str(tmp_path / ".sql_checkpoint"))
    monkeypatch.setattr(agent_logic, "ELASTIC_CHECKPOINT_FILE", str(tmp_path / ".elastic_checkpoint"))
    monkeypatch.setattr(agent_logic, "SCHEMA_VERSION_OVERRIDE_FILE", str(tmp_path / "override.txt"))
    return tmp_path


CFG_SQL = {"hospital_id": "H1", "enabled_sql": True, "sql": {"host": "h", "enabled_portal": True}}
CFG_EL = {"hospital_id": "H1", "enabled_elastic": True, "elastic": {"host": "es", "port": 29200, "enabled_portal": True}}

AHORA = "2026-09-30T10:00:00"
# (origin, code, state, total, last_24h, pending_iso, with_iso, oldest, checked_at) -- orden de _CAMPOS
FILAS_RIS = [
    ("RIS", "4", "To be published", 280, 12, 0, 0, "2026-09-24T08:03:00", AHORA),
    ("RIS", "1", "Published", 101, 30, 0, 0, "2026-09-23T18:10:00", AHORA),
    ("RIS", "NULL", "Not Ready / In Progress", 114, 20, 0, 0, "2026-09-23T18:10:00", AHORA),
]
FILAS_MPS = [
    ("MPS", "1", "IDLE", 114, 5, 111, 3, "2026-09-24T12:56:11", AHORA),
    ("MPS", "6", "BLOCKED", 5, 0, 0, 5, "2026-09-24T21:05:49", AHORA),
    ("MPS", "2", "WAITING", 0, 0, 0, 0, None, AHORA),                 # estado del catálogo sin estudios
]


def _conn(*resultados):
    """Una conexión simulada: cada execute() devuelve el siguiente resultado (o lanza si es Exception)."""
    conn = mock.Mock()
    pendientes = list(resultados)
    cur = conn.cursor.return_value

    def execute(_):
        actual = pendientes.pop(0)
        if isinstance(actual, Exception):
            raise actual
        cur.fetchall.return_value = actual

    cur.execute.side_effect = execute
    return conn


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
def test_habilitacion_de_cada_camino():
    assert pp.habilitado_sql(CFG_SQL) and not pp.habilitado_elastic(CFG_SQL)
    assert pp.habilitado_elastic(CFG_EL) and not pp.habilitado_sql(CFG_EL)
    assert not pp.habilitado({"enabled_sql": True, "sql": {"host": "h"}}), "sin el sub-toggle no se activa"
    assert not pp.habilitado({"enabled_sql": False, "sql": {"host": "h", "enabled_portal": True}}), \
        "con la medición SQL quitada no corre aunque el sub-toggle quede tildado"


def test_si_ambos_estan_activos_gana_elastic():
    cfg = {**CFG_SQL, **CFG_EL}
    with mock.patch.object(pp, "recolectar_elastic", return_value=pp._respuesta(origen="elastic")) as el, \
         mock.patch.object(pp, "recolectar_sql") as sq:
        pp.recolectar(cfg)
    el.assert_called_once()
    sq.assert_not_called()


def test_recolectar_nunca_lanza():
    with mock.patch.object(pp, "recolectar_sql", side_effect=RuntimeError("boom")):
        r = pp.recolectar(CFG_SQL, log_func=lambda m: None)
    assert r["status"] == "error" and r["payload"] is None


# ---------------------------------------------------------------------------
# Camino SQL directo
# ---------------------------------------------------------------------------
def test_sql_manda_todos_los_estados_de_ris_y_mps():
    with mock.patch.object(pp.sql_integrity, "conectar", return_value=_conn(FILAS_RIS, FILAS_MPS)):
        r = pp.recolectar(CFG_SQL)
    assert r["status"] == "ok" and r["source"] == "sql" and r["extra"] == {"total": 6}
    p = r["payload"]
    assert p["source"] == "sql" and p["collected_at"] == AHORA and p["window_days"] == 30
    assert [(e["origin"], e["code"]) for e in p["states"]] == [
        ("RIS", "1"), ("RIS", "4"), ("RIS", "NULL"), ("MPS", "1"), ("MPS", "2"), ("MPS", "6")]
    idle = next(e for e in p["states"] if e["origin"] == "MPS" and e["code"] == "1")
    assert idle == {"origin": "MPS", "code": "1", "state": "IDLE", "total": 114, "last_24h": 5,
                    "pending_iso": 111, "with_iso": 3, "oldest": "2026-09-24T12:56:11"}
    vacio = next(e for e in p["states"] if e["code"] == "2")
    assert vacio["total"] == 0 and vacio["oldest"] is None, "un estado sin estudios viaja con 0, no falta"


def test_sql_sin_base_del_mps_manda_el_ris_como_parcial():
    err = Exception("Database 'ExtensaMPS' does not exist")
    with mock.patch.object(pp.sql_integrity, "conectar", return_value=_conn(FILAS_RIS, err)):
        r = pp.recolectar(CFG_SQL, log_func=lambda m: None)
    assert r["status"] == "partial" and "MPS" in r["extra"]["error"]
    assert {e["origin"] for e in r["payload"]["states"]} == {"RIS"}


def test_sql_fallan_las_dos_consultas_es_error():
    with mock.patch.object(pp.sql_integrity, "conectar", return_value=_conn(Exception("a"), Exception("b"))):
        r = pp.recolectar(CFG_SQL, log_func=lambda m: None)
    assert r["status"] == "error" and r["payload"] is None


def test_sql_caido_es_error_sin_payload():
    with mock.patch.object(pp.sql_integrity, "conectar", side_effect=Exception("sin red")):
        r = pp.recolectar(CFG_SQL, log_func=lambda m: None)
    assert r["status"] == "error" and r["payload"] is None and "sin red" in r["extra"]["error"]


def test_filas_invalidas_se_ignoran_y_los_numeros_se_normalizan():
    filas = [("OTRO", "1", "x", 1, 0, 0, 0, None, AHORA),                      # origen desconocido
             ("RIS", None, None, "7", None, 0, 0, "fecha rara", AHORA)]       # código nulo, total en texto
    with mock.patch.object(pp.sql_integrity, "conectar", return_value=_conn(filas, [])):
        r = pp.recolectar(CFG_SQL)
    assert r["payload"]["states"] == [{"origin": "RIS", "code": "NULL", "state": "UNKNOWN", "total": 7,
                                       "last_24h": 0, "pending_iso": 0, "with_iso": 0, "oldest": None}]


# ---------------------------------------------------------------------------
# Camino Elastic
# ---------------------------------------------------------------------------
class _RespES:
    def __init__(self, status, hits=None):
        self.status_code, self._hits = status, hits or []

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return {"hits": {"hits": [{"_source": h} for h in self._hits]}}


def _doc(origin, code, state, total, checked="2026-09-30T10:00:05", oldest="2026-09-24T12:56:11", **extra):
    d = {"origin": origin, "code": code, "state": state, "total": total, "last_24h": 0,
         "pending_iso": 0, "with_iso": 0, "checked_at": checked, **extra}
    if oldest is not None:
        d["oldest"] = oldest               # Logstash omite el campo cuando la columna viene NULL
    return d


def test_elastic_indice_inexistente_es_empty():
    with mock.patch.object(pp.requests, "post", return_value=_RespES(404)):
        assert pp.recolectar(CFG_EL)["status"] == "empty"


def test_elastic_error_de_conexion_es_error():
    with mock.patch.object(pp.requests, "post", side_effect=requests.exceptions.ConnectionError("x")):
        r = pp.recolectar(CFG_EL, log_func=lambda m: None)
    assert r["status"] == "error" and r["payload"] is None


def test_elastic_arma_el_payload_y_descarta_documentos_de_corridas_viejas():
    hits = [_doc("MPS", "1", "IDLE", 114, pending_iso=111, with_iso=3),
            _doc("RIS", "4", "To be published", 280, checked="2026-09-30T10:00:01"),
            _doc("MPS", "3", "CREATING", 0, oldest=None),
            _doc("MPS", "8", "VIEJO", 9, checked="2026-09-29T08:00:00"),       # código que dejó de salir
            {"ORIGIN": "MPS", "CODE": "9", "STATE": "BURNER", "TOTAL": 102, "CHECKED_AT": "2026-09-30T10:00:04"},
            {"origin": "MPS", "code": "5"}]                                     # sin corrida: se ignora
    with mock.patch.object(pp.requests, "post", return_value=_RespES(200, hits)):
        r = pp.recolectar(CFG_EL)
    p = r["payload"]
    assert r["status"] == "ok" and p["source"] == "elastic"
    assert p["collected_at"] == "2026-09-30T10:00:05", "la lectura es la de la última corrida de Logstash"
    assert [(e["origin"], e["code"], e["total"]) for e in p["states"]] == [
        ("RIS", "4", 280), ("MPS", "1", 114), ("MPS", "3", 0), ("MPS", "9", 102)]
    assert p["states"][1]["pending_iso"] == 111 and p["states"][2]["oldest"] is None


def test_elastic_usa_https_e_indice_configurables():
    cfg = {"enabled_elastic": True, "elastic": {"host": "es", "port": 9200, "use_https": True,
                                                "enabled_portal": True, "portal_index": "mi_indice"}}
    with mock.patch.object(pp.requests, "post", return_value=_RespES(404)) as post:
        pp.recolectar(cfg)
    assert post.call_args[0][0] == "https://es:9200/mi_indice/_search"


# ---------------------------------------------------------------------------
# Las consultas del agente y del .conf de Logstash son las mismas
# ---------------------------------------------------------------------------
def _normalizar(sql):
    return " ".join(sql.replace(";", " ").split())


def test_las_consultas_coinciden_con_el_conf_de_logstash():
    import os
    ruta = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "elk", "ext_portal_paciente.conf")
    conf = _normalizar(open(ruta, encoding="utf-8").read())
    assert _normalizar(pp._RIS_SQL) in conf
    assert _normalizar(pp._MPS_SQL) in conf


def test_las_consultas_respetan_las_reglas_de_logstash():
    for sql in (pp._RIS_SQL, pp._MPS_SQL):
        assert '"' not in sql and "--" not in sql and ":" not in sql and "${" not in sql


# ---------------------------------------------------------------------------
# Integración con el ciclo del agente
# ---------------------------------------------------------------------------
class _RespCentral:
    status_code = 201

    def raise_for_status(self):
        pass


def _ciclo(cfg):
    enviados = []

    def post(url, **kw):
        if url.endswith("/ext_portal_paciente/_search"):
            return _RespES(200, [_doc("MPS", "1", "IDLE", 114)])
        enviados.append(kw["json"])
        return _RespCentral()

    with mock.patch.object(agent_logic, "obtener_salud_red_pasiva", return_value={}), \
         mock.patch.object(agent_logic, "recolectar_logs_elastic", return_value={"events": [], "meta": {"scan_time": "x", "new_alerts": 0}}), \
         mock.patch.object(agent_logic.requests, "post", side_effect=post):
        agent_logic.ejecutar_ciclo_agente({"auth_token": "t", "central_url": "http://central/x", **cfg},
                                          log_callback=lambda m: None)
    return enviados[0]


def test_ciclo_incluye_patient_portal_en_cada_envio(datos):
    rep = _ciclo(CFG_EL)
    assert rep["collection_meta"]["patient_portal"] == {"enabled": True, "status": "ok", "total": 1, "source": "elastic"}
    assert rep["software_monitoring"]["patient_portal"]["states"][0]["state"] == "IDLE"
    assert rep["envelope"]["agent_version"] == "4.5.4" and rep["envelope"]["schema_version"] == "4.5"


def test_ciclo_con_el_modulo_apagado_no_manda_nada(datos):
    rep = _ciclo({"hospital_id": "H1"})
    assert rep["collection_meta"]["patient_portal"] == {"enabled": False, "status": "disabled"}
    assert "patient_portal" not in rep["software_monitoring"]


# ---------------------------------------------------------------------------
# Botones de test de la GUI
# ---------------------------------------------------------------------------
def test_boton_sql_resume_por_origen():
    with mock.patch.object(pp.sql_integrity, "conectar", return_value=_conn(FILAS_RIS, FILAS_MPS)):
        r = pp.test_conexion_sql({"host": "h"})
    assert r["success"]
    assert "To be published 280" in r["msg"] and "MPS: IDLE 114, BLOCKED 5" in r["msg"]
    assert "WAITING" not in r["msg"], "los estados en 0 no se listan"


def test_boton_sql_sin_mps_avisa_el_fallo():
    with mock.patch.object(pp.sql_integrity, "conectar", return_value=_conn(FILAS_RIS, Exception("no MPS"))):
        r = pp.test_conexion_sql({"host": "h"})
    assert not r["success"] and "Parcial" in r["msg"] and "MPS: no MPS" in r["msg"]


def test_boton_indice_sin_datos_y_con_datos():
    with mock.patch.object(pp.requests, "post", return_value=_RespES(404)):
        assert "todavía sin datos" in pp.test_conexion_indice({"host": "es"})["msg"]
    with mock.patch.object(pp.requests, "post", return_value=_RespES(200, [_doc("MPS", "1", "IDLE", 3)])):
        r = pp.test_conexion_indice({"host": "es"})
    assert r["success"] and "MPS: IDLE 3" in r["msg"]
