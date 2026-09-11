from cryptography.fernet import Fernet
import hashlib
import os
import secrets

# ---------------------------------------------------------------------------
# RUTAS A PROGRAMDATA
# ---------------------------------------------------------------------------
def get_app_data_path():
    r"""Retorna la ruta segura C:\ProgramData\TecnoMonitor"""
    path = os.path.join(os.environ.get('PROGRAMDATA', os.path.expanduser('~')), 'TecnoMonitor')
    if not os.path.exists(path):
        try:
            os.makedirs(path)
        except Exception:
            pass
    return path


DATA_DIR = get_app_data_path()
KEY_FILE = os.path.join(DATA_DIR, "secret.key")


def cargar_o_generar_clave():
    """Carga la clave Fernet o la genera y la persiste de forma atómica."""
    if not os.path.exists(KEY_FILE):
        key = Fernet.generate_key()
        tmp = KEY_FILE + ".tmp"
        with open(tmp, "wb") as f:
            f.write(key)
        os.replace(tmp, KEY_FILE)          # escritura atómica: nunca deja el archivo a medias
    else:
        with open(KEY_FILE, "rb") as f:
            key = f.read()
    return key


cipher_suite = Fernet(cargar_o_generar_clave())


def encriptar(texto: str) -> str:
    if not texto:
        return ""
    try:
        return cipher_suite.encrypt(texto.encode()).decode()
    except Exception:
        return texto


def desencriptar(texto_encriptado: str) -> str:
    if not texto_encriptado:
        return ""
    try:
        return cipher_suite.decrypt(texto_encriptado.encode()).decode()
    except Exception:
        return texto_encriptado


# ---------------------------------------------------------------------------
# CÓDIGO DE ACCESO A LA GUI — único por instalación
#
# Reemplaza al hash fijo que antes vivía hardcodeado en main_gui.py (mismo
# valor en todos los hospitales: comprometer una instalación comprometía
# todas). Se genera un código al azar la primera vez que se necesita, se
# persiste solo su hash (nunca el texto plano), y se le muestra al
# administrador una única vez para que lo guarde — mismo criterio que ya usa
# el servidor central para los tokens de hospital.
#
# Recuperación de acceso: si se pierde el código, borrar ADMIN_HASH_FILE con
# el mismo nivel de acceso local que ya permite leer secret.key + config y
# descifrar todo — no se agrega ningún backdoor nuevo, se apoya en el acceso
# que ya es equivalente a control total sobre el equipo.
# ---------------------------------------------------------------------------
ADMIN_HASH_FILE = os.path.join(DATA_DIR, "admin.hash")

_ALFABETO_CODIGO = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # sin 0/O/1/I: ambiguos al transcribir a mano


def generar_codigo_acceso(longitud: int = 10) -> str:
    """Código legible en grupos de 5 (ej. 'AB3XZ-9KLM2'), generado con `secrets`."""
    crudo = "".join(secrets.choice(_ALFABETO_CODIGO) for _ in range(longitud))
    mitad = longitud // 2
    return f"{crudo[:mitad]}-{crudo[mitad:]}"


def _hash_codigo(codigo: str) -> str:
    return hashlib.sha256(codigo.encode()).hexdigest()


def obtener_o_generar_hash_admin():
    """
    Devuelve (hash_guardado, codigo_en_texto_plano_o_None).

    El texto plano solo viene poblado la primera vez que se genera el
    archivo (o tras borrarlo para resetear el acceso) — es la única
    oportunidad de mostrárselo al administrador.
    """
    if os.path.exists(ADMIN_HASH_FILE):
        with open(ADMIN_HASH_FILE, "r", encoding="utf-8") as f:
            return f.read().strip(), None

    codigo = generar_codigo_acceso()
    hash_codigo = _hash_codigo(codigo)

    tmp = ADMIN_HASH_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(hash_codigo)
    os.replace(tmp, ADMIN_HASH_FILE)  # escritura atómica, mismo patrón que secret.key

    return hash_codigo, codigo


def verificar_codigo_acceso(codigo_ingresado: str, hash_guardado: str) -> bool:
    if not codigo_ingresado:
        return False
    return _hash_codigo(codigo_ingresado) == hash_guardado
