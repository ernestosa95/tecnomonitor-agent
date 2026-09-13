"""
Lote de robustez/seguridad: versión única, rollback de schema_version, 401
explícito, HTTPS opcional de Elastic (+ advertencia si sigue en HTTP plano),
integridad de rules.json y tope de paginación de Elastic.

Ver docs/PLAN_MEJORAS_V4.5.md (secciones 2, 3.3, 3.6, 4.2, 5.2).
"""
import hashlib
import inspect
from unittest import mock

import pytest
import requests

import agent_logic


def test_version_unica_leida_de_version_file():
    assert agent_logic.AGENT_VERSION == "4.5.0"
    assert agent_logic.SCHEMA_VERSION == "4.5", "major.minor derivado de AGENT_VERSION"


def test_schema_version_efectiva_sin_override(perfil_aislado):
    assert agent_logic._schema_version_efectiva() == "4.5"


def test_rollback_de_schema_version_via_archivo_interno(perfil_aislado):
    with open(agent_logic.SCHEMA_VERSION_OVERRIDE_FILE, "w") as f:
        f.write("4.3")

    logs = []
    assert agent_logic._schema_version_efectiva(log_func=logs.append) == "4.3"
    assert any("4.3" in l and "override" in l.lower() for l in logs), \
        "debe quedar visible en el log mientras el override esté activo, no solo la primera vez"

    import os
    os.remove(agent_logic.SCHEMA_VERSION_OVERRIDE_FILE)
    assert agent_logic._schema_version_efectiva() == "4.5"


class _RespuestaHTTPError:
    def __init__(self, status_code):
        self.status_code = status_code

    def raise_for_status(self):
        err = requests.exceptions.HTTPError(f"{self.status_code} Client Error")
        err.response = self
        raise err


def test_401_queda_distinguido_de_una_caida_de_red(perfil_aislado):
    cfg = {"hospital_id": "H1", "auth_token": "tok", "central_url": "http://127.0.0.1:9/no-existe"}
    logs = []

    with mock.patch.object(agent_logic.requests, "post", return_value=_RespuestaHTTPError(401)):
        res = agent_logic.ejecutar_ciclo_agente(cfg, log_callback=logs.append)

    assert res["http_status"] == 401
    assert "401" in res["error"] and "token" in res["error"].lower()
    assert any("401" in l for l in logs)


def test_otros_errores_http_tambien_llevan_http_status_explicito(perfil_aislado):
    cfg = {"hospital_id": "H1", "auth_token": "tok", "central_url": "http://127.0.0.1:9/no-existe"}
    with mock.patch.object(agent_logic.requests, "post", return_value=_RespuestaHTTPError(500)):
        res = agent_logic.ejecutar_ciclo_agente(cfg)
    assert res["http_status"] == 500


def test_caida_de_red_sin_servidor_no_crashea(perfil_aislado):
    cfg = {"hospital_id": "H1", "auth_token": "tok", "central_url": "http://127.0.0.1:9/no-existe"}
    with mock.patch.object(agent_logic.requests, "post",
                            side_effect=requests.exceptions.ConnectionError("nadie escucha")):
        res = agent_logic.ejecutar_ciclo_agente(cfg)
    assert res["status"] == "Error"


def test_esquema_elastic_retrocompatible_por_defecto_http():
    assert agent_logic._esquema_elastic({}) == "http"
    assert agent_logic._esquema_elastic({"use_https": False}) == "http"
    assert agent_logic._esquema_elastic({"use_https": True}) == "https"


def test_advertencia_de_http_plano_en_elastic_se_loguea_cada_ciclo(perfil_aislado):
    cfg = {
        "hospital_id": "H1", "auth_token": "tok", "central_url": "http://127.0.0.1:9/no-existe",
        "enabled_elastic": True, "elastic": {"host": "127.0.0.1", "use_https": False},
    }
    logs = []
    with mock.patch.object(agent_logic, "recolectar_logs_elastic", return_value={"events": [], "meta": {"scan_time": "x", "new_alerts": 0}}), \
         mock.patch.object(agent_logic.requests, "post",
                            side_effect=requests.exceptions.ConnectionError("nadie escucha")):
        agent_logic.ejecutar_ciclo_agente(cfg, log_callback=logs.append)

    assert any("HTTPS" in l and "texto plano" in l for l in logs)


def test_sin_advertencia_cuando_use_https_esta_activo(perfil_aislado):
    cfg = {
        "hospital_id": "H1", "auth_token": "tok", "central_url": "http://127.0.0.1:9/no-existe",
        "enabled_elastic": True, "elastic": {"host": "127.0.0.1", "use_https": True},
    }
    logs = []
    with mock.patch.object(agent_logic, "recolectar_logs_elastic", return_value={"events": [], "meta": {"scan_time": "x", "new_alerts": 0}}), \
         mock.patch.object(agent_logic.requests, "post",
                            side_effect=requests.exceptions.ConnectionError("nadie escucha")):
        agent_logic.ejecutar_ciclo_agente(cfg, log_callback=logs.append)

    assert not any("texto plano" in l for l in logs)


def test_integridad_de_rules_json(tmp_path):
    rules_file = tmp_path / "rules.json"
    rules_file.write_text('[{"id": "X", "regex": "foo", "service_target": "*"}]', encoding="utf-8")
    sha_file = tmp_path / "rules.json.sha256"

    with mock.patch.object(agent_logic, "RULES_FILE", str(rules_file)), \
         mock.patch.object(agent_logic, "RULES_FILE_SHA256", str(sha_file)):

        assert agent_logic._rules_json_integro() is True, \
            "sin archivo .sha256 se omite el chequeo (retrocompatible con builds viejos)"

        hash_real = hashlib.sha256(rules_file.read_bytes()).hexdigest()
        sha_file.write_text(hash_real, encoding="utf-8")
        assert agent_logic._rules_json_integro() is True

        sha_file.write_text("0" * 64, encoding="utf-8")
        logs = []
        assert agent_logic._rules_json_integro(logs.append) is False, "checksum incorrecto: fail-safe"
        assert any("checksum" in l for l in logs)


def test_recolectar_logs_elastic_tiene_tope_de_paginacion():
    src = inspect.getsource(agent_logic.recolectar_logs_elastic)
    assert "MAX_PAGINAS_ELASTIC" in src
    assert "MAX_SEGUNDOS_PAGINACION_ELASTIC" in src


def test_obtener_storage_fisico_v3_no_usa_10_workers_para_volumenes_y_discos():
    src = inspect.getsource(agent_logic.obtener_storage_fisico_v3)
    assert "ThreadPoolExecutor(max_workers=10)" not in src, \
        "bajado a 4 (ver docs/PLAN_MEJORAS_V4.5.md §5.2) -- los BMC Dell tienen límites bajos de sesiones concurrentes"
