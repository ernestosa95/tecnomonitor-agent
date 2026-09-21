"""
KPIs de RIS/PACS/usuarios vía ElasticSearch (extraer_metricas_ris_elastic) --
lee los índices horarios que publica Logstash (ver docs/ELK_RIS_METRICS.md).

Encontrado al poner en marcha las Tareas Programadas en el primer hospital
real (2026-09-16): si un índice (ej. ext_ris_metrics_hourly) todavía no
recibió su primer documento -- una hora sin ningún admitido/ejecutado/login
es normal en un hospital de bajo volumen -- Elastic devuelve 404 en el
_search. _buscar_bucket_horario no distinguía eso de un error de conexión:
la excepción cortaba también la consulta de los otros 2 índices (aunque sí
tuvieran datos) y el checkpoint nunca avanzaba, logueando error cada ciclo.
"""
from datetime import datetime
from unittest import mock

import agent_logic


class _FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise agent_logic.requests.exceptions.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def test_buscar_bucket_horario_404_devuelve_lista_vacia_no_crashea():
    with mock.patch.object(agent_logic.requests, "post", return_value=_FakeResponse(404)):
        docs = agent_logic._buscar_bucket_horario(
            {"host": "127.0.0.1"}, "ext_ris_metrics_hourly", "hour_start",
            datetime(2026, 9, 16, 16, 0), datetime(2026, 9, 16, 17, 0),
        )
    assert docs == []


def test_buscar_bucket_horario_otros_errores_http_siguen_lanzando():
    with mock.patch.object(agent_logic.requests, "post", return_value=_FakeResponse(500)):
        try:
            agent_logic._buscar_bucket_horario(
                {"host": "127.0.0.1"}, "ext_ris_metrics_hourly", "hour_start",
                datetime(2026, 9, 16, 16, 0), datetime(2026, 9, 16, 17, 0),
            )
            assert False, "un 500 real tiene que seguir propagando la excepción"
        except agent_logic.requests.exceptions.HTTPError:
            pass


def test_extraer_metricas_ris_elastic_indice_ris_inexistente_no_bloquea_pacs():
    respuestas = {
        "ext_ris_metrics_hourly": _FakeResponse(404),
        "ext_pacs_metrics_hourly": _FakeResponse(200, {"hits": {"hits": [
            {"_source": {"aet": "PACS1", "mod": "CR", "almacenados": 5}, "sort": [1, "a"]},
        ]}}),
        "ext_users_metrics_hourly": _FakeResponse(200, {"hits": {"hits": []}}),
    }

    def fake_post(url, json=None, **kwargs):
        for index_name, resp in respuestas.items():
            if index_name in url:
                return resp
        raise AssertionError(f"URL inesperada: {url}")

    with mock.patch.object(agent_logic.requests, "post", side_effect=fake_post), \
         mock.patch.object(agent_logic, "_calcular_ventana_extraccion",
                            return_value=(datetime(2026, 9, 16, 16, 0), datetime(2026, 9, 16, 17, 0), 1)):
        resultado = agent_logic.extraer_metricas_ris_elastic({"host": "127.0.0.1"}, "H1")

    assert resultado is not None, "un índice vacío/inexistente no debería tumbar todo el bloque"
    assert resultado["application_metrics"]["ris"] == []
    assert resultado["application_metrics"]["pacs"] == [{"aet": "PACS1", "mod": "CR", "almacenados": 5}]
    assert resultado["_checkpoint_to_save"] == datetime(2026, 9, 16, 17, 0)


# ---------------------------------------------------------------------------
# user_guids llegando como string suelto en vez de lista de un elemento --
# ver docs/ELK_RIS_METRICS.md y elk/ext_users_metrics.conf. STRING_AGG en SQL
# Server no deja coma que partir cuando hubo un único usuario logueado para
# un rol en esa hora, y el pipeline de Logstash puede dejar ese único GUID
# como string en vez de array de un elemento. Encontrado en producción
# (2026-09-18): el bloque quedaba rechazado para siempre (checkpoint nunca
# avanza si la validación falla), bloqueando ris+pacs+users juntos.
# ---------------------------------------------------------------------------

def test_normalizar_user_guids_envuelve_string_suelto_en_lista():
    docs = [
        {"rol": "Tecnico", "user_guids": "54355A6C-E467-456E-A671-49ED890C3DEA"},
        {"rol": "Medico", "user_guids": ["guid-a", "guid-b"]},
        {"rol": "Admin", "user_guids": []},
        {"rol": "Sin datos"},  # sin la clave -- no debe explotar
    ]
    agent_logic._normalizar_user_guids(docs)
    assert docs[0]["user_guids"] == ["54355A6C-E467-456E-A671-49ED890C3DEA"]
    assert docs[1]["user_guids"] == ["guid-a", "guid-b"], "una lista ya válida no se toca"
    assert docs[2]["user_guids"] == [], "una lista vacía no se toca"
    assert "user_guids" not in docs[3]


def test_extraer_metricas_ris_elastic_user_guids_string_no_bloquea_el_bloque():
    respuestas = {
        "ext_ris_metrics_hourly": _FakeResponse(200, {"hits": {"hits": []}}),
        "ext_pacs_metrics_hourly": _FakeResponse(200, {"hits": {"hits": []}}),
        "ext_users_metrics_hourly": _FakeResponse(200, {"hits": {"hits": [
            {"_source": {"rol": "Tecnico", "inicios_sesion": 1,
                         "user_guids": "54355A6C-E467-456E-A671-49ED890C3DEA"},
             "sort": [1, "a"]},
        ]}}),
    }

    def fake_post(url, json=None, **kwargs):
        for index_name, resp in respuestas.items():
            if index_name in url:
                return resp
        raise AssertionError(f"URL inesperada: {url}")

    with mock.patch.object(agent_logic.requests, "post", side_effect=fake_post), \
         mock.patch.object(agent_logic, "_calcular_ventana_extraccion",
                            return_value=(datetime(2026, 9, 16, 16, 0), datetime(2026, 9, 16, 17, 0), 1)):
        resultado = agent_logic.extraer_metricas_ris_elastic({"host": "127.0.0.1"}, "H1")

    assert resultado is not None, "un user_guids string suelto no debería tumbar el bloque entero"
    assert resultado["application_metrics"]["users"] == [
        {"rol": "Tecnico", "usuarios_unicos": 1, "inicios_sesion": 1}
    ]
    assert resultado["_checkpoint_to_save"] == datetime(2026, 9, 16, 17, 0), \
        "el checkpoint tiene que poder avanzar -- antes del fix quedaba trabado en este mismo bloque"
