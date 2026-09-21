"""
Chequeo de integridad de bases SQL Server (DBCC CHECKDB) tras un reinicio -- sql_integrity.py.
Ver docs/PLAN_CHECKDB_POST_REINICIO.md.

No hay un SQL Server real en la suite: la conexión, el arranque de SQL, el lanzamiento del
trabajador y las respuestas de Elastic se simulan. Lo verdaderamente probado contra SQL/Logstash
reales se valida en el hospital piloto (fase F5 del plan).
"""
import json
from datetime import datetime, timedelta
from unittest import mock

import pytest
import pyodbc
import requests

import agent_logic
import sql_integrity as si


@pytest.fixture()
def datos(tmp_path, monkeypatch):
    """Aísla los archivos de estado/resultados del módulo (y los de agent_logic)."""
    monkeypatch.setattr(si, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(agent_logic, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(agent_logic, "SQL_CHECKPOINT_FILE", str(tmp_path / ".sql_checkpoint"))
    monkeypatch.setattr(agent_logic, "ELASTIC_CHECKPOINT_FILE", str(tmp_path / ".elastic_checkpoint"))
    monkeypatch.setattr(agent_logic, "SCHEMA_VERSION_OVERRIDE_FILE", str(tmp_path / "override.txt"))
    return tmp_path


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
def test_habilitacion_de_cada_camino_y_prioridad_de_elastic():
    solo_sql = {"enabled_sql": True, "sql": {"host": "h", "enabled_checkdb": True}}
    solo_el = {"enabled_elastic": True, "elastic": {"host": "e", "enabled_checkdb": True}}
    assert si.habilitado_sql(solo_sql) and not si.habilitado_elastic(solo_sql) and si.habilitado(solo_sql)
    assert si.habilitado_elastic(solo_el) and not si.habilitado_sql(solo_el)
    assert not si.habilitado({"enabled_sql": True, "sql": {"host": "h"}}), "sin el flag propio no corre"
    assert not si.habilitado({"enabled_sql": True, "sql": {"enabled_checkdb": True}}), "sin host no corre"


def test_si_ambos_estan_activos_gana_elastic():
    cfg = {"enabled_sql": True, "sql": {"host": "h", "enabled_checkdb": True},
           "enabled_elastic": True, "elastic": {"host": "e", "enabled_checkdb": True}}
    with mock.patch.object(si, "recolectar_elastic", return_value={"payload": None, "status": "ok", "extra": {}, "commit": None, "source": "elastic"}) as el, \
         mock.patch.object(si, "recolectar_sql") as sq:
        assert si.recolectar(cfg)["source"] == "elastic"
    el.assert_called_once()
    sq.assert_not_called()


def test_bases_por_defecto_editables_sin_vacias_ni_repetidas():
    assert len(si.BASES_POR_DEFECTO) == 26 and "DICOMedP@CS" in si.BASES_POR_DEFECTO
    assert si.bases_configuradas({}) == si.BASES_POR_DEFECTO
    assert si.bases_configuradas({"checkdb_databases": []}) == si.BASES_POR_DEFECTO
    assert si.bases_configuradas({"checkdb_databases": [" A ", "a", "", "B"]}) == ["A", "B"]
    assert si.bases_configuradas({"checkdb_databases": "A, B;C\nD"}) == ["A", "B", "C", "D"]


def test_tipo_de_chequeo_configurable():
    assert si.tipo_chequeo({}) == "full"
    assert si.tipo_chequeo({"checkdb_type": "physical_only"}) == "physical_only"
    assert si.tipo_chequeo({"checkdb_type": "PHYSICAL_ONLY"}) == "physical_only"
    assert si.tipo_chequeo({"checkdb_type": "cualquier cosa"}) == "full"


def test_recolectar_nunca_lanza(datos):
    cfg = {"hospital_id": "H1", "enabled_sql": True, "sql": {"host": "h", "enabled_checkdb": True}}
    with mock.patch.object(si, "recolectar_sql", side_effect=RuntimeError("boom")):
        r = si.recolectar(cfg)
    assert r["status"] == "error" and r["payload"] is None
    assert si.recolectar({"hospital_id": "H1"})["status"] == "disabled"


# ---------------------------------------------------------------------------
# CHECKDB de una base
# ---------------------------------------------------------------------------
class _Fila:
    def __init__(self, texto):
        self.MessageText = texto


class _Cursor:
    def __init__(self, filas=None, error=None, sin_result_set=False):
        self.filas, self.error, self.sin_result_set, self.sql = filas or [], error, sin_result_set, None

    def execute(self, sql):
        self.sql = sql
        if self.error:
            raise self.error

    def fetchall(self):
        if self.sin_result_set:
            raise pyodbc.ProgrammingError("No results. Previous SQL was not a query.")
        return self.filas


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def test_checkdb_base_limpia_da_ok():
    for cur in (_Cursor(filas=[]), _Cursor(sin_result_set=True)):
        r = si.ejecutar_checkdb(_Conn(cur), "ExtensaRadio", "full")
        assert r["status"] == "OK" and r["error_count"] == 0 and r["detail"] == ""
        assert r["db"] == "ExtensaRadio" and r["checked_at"]


def test_checkdb_con_errores_cuenta_por_el_resumen_y_lo_pone_primero_en_el_detalle():
    filas = [_Fila("Msg 8939, Table error: Object ID 1"), _Fila("Msg 8928, Object ID 2"), _Fila("Msg 8990 x"),
             _Fila("CHECKDB found 0 allocation errors and 3 consistency errors in database 'X'.")]
    cur = _Cursor(filas=filas)
    r = si.ejecutar_checkdb(_Conn(cur), "X", "full")
    assert r["status"] == "ERROR" and r["error_count"] == 3
    assert r["detail"].startswith("CHECKDB found 0 allocation errors and 3 consistency errors")
    assert len(r["detail"]) <= si.MAX_DETALLE
    assert "PHYSICAL_ONLY" not in cur.sql and "WITH NO_INFOMSGS, ALL_ERRORMSGS, TABLERESULTS" in cur.sql


def test_checkdb_sin_resumen_cuenta_las_filas_y_recorta_el_detalle():
    cur = _Cursor(filas=[_Fila("m" * 400) for _ in range(5)])
    r = si.ejecutar_checkdb(_Conn(cur), "X", "full")
    assert r["error_count"] == 5 and len(r["detail"]) == si.MAX_DETALLE


def test_checkdb_physical_only_y_nombre_con_corchete_escapado():
    cur = _Cursor()
    si.ejecutar_checkdb(_Conn(cur), "raro]nombre", "physical_only")
    assert "[raro]]nombre]" in cur.sql and cur.sql.rstrip().endswith("PHYSICAL_ONLY")


def test_checkdb_que_falla_queda_como_error_de_esa_base():
    r = si.ejecutar_checkdb(_Conn(_Cursor(error=pyodbc.Error("La base esta danada" * 100))), "X", "full")
    assert r["status"] == "ERROR" and r["error_count"] == 0 and 0 < len(r["detail"]) <= si.MAX_DETALLE


# ---------------------------------------------------------------------------
# Trabajador
# ---------------------------------------------------------------------------
BOOT = "2026-09-21T08:14:03"


class _ConnSrv:
    """Conexión simulada del trabajador: responde arranque/bases y registra los CHECKDB pedidos."""
    def __init__(self, bases, arranque=BOOT, checkdb=None):
        self.bases, self.arranque, self.pedidos = bases, arranque, []
        self.checkdb = checkdb or (lambda base: [])
        self.cerrada = False

    def close(self):
        self.cerrada = True


def _preparar_corrida(datos, bases, tipo="full", hid="H1"):
    si.guardar_estado(hid, {"baseline_boot": BOOT, "run": {
        "boot": BOOT, "status": "running", "check_type": tipo, "databases": bases, "sent": False, "lanzamientos": 1}})
    return {"hospital_id": hid, "sql": {"host": "h", "user": "u", "pass": "p"}}


def _parchear_srv(srv):
    def leer(conn):
        return datetime.fromisoformat(conn.arranque), datetime.fromisoformat(conn.arranque) + timedelta(hours=1), conn.bases

    def checkdb(conn, base, tipo):
        conn.pedidos.append((base, tipo))
        filas = conn.checkdb(base)
        return {"db": base, "status": "ERROR" if filas else "OK", "error_count": len(filas),
                "detail": "x" if filas else "", "duration_s": 1, "checked_at": "2026-09-21T09:00:00"}
    return mock.patch.object(si, "conectar", return_value=srv), mock.patch.object(si, "leer_servidor", side_effect=leer), \
        mock.patch.object(si, "ejecutar_checkdb", side_effect=checkdb)


def test_trabajador_chequea_todas_las_bases_y_marca_no_online(datos):
    perfil = _preparar_corrida(datos, ["A", "B", "C"], tipo="physical_only")
    srv = _ConnSrv({"A": "ONLINE", "B": "RECOVERY_PENDING", "C": "ONLINE"}, checkdb=lambda b: ["e1", "e2"] if b == "C" else [])
    p1, p2, p3 = _parchear_srv(srv)
    with p1, p2, p3:
        si.trabajador_main(perfil, log_func=lambda m: None)
    res = si.cargar_resultados("H1")
    assert res["status"] == "done" and res["boot"] == BOOT and res["pid"]
    por_base = {r["db"]: r for r in res["results"]}
    assert por_base["A"]["status"] == "OK"
    assert por_base["B"]["status"] == "NOT_ONLINE" and por_base["B"]["detail"] == "RECOVERY_PENDING"
    assert por_base["C"]["status"] == "ERROR" and por_base["C"]["error_count"] == 2
    assert srv.pedidos == [("A", "physical_only"), ("C", "physical_only")], "la NOT_ONLINE no se chequea"
    assert srv.cerrada


def test_trabajador_relanzado_retoma_sin_repetir_las_bases_hechas(datos):
    perfil = _preparar_corrida(datos, ["A", "B"])
    si.guardar_resultados("H1", {"boot": BOOT, "status": "aborted", "results": [
        {"db": "A", "status": "OK", "error_count": 0, "detail": "", "duration_s": 5, "checked_at": "2026-09-21T09:00:00"}]})
    srv = _ConnSrv({"A": "ONLINE", "B": "ONLINE"})
    p1, p2, p3 = _parchear_srv(srv)
    with p1, p2, p3:
        si.trabajador_main(perfil, log_func=lambda m: None)
    res = si.cargar_resultados("H1")
    assert srv.pedidos == [("B", "full")]
    assert [r["db"] for r in res["results"]] == ["A", "B"] and res["status"] == "done"


def test_trabajador_se_abandona_si_sql_se_reinicia_a_mitad(datos):
    perfil = _preparar_corrida(datos, ["A", "B"])
    srv = _ConnSrv({"A": "ONLINE", "B": "ONLINE"})
    p1, p2, p3 = _parchear_srv(srv)
    original = srv.checkdb

    def reinicia_tras_a(base):
        srv.arranque = "2026-09-21T11:00:00"       # SQL se reinició después de chequear A
        return original(base)
    srv.checkdb = reinicia_tras_a
    with p1, p2, p3:
        si.trabajador_main(perfil, log_func=lambda m: None)
    res = si.cargar_resultados("H1")
    assert res["status"] == "aborted" and "reinició" in res["error"]
    assert [r["db"] for r in res["results"]] == ["A"]


def test_trabajador_sin_corrida_en_curso_no_hace_nada(datos):
    si.guardar_estado("H1", {"baseline_boot": BOOT})
    with mock.patch.object(si, "conectar") as con:
        si.trabajador_main({"hospital_id": "H1", "sql": {}}, log_func=lambda m: None)
    con.assert_not_called()


def test_trabajador_error_de_conexion_queda_aborted(datos):
    perfil = _preparar_corrida(datos, ["A"])
    with mock.patch.object(si, "conectar", side_effect=pyodbc.Error("sin red")), \
         mock.patch.object(si, "INTENTOS_RECONEXION", 2), mock.patch.object(si.time, "sleep"):
        si.trabajador_main(perfil, log_func=lambda m: None)
    assert si.cargar_resultados("H1")["status"] == "aborted"


# ---------------------------------------------------------------------------
# Camino SQL directo: máquina de estados del agente
# ---------------------------------------------------------------------------
CFG_SQL = {"hospital_id": "H1", "enabled_sql": True,
           "sql": {"host": "h", "user": "u", "pass": "p", "enabled_checkdb": True, "checkdb_databases": ["A", "B"]}}


class _Servidor:
    """SQL simulado: arranque, hora del servidor y estado de las bases, editables entre ciclos."""
    def __init__(self, arranque=BOOT, minutos_desde_arranque=120, bases=None, caido=False):
        self.arranque, self.minutos, self.bases, self.caido = arranque, minutos_desde_arranque, bases or {"A": "ONLINE", "B": "ONLINE"}, caido

    def patch(self):
        def conectar(cfg, base="master", timeout=10):
            if self.caido:
                raise pyodbc.Error("SQL caído")
            return mock.Mock()

        def leer(conn):
            a = datetime.fromisoformat(self.arranque)
            return a, a + timedelta(minutes=self.minutos), dict(self.bases)
        return mock.patch.object(si, "conectar", side_effect=conectar), mock.patch.object(si, "leer_servidor", side_effect=leer)


def _ciclo(srv, lanzar=None, vivo=False):
    """Un ciclo del camino SQL contra el servidor simulado."""
    p1, p2 = srv.patch()
    with p1, p2, mock.patch.object(si, "lanzar_trabajador", lanzar or mock.Mock(return_value=4321)) as lz, \
         mock.patch.object(si, "trabajador_vivo", return_value=vivo):
        return si.recolectar_sql(CFG_SQL, log_func=lambda m: None), lz


def test_primera_vez_solo_registra_la_linea_base_y_no_chequea(datos):
    r, lz = _ciclo(_Servidor())
    assert r["status"] == "ok" and r["extra"] == {"baseline": True} and r["payload"] is None
    lz.assert_not_called()
    assert si.cargar_estado("H1")["baseline_boot"] == BOOT
    r, lz = _ciclo(_Servidor())                       # mismo arranque: nada que hacer
    assert r["status"] == "ok" and lz.call_count == 0 and "run" not in si.cargar_estado("H1")


def test_reinicio_espera_a_que_sql_se_asiente(datos):
    _ciclo(_Servidor())
    r, lz = _ciclo(_Servidor(arranque="2026-09-22T03:00:00", minutos_desde_arranque=3))
    assert r["status"] == "pending" and r["extra"]["minutes_since_boot"] == 3.0
    lz.assert_not_called()
    assert si.cargar_estado("H1")["run"]["status"] == "pending"


def test_reinicio_con_bases_ok_lanza_un_solo_trabajador(datos):
    _ciclo(_Servidor())
    nuevo = "2026-09-22T03:00:00"
    r, lz = _ciclo(_Servidor(arranque=nuevo, minutos_desde_arranque=15))
    assert r["status"] == "running" and lz.call_count == 1
    run = si.cargar_estado("H1")["run"]
    assert run["status"] == "running" and run["databases"] == ["A", "B"] and run["lanzamientos"] == 1
    # siguiente ciclo con el trabajador vivo: no se lanza otro
    r, lz = _ciclo(_Servidor(arranque=nuevo, minutos_desde_arranque=20), vivo=True)
    assert r["status"] == "running" and lz.call_count == 0


def test_espera_a_que_todas_las_bases_esten_online_hasta_el_maximo(datos):
    _ciclo(_Servidor())
    nuevo = "2026-09-22T03:00:00"
    recuperando = {"A": "ONLINE", "B": "RECOVERING"}
    r, lz = _ciclo(_Servidor(arranque=nuevo, minutos_desde_arranque=30, bases=recuperando))
    assert r["status"] == "pending" and lz.call_count == 0
    r, lz = _ciclo(_Servidor(arranque=nuevo, minutos_desde_arranque=61, bases=recuperando))
    assert r["status"] == "running" and lz.call_count == 1, "vencida la espera máxima se chequea igual (B saldrá NOT_ONLINE)"


def test_solo_se_chequean_las_bases_configuradas_que_existen(datos):
    _ciclo(_Servidor())
    r, lz = _ciclo(_Servidor(arranque="2026-09-22T03:00:00", minutos_desde_arranque=15, bases={"A": "ONLINE", "OTRA": "ONLINE"}))
    assert si.cargar_estado("H1")["run"]["databases"] == ["A"], "B no existe en el servidor; OTRA no está configurada"


def test_trabajador_terminado_devuelve_el_payload_y_commit_lo_marca_enviado(datos):
    _ciclo(_Servidor())
    nuevo = "2026-09-22T03:00:00"
    _ciclo(_Servidor(arranque=nuevo, minutos_desde_arranque=15))
    resultados = [{"db": "A", "status": "OK", "error_count": 0, "detail": "", "duration_s": 5, "checked_at": "2026-09-22T03:20:00"},
                  {"db": "B", "status": "ERROR", "error_count": 2, "detail": "x", "duration_s": 9, "checked_at": "2026-09-22T03:30:00"}]
    si.guardar_resultados("H1", {"boot": nuevo, "status": "done", "results": resultados})
    r, _ = _ciclo(_Servidor(arranque=nuevo, minutos_desde_arranque=40))
    assert r["payload"] == {"sqlserver_start_time": nuevo, "check_type": "full", "source": "sql", "databases": resultados}
    assert r["commit"] is not None
    # si el POST falla, no se llama a commit y el mismo resultado se reintenta:
    r2, _ = _ciclo(_Servidor(arranque=nuevo, minutos_desde_arranque=45))
    assert r2["payload"] == r["payload"]
    r["commit"]()
    r3, _ = _ciclo(_Servidor(arranque=nuevo, minutos_desde_arranque=50))
    assert r3["payload"] is None and r3["status"] == "ok"
    assert si.cargar_estado("H1")["run"]["sent"] is True


def test_un_reinicio_a_mitad_descarta_la_corrida_anterior(datos):
    _ciclo(_Servidor())
    _ciclo(_Servidor(arranque="2026-09-22T03:00:00", minutos_desde_arranque=15))        # corrida 1 en marcha
    r, lz = _ciclo(_Servidor(arranque="2026-09-22T09:00:00", minutos_desde_arranque=2))  # SQL se reinició de nuevo
    run = si.cargar_estado("H1")["run"]
    assert run["boot"] == "2026-09-22T09:00:00" and run["status"] == "pending" and r["status"] == "pending"


def test_trabajador_que_muere_se_relanza_y_al_agotar_intentos_se_informa_lo_hecho(datos):
    _ciclo(_Servidor())
    nuevo = "2026-09-22T03:00:00"
    srv = _Servidor(arranque=nuevo, minutos_desde_arranque=15)
    _ciclo(srv)                                                     # lanzamiento 1
    si.guardar_resultados("H1", {"boot": nuevo, "status": "aborted", "results": [
        {"db": "A", "status": "OK", "error_count": 0, "detail": "", "duration_s": 5, "checked_at": "2026-09-22T03:20:00"}]})
    for esperado in (2, 3, 4):                                       # el trabajador sigue muerto: se relanza
        r, lz = _ciclo(srv, vivo=False)
        assert lz.call_count == 1 and si.cargar_estado("H1")["run"]["lanzamientos"] == esperado
    r, lz = _ciclo(srv, vivo=False)                                  # agotados: se cierra con lo hecho
    assert lz.call_count == 0 and r["payload"] is not None
    por_base = {b["db"]: b for b in r["payload"]["databases"]}
    assert por_base["A"]["status"] == "OK" and por_base["B"]["status"] == "ERROR"
    assert "no pudo completarse" in por_base["B"]["detail"]


def test_si_no_se_puede_lanzar_el_trabajador_queda_en_error_sin_romper(datos):
    _ciclo(_Servidor())
    r, _ = _ciclo(_Servidor(arranque="2026-09-22T03:00:00", minutos_desde_arranque=15), lanzar=mock.Mock(side_effect=OSError("sin permisos")))
    assert r["status"] == "error" and "sin permisos" in r["extra"]["error"]


def test_sql_caido_no_pierde_el_estado(datos):
    _ciclo(_Servidor())
    r, _ = _ciclo(_Servidor(caido=True))
    assert r["status"] == "error", "sin corrida vigente, un SQL caído es solo un error de este ciclo"
    _ciclo(_Servidor(arranque="2026-09-22T03:00:00", minutos_desde_arranque=15))
    r, _ = _ciclo(_Servidor(caido=True))
    assert r["status"] == "running", "con una corrida vigente, se sigue informando que está en curso"
    assert si.cargar_estado("H1")["run"]["boot"] == "2026-09-22T03:00:00"


# ---------------------------------------------------------------------------
# Camino Elastic
# ---------------------------------------------------------------------------
CFG_EL = {"hospital_id": "H1", "enabled_elastic": True,
          "elastic": {"host": "es", "port": 29200, "enabled_checkdb": True}}


class _RespES:
    def __init__(self, status, hits=None):
        self.status_code, self._hits = status, hits or []

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return {"hits": {"hits": [{"_source": h} for h in self._hits]}}


def _doc(db, estado="OK", epoca=1000, errores=0, detalle="", tipo="full", arranque="2026-09-21T08:14:03"):
    return {"dbname": db, "estado": estado, "error_count": errores, "detalle": detalle, "duration_s": 7,
            "check_type": tipo, "checked_at": "2026-09-21T09:41:10", "sqlserver_start_time": arranque,
            "sqlserver_start_epoch": epoca}


def _elastic(hits, status=200):
    return mock.patch.object(si.requests, "post", return_value=_RespES(status, hits))


def test_elastic_indice_inexistente_es_empty_no_error(datos):
    with _elastic([], 404):
        assert si.recolectar_elastic(CFG_EL)["status"] == "empty"


def test_elastic_error_de_conexion_es_error(datos):
    with mock.patch.object(si.requests, "post", side_effect=requests.exceptions.ConnectionError("x")):
        assert si.recolectar_elastic(CFG_EL)["status"] == "error"


def test_elastic_toma_solo_el_ultimo_reinicio_e_ignora_el_baseline(datos):
    hits = [_doc("A", epoca=2000, errores=0), _doc("B", "ERROR", epoca=2000, errores=3, detalle="Msg 8939"),
            _doc("A", epoca=1000), {"dbname": "", "estado": "BASELINE", "sqlserver_start_epoch": 3000}]
    with _elastic(hits):
        r = si.recolectar_elastic(CFG_EL)
    assert r["status"] == "ok" and r["source"] == "elastic"
    p = r["payload"]
    assert p["source"] == "elastic" and p["check_type"] == "full" and p["sqlserver_start_time"] == "2026-09-21T08:14:03"
    assert [(b["db"], b["status"], b["error_count"]) for b in p["databases"]] == [("A", "OK", 0), ("B", "ERROR", 3)]
    assert p["databases"][1]["detail"] == "Msg 8939" and p["databases"][0]["checked_at"] == "2026-09-21T09:41:10"


def test_elastic_se_manda_una_sola_vez_por_reinicio(datos):
    hits = [_doc("A", epoca=2000)]
    with _elastic(hits):
        r = si.recolectar_elastic(CFG_EL)
        assert r["payload"] is not None
        r2 = si.recolectar_elastic(CFG_EL)                      # sin commit (POST fallido): se reintenta
        assert r2["payload"] is not None
        r["commit"]()
        assert si.recolectar_elastic(CFG_EL)["payload"] is None  # ya enviado
    with _elastic([_doc("A", epoca=5000, arranque="2026-09-25T03:00:00")] + hits):
        assert si.recolectar_elastic(CFG_EL)["payload"]["sqlserver_start_time"] == "2026-09-25T03:00:00"  # reinicio nuevo


def test_elastic_documentos_incompletos_no_rompen(datos):
    hits = [{"estado": "OK"}, {"dbname": "A", "estado": "ok", "error_count": "abc", "sqlserver_start_epoch": "2000"},
            {"dbname": "B", "sqlserver_start_epoch": None}]
    with _elastic(hits):
        r = si.recolectar_elastic(CFG_EL)
    assert [(b["db"], b["status"], b["error_count"]) for b in r["payload"]["databases"]] == [("A", "OK", 0)]


def test_elastic_usa_https_e_indice_configurables(datos):
    cfg = {"hospital_id": "H1", "elastic": {"host": "es", "port": 9200, "use_https": True, "checkdb_index": "mi_indice"}}
    with mock.patch.object(si.requests, "post", return_value=_RespES(404)) as post:
        si.recolectar_elastic(cfg)
    assert post.call_args.args[0] == "https://es:9200/mi_indice/_search"


# ---------------------------------------------------------------------------
# Integración con el ciclo del agente
# ---------------------------------------------------------------------------
def _ciclo_con_elastic(post_central):
    """ejecutar_ciclo_agente con solo el módulo de integridad (Elastic) activo."""
    cfg = {"hospital_id": "H1", "auth_token": "t", "central_url": "http://central/v1/hospital-status", **{k: CFG_EL[k] for k in ("enabled_elastic", "elastic")}}
    enviados = []

    def post(url, **kw):
        if url.endswith("/ext_checkdb/_search"):
            return _RespES(200, [_doc("A", epoca=2000), _doc("B", "ERROR", epoca=2000, errores=1)])
        enviados.append(kw["json"])
        return post_central()

    with mock.patch.object(agent_logic, "obtener_salud_red_pasiva", return_value={}), \
         mock.patch.object(agent_logic, "recolectar_logs_elastic", return_value={"events": [], "meta": {"scan_time": "x", "new_alerts": 0}}), \
         mock.patch.object(agent_logic.requests, "post", side_effect=post):
        res = agent_logic.ejecutar_ciclo_agente(cfg, log_callback=lambda m: None)
    return res, enviados


class _RespCentral:
    def __init__(self, status):
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.exceptions.HTTPError(f"{self.status_code}")
            err.response = self
            raise err


def test_ciclo_incluye_sql_integrity_y_solo_lo_marca_enviado_tras_un_post_exitoso(datos):
    res, enviados = _ciclo_con_elastic(lambda: _RespCentral(500))          # el servidor central falla
    assert res["status"] == "Error"
    rep = enviados[0]
    assert rep["collection_meta"]["sql_integrity"] == {"enabled": True, "status": "ok", "total": 2, "source": "elastic"}
    assert [b["db"] for b in rep["software_monitoring"]["sql_integrity"]["databases"]] == ["A", "B"]
    assert si.cargar_estado("H1").get("last_sent_epoch") is None, "un POST fallido no marca el resultado como enviado"

    res, enviados = _ciclo_con_elastic(lambda: _RespCentral(201))          # se reintenta y esta vez llega
    assert res["status"] == "OK" and "sql_integrity" in enviados[0]["software_monitoring"]
    assert si.cargar_estado("H1")["last_sent_epoch"] == 2000

    res, enviados = _ciclo_con_elastic(lambda: _RespCentral(201))          # el siguiente ciclo ya no lo repite
    assert "sql_integrity" not in enviados[0]["software_monitoring"]
    assert enviados[0]["collection_meta"]["sql_integrity"]["status"] == "ok"


def test_ciclo_con_el_modulo_apagado_no_manda_nada_y_no_toca_el_disco(datos):
    cfg = {"hospital_id": "H1", "auth_token": "t", "central_url": "http://central/x"}
    enviados = []
    with mock.patch.object(agent_logic, "obtener_salud_red_pasiva", return_value={}), \
         mock.patch.object(agent_logic.requests, "post", side_effect=lambda url, **kw: (enviados.append(kw["json"]), _RespCentral(201))[1]):
        agent_logic.ejecutar_ciclo_agente(cfg)
    assert enviados[0]["collection_meta"]["sql_integrity"] == {"enabled": False, "status": "disabled"}
    assert "sql_integrity" not in enviados[0]["software_monitoring"]
    assert not list(datos.glob(".sql_integrity*"))


# ---------------------------------------------------------------------------
# Tests de conexión (botones de la GUI)
# ---------------------------------------------------------------------------
class _CursorTest:
    def __init__(self, sysadmin=True, db_owner=None):
        self.sysadmin, self.db_owner, self.ultimo = sysadmin, db_owner or {}, None

    def execute(self, sql):
        self.ultimo = sql

    def fetchone(self):
        if "IS_SRVROLEMEMBER" in self.ultimo:
            return (1 if self.sysadmin else 0,)
        base = self.ultimo.split("[", 1)[1].split("]", 1)[0]
        return (1 if self.db_owner.get(base) else 0,)


def _test_sql(cur, bases=None, cfg=None):
    conn = mock.Mock()
    conn.cursor.return_value = cur
    with mock.patch.object(si, "conectar", return_value=conn), \
         mock.patch.object(si, "leer_servidor", return_value=(datetime(2026, 9, 21), datetime(2026, 9, 21), bases or {"A": "ONLINE", "B": "ONLINE"})):
        return si.test_conexion_sql(cfg or {"host": "h", "checkdb_databases": ["A", "B", "Z"]})


def test_boton_sql_sysadmin_ok_y_cuenta_las_bases_encontradas():
    r = _test_sql(_CursorTest(sysadmin=True))
    assert r["success"] and "2 de 3" in r["msg"]


def test_boton_sql_sin_permisos_lo_dice():
    r = _test_sql(_CursorTest(sysadmin=False, db_owner={"A": True}))
    assert not r["success"] and "B" in r["msg"] and "db_owner" in r["msg"]
    assert _test_sql(_CursorTest(sysadmin=False, db_owner={"A": True, "B": True}))["success"]


def test_boton_sql_ninguna_base_existe_o_falta_host():
    assert not _test_sql(_CursorTest(), bases={"OTRA": "ONLINE"})["success"]
    assert not si.test_conexion_sql({})["success"]
    with mock.patch.object(si, "conectar", side_effect=pyodbc.Error("no")):
        assert not si.test_conexion_sql({"host": "h"})["success"]


def test_boton_elastic_indice_vacio_y_con_resultados(datos):
    with _elastic([], 404):
        assert si.test_conexion_indice({"host": "es"})["success"]
    with _elastic([_doc("A", epoca=2000), _doc("B", "ERROR", epoca=2000, errores=1)]):
        r = si.test_conexion_indice({"host": "es"})
    assert r["success"] and "2 bases" in r["msg"] and "1 con problemas" in r["msg"]
    with mock.patch.object(si.requests, "post", side_effect=requests.exceptions.ConnectionError("x")):
        assert not si.test_conexion_indice({"host": "es"})["success"]
    assert not si.test_conexion_indice({})["success"]


# ---------------------------------------------------------------------------
# Detalles de infraestructura
# ---------------------------------------------------------------------------
def test_estado_se_escribe_atomico_y_un_archivo_corrupto_se_ignora(datos):
    si.guardar_estado("H/1", {"baseline_boot": BOOT})
    assert si.cargar_estado("H/1") == {"baseline_boot": BOOT}
    assert "/" not in si.ruta_estado("H/1").rsplit("state_", 1)[1]
    (datos / ".sql_integrity_state_X").write_text("{no es json", encoding="utf-8")
    assert si.cargar_estado("X") == {}


def test_comando_del_trabajador_para_desarrollo_y_para_el_exe(monkeypatch):
    cmd = si.comando_trabajador("H1")
    assert cmd[-2:] == ["--sql-integrity-worker", "H1"] and cmd[1].endswith("headless_service.py")
    monkeypatch.setattr(si.sys, "frozen", True, raising=False)
    assert si.comando_trabajador("H1") == [si.sys.executable, "--sql-integrity-worker", "H1"]
