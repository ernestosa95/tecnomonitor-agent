"""
Ciclo multi-hospital del servicio (headless_service.ejecutar_todos_los_perfiles):
respeta enabled=false y aísla fallas de un perfil del resto.

Ver docs/PLAN_MEJORAS_V4.5.md §9.1.
"""
from unittest import mock

import headless_service as hs


def test_enabled_false_se_saltea_y_una_falla_no_bloquea_al_resto():
    llamados = []

    def fake_ejecutar_un_ciclo(perfil, log_func, debe_continuar=lambda: True):
        llamados.append(perfil["hospital_id"])
        log_func(f"ciclo simulado para {perfil['hospital_id']}")
        if perfil["hospital_id"] == "ROTO":
            raise RuntimeError("credenciales SQL vencidas (simulado)")

    logs = []
    with mock.patch.object(hs, "log", side_effect=lambda msg: logs.append(msg)), \
         mock.patch.object(hs, "ejecutar_un_ciclo", fake_ejecutar_un_ciclo):
        cfg_raiz = {
            "interval_minutes": 5,
            "instalaciones": [
                {"hospital_id": "H1-OK", "enabled": True},
                {"hospital_id": "H2-DESACTIVADO", "enabled": False},
                {"hospital_id": "ROTO", "enabled": True},
                {"hospital_id": "H3-DESPUES-DEL-ROTO", "enabled": True},
            ],
        }
        hs.ejecutar_todos_los_perfiles(cfg_raiz)

    assert llamados == ["H1-OK", "ROTO", "H3-DESPUES-DEL-ROTO"], \
        "H2 (desactivado) se saltea; el resto se intenta en orden"
    assert any("H2-DESACTIVADO" in l and "omite" in l for l in logs)
    assert any("[ROTO]" in l and "Error de ciclo" in l for l in logs)
    assert "H3-DESPUES-DEL-ROTO" in llamados, \
        "el perfil siguiente al roto igual se ejecuta -- aislamiento de fallas"


def test_debe_continuar_corta_el_recorrido_entre_perfiles():
    llamados = []

    def fake_ejecutar_un_ciclo(perfil, log_func, debe_continuar=lambda: True):
        llamados.append(perfil["hospital_id"])

    with mock.patch.object(hs, "log", side_effect=lambda msg: None), \
         mock.patch.object(hs, "ejecutar_un_ciclo", fake_ejecutar_un_ciclo):
        cfg_raiz = {
            "instalaciones": [
                {"hospital_id": "H1", "enabled": True},
                {"hospital_id": "H2", "enabled": True},
                {"hospital_id": "H3", "enabled": True},
            ],
        }
        # Se pide detener justo después de terminar el primer perfil -- típico
        # de un SvcStop a mitad de ciclo. debe_continuar() se chequea después
        # de cada perfil, así que la primera llamada ya debe decir "parar".
        def debe_continuar():
            return False

        hs.ejecutar_todos_los_perfiles(cfg_raiz, debe_continuar=debe_continuar)

    assert llamados == ["H1"], "un pedido de detención entre perfiles no debe seguir con los siguientes"
