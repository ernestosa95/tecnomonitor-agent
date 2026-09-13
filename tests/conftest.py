"""
Fixtures compartidas de la suite de tests del agente.

Este proyecto corre en Windows en producción (WMI, pywin32, servicio del
SCM), pero la lógica de negocio (config, cifrado, ciclo de recolección,
parseo SSH) no depende de nada específico de Windows — se puede probar en
cualquier plataforma stubeando únicamente los módulos que sí lo son
(wmi, pythoncom, pywin32) ANTES de que algo importe agent_logic/main_gui/
headless_service. Por eso el stubeo y el PROGRAMDATA de prueba se hacen acá
arriba de todo, a nivel de módulo: conftest.py se carga antes de recolectar
cualquier test file de esta carpeta.

Nota sobre aislamiento: agent_logic.py/security.py/main_gui.py calculan sus
rutas de estado (DATA_DIR, CONFIG_FILE, ADMIN_HASH_FILE, los *_CHECKPOINT_FILE,
etc.) como CONSTANTES DE MÓDULO, una sola vez al importar. Pisar
os.environ["PROGRAMDATA"] después de ese primer import no cambia esas
constantes ya calculadas. Por eso el aislamiento por test acá NO usa
monkeypatch.setenv, sino monkeypatch.setattr directo sobre esas constantes
de módulo — así cada test que lo necesite corre contra su propio tmp_path,
sin pisar el estado de otro test (admin.hash, checkpoints, config).
"""
import os
import sys
import types
import tempfile

# --- PROGRAMDATA de prueba (nunca tocar el ProgramData real de la máquina).
# Sirve de default para todo lo que no se aísla explícitamente por test
# (ej. secret.key: compartirlo entre tests es inofensivo, es solo la clave
# simétrica de cifrado, no tiene "estado" que un test pueda ensuciarle a otro). ---
_TEST_DATA_DIR = tempfile.mkdtemp(prefix="tecnomonitor_tests_")
os.environ["PROGRAMDATA"] = _TEST_DATA_DIR

# --- Stub de módulos Windows-only, para poder importar agent_logic.py /
# headless_service.py / main_gui.py en Linux/Mac/CI. En Windows real esto no
# hace falta (los módulos verdaderos ya están instalados), pero igual es
# inofensivo pisarlos acá: nada de esta suite ejercita WMI/SCM reales. ---
for _name in (
    "wmi", "pythoncom",
    "win32event", "win32service", "win32serviceutil", "win32api", "winerror",
    "servicemanager", "win32timezone", "win32com", "win32com.client", "pywintypes",
):
    if _name not in sys.modules:
        sys.modules[_name] = types.ModuleType(_name)

sys.modules["win32event"].CreateEvent = lambda *a, **k: None
sys.modules["win32event"].INFINITE = 0xFFFFFFFF
sys.modules["win32event"].WAIT_OBJECT_0 = 0
sys.modules["win32service"].SERVICE_STOPPED = 1
sys.modules["win32serviceutil"].ServiceFramework = object
sys.modules["servicemanager"].LogMsg = lambda *a, **k: None
sys.modules["servicemanager"].EVENTLOG_ERROR_TYPE = 1
sys.modules["servicemanager"].EVENTLOG_INFORMATION_TYPE = 1
sys.modules["servicemanager"].PYS_SERVICE_STOPPED = 1
sys.modules["winerror"].ERROR_SERVICE_DOES_NOT_EXIST = 1060

# --- service_control real depende de pywin32 (SCM de Windows) — se
# reemplaza por un stub inerte para los tests de main_gui.Api, que no
# ejercitan de verdad start/stop de servicio (eso requiere Windows real). ---
if "service_control" not in sys.modules:
    _fake_sc = types.ModuleType("service_control")
    _fake_sc.iniciar        = lambda: {"success": True}
    _fake_sc.detener        = lambda: {"success": True}
    _fake_sc.esta_corriendo = lambda: False
    _fake_sc.estado_legible = lambda: "Detenido (simulado)"
    _fake_sc.esta_instalado = lambda: True
    _fake_sc.reiniciar      = lambda *a, **k: {"success": True}
    sys.modules["service_control"] = _fake_sc

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import pytest
import agent_logic
import security


@pytest.fixture()
def perfil_aislado(tmp_path, monkeypatch):
    """
    Aísla las rutas de checkpoint (SQL/Elastic) de agent_logic para que un
    test no vea el checkpoint que dejó otro. No aísla secret.key (compartirla
    es inofensivo) ni ADMIN_HASH_FILE (ver fixture `sesion_gui_aislada` para
    lo que sí necesita eso).
    """
    monkeypatch.setattr(agent_logic, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(agent_logic, "SQL_CHECKPOINT_FILE", str(tmp_path / ".sql_checkpoint"))
    monkeypatch.setattr(agent_logic, "ELASTIC_CHECKPOINT_FILE", str(tmp_path / ".elastic_checkpoint"))
    monkeypatch.setattr(agent_logic, "SCHEMA_VERSION_OVERRIDE_FILE", str(tmp_path / "schema_version_override.txt"))
    return tmp_path


@pytest.fixture()
def sesion_gui_aislada(tmp_path, monkeypatch):
    """
    Aísla admin.hash (para poder probar "primera vez" de forma determinística,
    sin depender de qué otro test corrió antes en el mismo proceso) y
    monitor_config.json, para los tests de main_gui.Api.
    """
    monkeypatch.setattr(security, "ADMIN_HASH_FILE", str(tmp_path / "admin.hash"))
    config_file = tmp_path / "monitor_config.json"

    import main_gui
    monkeypatch.setattr(main_gui, "CONFIG_FILE", str(config_file))
    monkeypatch.setattr(main_gui, "LOG_FILE", str(tmp_path / "activity.log"))

    return types.SimpleNamespace(tmp_path=tmp_path, config_file=str(config_file), main_gui=main_gui)
