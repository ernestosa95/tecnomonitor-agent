"""
Migración de monitor_config.json plano (pre-v4.6) a instalaciones[], cifrado
de credenciales, y checkpoints por hospital_id.

Ver docs/PLAN_MEJORAS_V4.5.md §9.1.1.
"""
import copy
import json
from datetime import datetime

import agent_logic
import security


def test_migrar_config_legacy_envuelve_el_dict_plano_como_unico_perfil():
    config_plana = {
        "hospital_id": "H42",
        "auth_token": "token-cifrado",
        "interval_minutes": 7,
    }

    migrada = agent_logic.migrar_config_legacy(config_plana)

    assert "instalaciones" in migrada
    assert len(migrada["instalaciones"]) == 1
    assert migrada["instalaciones"][0]["hospital_id"] == "H42"
    assert migrada["instalaciones"][0]["enabled"] is True
    assert migrada["config_version"] == 2


def test_migrar_config_legacy_sube_interval_minutes_a_la_raiz():
    migrada = agent_logic.migrar_config_legacy({"hospital_id": "H1", "interval_minutes": 7})

    assert migrada["interval_minutes"] == 7
    assert "interval_minutes" not in migrada["instalaciones"][0], \
        "interval_minutes es cadencia del agente (global), no de un hospital en particular"


def test_migrar_config_legacy_sube_central_url_a_la_raiz():
    migrada = agent_logic.migrar_config_legacy({"hospital_id": "H1", "central_url": "https://viejo/x"})

    assert migrada["central_url"] == "https://viejo/x"
    assert "central_url" not in migrada["instalaciones"][0], \
        "central_url es del agente (un solo servidor central), no de un hospital en particular"


def test_migrar_config_legacy_usa_default_si_no_habia_central_url():
    migrada = agent_logic.migrar_config_legacy({"hospital_id": "H1"})
    assert migrada["central_url"] == agent_logic.DEFAULT_CENTRAL_URL


def test_migrar_config_legacy_es_no_op_si_ya_esta_migrado():
    ya_migrado = {"instalaciones": [{"hospital_id": "H1"}], "config_version": 2}
    assert agent_logic.migrar_config_legacy(ya_migrado) is ya_migrado


def test_migrar_y_persistir_deja_el_archivo_migrado_en_disco(tmp_path):
    config_file = str(tmp_path / "monitor_config.json")
    with open(config_file, "w", encoding="utf-8") as f:
        json.dump({"hospital_id": "H42", "interval_minutes": 5}, f)

    with open(config_file, "r", encoding="utf-8") as f:
        data_disco = json.load(f)
    migrada = agent_logic.migrar_y_persistir_si_hace_falta(data_disco, config_file)

    assert "instalaciones" in migrada
    with open(config_file, "r", encoding="utf-8") as f:
        en_disco = json.load(f)
    assert "instalaciones" in en_disco, "self-healing: el archivo en disco queda migrado, no solo el dict en memoria"


def test_migrar_y_persistir_segunda_carga_es_no_op(tmp_path):
    config_file = str(tmp_path / "monitor_config.json")
    with open(config_file, "w", encoding="utf-8") as f:
        json.dump({"hospital_id": "H42"}, f)

    with open(config_file, "r", encoding="utf-8") as f:
        primera = agent_logic.migrar_y_persistir_si_hace_falta(json.load(f), config_file)

    segunda = agent_logic.migrar_y_persistir_si_hace_falta(copy.deepcopy(primera), config_file)
    assert segunda == primera


