"""
Detección de Microsoft Edge WebView2 Runtime, que la GUI (pywebview) necesita para dibujarse.

Sin el runtime, pywebview cae al motor viejo de Internet Explorer (backend winforms/MSHTML),
que no entiende el HTML/JS de la GUI: la ventana se abre casi en blanco, sin formato, y no se
puede ni ingresar el código de acceso ni cargar el token. Pasó en Windows Server 2022 (hospital
Milstein, 2026-10-01): a diferencia de Windows 10/11, los Windows Server no traen WebView2.

La detección sigue lo que documenta Microsoft ("Detect if a WebView2 Runtime is already
installed"): el valor `pv` de la clave del cliente de EdgeUpdate, en HKLM (64 y 32 bits) o en
HKCU (instalación por usuario); vacío o "0.0.0.0" cuenta como no instalado.
El instalador (TecnoMonitor.iss, WebView2Instalado) mira las mismas claves.
"""
import sys

CLIENTE_WEBVIEW2 = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
_CLAVES = (
    ("HKEY_LOCAL_MACHINE", rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{CLIENTE_WEBVIEW2}"),
    ("HKEY_LOCAL_MACHINE", rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{CLIENTE_WEBVIEW2}"),
    ("HKEY_CURRENT_USER", rf"Software\Microsoft\EdgeUpdate\Clients\{CLIENTE_WEBVIEW2}"),
)

URL_DESCARGA = "https://developer.microsoft.com/microsoft-edge/webview2/"

MENSAJE_FALTA = (
    "Falta Microsoft Edge WebView2 Runtime, que la configuración de TecnoMonitor necesita "
    "para mostrarse (los Windows Server no lo traen instalado).\n\n"
    "El servicio del agente sigue funcionando; lo que no anda es esta pantalla.\n\n"
    "Cómo resolverlo:\n"
    "1. Descargar el \"Evergreen Standalone Installer\" x64 de WebView2 desde\n"
    f"   {URL_DESCARGA}\n"
    "   (se puede bajar en otra PC y copiarlo; no necesita internet en este equipo).\n"
    "2. Ejecutarlo como administrador.\n"
    "3. Volver a abrir TecnoMonitor.\n\n"
    "Si nunca llegó a ver el código de acceso de la configuración, borre\n"
    r"C:\ProgramData\TecnoMonitor\admin.hash antes de volver a abrir: se genera uno nuevo."
)


def version_instalada(winreg_mod=None):
    """Versión del runtime de WebView2, o None si no está instalado (o no es Windows)."""
    if winreg_mod is None:
        if sys.platform != "win32":
            return None
        import winreg as winreg_mod
    for raiz, ruta in _CLAVES:
        try:
            with winreg_mod.OpenKey(getattr(winreg_mod, raiz), ruta) as clave:
                valor, _tipo = winreg_mod.QueryValueEx(clave, "pv")
        except OSError:
            continue
        if valor and str(valor).strip() not in ("", "0.0.0.0"):
            return str(valor).strip()
    return None


def avisar_falta(log_file=None):
    """Deja constancia en el log y muestra el aviso en un cuadro de mensaje nativo de Windows."""
    if log_file:
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write("--- GUI: falta Microsoft Edge WebView2 Runtime; no se abre la configuración ---\n")
        except Exception:
            pass
    if sys.platform == "win32":
        try:
            import ctypes
            MB_ICONWARNING = 0x30
            ctypes.windll.user32.MessageBoxW(None, MENSAJE_FALTA, "TecnoMonitor: falta WebView2", MB_ICONWARNING)
        except Exception:
            pass
