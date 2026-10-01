"""Detección de WebView2 Runtime (webview2.py) con un registro de Windows simulado."""
import types

import webview2

CLIENTE = webview2.CLIENTE_WEBVIEW2
HKLM_32 = ("HKEY_LOCAL_MACHINE", rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{CLIENTE}")
HKLM_64 = ("HKEY_LOCAL_MACHINE", rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{CLIENTE}")
HKCU = ("HKEY_CURRENT_USER", rf"Software\Microsoft\EdgeUpdate\Clients\{CLIENTE}")


def _registro(valores):
    """winreg falso: `valores` = {(raíz, ruta): pv}."""
    class _Clave:
        def __init__(self, raiz, ruta):
            self.k = (raiz, ruta)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def open_key(raiz, ruta):
        if (raiz, ruta) not in valores:
            raise OSError("no existe")
        return _Clave(raiz, ruta)

    def query_value_ex(clave, nombre):
        assert nombre == "pv"
        return valores[clave.k], 1

    return types.SimpleNamespace(HKEY_LOCAL_MACHINE="HKEY_LOCAL_MACHINE", HKEY_CURRENT_USER="HKEY_CURRENT_USER",
                                 OpenKey=open_key, QueryValueEx=query_value_ex)


def test_sin_webview2():
    assert webview2.version_instalada(_registro({})) is None


def test_instalado_por_maquina_o_por_usuario():
    assert webview2.version_instalada(_registro({HKLM_32: "129.0.2792.65"})) == "129.0.2792.65"
    assert webview2.version_instalada(_registro({HKLM_64: "129.0.2792.65"})) == "129.0.2792.65"
    assert webview2.version_instalada(_registro({HKCU: "128.0.1"})) == "128.0.1"


def test_version_vacia_o_cero_no_cuenta():
    assert webview2.version_instalada(_registro({HKLM_32: "", HKLM_64: "0.0.0.0"})) is None
    assert webview2.version_instalada(_registro({HKLM_32: "0.0.0.0", HKCU: "130.0.1"})) == "130.0.1"


def test_fuera_de_windows_no_rompe(monkeypatch):
    monkeypatch.setattr(webview2.sys, "platform", "linux")
    assert webview2.version_instalada() is None


def test_aviso_queda_en_el_log(tmp_path, monkeypatch):
    monkeypatch.setattr(webview2.sys, "platform", "linux")      # sin cuadro de mensaje
    log = tmp_path / "activity.log"
    webview2.avisar_falta(str(log))
    assert "WebView2" in log.read_text(encoding="utf-8")


def test_la_gui_importa_el_chequeo():
    import main_gui
    assert main_gui.webview2 is webview2