def test_desencriptar_y_encriptar_config_hacen_roundtrip():
    perfil_plano = {
        "hospital_id": "H42",
        "auth_token": security.encriptar("token-secreto-h42"),
        "idrac": {"ip": "192.168.1.50", "user": "root", "pass": security.encriptar("calvin")},
        "sql": {"host": "SRVDB", "pass": security.encriptar("saPass123")},
    }
    data = {"instalaciones": [perfil_plano]}

    desencriptada = agent_logic.desencriptar_config(copy.deepcopy(data))
    perfil = desencriptada["instalaciones"][0]
    assert perfil["auth_token"] == "token-secreto-h42"
    assert perfil["idrac"]["pass"] == "calvin"
    assert perfil["sql"]["pass"] == "saPass123"

    reencriptada = agent_logic.encriptar_config(copy.deepcopy(desencriptada))
    assert reencriptada["instalaciones"][0]["auth_token"] != "token-secreto-h42", \
        "encriptar_config no debe dejar credenciales en texto plano en lo que se persiste"

    de_nuevo = agent_logic.desencriptar_config(copy.deepcopy(reencriptada))
    assert de_nuevo["instalaciones"][0]["auth_token"] == "token-secreto-h42"


def test_desencriptar_config_migra_automaticamente_si_hace_falta():
    plana = {"hospital_id": "H1", "auth_token": security.encriptar("tok")}
    desencriptada = agent_logic.desencriptar_config(plana)
    assert desencriptada["instalaciones"][0]["auth_token"] == "tok"


def test_encriptar_config_sincroniza_central_url_a_cada_perfil():
    data = {
        "central_url": "https://central-unico/v1/hospital-status",
        "instalaciones": [{"hospital_id": "H1"}, {"hospital_id": "H2", "central_url": "https://viejo-y-distinto"}],
    }
    reencriptada = agent_logic.encriptar_config(copy.deepcopy(data))

    assert reencriptada["instalaciones"][0]["central_url"] == "https://central-unico/v1/hospital-status"
    assert reencriptada["instalaciones"][1]["central_url"] == "https://central-unico/v1/hospital-status", \
        "central_url es global: al guardar, pisa cualquier valor distinto que hubiera quedado por perfil"


def test_encriptar_config_usa_default_si_la_raiz_no_tiene_central_url():
    data = {"instalaciones": [{"hospital_id": "H1"}]}
    reencriptada = agent_logic.encriptar_config(copy.deepcopy(data))
    assert reencriptada["central_url"] == agent_logic.DEFAULT_CENTRAL_URL
    assert reencriptada["instalaciones"][0]["central_url"] == agent_logic.DEFAULT_CENTRAL_URL


def test_desencriptar_config_toma_central_url_de_un_perfil_viejo_si_falta_en_la_raiz():
    data = {"instalaciones": [{"hospital_id": "H1", "central_url": "https://de-un-perfil-de-antes"}]}
    desencriptada = agent_logic.desencriptar_config(data)
    assert desencriptada["central_url"] == "https://de-un-perfil-de-antes"


# --- Checkpoints por hospital_id ---

def test_checkpoint_sql_guarda_y_lee_por_hospital(perfil_aislado):
    dt = datetime(2026, 9, 12, 10, 30, 0)
    agent_logic.save_checkpoint("H42", dt)

    assert agent_logic.get_last_checkpoint("H42") == dt
    assert agent_logic.get_last_checkpoint("OTRO-HOSPITAL") is None, \
        "el checkpoint de un hospital no debe filtrar a otro"


def test_reset_checkpoint_borra_solo_el_del_hospital_pedido(perfil_aislado):
    agent_logic.save_checkpoint("H1", datetime(2026, 1, 1))
    agent_logic.save_checkpoint("H2", datetime(2026, 1, 1))

    agent_logic.reset_checkpoint("H1")

    assert agent_logic.get_last_checkpoint("H1") is None
    assert agent_logic.get_last_checkpoint("H2") is not None


def test_checkpoint_legacy_sin_sufijo_se_migra_por_rename(perfil_aislado):
    import os
    with open(agent_logic.SQL_CHECKPOINT_FILE, "w") as f:
        f.write("2026-09-01 00:00:00")

    leido = agent_logic.get_last_checkpoint("HOSPITAL-QUE-HEREDA-EL-VIEJO-ARCHIVO")

    assert leido is not None
    assert not os.path.exists(agent_logic.SQL_CHECKPOINT_FILE), \
        "el archivo viejo sin sufijo se renombra (no se duplica) al primer perfil que lo reclama"
