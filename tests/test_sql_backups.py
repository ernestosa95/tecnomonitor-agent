"""
Último backup completo de las bases SQL Server (REQ-06) -- sql_backups.py.

No hay un SQL Server ni un Elastic reales en la suite: la conexión y las respuestas se simulan.
Lo probado contra SQL/Logstash reales se valida en el hospital piloto.
"""
from unittest import mock

import pytest
import requests

import agent_logic
import sql_backups as sb


@pytest.fixture()
def datos(tmp_path, monkeypatch):
    """Aísla los archivos de estado de agent_logic (el ciclo completo escribe checkpoints)."""
    monkeypatch.setattr(agent_logic, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(agent_logic, "SQL_CHECKPOINT_FILE", str(tmp_path / ".sql_checkpoint"))
    monkeypatch.setattr(agent_logic, "ELASTIC_CHECKPOINT_FILE", str(tmp_path / ".elastic_checkpoint"))
    monkeypatch.setattr(agent_logic, "SCHEMA_VERSION_OVERRIDE_FILE", str(tmp_path / "override.txt"))
    return tmp_path


CFG_SQL = {"hospital_id": "H1", "enabled_sql": True, "sql": {"host": "h", "enabled_backups": True,
                                                            "checkdb_databases": ["ExtensaRadio", "ExtensaPACS", "NoExiste"]}}
CFG_EL = {"hospital_id": "H1", "enabled_elastic": True, "elastic": {"host": "es", "port": 29200, "enabled_backups": True}}


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
def test_habilitacion_de_cada_camino():
    assert sb.habilitado_sql(CFG_SQL) and not sb.habilitado_elastic(CFG_SQL)
    assert sb.habilitado_elastic(CFG_EL) and not sb.habilitado_sql(CFG_EL)
    assert not sb.habilitado({"enabled_sql": True, "sql": {"host": "h"}}), "sin el sub-toggle no se activa"
    assert not sb.habilitado({"enabled_sql": False, "sql": {"host": "h", "enabled_backups": True}}), \
        "con la medición SQL quitada no corre aunque el sub-toggle quede tildado"


def test_si_ambos_estan_activos_gana_elastic():
    cfg = {**CFG_SQL, **CFG_EL}
    with mock.patch.object(sb, "recolectar_elastic", return_value=sb._respuesta(origen="elastic")) as el, \
         mock.patch.object(sb, "recolectar_sql") as sq:
        sb.recolectar(cfg)
    el.assert_called_once()
    sq.assert_not_called()


def test_recolectar_nunca_lanza():
    with mock.patch.object(sb, "recolectar_sql", side_effect=RuntimeError("boom")):
        r = sb.recolectar(CFG_SQL, log_func=lambda m: None)
    assert r["status"] == "error" and r["payload"] is None


# ---------------------------------------------------------------------------
# Camino SQL directo
# ---------------------------------------------------------------------------
def _conn(filas):
    conn = mock.Mock()
    conn.cursor.return_value.fetchall.return_value = filas
    return conn


FILAS_SQL = [
    ("ExtensaRadio", "2026-09-28T02:10:00", "2026-09-28T15:00:00"),
    ("extensapacs", None, "2026-09-28T15:00:00"),                    # nunca tuvo backup completo
    ("master", "2026-09-27T02:00:00", "2026-09-28T15:00:00"),        # no está en la lista: no va
]


def test_sql_manda_solo_las_bases_configuradas_que_existen_con_su_ultimo_full():
    with mock.patch.object(sb.sql_integrity, "conectar", return_value=_conn(FILAS_SQL)):
        r = sb.recolectar(CFG_SQL)
    assert r["status"] == "ok" and r["source"] == "sql"
    p = r["payload"]
    assert p["source"] == "sql" and p["collected_at"] == "2026-09-28T15:00:00", "la hora de lectura es la del SQL"
    assert p["databases"] == [
        {"db": "ExtensaRadio", "last_full": "2026-09-28T02:10:00"},
        {"db": "extensapacs", "last_full": None},
    ], "sin distinguir mayúsculas; una base sin backup aparece con fecha nula, no falta"


def test_sql_sin_lista_configurada_usa_las_26_por_defecto():
    cfg = {"enabled_sql": True, "sql": {"host": "h", "enabled_backups": True}}
    with mock.patch.object(sb.sql_integrity, "conectar", return_value=_conn(FILAS_SQL)):
        r = sb.recolectar(cfg)
    assert [b["db"] for b in r["payload"]["databases"]] == ["extensapacs", "ExtensaRadio"]


def test_sql_caido_es_error_sin_payload():
    with mock.patch.object(sb.sql_integrity, "conectar", side_effect=Exception("sin red")):
        r = sb.recolectar(CFG_SQL, log_func=lambda m: None)
    assert r["status"] == "error" and r["payload"] is None and "sin red" in r["extra"]["error"]


def test_sql_sin_ninguna_base_configurada_existente_es_empty():
    with mock.patch.object(sb.sql_integrity, "conectar", return_value=_conn([("otra", None, "2026-09-28T15:00:00")])):
        r = sb.recolectar(CFG_SQL)
    assert r["status"] == "empty" and r["payload"] is None


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


def _doc(db, last_full="2026-09-28T02:10:00", checked="2026-09-28T15:00:05"):
    d = {"dbname": db, "checked_at": checked}
    if last_full is not None:
        d["last_full"] = last_full          # Logstash omite el campo cuando la columna viene NULL
    return d


def test_elastic_indice_inexistente_es_empty():
    with mock.patch.object(sb.requests, "post", return_value=_RespES(404)):
        assert sb.recolectar(CFG_EL)["status"] == "empty"


def test_elastic_error_de_conexion_es_error():
    with mock.patch.object(sb.requests, "post", side_effect=requests.exceptions.ConnectionError("x")):
        r = sb.recolectar(CFG_EL, log_func=lambda m: None)
    assert r["status"] == "error" and r["payload"] is None


def test_elastic_arma_el_payload_y_descarta_bases_que_ya_no_estan_en_el_conf():
    hits = [_doc("ExtensaRadio"), _doc("ExtensaPACS", last_full=None),
            _doc("SacadaDeLaLista", checked="2026-09-20T15:00:05"),   # documento viejo de otra lista
            {"DBNAME": "Mayus", "LAST_FULL": "2026-09-27T23:00:00", "CHECKED_AT": "2026-09-28T14:59:59"},
            {"checked_at": "2026-09-28T15:00:05"}]                     # sin nombre: se ignora
    with mock.patch.object(sb.requests, "post", return_value=_RespES(200, hits)):
        r = sb.recolectar(CFG_EL)
    p = r["payload"]
    assert r["status"] == "ok" and p["source"] == "elastic"
    assert p["collected_at"] == "2026-09-28T15:00:05", "la lectura es la de la última corrida de Logstash"
    assert p["databases"] == [
        {"db": "ExtensaPACS", "last_full": None},
        {"db": "ExtensaRadio", "last_full": "2026-09-28T02:10:00"},
        {"db": "Mayus", "last_full": "2026-09-27T23:00:00"},
    ]


def test_elastic_usa_https_e_indice_configurables():
    cfg = {"enabled_elastic": True, "elastic": {"host": "es", "port": 9200, "use_https": True,
                                                "enabled_backups": True, "backups_index": "mi_indice"}}
    with mock.patch.object(sb.requests, "post", return_value=_RespES(404)) as post:
        sb.recolectar(cfg)
    assert post.call_args[0][0] == "https://es:9200/mi_indice/_search"


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
        if url.endswith("/ext_sql_backups/_search"):
            return _RespES(200, [_doc("ExtensaRadio")])
        enviados.append(kw["json"])
        return _RespCentral()

    with mock.patch.object(agent_logic, "obtener_salud_red_pasiva", return_value={}), \
         mock.patch.object(agent_logic, "recolectar_logs_elastic", return_value={"events": [], "meta": {"scan_time": "x", "new_alerts": 0}}), \
         mock.patch.object(agent_logic.requests, "post", side_effect=post):
        agent_logic.ejecutar_ciclo_agente({"auth_token": "t", "central_url": "http://central/x", **cfg},
                                          log_callback=lambda m: None)
    return enviados[0]


def test_ciclo_incluye_sql_backups_en_cada_envio(datos):
    rep = _ciclo(CFG_EL)
    assert rep["collection_meta"]["sql_backups"] == {"enabled": True, "status": "ok", "total": 1, "source": "elastic"}
    assert rep["software_monitoring"]["sql_backups"]["databases"] == [{"db": "ExtensaRadio", "last_full": "2026-09-28T02:10:00"}]
    assert rep["envelope"]["agent_version"] == "4.5.3" and rep["envelope"]["schema_version"] == "4.5"


def test_ciclo_con_el_modulo_apagado_no_manda_nada(datos):
    rep = _ciclo({"hospital_id": "H1"})
    assert rep["collection_meta"]["sql_backups"] == {"enabled": False, "status": "disabled"}
    assert "sql_backups" not in rep["software_monitoring"]


# ---------------------------------------------------------------------------
# Botones de test de la GUI
# ---------------------------------------------------------------------------
def test_boton_sql_resume_bases_sin_backup_y_viejas():
    filas = FILAS_SQL + [("ExtensaHistory", "2026-09-26T02:00:00", "2026-09-28T15:00:00")]
    cfg = {"host": "h", "checkdb_databases": ["ExtensaRadio", "ExtensaPACS", "ExtensaHistory"]}
    with mock.patch.object(sb.sql_integrity, "conectar", return_value=_conn(filas)):
        r = sb.test_conexion_sql(cfg)
    assert r["success"]
    assert "3 bases" in r["msg"] and "1 sin ningún backup completo (extensapacs)" in r["msg"] and "1 con el último" in r["msg"]


def test_boton_sql_sin_permiso_sobre_msdb():
    conn = mock.Mock()
    conn.cursor.return_value.execute.side_effect = Exception("The SELECT permission was denied on the object 'backupset'")
    with mock.patch.object(sb.sql_integrity, "conectar", return_value=conn):
        r = sb.test_conexion_sql({"host": "h"})
    assert not r["success"] and "msdb.dbo.backupset" in r["msg"]


def test_boton_indice_sin_datos_y_con_datos():
    with mock.patch.object(sb.requests, "post", return_value=_RespES(404)):
        assert "todavía sin datos" in sb.test_conexion_indice({"host": "es"})["msg"]
    with mock.patch.object(sb.requests, "post", return_value=_RespES(200, [_doc("A")])):
        r = sb.test_conexion_indice({"host": "es"})
    assert r["success"] and "todas con backup completo" in r["msg"]
