import requests
import wmi
import pythoncom
import paramiko
import hashlib
import time
import socket
import urllib3
import json
import pyodbc
import os
import threading
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, wait as futures_wait, FIRST_COMPLETED
from requests.auth import HTTPBasicAuth
import security
import psutil
import mirth_collector
import ssl
from urllib.parse import urlparse
from cryptography import x509
from cryptography.hazmat.backends import default_backend
import re

# ---------------------------------------------------------------------------
# RUTAS Y CONSTANTES
# ---------------------------------------------------------------------------
DATA_DIR = security.get_app_data_path()
SQL_CHECKPOINT_FILE = os.path.join(DATA_DIR, ".sql_checkpoint")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- NUEVAS RUTAS ELASTIC ---
ELASTIC_CHECKPOINT_FILE = os.path.join(DATA_DIR, ".elastic_checkpoint")
UNKNOWNS_LAB_FILE = os.path.join(DATA_DIR, "unknowns_lab.json")
# Asumimos que distribuirás el 'rules.json' junto al ejecutable o en el DATA_DIR
RULES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules.json")
# Checksum opcional de rules.json (ver docs/SEGURIDAD.md §3.6) — si build.bat
# lo generó y lo empaquetó junto al .exe, se valida al cargar las reglas.
RULES_FILE_SHA256 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules.json.sha256")


def _rules_json_integro(log_func=None):
    """
    Verificación opcional de integridad de rules.json (ver
    docs/PLAN_MEJORAS_V4.5.md §3.6). Si no existe rules.json.sha256 (builds
    de antes de este cambio, o quien corre desde código fuente sin
    regenerarlo), se omite el chequeo y se sigue como siempre — no rompe
    nada retroactivamente. Si existe y no coincide, se tratan las reglas
    como no confiables: se omiten este ciclo (fail-safe) en vez de usarlas
    a ciegas.
    """
    if not os.path.exists(RULES_FILE_SHA256):
        return True
    try:
        with open(RULES_FILE, 'rb') as f:
            hash_real = hashlib.sha256(f.read()).hexdigest()
        with open(RULES_FILE_SHA256, 'r', encoding='utf-8') as f:
            # Tolera tanto "hash" solo como el formato "hash  nombre_archivo"
            # que generan sha256sum/certutil.
            hash_esperado = f.read().strip().split()[0].lower()
        if hash_real.lower() != hash_esperado:
            if log_func:
                log_func("🚨 rules.json no coincide con su checksum esperado (rules.json.sha256) "
                          "— se omiten las reglas este ciclo, posible modificación no autorizada del archivo.")
            return False
    except Exception as e:
        if log_func:
            log_func(f"⚠️ No se pudo verificar la integridad de rules.json: {e}")
    return True

# ---------------------------------------------------------------------------
# VERSIÓN — fuente única de verdad en /VERSION (ver docs/PLAN_MEJORAS_V4.5.md
# §6). agent_version es el contenido tal cual (ej. "4.5.0"); schema_version
# es major.minor (ej. "4.5") — mismo criterio que ya se usaba a mano antes de
# esto. build.bat empaqueta VERSION junto al ejecutable (--add-data), igual
# que rules.json; si por algún motivo no está (build roto, ejecución desde
# código fuente sin el archivo), cae a un default hardcodeado en vez de
# romper el arranque del agente.
# ---------------------------------------------------------------------------
def _leer_version():
    ruta = os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION")
    try:
        with open(ruta, "r", encoding="utf-8") as f:
            valor = f.read().strip()
            if valor:
                return valor
    except Exception:
        pass
    return "4.5.0"


AGENT_VERSION  = _leer_version()
SCHEMA_VERSION = ".".join(AGENT_VERSION.split(".")[:2]) if AGENT_VERSION else "4.5"

# Rollback de emergencia (ver docs/PLAN_MEJORAS_V4.5.md §2, riesgo 5): un
# archivo interno, NO expuesto en la GUI, que si existe fuerza el
# schema_version a enviar. Pensado para revertir un hospital a "4.3" (formato
# sin exigencia de token) sin recompilar, si algo sale mal desplegando la
# autenticación obligatoria — se crea/borra a mano en
# ProgramData\TecnoMonitor\schema_version_override.txt.
SCHEMA_VERSION_OVERRIDE_FILE = os.path.join(DATA_DIR, "schema_version_override.txt")


def _schema_version_efectiva(log_func=None):
    if os.path.exists(SCHEMA_VERSION_OVERRIDE_FILE):
        try:
            with open(SCHEMA_VERSION_OVERRIDE_FILE, "r", encoding="utf-8") as f:
                valor = f.read().strip()
            if valor:
                # Se loguea en cada ciclo (no solo al detectarlo la primera
                # vez) para que un override de emergencia que alguien se
                # olvidó de borrar quede visible en activity.log de forma
                # persistente, no como una línea suelta fácil de perder.
                if log_func:
                    log_func(f"⚠️ schema_version_override.txt presente: enviando \"{valor}\" en vez de "
                              f"\"{SCHEMA_VERSION}\" — borrar ese archivo para volver al comportamiento normal.")
                return valor
        except Exception:
            pass
    return SCHEMA_VERSION

# ---------------------------------------------------------------------------
# QUERY SQL (sin cambios de lógica, solo se mantiene)
# ---------------------------------------------------------------------------
SQL_QUERY = """
DECLARE @StartDate DATETIME = '{start_date_sql}';
DECLARE @EndDate DATETIME = '{end_date_sql}';

SELECT 
    (
        SELECT 
            ISNULL(e.[Description], 'Unknown') AS equipo, 
            ISNULL(e.[AETitle], '') AS aet, 
            ISNULL(e.[DICOMModalityCode], '') AS mod,
            SUM(CASE WHEN t.[CreatedOn] >= @StartDate AND t.[CreatedOn] < @EndDate THEN 1 ELSE 0 END) AS totales,
            SUM(CASE WHEN t.[PlanningDate] >= @StartDate AND t.[PlanningDate] < @EndDate AND t.[IsPlanned] = 1 THEN 1 ELSE 0 END) AS citados,
            SUM(CASE WHEN t.[AdmissionDate] >= @StartDate AND t.[AdmissionDate] < @EndDate AND t.[IsAdmitted] = 1 THEN 1 ELSE 0 END) AS admitidos,
            SUM(CASE WHEN t.[ExecutionDate] >= @StartDate AND t.[ExecutionDate] < @EndDate AND t.[IsExecuted] = 1 THEN 1 ELSE 0 END) AS ejecutados,
            SUM(
                CASE 
                    WHEN t.[ExecutionDate] >= @StartDate AND t.[ExecutionDate] < @EndDate AND t.[ImageAvailability] = 1 THEN 1
                    WHEN t.[ExecutionDate] IS NULL AND t.[AdmissionDate] >= @StartDate AND t.[AdmissionDate] < @EndDate AND t.[ImageAvailability] = 1 THEN 1
                    ELSE 0 
                END
            ) AS con_imagen,
            SUM(CASE WHEN t.[ReportDate] >= @StartDate AND t.[ReportDate] < @EndDate AND t.[IsReported] = 1 THEN 1 ELSE 0 END) AS borradores,
            SUM(CASE WHEN t.[ApprovalDate] >= @StartDate AND t.[ApprovalDate] < @EndDate AND t.[IsApproved] = 1 THEN 1 ELSE 0 END) AS definitivos,
            SUM(
                CASE 
                    WHEN COALESCE(t.[ExecutionDate], t.[AdmissionDate], t.[CreatedOn]) >= @StartDate 
                     AND COALESCE(t.[ExecutionDate], t.[AdmissionDate], t.[CreatedOn]) < @EndDate 
                     AND t.[IsSuspended] = 1 THEN 1 
                    ELSE 0 
                END
            ) AS suspendidos
        FROM [ExtensaRadio].[ExtRadio].[tbExamination] t WITH(NOLOCK)
        INNER JOIN [ExtensaRadio].[ExtRadio].[lsEquipment] e WITH(NOLOCK) ON t.[IdEquipment] = e.[Guid]
        WHERE 
            (t.[CreatedOn] >= @StartDate AND t.[CreatedOn] < @EndDate)
            OR (t.[PlanningDate] >= @StartDate AND t.[PlanningDate] < @EndDate)
            OR (t.[AdmissionDate] >= @StartDate AND t.[AdmissionDate] < @EndDate)
            OR (t.[ExecutionDate] >= @StartDate AND t.[ExecutionDate] < @EndDate)
            OR (t.[ReportDate] >= @StartDate AND t.[ReportDate] < @EndDate)
            OR (t.[ApprovalDate] >= @StartDate AND t.[ApprovalDate] < @EndDate)
            OR (t.[ModifiedOn] >= @StartDate AND t.[ModifiedOn] < @EndDate)
        GROUP BY e.[Description], e.[AETitle], e.[DICOMModalityCode]
        ORDER BY e.[Description]
        FOR JSON PATH
    ) AS [application_metrics.ris],
    (
        SELECT 
            ISNULL([AET], '') AS aet, 
            ISNULL([MOD_IN_STUDY], '') AS mod, 
            COUNT(DISTINCT [STUDY_KEY]) AS almacenados
        FROM [ExtensaPACS].[ExtPacs].[DICOMSTUDIES] WITH(NOLOCK)
        WHERE [LASTUPDATE_DT] >= @StartDate AND [LASTUPDATE_DT] < @EndDate AND ([DELETED] IS NULL OR [DELETED] = '0')
        GROUP BY [AET], [MOD_IN_STUDY]
        ORDER BY almacenados DESC, [AET]
        FOR JSON PATH
    ) AS [application_metrics.pacs],
    (
        SELECT 
            ISNULL(r.[Description], 'Unknown') AS rol, 
            COUNT(DISTINCT a.[User_GUID]) AS usuarios_unicos, 
            COUNT(a.[GUID]) AS inicios_sesion
        FROM [SL_UserAndConfig].[ExtConfig].[UserAuditHistory] a WITH(NOLOCK)
        INNER JOIN [ExtensaRadio].[ExtRadio].[tbUser] u WITH(NOLOCK) ON a.[User_GUID] = u.[Guid]
        INNER JOIN [ExtensaRadio].[ExtRadio].[lsRole] r WITH(NOLOCK) ON u.[IdRole] = r.[Guid]
        WHERE a.[CreatedOn] >= @StartDate AND a.[CreatedOn] < @EndDate AND a.[AuditText] = 'User Logon OK'
        GROUP BY r.[Description]
        ORDER BY usuarios_unicos DESC
        FOR JSON PATH
    ) AS [application_metrics.users]
FOR JSON PATH, WITHOUT_ARRAY_WRAPPER;
"""

# ---------------------------------------------------------------------------
# HELPERS GENERALES
# ---------------------------------------------------------------------------
def safe_int(value):
    try:
        return int(value) if value is not None else 0
    except Exception:
        return 0

def safe_float(value, decimals=2):
    try:
        return round(float(value), decimals) if value is not None else 0.0
    except Exception:
        return 0.0

def verificar_puerto(ip, puerto, timeout=2):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((ip, int(puerto)))
        sock.close()
        return result == 0
    except Exception:
        return False

def _esquema_elastic(cfg):
    """
    HTTPS opcional para ElasticSearch (ver docs/PLAN_MEJORAS_V4.5.md §3.3):
    por defecto sigue siendo http:// (mismo comportamiento de siempre, cero
    migración para configs existentes) — si el hospital marca "Usar HTTPS"
    en la tarjeta de Elastic, se arma la URL con https:// en su lugar. Se usa
    `verify=False` en los requests igual que con https activado, porque el
    caso típico es un certificado autofirmado del propio clúster interno del
    hospital, no uno emitido por una CA pública (mismo criterio ya aceptado
    para iDRAC y el servidor central — ver SEGURIDAD.md, pendiente §3.4).
    """
    return "https" if cfg.get("use_https") else "http"


def parse_wmi_date(wmi_date):
    try:
        return datetime.strptime(wmi_date.split('.')[0], "%Y%m%d%H%M%S")
    except Exception:
        return datetime.now()

def _dicom_routing_habilitado(config):
    """
    Autoenrute DICOM tiene dos caminos independientes, igual que los KPIs de
    negocio (extraer_metricas_sql / extraer_metricas_ris_elastic): directo a
    SQL Server (sql.enabled_dicom_routing, restaurado en v4.6 — hasta v4.3 el
    colector ya leía SQL directo, entre v4.4 y v4.5 solo existía vía
    ElasticSearch) o vía ElasticSearch (elastic.enabled_dicom_routing). Si
    ambos están activos, gana Elastic (mismo criterio de prioridad que
    RIS/PACS/usuarios) — ver docs/PLAN_MEJORAS_V4.5.md.

    Devuelve True si cualquiera de los dos está activo y con su host
    configurado (usado solo para `collection_meta`; qué camino se usa
    realmente se decide en `ejecutar_ciclo_agente`).
    """
    elastic_cfg = config.get("elastic") or {}
    sql_cfg     = config.get("sql") or {}
    por_elastic = bool(config.get("enabled_elastic") and elastic_cfg.get("enabled_dicom_routing") and elastic_cfg.get("host"))
    por_sql     = bool(config.get("enabled_sql") and sql_cfg.get("enabled_dicom_routing") and sql_cfg.get("host"))
    return por_elastic or por_sql


def _logs_suitestensa_habilitado(config):
    """
    Logs de Suitestensa vía Elastic (v4.6): sub-toggle independiente de la
    tarjeta Elastic en general (`elastic.enabled_logs`), igual patrón que el
    autoenrute DICOM y los KPIs de RIS — antes, tener la tarjeta Elastic
    activa significaba automáticamente que los logs se leían, sin forma de
    desactivarlos por separado.

    Retrocompatible: configs guardadas antes de este campo no lo tienen, y
    los logs ya estaban activos siempre que la tarjeta Elastic lo estaba —
    "ausente o true" = activo, solo un `false` explícito lo desactiva.
    """
    elastic_cfg = config.get("elastic") or {}
    return bool(config.get("enabled_elastic") and elastic_cfg.get("host") and elastic_cfg.get("enabled_logs", True))

# ---------------------------------------------------------------------------
# CHECKPOINT SQL — escritura atómica, guardado solo al confirmar envío exitoso
#
# v4.6: un archivo por hospital_id (soporte multi-perfil, ver
# docs/PLAN_MEJORAS_V4.5.md §9.1). Si el archivo con sufijo todavía no existe
# pero sí el archivo viejo sin sufijo (instalación pre-multi-perfil recién
# migrada), se renombra una sola vez en vez de perder el checkpoint y forzar
# una re-extracción completa del historial.
# ---------------------------------------------------------------------------
def _sufijo_seguro(hospital_id):
    return re.sub(r'[^A-Za-z0-9_.-]', '_', str(hospital_id or "default"))


def _ruta_checkpoint_sql(hospital_id):
    path = os.path.join(DATA_DIR, f".sql_checkpoint_{_sufijo_seguro(hospital_id)}")
    if not os.path.exists(path) and os.path.exists(SQL_CHECKPOINT_FILE):
        try:
            os.replace(SQL_CHECKPOINT_FILE, path)
        except Exception:
            pass
    return path


def get_last_checkpoint(hospital_id, log_func=None):
    path = _ruta_checkpoint_sql(hospital_id)
    if os.path.exists(path):
        try:
            with open(path, 'r') as f:
                return datetime.strptime(f.read().strip(), "%Y-%m-%d %H:%M:%S")
        except Exception as e:
            if log_func:
                log_func(f"⚠️ Checkpoint SQL de [{hospital_id}] ilegible, se ignora: {e}")
    return None

def save_checkpoint(hospital_id, dt, log_func=None):
    """Escritura atómica: escribe en .tmp y renombra. Nunca deja el archivo a medias."""
    path = _ruta_checkpoint_sql(hospital_id)
    try:
        tmp = path + ".tmp"
        with open(tmp, 'w') as f:
            f.write(dt.strftime("%Y-%m-%d %H:%M:%S"))
        os.replace(tmp, path)
    except Exception as e:
        if log_func:
            log_func(f"⚠️ No se pudo guardar el checkpoint SQL de [{hospital_id}]: {e}")

def reset_checkpoint(hospital_id, log_func=None):
    path = _ruta_checkpoint_sql(hospital_id)
    for p in [path, path + ".tmp"]:
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception as e:
                if log_func:
                    log_func(f"⚠️ No se pudo borrar {p}: {e}")


def _ruta_checkpoint_elastic(hospital_id):
    path = os.path.join(DATA_DIR, f".elastic_checkpoint_{_sufijo_seguro(hospital_id)}")
    if not os.path.exists(path) and os.path.exists(ELASTIC_CHECKPOINT_FILE):
        try:
            os.replace(ELASTIC_CHECKPOINT_FILE, path)
        except Exception:
            pass
    return path


# ---------------------------------------------------------------------------
# CONFIGURACIÓN MULTI-PERFIL (v4.6) — ver docs/PLAN_MEJORAS_V4.5.md §9.1/§9.1.1
#
# monitor_config.json pasa de un objeto plano (un agente = un hospital) a
# {"instalaciones": [...], "config_version": 2}, donde cada elemento del
# array es un perfil completo tratado como si fuera un hospital
# independiente (config de conexión propia, hospital_id y auth_token
# propios). Como ya hay hospitales reales en producción con el formato
# viejo, la migración es transparente y autocurativa: se detecta al cargar,
# se aplica en memoria y se persiste a disco una sola vez.
#
# Estas funciones son el único lugar donde vive la lista de campos
# cifrados — antes estaba duplicada en main_gui.py (_desencriptar_config /
# guardar_config) y en headless_service.py (cargar_config_segura).
# ---------------------------------------------------------------------------

# La URL del servidor central es siempre la misma para todos los hospitales
# de un mismo agente (no es una config por hospital) — ver
# docs/PLAN_MEJORAS_V4.5.md §9.1. Vive en la raíz de monitor_config.json,
# junto a interval_minutes, y se sincroniza a cada perfil recién al
# encriptar/persistir para no tener que tocar ejecutar_ciclo_agente (que
# sigue leyendo config.get("central_url") del perfil).
DEFAULT_CENTRAL_URL = "https://tecnomonitor.tecnoimagen.com.ar/v1/hospital-status"


def migrar_config_legacy(data: dict) -> dict:
    """Envuelve un monitor_config.json en formato plano (pre-v4.6) como el
    único perfil de `instalaciones[]`. Si ya está migrado, no hace nada.

    `interval_minutes` y `central_url` suben a la raíz: con múltiples
    perfiles la cadencia es del agente (un único ciclo de servicio) y el
    servidor central es uno solo, no una config por hospital (ver
    docs/PLAN_MEJORAS_V4.5.md §9.1, decisiones de cadencia global y URL
    central única)."""
    if "instalaciones" in data:
        return data
    perfil = dict(data)
    perfil["enabled"] = True
    interval_minutes = perfil.pop("interval_minutes", 5)
    central_url = perfil.pop("central_url", None)
    return {
        "instalaciones": [perfil],
        "config_version": 2,
        "interval_minutes": interval_minutes,
        "central_url": central_url or DEFAULT_CENTRAL_URL,
    }


def migrar_y_persistir_si_hace_falta(data: dict, config_file_path: str) -> dict:
    """Como migrar_config_legacy, pero además persiste el resultado a disco
    (escritura atómica .tmp + os.replace, mismo patrón que guardar_config) si
    hubo migración — así el archivo queda en formato nuevo desde el primer
    ciclo tras actualizar, sin depender de que alguien abra la GUI. Se llama
    ANTES de desencriptar: la migración es solo estructural (mueve el dict
    plano a instalaciones[0]) y no toca los valores cifrados, así que lo que
    se persiste sigue teniendo las credenciales cifradas como siempre."""
    if "instalaciones" in data:
        return data
    data = migrar_config_legacy(data)
    try:
        tmp = config_file_path + ".tmp"
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        os.replace(tmp, config_file_path)
    except Exception:
        pass  # si no se pudo persistir, se reintenta la migración en la próxima carga
    return data


def _desencriptar_perfil(perfil: dict) -> dict:
    if perfil.get("auth_token"):
        perfil["auth_token"] = security.desencriptar(perfil["auth_token"])

    if isinstance(perfil.get("proxmox"), dict) and perfil["proxmox"].get("pass"):
        perfil["proxmox"]["pass"] = security.desencriptar(perfil["proxmox"]["pass"])

    if isinstance(perfil.get("idrac"), dict) and perfil["idrac"].get("pass"):
        perfil["idrac"]["pass"] = security.desencriptar(perfil["idrac"]["pass"])

    if isinstance(perfil.get("sql"), dict) and perfil["sql"].get("pass"):
        perfil["sql"]["pass"] = security.desencriptar(perfil["sql"]["pass"])

    if isinstance(perfil.get("vms"), list):
        for vm in perfil["vms"]:
            if isinstance(vm, dict) and vm.get("pass"):
                vm["pass"] = security.desencriptar(vm["pass"])

    if isinstance(perfil.get("mirth_servers"), list):
        for m in perfil["mirth_servers"]:
            if isinstance(m, dict) and m.get("pass"):
                m["pass"] = security.desencriptar(m["pass"])

    if isinstance(perfil.get("elastic"), dict) and perfil["elastic"].get("pass"):
        perfil["elastic"]["pass"] = security.desencriptar(perfil["elastic"]["pass"])

    return perfil


def _encriptar_perfil(perfil: dict) -> dict:
    if perfil.get("auth_token"):
        perfil["auth_token"] = security.encriptar(perfil["auth_token"])

    if isinstance(perfil.get("proxmox"), dict) and perfil["proxmox"].get("pass"):
        perfil["proxmox"]["pass"] = security.encriptar(perfil["proxmox"]["pass"])

    if isinstance(perfil.get("idrac"), dict) and perfil["idrac"].get("pass"):
        perfil["idrac"]["pass"] = security.encriptar(perfil["idrac"]["pass"])

    if isinstance(perfil.get("sql"), dict) and perfil["sql"].get("pass"):
        perfil["sql"]["pass"] = security.encriptar(perfil["sql"]["pass"])

    if isinstance(perfil.get("vms"), list):
        for vm in perfil["vms"]:
            if isinstance(vm, dict) and vm.get("pass"):
                vm["pass"] = security.encriptar(vm["pass"])

    if isinstance(perfil.get("mirth_servers"), list):
        for m in perfil["mirth_servers"]:
            if isinstance(m, dict) and m.get("pass"):
                m["pass"] = security.encriptar(m["pass"])

    if isinstance(perfil.get("elastic"), dict) and perfil["elastic"].get("pass"):
        perfil["elastic"]["pass"] = security.encriptar(perfil["elastic"]["pass"])

    return perfil


def desencriptar_config(data: dict) -> dict:
    """Migra si hace falta y desencripta las credenciales de cada perfil.
    Punto de entrada único usado por la GUI y por el servicio al cargar
    monitor_config.json."""
    data = migrar_config_legacy(data)
    if not data.get("central_url"):
        # Config ya en formato nuevo pero de antes de que central_url subiera
        # a la raíz: se toma la de cualquier perfil que la tuviera, si no la
        # default.
        de_perfil = next((p.get("central_url") for p in data.get("instalaciones", []) if p.get("central_url")), None)
        data["central_url"] = de_perfil or DEFAULT_CENTRAL_URL
    data["instalaciones"] = [_desencriptar_perfil(p) for p in data.get("instalaciones", [])]
    return data


def encriptar_config(data: dict) -> dict:
    """Encripta las credenciales de cada perfil antes de persistir a disco.
    Asume que `data` ya viene en formato nuevo (la GUI siempre guarda el
    array completo, nunca formato plano). Sincroniza central_url (único,
    global) a cada perfil para que ejecutar_ciclo_agente lo siga leyendo tal
    cual del perfil, sin tener que threadear la config raíz hasta ahí."""
    data["central_url"] = data.get("central_url") or DEFAULT_CENTRAL_URL
    for perfil in data.get("instalaciones", []):
        perfil["central_url"] = data["central_url"]
    data["instalaciones"] = [_encriptar_perfil(p) for p in data.get("instalaciones", [])]
    data["config_version"] = 2
    return data


# ---------------------------------------------------------------------------
# VALIDACIÓN DEFENSIVA DE application_metrics
#
# El servidor central valida esto con un schema estricto: un solo campo mal
# formado tira abajo el reporte completo (incluida la telemetría de
# infraestructura que viaja en el mismo POST), y el error que devuelve es
# genérico (no dice qué campo falló). Antes de adjuntar el bloque al envelope,
# lo chequeamos acá para poder loguear el detalle real y, si no pasa, omitirlo
# este ciclo en vez de dejar que el servidor rechace todo a ciegas.
# ---------------------------------------------------------------------------
_RIS_CAMPOS_STR = ("equipo", "aet", "mod")
_RIS_CAMPOS_NUM = ("totales", "citados", "admitidos", "ejecutados", "con_imagen",
                   "borradores", "definitivos", "suspendidos")
_PACS_CAMPOS_STR = ("aet", "mod")
_PACS_CAMPOS_NUM = ("almacenados",)
_USERS_CAMPOS_STR = ("rol",)
_USERS_CAMPOS_NUM = ("usuarios_unicos", "inicios_sesion")


def _normalizar_listas_application_metrics(app_metrics: dict):
    """
    Si una sub-consulta de SQL_QUERY no matchea ninguna fila, SQL Server
    devuelve NULL para esa columna JSON (no "[]"). Tras el json.loads() eso
    llega como None, y un servidor que espera una lista (no Optional) rechaza
    el reporte entero por esto. None == "no hubo filas" == lista vacía.
    """
    for clave in ("ris", "pacs", "users"):
        if app_metrics.get(clave) is None:
            app_metrics[clave] = []


def _validar_item(item, campos_str, campos_num, indice, nombre_lista, campos_lista=()):
    if not isinstance(item, dict):
        return f"{nombre_lista}[{indice}] no es un objeto (llegó {type(item).__name__})"
    for campo in campos_str:
        if not isinstance(item.get(campo), str):
            return f"{nombre_lista}[{indice}].{campo} debería ser texto y llegó {item.get(campo)!r}"
    for campo in campos_num:
        valor = item.get(campo)
        if not isinstance(valor, (int, float)) or isinstance(valor, bool):
            return f"{nombre_lista}[{indice}].{campo} debería ser numérico y llegó {valor!r}"
    for campo in campos_lista:
        if not isinstance(item.get(campo), list):
            return f"{nombre_lista}[{indice}].{campo} debería ser una lista y llegó {item.get(campo)!r}"
    return None


def _validar_documentos_horarios(docs, nombre_indice, campos_str, campos_num, campos_lista=()):
    """
    Valida los documentos CRUDOS que devuelve un índice horario de Elastic,
    ANTES de agregarlos. Necesario porque _sumar_horas_* usa safe_int(), que
    convierte un campo faltante/None en 0 silenciosamente — sin este chequeo
    previo, un bucket de Logstash mal formado (ej. un campo que no se mapeó)
    se leería como "0 esta hora" en vez de detectarse como dato inválido.
    """
    for i, doc in enumerate(docs):
        problema = _validar_item(doc, campos_str, campos_num, i, nombre_indice, campos_lista=campos_lista)
        if problema:
            return problema
    return None


def _validar_application_metrics(app_metrics: dict):
    """
    Chequeo estructural mínimo (sin coaccionar ni completar valores) contra
    los campos que el contrato de ingesta exige para cada ítem de ris/pacs/users.
    Devuelve None si está todo bien, o una descripción puntual del primer
    problema encontrado (para loguearlo — el 500 del servidor no lo dice).
    """
    listas = {
        "ris":   (app_metrics.get("ris", []),   _RIS_CAMPOS_STR,   _RIS_CAMPOS_NUM),
        "pacs":  (app_metrics.get("pacs", []),  _PACS_CAMPOS_STR,  _PACS_CAMPOS_NUM),
        "users": (app_metrics.get("users", []), _USERS_CAMPOS_STR, _USERS_CAMPOS_NUM),
    }
    for nombre_lista, (items, campos_str, campos_num) in listas.items():
        if not isinstance(items, list):
            return f"{nombre_lista} debería ser una lista y llegó {type(items).__name__}"
        for i, item in enumerate(items):
            problema = _validar_item(item, campos_str, campos_num, i, nombre_lista)
            if problema:
                return problema
    return None


# ---------------------------------------------------------------------------
# VENTANA DE EXTRACCIÓN (checkpoint + backfill) — compartida entre el camino
# SQL directo (extraer_metricas_sql) y el camino vía Elasticsearch
# (extraer_metricas_ris_elastic). El concepto de "qué bloque de tiempo toca
# extraer ahora" es el mismo sin importar de dónde salgan después los
# números — separarlo evita mantener esta lógica duplicada en dos lugares.
# ---------------------------------------------------------------------------
def _calcular_ventana_extraccion(hospital_id, executions_per_day_raw, historical_start_date, log_func=None):
    """
    Devuelve (target_start_time, target_end_time, interval_hours), o None si
    el bloque todavía no terminó (hay que esperar al próximo ciclo).
    """
    executions_per_day = int(executions_per_day_raw)
    if executions_per_day <= 0:
        executions_per_day = 3
    interval_hours = 24.0 / executions_per_day

    ahora             = datetime.now()
    ultimo_checkpoint = get_last_checkpoint(hospital_id, log_func=log_func)

    if not ultimo_checkpoint:
        if historical_start_date:
            try:
                target_start_time = datetime.strptime(historical_start_date, "%Y-%m-%d")
                if log_func:
                    log_func(f"🕰️ INICIANDO BACKFILL HISTÓRICO desde {historical_start_date}")
            except Exception:
                target_start_time = ahora.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            target_start_time = ahora.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        target_start_time = ultimo_checkpoint

    target_end_time = target_start_time + timedelta(hours=interval_hours)
    if target_end_time > ahora:
        return None

    return target_start_time, target_end_time, interval_hours


# ---------------------------------------------------------------------------
# MÉTRICAS SQL (NIVEL NEGOCIO - EJECUCIÓN LENTA)
# ---------------------------------------------------------------------------
def extraer_metricas_sql(sql_config, hospital_id, log_func=None):
    if not sql_config or not sql_config.get("host"):
        return None

    conn_str          = f"DRIVER={{ODBC Driver 17 for SQL Server}};SERVER={sql_config['host']};DATABASE={sql_config['db']};UID={sql_config['user']};PWD={sql_config['pass']}"
    conn_str_fallback = f"DRIVER={{SQL Server}};SERVER={sql_config['host']};DATABASE={sql_config['db']};UID={sql_config['user']};PWD={sql_config['pass']}"

    conn = None
    try:
        try:
            conn = pyodbc.connect(conn_str, timeout=10)
        except pyodbc.Error:
            conn = pyodbc.connect(conn_str_fallback, timeout=10)

        cursor = conn.cursor()

        ventana = _calcular_ventana_extraccion(
            hospital_id,
            sql_config.get("executions_per_day", 3),
            sql_config.get("historical_start_date"),
            log_func=log_func,
        )
        if ventana is None:
            return None
        target_start_time, target_end_time, interval_hours = ventana

        ahora = datetime.now()
        start_date_sql = target_start_time.strftime("%Y-%m-%dT%H:%M:%S")
        end_date_sql   = target_end_time.strftime("%Y-%m-%dT%H:%M:%S")

        if (ahora - target_end_time).total_seconds() > 86400:
            if log_func:
                log_func(f"⏳ Backfill: Recuperando bloque [{start_date_sql} >> {end_date_sql}]...")
        else:
            if log_func:
                log_func(f"⚙️ SQL: Extrayendo bloque regular [{start_date_sql} >> {end_date_sql}]...")

        query_dinamica = SQL_QUERY.replace("{start_date_sql}", start_date_sql).replace("{end_date_sql}", end_date_sql)
        cursor.execute(query_dinamica)
        row = cursor.fetchone()
        if not row:
            return None

        json_fragments = []
        while row:
            json_fragments.append(row[0])
            row = cursor.fetchone()

        data_json = json.loads("".join(json_fragments))

        if "application_metrics" not in data_json:
            data_json["application_metrics"] = {}

        _normalizar_listas_application_metrics(data_json["application_metrics"])

        data_json["application_metrics"]["extraction_interval_hours"] = interval_hours
        data_json["application_metrics"]["start_time_extraction"]     = start_date_sql
        data_json["application_metrics"]["end_time_extraction"]       = end_date_sql

        problema = _validar_application_metrics(data_json["application_metrics"])
        if problema:
            if log_func:
                log_func(
                    f"❌ application_metrics inválido, se omite este bloque "
                    f"[{start_date_sql} → {end_date_sql}]: {problema}. Se reintentará en el próximo ciclo."
                )
            return None

        # IMPORTANTE: el checkpoint se guarda solo cuando el POST confirme éxito.
        data_json["_checkpoint_to_save"] = target_end_time
        return data_json

    except Exception as e:
        if log_func:
            log_func(f"❌ Error conectando a SQL Server: {e}")
        return None
    finally:
        if conn:
            conn.close()

# ---------------------------------------------------------------------------
# MÉTRICAS SQL — VÍA ELASTICSEARCH (mismo checkpoint, otra fuente)
#
# Alternativa a extraer_metricas_sql para hospitales cuyo Logstash ya publica
# los buckets horarios de ris/pacs/users (ver elk/ext_ris_metrics.conf,
# ext_pacs_metrics.conf, ext_users_metrics.conf). El agente deja de conectar
# directo a SQL Server: solo suma los documentos horarios que caen dentro del
# bloque que le toca extraer, usando el mismo checkpoint/backfill de siempre
# (_calcular_ventana_extraccion, .sql_checkpoint compartido).
#
# usuarios_unicos es la única cuenta que no es una simple suma: cada bucket
# horario trae el ARRAY de user_guid distintos de esa hora (no un conteo), y
# acá se arma la unión a través de todas las horas del bloque antes de
# contar — sumar "únicos por hora" sobreestimaría a quien se logueó en más
# de una hora del mismo bloque.
# ---------------------------------------------------------------------------
def _buscar_bucket_horario(elastic_cfg, index_name, campo_fecha, desde, hasta, log_func=None):
    """
    Trae todos los documentos de `index_name` cuyo `campo_fecha` cae en
    [desde, hasta). Pagina con search_after (mismo estilo que
    recolectar_logs_elastic) por si el bloque abarca muchas horas/equipos.
    """
    host = elastic_cfg.get("host", "").strip()
    port = elastic_cfg.get("port", 29200)
    url  = f"{_esquema_elastic(elastic_cfg)}://{host}:{port}/{index_name}/_search"
    auth = HTTPBasicAuth(elastic_cfg.get("user", ""), elastic_cfg.get("pass", "")) \
        if elastic_cfg.get("user") else None

    desde_iso = desde.strftime("%Y-%m-%dT%H:%M:%S")
    hasta_iso = hasta.strftime("%Y-%m-%dT%H:%M:%S")

    docs = []
    search_after = None
    batch_size = 1000

    while True:
        payload = {
            "size": batch_size,
            "query": {"range": {campo_fecha: {"gte": desde_iso, "lt": hasta_iso}}},
            "sort": [{campo_fecha: {"order": "asc"}}, {"_id": {"order": "asc"}}],
        }
        if search_after:
            payload["search_after"] = search_after

        resp = requests.post(url, json=payload, auth=auth, timeout=15, verify=False)
        resp.raise_for_status()
        hits = resp.json().get("hits", {}).get("hits", [])
        if not hits:
            break

        docs.extend(h.get("_source", {}) for h in hits)
        if len(hits) < batch_size:
            break
        search_after = hits[-1].get("sort")

    return docs


def _sumar_horas_ris(docs):
    campos_num = ("totales", "citados", "admitidos", "ejecutados", "con_imagen",
                  "borradores", "definitivos", "suspendidos")
    acumulado = {}
    for doc in docs:
        clave = (doc.get("equipo"), doc.get("aet"), doc.get("mod"))
        if clave not in acumulado:
            acumulado[clave] = {"equipo": doc.get("equipo"), "aet": doc.get("aet"), "mod": doc.get("mod")}
            acumulado[clave].update({c: 0 for c in campos_num})
        for campo in campos_num:
            acumulado[clave][campo] += safe_int(doc.get(campo))
    return list(acumulado.values())


def _sumar_horas_pacs(docs):
    acumulado = {}
    for doc in docs:
        clave = (doc.get("aet"), doc.get("mod"))
        if clave not in acumulado:
            acumulado[clave] = {"aet": doc.get("aet"), "mod": doc.get("mod"), "almacenados": 0}
        acumulado[clave]["almacenados"] += safe_int(doc.get("almacenados"))
    return list(acumulado.values())


def _sumar_horas_users(docs):
    acumulado = {}
    for doc in docs:
        rol = doc.get("rol")
        if rol not in acumulado:
            acumulado[rol] = {"rol": rol, "inicios_sesion": 0, "_guids": set()}
        acumulado[rol]["inicios_sesion"] += safe_int(doc.get("inicios_sesion"))
        for guid in (doc.get("user_guids") or []):
            acumulado[rol]["_guids"].add(guid)

    return [
        {"rol": v["rol"], "usuarios_unicos": len(v["_guids"]), "inicios_sesion": v["inicios_sesion"]}
        for v in acumulado.values()
    ]


def extraer_metricas_ris_elastic(elastic_cfg, hospital_id, log_func=None):
    """
    Equivalente a extraer_metricas_sql pero leyendo de los índices horarios
    que publica Logstash en vez de conectar directo a SQL Server. Mismo
    contrato de retorno: None si el bloque todavía no está listo o los datos
    no pasan la validación, o el dict con application_metrics + checkpoint
    listo para adjuntar al envelope.
    """
    if not elastic_cfg or not elastic_cfg.get("host"):
        return None

    ventana = _calcular_ventana_extraccion(
        hospital_id,
        elastic_cfg.get("ris_executions_per_day", 3),
        elastic_cfg.get("ris_historical_start_date"),
        log_func=log_func,
    )
    if ventana is None:
        return None
    target_start_time, target_end_time, interval_hours = ventana

    start_date_sql = target_start_time.strftime("%Y-%m-%dT%H:%M:%S")
    end_date_sql   = target_end_time.strftime("%Y-%m-%dT%H:%M:%S")

    ahora = datetime.now()
    if (ahora - target_end_time).total_seconds() > 86400:
        if log_func:
            log_func(f"⏳ Backfill (RIS/Elastic): Recuperando bloque [{start_date_sql} >> {end_date_sql}]...")
    else:
        if log_func:
            log_func(f"⚙️ RIS/Elastic: Extrayendo bloque regular [{start_date_sql} >> {end_date_sql}]...")

    idx_ris   = elastic_cfg.get("ris_index_ris")   or "ext_ris_metrics_hourly"
    idx_pacs  = elastic_cfg.get("ris_index_pacs")  or "ext_pacs_metrics_hourly"
    idx_users = elastic_cfg.get("ris_index_users") or "ext_users_metrics_hourly"

    try:
        docs_ris   = _buscar_bucket_horario(elastic_cfg, idx_ris,   "hour_start", target_start_time, target_end_time, log_func)
        docs_pacs  = _buscar_bucket_horario(elastic_cfg, idx_pacs,  "hour_start", target_start_time, target_end_time, log_func)
        docs_users = _buscar_bucket_horario(elastic_cfg, idx_users, "hour_start", target_start_time, target_end_time, log_func)
    except Exception as e:
        if log_func:
            log_func(f"❌ Error consultando ElasticSearch (RIS/PACS/users): {e}")
        return None

    problema_bucket = (
        _validar_documentos_horarios(docs_ris,   idx_ris,   _RIS_CAMPOS_STR,   _RIS_CAMPOS_NUM)
        or _validar_documentos_horarios(docs_pacs,  idx_pacs,  _PACS_CAMPOS_STR,  _PACS_CAMPOS_NUM)
        or _validar_documentos_horarios(docs_users, idx_users, _USERS_CAMPOS_STR, ("inicios_sesion",),
                                         campos_lista=("user_guids",))
    )
    if problema_bucket:
        if log_func:
            log_func(
                f"❌ Documento horario inválido en Elastic, se omite este bloque "
                f"[{start_date_sql} → {end_date_sql}]: {problema_bucket}. Se reintentará en el próximo ciclo."
            )
        return None

    application_metrics = {
        "ris":   _sumar_horas_ris(docs_ris),
        "pacs":  _sumar_horas_pacs(docs_pacs),
        "users": _sumar_horas_users(docs_users),
        "extraction_interval_hours": interval_hours,
        "start_time_extraction":     start_date_sql,
        "end_time_extraction":       end_date_sql,
    }

    problema = _validar_application_metrics(application_metrics)
    if problema:
        if log_func:
            log_func(
                f"❌ application_metrics inválido (RIS/Elastic), se omite este bloque "
                f"[{start_date_sql} → {end_date_sql}]: {problema}. Se reintentará en el próximo ciclo."
            )
        return None

    return {
        "application_metrics": application_metrics,
        "_checkpoint_to_save": target_end_time,
    }


# ---------------------------------------------------------------------------
# AUTOENRUTE DICOM — directo a SQL Server (v4.6, restaura el camino directo
# que existía hasta v4.3 — ver docs/PLAN_MEJORAS_V4.5.md)
#
# Misma query confirmada en producción que usa elk/ext_dicom_queues.conf para
# poblar el índice de Elastic (ver get_dicom_routing_queues más abajo), pero
# sin depender de un pipeline de Logstash intermedio: el dato es siempre en
# vivo, no hay "antigüedad de documento" que controlar.
#
# Corre en CADA ciclo (interval_minutes global) igual que la variante
# Elastic — a diferencia de extraer_metricas_sql (KPIs de negocio), NO tiene
# checkpoint ni ventana de extracción: es una foto del estado actual de las
# reglas, no una serie histórica.
# ---------------------------------------------------------------------------
_DICOM_ROUTING_QUERY = """
SELECT
    r.[IDRULE],
    r.[FROMNODE] AS [FROMNODE_KEY],
    c_from.[NICKNAME] AS [FROMNODE_NICKNAME],
    c_from.[HOSTNAME] AS [FROMNODE_HOSTNAME],
    r.[TONODE] AS [TONODE_KEY],
    c_to.[NICKNAME] AS [TONODE_NICKNAME],
    c_to.[HOSTNAME] AS [TONODE_HOSTNAME],
    ISNULL(p.[PENDING_COUNT], 0) AS [PENDING_INSTANCES]
FROM [ExtensaPACS].[ExtPacs].[DICOMAUTOROUTINGRULES] r WITH (NOLOCK)
LEFT JOIN [ExtensaPACS].[ExtPacs].[DICOMCLIENT] c_from WITH (NOLOCK)
    ON r.[FROMNODE] = c_from.[CLIENT_KEY]
LEFT JOIN [ExtensaPACS].[ExtPacs].[DICOMCLIENT] c_to WITH (NOLOCK)
    ON r.[TONODE] = c_to.[CLIENT_KEY]
LEFT JOIN (
    SELECT [IDRULE], COUNT(*) AS [PENDING_COUNT]
    FROM [ExtensaPACS].[ExtPacs].[DICOMAUTOROUTINGQUEUE] WITH (NOLOCK)
    GROUP BY [IDRULE]
) p ON r.[IDRULE] = p.[IDRULE]
WHERE r.[ACTIVE] = 1
"""


def obtener_dicom_routing_sql(sql_cfg, log_func=None):
    """
    Devuelve (routing_queues, status, errores) — mismo contrato y misma
    forma de `routing_queues` que get_dicom_routing_queues (vía Elastic),
    así ejecutar_ciclo_agente arma `dicom_routing_queues` igual sin importar
    qué camino se usó. `snapshot_age_minutes` va siempre en `0.0`: no hay
    lag de pipeline que medir, el dato es la foto actual de la base.
    status: "ok" | "empty" | "error"
    """
    if not sql_cfg or not sql_cfg.get("host"):
        return [], "error", 1

    conn_str          = f"DRIVER={{ODBC Driver 17 for SQL Server}};SERVER={sql_cfg['host']};DATABASE=ExtensaPACS;UID={sql_cfg['user']};PWD={sql_cfg['pass']}"
    conn_str_fallback = f"DRIVER={{SQL Server}};SERVER={sql_cfg['host']};DATABASE=ExtensaPACS;UID={sql_cfg['user']};PWD={sql_cfg['pass']}"

    conn = None
    try:
        try:
            conn = pyodbc.connect(conn_str, timeout=10)
        except pyodbc.Error:
            conn = pyodbc.connect(conn_str_fallback, timeout=10)

        cursor = conn.cursor()
        cursor.execute(_DICOM_ROUTING_QUERY)

        routing_queues = []
        for idrule, from_key, from_nick, from_host, to_key, to_nick, to_host, pending in cursor.fetchall():
            routing_queues.append({
                "id_rule": idrule,
                "from_node": {"key": from_key, "nickname": from_nick, "hostname": from_host},
                "to_node":   {"key": to_key,   "nickname": to_nick,   "hostname": to_host},
                "pending_instances": int(pending or 0),
                "snapshot_age_minutes": 0.0,
            })

        if log_func:
            log_func(f"✅ Autoenrute DICOM (SQL directo): {len(routing_queues)} reglas leídas.")
        return routing_queues, ("ok" if routing_queues else "empty"), 0

    except Exception as e:
        if log_func:
            log_func(f"❌ Error SQL extrayendo colas de enrute: {e}")
        return [], "error", 1
    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------------------------
# AUTOENRUTE DICOM (vía ElasticSearch - índice de estado actual)
#
# Logstash indexa el índice cada 5 min con upsert por document_id => IDRULE,
# así que refleja la "foto" del último ciclo y sólo contiene reglas activas
# (WHERE ACTIVE=1 en el pipeline).
#
# A diferencia de la lectura directa a SQL, acá un pipeline caído NO produce
# error: el índice devuelve los últimos valores conocidos como si fueran
# actuales. Por eso se controla la antigüedad de cada documento.
# ---------------------------------------------------------------------------
def get_dicom_routing_queues(elastic_cfg, log_func=None):
    """
    Devuelve: (routing_queues, status, errores)
      status: "ok" | "stale" | "empty" | "error"
    """
    if not elastic_cfg or not elastic_cfg.get("host"):
        return [], "error", 1

    host       = elastic_cfg.get("host", "").strip()
    port       = elastic_cfg.get("port", 29200)
    index_name = elastic_cfg.get("dicom_index") or "ext_dicom_queues"
    try:
        max_age = int(elastic_cfg.get("dicom_max_age_minutes") or 15)
    except (TypeError, ValueError):
        max_age = 15

    url  = f"{_esquema_elastic(elastic_cfg)}://{host}:{port}/{index_name}/_search"
    auth = HTTPBasicAuth(elastic_cfg.get("user", ""), elastic_cfg.get("pass", "")) \
        if elastic_cfg.get("user") else None

    # docvalue_fields fuerza el @timestamp en epoch_millis: no dependemos de
    # cómo lo serializó el pipeline ni de la zona horaria del servidor.
    payload = {
        "size": 1000,
        "query": {"match_all": {}},
        "sort": [{"pending_instances": {"order": "desc"}}],
        "docvalue_fields": [{"field": "@timestamp", "format": "epoch_millis"}],
    }

    try:
        resp = requests.post(url, json=payload, auth=auth, timeout=15, verify=False)
        resp.raise_for_status()
        hits = resp.json().get("hits", {}).get("hits", [])
    except Exception as e:
        if log_func:
            log_func(f"⚠️ Error ElasticSearch extrayendo colas de enrute: {e}")
        return [], "error", 1

    if not hits:
        if log_func:
            log_func(f"⚠️ Autoenrute DICOM: el índice '{index_name}' no devolvió documentos.")
        return [], "empty", 0

    ahora_ms       = time.time() * 1000.0
    routing_queues = []
    descartados    = 0
    edad_min_vista = None

    for hit in hits:
        # Normalizamos claves a minúscula por si cambia lowercase_column_names.
        src = {str(k).lower(): v for k, v in (hit.get("_source") or {}).items()}

        # --- Antigüedad del documento (detecta pipeline de Logstash caído) ---
        edad_min = None
        try:
            dv = (hit.get("fields") or {}).get("@timestamp") or []
            if dv:
                edad_min = (ahora_ms - float(dv[0])) / 60000.0
        except (TypeError, ValueError, IndexError):
            edad_min = None

        if edad_min is not None:
            if edad_min_vista is None or edad_min < edad_min_vista:
                edad_min_vista = edad_min
            if edad_min > max_age:
                descartados += 1
                continue

        try:
            pending = int(src.get("pending_instances", 0) or 0)
        except (TypeError, ValueError):
            pending = 0

        routing_queues.append({
            "id_rule": src.get("idrule"),
            "from_node": {
                "key":      src.get("fromnode_key"),
                "nickname": src.get("fromnode_nickname"),
                "hostname": src.get("fromnode_hostname"),
            },
            "to_node": {
                "key":      src.get("tonode_key"),
                "nickname": src.get("tonode_nickname"),
                "hostname": src.get("tonode_hostname"),
            },
            "pending_instances": pending,
            "snapshot_age_minutes": round(edad_min, 1) if edad_min is not None else None,
        })

    # Si TODO lo que había estaba vencido, el pipeline está caído. Devolvemos
    # lista vacía a propósito: preferimos un hueco visible en el gráfico antes
    # que una línea plana con datos viejos que parece normal.
    if descartados and not routing_queues:
        if log_func:
            edad_txt = f"{edad_min_vista:.0f} min" if edad_min_vista is not None else "desconocida"
            log_func(
                f"❌ Autoenrute DICOM: índice '{index_name}' desactualizado "
                f"(dato más reciente: {edad_txt}, máximo {max_age} min). "
                f"Revisar el pipeline de Logstash."
            )
        return [], "stale", descartados

    if log_func:
        extra = f" ({descartados} descartadas por antigüedad)" if descartados else ""
        log_func(f"✅ Autoenrute DICOM: {len(routing_queues)} reglas leídas de '{index_name}'{extra}.")

    return routing_queues, ("stale" if descartados else "ok"), descartados


def test_connection_dicom_index(data):
    """
    Test específico del índice de autoenrute (botón de la tarjeta Elastic).

    Existe porque un usuario válido para los logs puede no tener permiso sobre
    el índice de autoenrute: ese 403 es muy difícil de diagnosticar en producción.
    Expuesto como método de la clase Api en main_gui.py:

        def test_dicom_index_gui(self, data):
            return agent_logic.test_connection_dicom_index(data)
    """
    host  = (data.get("host") or "").strip()
    port  = data.get("port", 29200)
    index = (data.get("dicom_index") or "ext_dicom_queues").strip()

    if not host:
        return {"success": False, "msg": "Host de ElasticSearch no configurado"}

    auth = HTTPBasicAuth(data.get("user", ""), data.get("pass", "")) if data.get("user") else None
    url  = f"{_esquema_elastic(data)}://{host}:{port}/{index}/_search"

    try:
        r = requests.post(
            url,
            json={"size": 1, "docvalue_fields": [{"field": "@timestamp", "format": "epoch_millis"}]},
            auth=auth, timeout=8, verify=False
        )
    except Exception as e:
        return {"success": False, "msg": f"Error de conexión: {e}"}

    if r.status_code == 403:
        return {"success": False,
                "msg": f"El usuario no tiene permiso de lectura sobre '{index}' (HTTP 403).\n"
                       f"Agregar el índice al rol asignado a ese usuario."}
    if r.status_code == 404:
        return {"success": False,
                "msg": f"El índice '{index}' no existe (HTTP 404).\n"
                       f"Verificar que el pipeline de Logstash esté corriendo."}
    if r.status_code != 200:
        return {"success": False, "msg": f"HTTP {r.status_code}: {r.text[:200]}"}

    try:
        hits = r.json().get("hits", {}).get("hits", [])
    except Exception:
        return {"success": False, "msg": "Respuesta no interpretable de ElasticSearch"}

    if not hits:
        return {"success": False, "msg": f"Acceso OK pero el índice '{index}' está vacío."}

    edad_txt = "desconocida"
    try:
        dv = (hits[0].get("fields") or {}).get("@timestamp") or []
        if dv:
            edad_txt = f"{(time.time() * 1000.0 - float(dv[0])) / 60000.0:.1f} min"
    except (TypeError, ValueError, IndexError):
        pass

    return {"success": True,
            "msg": f"Índice '{index}' accesible. Antigüedad del último dato: {edad_txt}."}


def test_connection_ris_metrics(data):
    """
    Test de la tarjeta "KPIs de RIS vía Elastic" (botón de la GUI).

    Igual motivo que test_connection_dicom_index: un usuario válido para un
    índice puede no tener permiso sobre otro, y ese 403 es difícil de
    diagnosticar en producción. Prueba los tres índices (ris/pacs/users) en
    un solo llamado.
    """
    host = (data.get("host") or "").strip()
    if not host:
        return {"success": False, "msg": "Host de ElasticSearch no configurado"}

    port = data.get("port", 29200)
    auth = HTTPBasicAuth(data.get("user", ""), data.get("pass", "")) if data.get("user") else None

    indices = {
        "RIS":      data.get("ris_index_ris")   or "ext_ris_metrics_hourly",
        "PACS":     data.get("ris_index_pacs")  or "ext_pacs_metrics_hourly",
        "Usuarios": data.get("ris_index_users") or "ext_users_metrics_hourly",
    }

    resultados = []
    for etiqueta, index in indices.items():
        url = f"{_esquema_elastic(data)}://{host}:{port}/{index}/_search"
        try:
            r = requests.post(url, json={"size": 1, "query": {"match_all": {}}}, auth=auth, timeout=8, verify=False)
        except Exception as e:
            resultados.append(f"{etiqueta} ('{index}'): error de conexión — {e}")
            continue

        if r.status_code == 403:
            resultados.append(f"{etiqueta} ('{index}'): sin permiso de lectura (HTTP 403)")
        elif r.status_code == 404:
            resultados.append(f"{etiqueta} ('{index}'): el índice no existe (HTTP 404) — ¿ya corrió el pipeline de Logstash?")
        elif r.status_code != 200:
            resultados.append(f"{etiqueta} ('{index}'): HTTP {r.status_code}")
        else:
            try:
                hits = r.json().get("hits", {}).get("hits", [])
            except Exception:
                resultados.append(f"{etiqueta} ('{index}'): respuesta no interpretable de ElasticSearch")
                continue
            resultados.append(f"{etiqueta} ('{index}'): OK" + (" (todavía sin documentos)" if not hits else ""))

    hubo_error = any(": OK" not in r for r in resultados)
    return {"success": not hubo_error, "msg": "\n".join(resultados)}


# ---------------------------------------------------------------------------
# PROXMOX
# ---------------------------------------------------------------------------
def test_connection_proxmox(config):
    host = config.get("host") or config.get("ip")
    if not host or not verificar_puerto(host, 8006):
        return {"success": False, "msg": "Puerto 8006 cerrado o no alcanzable"}
    try:
        auth = {"username": config['user'], "password": config['pass']}
        r = requests.post(
            f"https://{host}:8006/api2/json/access/ticket",
            data=auth, verify=False, timeout=5
        )
        if r.status_code == 200:
            return {"success": True, "msg": "Proxmox OK"}
        return {"success": False, "msg": f"Credenciales inválidas (HTTP {r.status_code})"}
    except Exception as e:
        return {"success": False, "msg": str(e)}

def obtener_physical_layer(config):
    host = config.get("host") or config.get("ip")
    res = {
        "host_info":  {"hostname": host, "type": "proxmox", "model": "Unknown", "uptime_seconds": 0},
        "telemetry":  {},
        "sensors":    {},
    }
    try:
        auth    = {"username": config['user'], "password": config['pass']}
        r_auth  = requests.post(
            f"https://{host}:8006/api2/json/access/ticket",
            data=auth, verify=False, timeout=5
        )
        if r_auth.status_code == 200:
            tk     = r_auth.json()['data']
            r_node = requests.get(
                f"https://{host}:8006/api2/json/nodes/{config.get('node', 'pve')}/status",
                cookies={"PVEAuthCookie": tk['ticket']},
                headers={"CSRFPreventionToken": tk['CSRFPreventionToken']},
                verify=False, timeout=5
            )
            raw = r_node.json()['data']
            res["host_info"].update({
                "model":          raw.get("cpuinfo", {}).get("model"),
                "uptime_seconds": safe_int(raw.get("uptime")),
            })
            u_mem = safe_int(raw.get("memory", {}).get("used"))
            t_mem = safe_int(raw.get("memory", {}).get("total"))
            res["telemetry"] = {
                "cpu": {"usage_percent": safe_float(raw.get("cpu", 0) * 100)},
                "ram": {
                    "total_gb":     round(t_mem / 1073741824, 2),
                    "used_gb":      round(u_mem / 1073741824, 2),
                    "usage_percent": safe_float(u_mem / t_mem * 100 if t_mem > 0 else 0),
                },
            }
    except Exception as e:
        res["host_info"]["error"] = str(e)
    return res


# ---------------------------------------------------------------------------
# VMWARE — implementación real con pyVmomi
# ---------------------------------------------------------------------------
def test_connection_vmware(config):
    """
    Prueba conexión a vCenter o ESXi directo.
    Requiere: pip install pyVmomi
    """
    host = config.get("host") or config.get("ip")
    if not host:
        return {"success": False, "msg": "IP / Hostname no configurado"}

    if not verificar_puerto(host, 443):
        return {"success": False, "msg": "Puerto 443 cerrado o no alcanzable"}

    try:
        from pyVim.connect import SmartConnect, Disconnect
        import ssl

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode    = ssl.CERT_NONE

        si = SmartConnect(
            host=host,
            user=config.get("user", ""),
            pwd=config.get("pass", ""),
            sslContext=context,
            connectionPoolTimeout=10,
        )
        about = si.content.about
        version_info = f"{about.fullName} (API {about.apiVersion})"
        Disconnect(si)
        return {"success": True, "msg": f"VMware OK — {version_info}"}

    except ImportError:
        return {
            "success": False,
            "msg": "Módulo pyVmomi no instalado. Ejecutar: pip install pyVmomi",
        }
    except Exception as e:
        return {"success": False, "msg": f"Error VMware: {str(e)}"}


def obtener_vmware_layer(config, log_func=None):
    """
    Recolecta métricas del host ESXi / vCenter usando pyVmomi.
    Devuelve la misma estructura que obtener_physical_layer() para Proxmox,
    más la lista de VMs con telemetría básica.
    """
    host = config.get("host") or config.get("ip")
    res = {
        "host_info": {"hostname": host, "type": "vmware", "model": "Unknown", "uptime_seconds": 0},
        "telemetry": {},
        "sensors":   {},
        "vms":       [],
    }

    try:
        from pyVim.connect  import SmartConnect, Disconnect
        from pyVmomi        import vim
        import ssl

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode    = ssl.CERT_NONE

        si = SmartConnect(
            host=host,
            user=config.get("user", ""),
            pwd=config.get("pass", ""),
            sslContext=context,
            connectionPoolTimeout=10,
        )

        content = si.content

        # --- Info del host ---
        container  = content.viewManager.CreateContainerView(content.rootFolder, [vim.HostSystem], True)
        host_list  = container.view
        container.Destroy()

        if host_list:
            hs = host_list[0]
            summary = hs.summary
            hw      = summary.hardware
            rt      = summary.runtime
            qs      = summary.quickStats

            uptime = 0
            if rt.bootTime:
                uptime = int((datetime.utcnow() - rt.bootTime.replace(tzinfo=None)).total_seconds())

            total_ram_bytes = safe_int(hw.memorySize) if hw else 0
            used_ram_bytes  = safe_int(qs.overallMemoryUsage) * 1048576 if qs else 0

            res["host_info"].update({
                "model":          hw.cpuModel if hw else "Unknown",
                "uptime_seconds": uptime,
                "vendor":         hw.vendor  if hw else "Unknown",
            })
            res["telemetry"] = {
                "cpu": {
                    "usage_percent": safe_float(qs.overallCpuUsage / max(1, hw.numCpuCores * (hw.cpuMhz or 1)) * 100 if (qs and hw) else 0),
                },
                "ram": {
                    "total_gb":      round(total_ram_bytes / 1073741824, 2),
                    "used_gb":       round(used_ram_bytes  / 1073741824, 2),
                    "usage_percent": safe_float(used_ram_bytes / total_ram_bytes * 100 if total_ram_bytes > 0 else 0),
                },
            }

        # --- Lista de VMs ---
        vm_container = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
        vms          = vm_container.view
        vm_container.Destroy()

        for vm in vms:
            try:
                cfg_vm  = vm.config
                summary = vm.summary
                qs      = summary.quickStats
                state   = "Online" if summary.runtime.powerState == "poweredOn" else "Offline"

                total_ram_mb = safe_int(cfg_vm.hardware.memoryMB) if cfg_vm else 0
                used_ram_mb  = safe_int(qs.guestMemoryUsage)      if qs      else 0

                res["vms"].append({
                    "id":    cfg_vm.name if cfg_vm else vm.name,
                    "type":  "vm",
                    "state": state,
                    "telemetry": {
                        "cpu": {"usage_percent": safe_float(qs.overallCpuUsage if qs else 0)},
                        "ram": {
                            "total_gb":      round(total_ram_mb / 1024, 2),
                            "used_gb":       round(used_ram_mb  / 1024, 2),
                            "usage_percent": safe_float(used_ram_mb / total_ram_mb * 100 if total_ram_mb > 0 else 0),
                        },
                    },
                    "storage":           [],
                    "application_layer": {"services": []},
                })
            except Exception as vm_err:
                if log_func:
                    log_func(f"⚠️ VMware: error en VM {getattr(vm, 'name', '?')}: {vm_err}")

        Disconnect(si)

    except ImportError:
        msg = "pyVmomi no instalado. Ejecutar: pip install pyVmomi"
        if log_func:
            log_func(f"❌ VMware: {msg}")
        res["host_info"]["error"] = msg

    except Exception as e:
        if log_func:
            log_func(f"❌ Error recolectando VMware: {e}")
        res["host_info"]["error"] = str(e)

    return res


# ---------------------------------------------------------------------------
# iDRAC — Sensores y Storage
# ---------------------------------------------------------------------------
def test_connection_idrac(config):
    try:
        auth = HTTPBasicAuth(config.get('user'), config.get('pass'))
        r    = requests.get(
            f"https://{config['ip']}/redfish/v1/Systems/System.Embedded.1",
            auth=auth, verify=False, timeout=5
        )
        return {"success": True, "msg": "iDRAC OK"} if r.status_code == 200 else {"success": False, "msg": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"success": False, "msg": str(e)}


def obtener_sensors_idrac(config):
    sensors = {
        "status":       "OK",
        "temperatures": [],
        "fans":         [],
        "power":        {"watts_current": 0, "supplies": []},
    }
    try:
        auth  = HTTPBasicAuth(config.get('user'), config.get('pass'))
        base  = f"https://{config['ip']}/redfish/v1/Chassis/System.Embedded.1"
        r_th  = requests.get(f"{base}/Thermal", auth=auth, verify=False, timeout=5)
        if r_th.status_code == 200:
            d = r_th.json()
            for t in d.get("Temperatures", []):
                sensors["temperatures"].append({
                    "name": t.get("Name"), "value": t.get("ReadingCelsius"), "unit": "C",
                    "status": t.get("Status", {}).get("Health", "Unknown"),
                })
            for f in d.get("Fans", []):
                sensors["fans"].append({
                    "name": f.get("FanName") or f.get("Name"), "value": f.get("Reading"), "unit": "RPM",
                    "status": f.get("Status", {}).get("Health", "Unknown"),
                })
        r_pw = requests.get(f"{base}/Power", auth=auth, verify=False, timeout=5)
        if r_pw.status_code == 200:
            d = r_pw.json()
            sensors["power"]["watts_current"] = safe_int(d.get("PowerControl", [{}])[0].get("PowerConsumedWatts"))
            for ps in d.get("PowerSupplies", []):
                sensors["power"]["supplies"].append({
                    "name": ps.get("Name"), "watts": safe_float(ps.get("LastPowerOutputWatts")),
                    "status": ps.get("Status", {}).get("Health", "Unknown"),
                })
    except Exception as e:
        sensors["status"] = f"error: {str(e)}"
    return sensors

def obtener_storage_fisico_v3(config, log_func=None):
    """
    Recorre la API Redfish de iDRAC de forma PARALELA.
    Evita alcanzar el timeout global de 60s en servidores de PACS con múltiples arreglos RAID.
    """
    if log_func:
        log_func(f"📦 Recolectando Storage iDRAC (RAID) Concurrente: {config.get('ip')}")

    storage_data = {
        "controllers":      [],
        "logical_volumes":  [],
        "physical_drives":  [],
        "collection_complete": False,
    }

    auth = HTTPBasicAuth(config.get('user'), config.get('pass'))
    base_url = f"https://{config['ip']}/redfish/v1/Systems/System.Embedded.1/Storage"

    try:
        r_store = requests.get(base_url, auth=auth, verify=False, timeout=8)
        if r_store.status_code != 200:
            return storage_data

        members = r_store.json().get("Members", [])
        
        # Funciones auxiliares para hilos paralelos
        def fetch_controller(m):
            try:
                return requests.get(f"https://{config['ip']}{m['@odata.id']}", auth=auth, verify=False, timeout=10).json()
            except Exception:
                return None

        def fetch_volume(v_url):
            try:
                return requests.get(f"https://{config['ip']}{v_url}", auth=auth, verify=False, timeout=10).json()
            except Exception:
                return None

        def fetch_drive(d_url):
            try:
                return requests.get(f"https://{config['ip']}{d_url}", auth=auth, verify=False, timeout=10).json()
            except Exception:
                return None

        # 1. Obtener Controladoras en paralelo
        with ThreadPoolExecutor(max_workers=5) as executor:
            controllers_raw = list(executor.map(fetch_controller, members))

        volumes_urls = []
        drives_urls = []

        for c in controllers_raw:
            if not c: continue
            
            ctrl_name = c.get("Id", "Unknown Controller")
            storage_data["controllers"].append({
                "name":   ctrl_name,
                "status": c.get("Status", {}).get("Health", "Unknown"),
                "model":  c.get("Summary", {}).get("Model", "Dell PERC"),
            })

            # Extraer URLs para el siguiente paso
            if "Volumes" in c:
                # Requiere otra petición para ver los miembros del volumen, la hacemos síncrona por simplicidad
                v_disks_url = f"https://{config['ip']}{c['@odata.id']}/Volumes"
                r_vd = requests.get(v_disks_url, auth=auth, verify=False, timeout=5)
                if r_vd.status_code == 200:
                    for v in r_vd.json().get("Members", []):
                        volumes_urls.append(v['@odata.id'])
            
            for d in c.get("Drives", []):
                drives_urls.append(d['@odata.id'])

        # 2. Obtener Volúmenes Lógicos y Discos Físicos en paralelo
        # v4.6: bajado de 10 a 4 workers (ver docs/PLAN_MEJORAS_V4.5.md §5.2)
        # — los BMC Dell suelen tener límites bajos de sesiones concurrentes;
        # 10 en paralelo arriesgaba 503/timeouts intermitentes en RAID grandes.
        with ThreadPoolExecutor(max_workers=4) as executor:
            volumes_raw = list(executor.map(fetch_volume, volumes_urls))
            drives_raw = list(executor.map(fetch_drive, drives_urls))

        for vd in volumes_raw:
            if vd:
                storage_data["logical_volumes"].append({
                    "name":       vd.get("Name"),
                    "raid_level": vd.get("VolumeType"),
                    "size_gb":    round(safe_int(vd.get("CapacityBytes")) / 1073741824, 2),
                    "status":     vd.get("Status", {}).get("Health", "Unknown"),
                })

        for drive in drives_raw:
            if drive:
                storage_data["physical_drives"].append({
                    "slot":       drive.get("Id"),
                    "model":      drive.get("Model"),
                    "size_gb":    round(safe_int(drive.get("CapacityBytes")) / 1073741824, 2),
                    "media_type": drive.get("MediaType"),
                    "status":     drive.get("Status", {}).get("Health", "Unknown"),
                })

        storage_data["collection_complete"] = True

    except Exception as e:
        if log_func:
            log_func(f"❌ Error RAID iDRAC Concurrente: {e}")
        storage_data["error"] = str(e)

    return storage_data

# ---------------------------------------------------------------------------
# WMI — VMs y Workstations
# Corrección clave: timeout por VM via thread, muestras de disco reducidas,
# state_reason para distinguir offline real de error de conexión.
# ---------------------------------------------------------------------------
def test_connection_vm_wmi(vm_info):
    ip = vm_info.get("ip")
    if not verificar_puerto(ip, 135):
        return {"success": False, "msg": "Puerto 135 cerrado"}
    try:
        pythoncom.CoInitialize()
        c = wmi.WMI(
            ip,
            user=vm_info.get("user"),
            password=vm_info.get("pass"),
            impersonation_level="Impersonate",
            authentication_level="Pktprivacy",
        )
        hostname = c.Win32_ComputerSystem()[0].Name
        return {"success": True, "msg": f"WMI OK: {hostname}", "hostname": hostname}
    except Exception as e:
        return {"success": False, "msg": str(e)}
    finally:
        pythoncom.CoUninitialize()

def _recolectar_wmi_interno(vm_info, log_func):
    """
    Ejecutado en un thread separado con timeout controlado desde obtener_vm_data().
    Retorna el objeto vm_obj completo.
    """
    ip             = vm_info.get("ip")
    tipo_maquina   = vm_info.get("type", "vm")
    nombre_manual  = vm_info.get("nombre", "").strip()

    vm_obj = {
        "id":                nombre_manual if nombre_manual else ip,
        "type":              tipo_maquina,
        "os":                "windows",
        "state":             "Offline",
        "state_reason":      "unknown",
        "telemetry":         {},
        "storage":           [],
        "application_layer": {"services": []},
    }

    if not verificar_puerto(ip, 135):
        vm_obj["state_reason"] = "port_closed"
        return vm_obj

    try:
        pythoncom.CoInitialize()
        c = wmi.WMI(
            ip,
            user=vm_info.get("user"),
            password=vm_info.get("pass"),
            impersonation_level="Impersonate",
            authentication_level="Pktprivacy",
        )

        hostname_real    = c.Win32_ComputerSystem()[0].Name
        vm_obj["id"]     = nombre_manual if nombre_manual else (hostname_real or ip)
        vm_obj["state"]  = "Online"
        vm_obj["state_reason"] = "ok"

        os_sys = c.Win32_OperatingSystem()[0]
        t_ram  = safe_int(os_sys.TotalVisibleMemorySize)
        u_ram  = t_ram - safe_int(os_sys.FreePhysicalMemory)

        vm_obj["telemetry"] = {
            "cpu": {
                "usage_percent": safe_float(
                    sum(safe_int(x.LoadPercentage) for x in c.Win32_Processor()) /
                    max(1, len(c.Win32_Processor()))
                )
            },
            "ram": {
                "total_gb":      round(t_ram / 1048576, 2),
                "used_gb":       round(u_ram / 1048576, 2),
                "usage_percent": round((u_ram / t_ram) * 100, 2) if t_ram > 0 else 0,
            },
            "uptime_seconds": int((datetime.now() - parse_wmi_date(os_sys.LastBootUpTime)).total_seconds()),
        }

        # --- Latencia de disco: 3 muestras con 5s de espera (antes eran 6x10s) ---
        perf_map_promedio = {}
        try:
            muestras    = 3
            espera_seg  = 5
            perf_map_temp = {}
            for i in range(muestras):
                for p in c.Win32_PerfFormattedData_PerfDisk_LogicalDisk():
                    letra = p.Name
                    if letra not in perf_map_temp:
                        perf_map_temp[letra] = []
                    latencia_ms = safe_float(p.AvgDisksecPerTransfer) * 1000.0
                    perf_map_temp[letra].append(latencia_ms)
                if i < muestras - 1:
                    time.sleep(espera_seg)

            for letra, valores in perf_map_temp.items():
                reales = [v for v in valores if v > 0]
                perf_map_promedio[letra] = sum(reales) / len(reales) if reales else 0.0
        except Exception as disk_err:
            if log_func:
                log_func(f"⚠️ WMI disco ({ip}): {disk_err}")

        for d in c.Win32_LogicalDisk(DriveType=3):
            letra     = d.DeviceID
            latencia  = perf_map_promedio.get(letra, 0.0)
            if latencia > 50.0:
                estado_perf = "Critical"
            elif latencia > 20.0:
                estado_perf = "Warning"
            else:
                estado_perf = "OK"

            vm_obj["storage"].append({
                "mount_point":   letra,
                "total_gb":      round(safe_int(d.Size)      / 1073741824, 2),
                "free_gb":       round(safe_int(d.FreeSpace) / 1073741824, 2),
                "usage_percent": round(((safe_int(d.Size) - safe_int(d.FreeSpace)) / max(1, safe_int(d.Size))) * 100, 1),
                "performance":   {"latency_ms": round(latencia, 2), "status": estado_perf},
            })

        # --- Servicios ---
        proc_map = {safe_int(p.IDProcess): p for p in c.Win32_PerfFormattedData_PerfProc_Process()}
        servicios_cfg = vm_info.get("servicios", "")
        if isinstance(servicios_cfg, list):
            servicios = [s.strip() for s in servicios_cfg if s.strip()]
        else:
            servicios = [s.strip() for s in servicios_cfg.split(",") if s.strip()]

        for s in c.Win32_Service():
            if s.Name in servicios:
                v = {"pid": safe_int(s.ProcessId), "health": "OK", "cpu_percent": 0.0, "ram_mb": 0.0, "threads": 0, "handles": 0}
                if v["pid"] in proc_map:
                    p = proc_map[v["pid"]]
                    v.update({
                        "cpu_percent": safe_float(p.PercentProcessorTime),
                        "ram_mb":      round(safe_int(p.WorkingSet) / 1048576, 1),
                        "threads":     safe_int(p.ThreadCount),
                        "handles":     safe_int(p.HandleCount),
                    })
                vm_obj["application_layer"]["services"].append({
                    "name": s.Name, "display_name": s.DisplayName, "state": s.State, "vital_signs": v,
                })

    except Exception as e:
        vm_obj["state"]          = "Offline"
        vm_obj["state_reason"]   = "wmi_error"
        vm_obj["collection_error"] = str(e)
        if log_func:
            log_func(f"⚠️ Error WMI ({tipo_maquina.upper()}) {ip}: {e}")
    finally:
        pythoncom.CoUninitialize()

    return vm_obj


# ---------------------------------------------------------------------------
# SSH — VMs y equipos Linux (v4.6, mismo patrón que WMI pero para Linux)
#
# Mismo vm_obj de salida que _recolectar_wmi_interno (ver
# docs/PLAN_MEJORAS_V4.5.md §9.2 y docs/CONTRATO_AGENTE.md §5). Dos campos sin
# equivalente limpio en Linux, resueltos sin forzar un dato falso:
#   - storage[].performance: se omite (mapear mountpoint->device real en
#     LVM/RAID para sacar una latencia comparable agrega complejidad para un
#     dato que nadie pidió todavía).
#   - vital_signs.handles: se reemplaza por la cantidad de file descriptors
#     abiertos del proceso (mismo propósito práctico, no es el mismo número).
# ---------------------------------------------------------------------------
def test_connection_vm_ssh(vm_info):
    ip = vm_info.get("ip")
    if not verificar_puerto(ip, 22):
        return {"success": False, "msg": "Puerto 22 cerrado"}
    try:
        cliente = paramiko.SSHClient()
        cliente.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        cliente.connect(
            ip,
            username=vm_info.get("user"),
            password=vm_info.get("pass"),
            timeout=10,
            banner_timeout=10,
            auth_timeout=10,
        )
        _, stdout, _ = cliente.exec_command("hostname", timeout=10)
        hostname = stdout.read().decode("utf-8", errors="replace").strip()
        return {"success": True, "msg": f"SSH OK: {hostname}", "hostname": hostname}
    except Exception as e:
        return {"success": False, "msg": str(e)}
    finally:
        try:
            cliente.close()
        except Exception:
            pass


def _ejecutar_ssh(cliente, comando, timeout=15):
    """Corre un comando y devuelve (stdout, stderr) como texto. No revisa el
    exit status: cada llamador decide qué hacer con una salida vacía."""
    _, stdout, stderr = cliente.exec_command(comando, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    return out, err


def _leer_cpu_stat(cliente):
    """Una lectura de /proc/stat (línea 'cpu '): (total, idle) en jiffies."""
    out, _ = _ejecutar_ssh(cliente, "grep '^cpu ' /proc/stat")
    valores = [safe_int(v) for v in out.split()[1:]]
    if len(valores) < 5:
        return None
    idle  = valores[3]
    total = sum(valores)
    return total, idle


def _recolectar_ssh_interno(vm_info, log_func):
    """
    Ejecutado en un thread separado con timeout controlado desde
    obtener_vm_data(). Retorna el objeto vm_obj completo, misma forma que
    _recolectar_wmi_interno.
    """
    ip             = vm_info.get("ip")
    tipo_maquina   = vm_info.get("type", "vm")
    nombre_manual  = vm_info.get("nombre", "").strip()

    vm_obj = {
        "id":                nombre_manual if nombre_manual else ip,
        "type":              tipo_maquina,
        "os":                "linux",
        "state":             "Offline",
        "state_reason":      "unknown",
        "telemetry":         {},
        "storage":           [],
        "application_layer": {"services": []},
    }

    if not verificar_puerto(ip, 22):
        vm_obj["state_reason"] = "port_closed"
        return vm_obj

    cliente = None
    try:
        cliente = paramiko.SSHClient()
        cliente.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        cliente.connect(
            ip,
            username=vm_info.get("user"),
            password=vm_info.get("pass"),
            timeout=10,
            banner_timeout=10,
            auth_timeout=10,
        )

        hostname_real, _ = _ejecutar_ssh(cliente, "hostname")
        hostname_real    = hostname_real.strip()
        vm_obj["id"]     = nombre_manual if nombre_manual else (hostname_real or ip)
        vm_obj["state"]  = "Online"
        vm_obj["state_reason"] = "ok"

        # --- RAM + uptime: una sola ida y vuelta ---
        meminfo_out, _ = _ejecutar_ssh(
            cliente,
            "awk '/MemTotal/{t=$2} /MemAvailable/{a=$2} END{print t, a}' /proc/meminfo"
        )
        t_ram_kb, disp_ram_kb = (safe_int(v) for v in (meminfo_out.split() + [0, 0])[:2])
        u_ram_kb = max(0, t_ram_kb - disp_ram_kb)

        uptime_out, _ = _ejecutar_ssh(cliente, "cat /proc/uptime")
        uptime_seconds = int(safe_float(uptime_out.split()[0])) if uptime_out.split() else 0

        # --- CPU: dos muestras de /proc/stat con ~1s de espera (mismo patrón
        # de muestreo antes/después que obtener_salud_red_pasiva) ---
        muestra_1 = _leer_cpu_stat(cliente)
        time.sleep(1.0)
        muestra_2 = _leer_cpu_stat(cliente)

        cpu_usage_percent = 0.0
        if muestra_1 and muestra_2:
            total_1, idle_1 = muestra_1
            total_2, idle_2 = muestra_2
            delta_total = total_2 - total_1
            delta_idle  = idle_2 - idle_1
            if delta_total > 0:
                cpu_usage_percent = round((1 - (delta_idle / delta_total)) * 100, 2)

        vm_obj["telemetry"] = {
            "cpu": {"usage_percent": cpu_usage_percent},
            "ram": {
                "total_gb":      round(t_ram_kb / 1048576, 2),
                "used_gb":       round(u_ram_kb / 1048576, 2),
                "usage_percent": round((u_ram_kb / t_ram_kb) * 100, 2) if t_ram_kb > 0 else 0,
            },
            "uptime_seconds": uptime_seconds,
        }

        # --- Disco: df, filtrando filesystems que no son discos reales ---
        df_out, _ = _ejecutar_ssh(
            cliente,
            "df -P -B1 -x tmpfs -x devtmpfs -x overlay -x squashfs 2>/dev/null | tail -n +2"
        )
        for linea in df_out.splitlines():
            campos = linea.split()
            if len(campos) < 6:
                continue
            _, size_bytes, _, avail_bytes, _, mount_point = campos[:6]
            size_bytes  = safe_int(size_bytes)
            avail_bytes = safe_int(avail_bytes)
            usados      = max(0, size_bytes - avail_bytes)
            vm_obj["storage"].append({
                "mount_point":   mount_point,
                "total_gb":      round(size_bytes  / 1073741824, 2),
                "free_gb":       round(avail_bytes / 1073741824, 2),
                "usage_percent": round((usados / size_bytes) * 100, 1) if size_bytes > 0 else 0,
                # Sin "performance": no hay un equivalente confiable a la
                # latencia de disco de WMI sin mapear mountpoint->device real
                # (LVM/RAID) — ver docs/PLAN_MEJORAS_V4.5.md §9.2.
            })

        # --- Servicios (unidades systemd) ---
        servicios_cfg = vm_info.get("servicios", "")
        if isinstance(servicios_cfg, list):
            servicios = [s.strip() for s in servicios_cfg if s.strip()]
        else:
            servicios = [s.strip() for s in servicios_cfg.split(",") if s.strip()]

        for unidad in servicios:
            estado_out, _ = _ejecutar_ssh(
                cliente,
                f"systemctl show {unidad} --property=ActiveState,SubState,MainPID --value"
            )
            partes = estado_out.strip().splitlines()
            if len(partes) < 3:
                continue
            active_state, sub_state, main_pid = partes[0], partes[1], safe_int(partes[2])

            v = {"pid": main_pid, "health": "OK", "cpu_percent": 0.0, "ram_mb": 0.0, "threads": 0, "handles": 0}
            if main_pid > 0:
                ps_out, _ = _ejecutar_ssh(cliente, f"ps -o %cpu,rss,nlwp --no-headers -p {main_pid}")
                ps_valores = ps_out.split()
                if len(ps_valores) == 3:
                    v.update({
                        "cpu_percent": safe_float(ps_valores[0]),
                        "ram_mb":      round(safe_int(ps_valores[1]) / 1024, 1),
                        "threads":     safe_int(ps_valores[2]),
                    })
                fd_out, _ = _ejecutar_ssh(cliente, f"ls /proc/{main_pid}/fd 2>/dev/null | wc -l")
                # "handles" en una entrada Linux son file descriptors abiertos,
                # no el mismo concepto que en Windows — ver docs/CONTRATO_AGENTE.md.
                v["handles"] = safe_int(fd_out.strip())

            vm_obj["application_layer"]["services"].append({
                "name": unidad, "display_name": unidad,
                "state": "Running" if active_state == "active" else sub_state or active_state,
                "vital_signs": v,
            })

    except Exception as e:
        vm_obj["state"]            = "Offline"
        vm_obj["state_reason"]     = "ssh_error"
        vm_obj["collection_error"] = str(e)
        if log_func:
            log_func(f"⚠️ Error SSH ({tipo_maquina.upper()}) {ip}: {e}")
    finally:
        if cliente:
            try:
                cliente.close()
            except Exception:
                pass

    return vm_obj


_hilos_recoleccion_vm_activos = 0
_lock_hilos_recoleccion_vm    = threading.Lock()
UMBRAL_ALERTA_HILOS_HUERFANOS = 10  # ver docs/PLAN_MEJORAS_V4.5.md §4.3


def obtener_vm_data(args):
    """
    Wrapper con timeout global de 90s por VM para evitar threads colgados.
    Despacha a WMI o SSH según vm_info["os"] ("windows" por default, para no
    romper configs existentes que no tienen este campo — ver
    docs/PLAN_MEJORAS_V4.5.md §9.2).

    No hay forma segura de cancelar un hilo de Python a mitad de una llamada
    WMI/SSH colgada (§4.3 del plan) — si el timeout se cumple, el hilo (y su
    sesión COM en el caso WMI) queda huérfano corriendo en segundo plano
    hasta que termine por su cuenta o el proceso se reinicie. Lo que sí se
    puede hacer, y es lo que se agrega acá, es contar cuántos de estos hilos
    siguen vivos simultáneamente — sin esto, una acumulación silenciosa solo
    se notaba como un problema de memoria/CPU genérico, difícil de rastrear
    hasta este módulo.
    """
    global _hilos_recoleccion_vm_activos

    vm_info, log_func = args
    ip            = vm_info.get("ip", "?")
    nombre_manual = vm_info.get("nombre", "").strip()
    es_linux      = vm_info.get("os", "windows") == "linux"

    resultado_holder = [None]

    def _worker():
        global _hilos_recoleccion_vm_activos
        with _lock_hilos_recoleccion_vm:
            _hilos_recoleccion_vm_activos += 1
        try:
            if es_linux:
                resultado_holder[0] = _recolectar_ssh_interno(vm_info, log_func)
            else:
                resultado_holder[0] = _recolectar_wmi_interno(vm_info, log_func)
        finally:
            with _lock_hilos_recoleccion_vm:
                _hilos_recoleccion_vm_activos -= 1

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=90)

    if resultado_holder[0] is None:
        protocolo = "SSH" if es_linux else "WMI"
        with _lock_hilos_recoleccion_vm:
            hilos_activos = _hilos_recoleccion_vm_activos
        if log_func:
            log_func(f"⏱️ Timeout {protocolo} ({ip}): no respondió en 90s. "
                      f"Hilos de recolección todavía activos (incluye huérfanos de timeouts previos): {hilos_activos}")
            if hilos_activos >= UMBRAL_ALERTA_HILOS_HUERFANOS:
                log_func(f"🚨 Posible fuga de hilos WMI/SSH: {hilos_activos} hilos de recolección "
                         f"corriendo en simultáneo. Si esto crece ciclo tras ciclo, considerar reiniciar el servicio.")
        return {
            "id":                nombre_manual if nombre_manual else ip,
            "type":              vm_info.get("type", "vm"),
            "os":                "linux" if es_linux else "windows",
            "state":             "Offline",
            "state_reason":      "ssh_timeout" if es_linux else "wmi_timeout",
            "telemetry":         {},
            "storage":           [],
            "application_layer": {"services": []},
        }

    return resultado_holder[0]

# ---------------------------------------------------------------------------
# NETWORK / INTERNET HEALTH (PASIVO + LATENCIA)
# ---------------------------------------------------------------------------
def obtener_salud_red_pasiva(host_nube="tecnomonitor.tecnoimagen.com.ar", puerto=443, log_func=None):
    """
    Mide el uso real de la placa de red en el último segundo y calcula la latencia
    TCP hacia el servidor en la nube sin saturar el ancho de banda.
    """
    try:
        # 1. Medición Pasiva de Tráfico (Velocidad actual en la placa de red)
        io_inicio = psutil.net_io_counters()
        time.sleep(1.0)  # Tomamos una muestra exacta de 1 segundo
        io_fin = psutil.net_io_counters()
        
        # Calculamos Megabits por segundo (Mbps)
        # 1 Byte = 8 bits. Dividimos por 1.000.000 para obtener Megabits decimales.
        bajada_actual_mbps = round((io_fin.bytes_recv - io_inicio.bytes_recv) * 8 / 1_000_000, 2)
        subida_actual_mbps = round((io_fin.bytes_sent - io_inicio.bytes_sent) * 8 / 1_000_000, 2)
        
        # 2. Medición de Latencia a la Nube (TCP Ping)
        latencia_ms = -1
        estado_nube = "inaccesible"
        inicio_ping = time.time()
        
        try:
            # Intentamos un "saludo" TCP ligero al puerto 443
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(3.0)  # Si tarda más de 3 segundos, asumimos red colapsada
            s.connect((host_nube, puerto))
            s.close()
            
            latencia_ms = round((time.time() - inicio_ping) * 1000, 2)
            estado_nube = "conectado"
        except Exception as e:
            if log_func:
                log_func(f"⚠️ Alerta de red: No se pudo alcanzar la nube ({host_nube}): {e}")

        # Log visual para la consola de monitoreo
        if log_func:
            log_func(f"🌐 Tráfico actual: Subida {subida_actual_mbps} Mbps | Latencia Cloud: {latencia_ms} ms")

        return {
            "status": "ok",
            "upload_usage_mbps": subida_actual_mbps,
            "download_usage_mbps": bajada_actual_mbps,
            "cloud_latency_ms": latencia_ms,
            "cloud_status": estado_nube,
            "last_check": datetime.now().isoformat()
        }

    except Exception as e:
        if log_func:
            log_func(f"❌ Error crítico leyendo red pasiva: {e}")
        return {
            "status": "error",
            "error": str(e),
            "last_check": datetime.now().isoformat()
        }

# ---------------------------------------------------------------------------
# MONITOREO DE CERTIFICADOS SSL
# ---------------------------------------------------------------------------
def test_ssl_gui(data):
    """Función de prueba rápida para la interfaz gráfica."""
    url_str = data.get("url", "").strip()
    if not url_str:
        return {"success": False, "msg": "URL vacía"}
    
    try:
        parsed = urlparse(url_str)
        host = parsed.hostname or url_str.replace("https://", "").replace("http://", "").split("/")[0]
        port = parsed.port or 443

        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # Lo leemos aunque esté vencido

        with socket.create_connection((host, port), timeout=5) as sock:
            with context.wrap_socket(sock, server_hostname=host) as ssock:
                
                # Pedimos el certificado en formato binario crudo (DER)
                cert_der = ssock.getpeercert(binary_form=True)
                if not cert_der:
                    return {"success": False, "msg": "El servidor no devolvió un certificado."}
                
                # Lo parseamos con la librería cryptography (ya instalada en el agente)
                cert = x509.load_der_x509_certificate(cert_der, default_backend())
                
                # cert.not_valid_after nos da un datetime en UTC
                expire_date = cert.not_valid_after
                days = (expire_date - datetime.utcnow()).days
                
                estado = "✅ Válido" if days > 30 else "⚠️ Próximo a vencer" if days > 0 else "❌ Vencido"
                return {"success": True, "msg": f"{estado} (Expira en {days} días)"}
                
    except Exception as e:
        return {"success": False, "msg": f"Error de conexión: {str(e)}"}

def obtener_certificados_ssl(ssl_configs, log_func=None):
    """
    Extrae las fechas de expiración de una lista de URLs leyendo el certificado 
    en formato binario crudo (DER) para evitar validaciones nativas de Python.
    """
    resultados = []
    meta_status = "ok"
    errores_globales = 0

    for item in ssl_configs:
        url_str = item.get("url", "").strip()
        if not url_str: 
            continue

        try:
            # Parsear la URL para obtener el host y el puerto
            parsed = urlparse(url_str)
            host = parsed.hostname or url_str.replace("https://", "").replace("http://", "").split("/")[0]
            port = parsed.port or 443

            # Configurar el contexto SSL para que no valide la cadena de confianza
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE

            with socket.create_connection((host, port), timeout=5) as sock:
                with context.wrap_socket(sock, server_hostname=host) as ssock:
                    
                    # Pedir el certificado en formato binario (DER)
                    cert_der = ssock.getpeercert(binary_form=True)
                    if not cert_der:
                        raise Exception("El servidor no presentó un certificado SSL/TLS.")

                    # Parsear el certificado usando la librería cryptography
                    cert = x509.load_der_x509_certificate(cert_der, default_backend())
                    
                    # Calcular días restantes
                    expire_date = cert.not_valid_after
                    days_remaining = (expire_date - datetime.utcnow()).days

                    # Clasificación semafórica del estado
                    if days_remaining < 0:
                        status = "CRITICAL" # Vencido
                    elif days_remaining < 7:
                        status = "CRITICAL" # Menos de una semana
                    elif days_remaining < 30:
                        status = "WARNING"  # Menos de un mes
                    else:
                        status = "OK"       # Saludable

                    # Extraer el Emisor (Issuer) buscando la Organización o el Common Name
                    issuer = "Desconocido"
                    for attribute in cert.issuer:
                        if attribute.oid == x509.oid.NameOID.ORGANIZATION_NAME or attribute.oid == x509.oid.NameOID.COMMON_NAME:
                            issuer = attribute.value
                            break

                    # Guardar el resultado exitoso
                    resultados.append({
                        "url": url_str,
                        "status": status,
                        "expiration_date": expire_date.isoformat() + "Z",
                        "days_remaining": days_remaining,
                        "issuer": issuer
                    })
                    
        except Exception as e:
            errores_globales += 1
            if log_func:
                log_func(f"⚠️ Error SSL ({url_str}): {e}")
            
            # Guardar el resultado de error para el servidor
            resultados.append({
                "url": url_str,
                "status": "ERROR",
                "last_error": str(e)[:100]
            })

    # Actualizar estado del meta-bloque
    if errores_globales > 0:
        meta_status = "partial"
    if errores_globales == len(ssl_configs) and len(ssl_configs) > 0:
        meta_status = "error"

    return resultados, meta_status, errores_globales

# ---------------------------------------------------------------------------
# ELASTIC SEARCH (SUITESTENSA LOGS)
# ---------------------------------------------------------------------------
def test_connection_elastic(data):
    try:
        host = data.get("host", "").strip()
        port = data.get("port", 9200)
        url = f"{_esquema_elastic(data)}://{host}:{port}/"
        auth = HTTPBasicAuth(data.get("user", ""), data.get("pass", "")) if data.get("user") else None

        r = requests.get(url, auth=auth, timeout=5, verify=False)
        if r.status_code == 200:
            return {"success": True, "msg": "Conexión a ElasticSearch OK"}
        return {"success": False, "msg": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"success": False, "msg": str(e)}

def recolectar_logs_elastic(elastic_cfg, hospital_id, global_interval_minutes=5, log_func=None):
    resultado = {"events": [], "meta": {"scan_time": datetime.now().isoformat() + "Z", "new_alerts": 0}}

    # 1. CARGAR Y PRE-COMPILAR REGLAS
    rules = []
    if os.path.exists(RULES_FILE) and _rules_json_integro(log_func):
        try:
            with open(RULES_FILE, 'r', encoding='utf-8') as f:
                raw_rules = json.load(f)
                for r in raw_rules:
                    if 'regex' in r and r['regex']:
                        r['compiled_regex'] = re.compile(r['regex'], re.I | re.S)
                        rules.append(r)
        except Exception as e:
            if log_func: log_func(f"⚠️ Error cargando rules.json: {e}")

    # 2. LEER CHECKPOINT (por hospital_id, ver _ruta_checkpoint_elastic)
    last_ts = None
    checkpoint_path = _ruta_checkpoint_elastic(hospital_id)
    if os.path.exists(checkpoint_path):
        try:
            with open(checkpoint_path, 'r') as f:
                last_ts = f.read().strip()
        except: pass

    if not last_ts:
        # Usamos el intervalo global del agente en lugar del 'lookback' específico
        last_ts = f"now-{global_interval_minutes}m"

    if log_func:
        log_func(f"🔍 Consultando ElasticSearch desde: {last_ts}")

    # 3. PAGINACIÓN CON SEARCH_AFTER (Escalabilidad)
    host = elastic_cfg.get("host", "").strip()
    port = elastic_cfg.get("port", 29200)
    index_pattern = elastic_cfg.get("index_pattern", "se-es-logging-*")
    url = f"{_esquema_elastic(elastic_cfg)}://{host}:{port}/{index_pattern}/_search"
    auth = HTTPBasicAuth(elastic_cfg.get("user", ""), elastic_cfg.get("pass", "")) if elastic_cfg.get("user") else None

    all_hits = []
    search_after = None
    batch_size = 1000
    # Tope de seguridad (ver docs/PLAN_MEJORAS_V4.5.md §4.2): sin esto, una
    # caída larga de Elastic (o del agente) podía dejar una ventana enorme
    # pendiente y el ciclo se colgaba paginando sin límite. Cortar acá no
    # pierde nada — el checkpoint solo avanza hasta el último documento
    # efectivamente procesado (`newest_ts` más abajo), así que lo que quede
    # afuera de este corte se retoma en el próximo ciclo.
    MAX_PAGINAS_ELASTIC = 200
    MAX_SEGUNDOS_PAGINACION_ELASTIC = 60
    paginas = 0
    inicio_paginacion = time.time()

    try:
        while True:
            payload = {
                "size": batch_size,
                "query": {"bool": {"must": [
                    {"terms": {"level.keyword": ["Error", "Fatal", "Critical"]}},
                    {"range": {"@timestamp": {"gt": last_ts}}}
                ]}},
                "sort": [{"@timestamp": {"order": "asc"}}, {"_id": {"order": "asc"}}]
            }

            if search_after:
                payload["search_after"] = search_after

            resp = requests.post(url, json=payload, auth=auth, timeout=15, verify=False)
            resp.raise_for_status()
            data = resp.json()
            hits = data.get('hits', {}).get('hits', [])

            if not hits:
                break

            all_hits.extend(hits)
            paginas += 1

            if len(hits) < batch_size:
                break

            if paginas >= MAX_PAGINAS_ELASTIC:
                if log_func:
                    log_func(f"⚠️ Elastic: tope de {MAX_PAGINAS_ELASTIC} páginas alcanzado, "
                              f"se corta y se continúa en el próximo ciclo.")
                break

            if time.time() - inicio_paginacion > MAX_SEGUNDOS_PAGINACION_ELASTIC:
                if log_func:
                    log_func(f"⚠️ Elastic: tope de {MAX_SEGUNDOS_PAGINACION_ELASTIC}s de paginación "
                              f"alcanzado, se corta y se continúa en el próximo ciclo.")
                break

            search_after = hits[-1].get('sort')

        if log_func:
            log_func(f"✅ ElasticSearch: {len(all_hits)} nuevos documentos extraídos.")
            
    except Exception as e:
        if log_func: log_func(f"❌ Error de conexión a ElasticSearch: {e}")
        raise e 

    # 4. PROCESAR REGLAS (Memoria Temporal)
    grouped_events = {}
    newest_ts = last_ts
    
    for log in all_hits:
        src = log['_source']
        msg = src.get('message', '')
        srv = src.get('Process', 'Unknown')
        ts = src.get('@timestamp')
        
        if ts > newest_ts:
            newest_ts = ts
        
        matched = False
        for rule in rules:
            target_match = (rule.get('service_target') == "*" or rule.get('service_target') in srv)
            
            if target_match and rule.get('compiled_regex') and rule['compiled_regex'].search(msg):
                rid = rule.get('id')
                if rid not in grouped_events:
                    # Guardamos metadata extra temporalmente para el laboratorio
                    grouped_events[rid] = {
                        "rule_id": rid, "count": 1,
                        "first_seen": ts, "last_seen": ts, "services_affected": [srv],
                        "sample_evidence": msg[:250]
                    }
                else:
                    grouped_events[rid]["count"] += 1
                    grouped_events[rid]["last_seen"] = ts
                    if srv not in grouped_events[rid]["services_affected"]: 
                        grouped_events[rid]["services_affected"].append(srv)
                matched = True
                break
                
        if not matched:
            rid = "UNKNOWN-ERR-99"
            if rid not in grouped_events:
                grouped_events[rid] = {
                    "rule_id": rid, "count": 1,
                    "first_seen": ts, "last_seen": ts, "services_affected": [srv],
                    "sample_evidence": msg[:250]
                }
            else:
                grouped_events[rid]["count"] += 1
                grouped_events[rid]["last_seen"] = ts
                if srv not in grouped_events[rid]["services_affected"]: 
                    grouped_events[rid]["services_affected"].append(srv)

    # 5. GESTIÓN DEL LABORATORIO LOCAL (Mantiene evidencia para analizar en el equipo)
    unknowns_detected = 0
    lab_data = {"patterns": {}}
    if os.path.exists(UNKNOWNS_LAB_FILE):
        try:
            with open(UNKNOWNS_LAB_FILE, 'r', encoding='utf-8') as f:
                lab_data = json.load(f)
        except: pass

    for ev in grouped_events.values():
        if ev["rule_id"] == "UNKNOWN-ERR-99":
            pattern_key = ev["sample_evidence"][:100] 
            if pattern_key not in lab_data["patterns"]:
                lab_data["patterns"][pattern_key] = {
                    "first_detected": ev["first_seen"], "last_detected": ev["last_seen"],
                    "total_hits": ev["count"], "services": ev["services_affected"],
                    "full_sample": ev["sample_evidence"]
                }
                unknowns_detected += 1
            else:
                lab_data["patterns"][pattern_key]["total_hits"] += ev["count"]
                lab_data["patterns"][pattern_key]["last_detected"] = ev["last_seen"]

    if unknowns_detected > 0:
        if log_func: log_func(f"🤖 Laboratorio: {unknowns_detected} nuevos patrones de error registrados localmente.")
        try:
            with open(UNKNOWNS_LAB_FILE, 'w', encoding='utf-8') as f:
                json.dump(lab_data, f, indent=4)
        except: pass

    # 6. FORMATEO FINAL DEL PAYLOAD (Se reduce a la mínima expresión requerida)
    resultado["events"] = [
        {
            "rule_id": ev["rule_id"],
            "count": ev["count"]
        }
        for ev in grouped_events.values()
    ]
    
    resultado["meta"]["new_alerts"] = len(resultado["events"])
    
    if all_hits:
        resultado["_checkpoint_to_save"] = newest_ts
        
    return resultado

# ---------------------------------------------------------------------------
# CICLO PRINCIPAL DEL AGENTE
# ---------------------------------------------------------------------------
def ejecutar_ciclo_agente(config, log_callback=None):
    """
    Recolecta todos los módulos habilitados, construye el envelope y lo envía.
    Incluye collection_meta para que el servidor distinga módulos desactivados.
    """
    collection_meta = {
        "proxmox": {"enabled": config.get("enabled_proxmox", False), "status": "disabled"},
        "idrac":   {"enabled": config.get("enabled_idrac",   False), "status": "disabled"},
        "wmi":     {"enabled": config.get("enabled_vms",     False), "status": "disabled"},
        "sql":     {"enabled": config.get("enabled_sql",     False), "status": "disabled"},
        "mirth":   {"enabled": config.get("enabled_mirth",   False), "status": "disabled"},
        "ssl_monitoring": {"enabled": config.get("enabled_ssl", False), "status": "disabled"}, # NUEVO
        # --- NUEVO v4.3, granular desde v4.6 (ver _logs_suitestensa_habilitado) ---
        "suitestensa_logs": {"enabled": _logs_suitestensa_habilitado(config), "status": "disabled"},
        # --- NUEVO v4.4: permite distinguir "apagado" de "activo sin datos" ---
        "dicom_routing": {"enabled": _dicom_routing_habilitado(config), "status": "disabled"},
    }

    reporte = {
        "envelope": {
            "schema_version": _schema_version_efectiva(log_func=log_callback),
            "agent_version":  AGENT_VERSION,
            "hospital_id":    config.get("hospital_id", "UNKNOWN"),
            "timestamp":      datetime.now().isoformat(),
        },
        "collection_meta":  collection_meta,
        "software_monitoring": {},
        "physical_layer":   {},
        "virtual_layer":    [],
    }

    # --- 1. Capa física: Proxmox o VMware ---
    if config.get("enabled_proxmox"):
        hyper_cfg  = config.get("proxmox", {})
        hyper_type = hyper_cfg.get("type", "proxmox")
        try:
            if hyper_type == "vmware":
                # Sobrescribe la capa física con la estructura base del host
                reporte["physical_layer"] = obtener_vmware_layer(hyper_cfg, log_func=log_callback)
            else:
                reporte["physical_layer"] = obtener_physical_layer(hyper_cfg)

            collection_meta["proxmox"]["status"] = "ok"
        except Exception as e:
            collection_meta["proxmox"]["status"] = "error"
            collection_meta["proxmox"]["error"]  = str(e)
            if log_callback:
                log_callback(f"❌ Error capa física ({hyper_type}): {e}")

    # --- 2. Capa física: iDRAC (Sensores y Storage) ---
    if config.get("enabled_idrac"):
        idrac_cfg = config.get("idrac", {})
        try:
            # Aseguramos que la llave exista por si VMware falló o está apagado
            if "physical_layer" not in reporte or not reporte["physical_layer"]:
                reporte["physical_layer"] = {"host_info": {}, "telemetry": {}}

            reporte["physical_layer"]["sensors"] = obtener_sensors_idrac(idrac_cfg)
            reporte["physical_layer"]["storage_layer"] = obtener_storage_fisico_v3(idrac_cfg, log_callback)
            collection_meta["idrac"]["status"] = "ok"
        except Exception as e:
            collection_meta["idrac"]["status"] = "error"
            collection_meta["idrac"]["error"] = str(e)
            if log_callback:
                log_callback(f"❌ Error iDRAC: {e}")

    # --- 3. Capa Física: Network & Latency ---
    if "physical_layer" not in reporte:
        reporte["physical_layer"] = {}
        
    # Agregamos la salud de red sin pisar lo que trajo VMware e iDRAC
    reporte["physical_layer"]["network_health"] = obtener_salud_red_pasiva(
        host_nube="tecnomonitor.tecnoimagen.com.ar", 
        puerto=443,
        log_func=log_callback
    )

    # --- 4. Capa virtual: VMs/WS via WMI ---
    if config.get("enabled_vms") and config.get("vms"):
        try:
            with ThreadPoolExecutor(max_workers=5) as ex:
                wmi_results = list(ex.map(
                    obtener_vm_data,
                    [(vm, log_callback) for vm in config.get("vms", [])],
                ))
            reporte["virtual_layer"].extend(wmi_results)
            errores_wmi = sum(1 for v in wmi_results if v.get("state_reason") not in ("ok", "port_closed"))
            collection_meta["wmi"]["status"] = "partial" if errores_wmi else "ok"
            collection_meta["wmi"]["total"]  = len(wmi_results)
            collection_meta["wmi"]["errors"] = errores_wmi
        except Exception as e:
            collection_meta["wmi"]["status"] = "error"
            collection_meta["wmi"]["error"]  = str(e)
            if log_callback:
                log_callback(f"❌ Error capa WMI: {e}")

    # --- 5. Métricas SQL (KPIs de Negocio) ---
    if "_sql_data_payload" in config:
        payload = config["_sql_data_payload"]
        reporte["application_metrics"] = payload.get("application_metrics", payload)
        collection_meta["sql"]["status"] = "ok"
        collection_meta["sql"]["block_start"] = payload.get("application_metrics", {}).get("start_time_extraction", "")
        collection_meta["sql"]["block_end"]   = payload.get("application_metrics", {}).get("end_time_extraction", "")

    # --- 5.5. Software Monitoring: Autoenrute DICOM ---
    # Dos caminos independientes, igual que los KPIs de negocio (§5): directo
    # a SQL Server (mismo host que extraer_metricas_sql, corre EN CADA ciclo
    # -- sin checkpoint ni ventana, es una foto del estado actual de las
    # reglas) o vía ElasticSearch. Si ambos están activos, gana Elastic
    # (mismo criterio de prioridad que RIS/PACS/usuarios).
    elastic_cfg_dicom = config.get("elastic") or {}
    sql_cfg_dicom     = config.get("sql") or {}

    if config.get("enabled_elastic") and elastic_cfg_dicom.get("enabled_dicom_routing") and elastic_cfg_dicom.get("host"):
        try:
            dicom_data, d_status, d_errors = get_dicom_routing_queues(elastic_cfg_dicom, log_callback)
            reporte["software_monitoring"]["dicom_routing_queues"] = dicom_data
            collection_meta["dicom_routing"]["status"] = d_status
            collection_meta["dicom_routing"]["total"]  = len(dicom_data)
            collection_meta["dicom_routing"]["errors"] = d_errors
        except Exception as e:
            reporte["software_monitoring"]["dicom_routing_queues"] = []
            collection_meta["dicom_routing"]["status"] = "error"
            collection_meta["dicom_routing"]["error"]  = str(e)
            if log_callback:
                log_callback(f"❌ Error autoenrute DICOM (Elastic): {e}")
    elif config.get("enabled_sql") and sql_cfg_dicom.get("enabled_dicom_routing") and sql_cfg_dicom.get("host"):
        try:
            dicom_data, d_status, d_errors = obtener_dicom_routing_sql(sql_cfg_dicom, log_callback)
            reporte["software_monitoring"]["dicom_routing_queues"] = dicom_data
            collection_meta["dicom_routing"]["status"] = d_status
            collection_meta["dicom_routing"]["total"]  = len(dicom_data)
            collection_meta["dicom_routing"]["errors"] = d_errors
        except Exception as e:
            reporte["software_monitoring"]["dicom_routing_queues"] = []
            collection_meta["dicom_routing"]["status"] = "error"
            collection_meta["dicom_routing"]["error"]  = str(e)
            if log_callback:
                log_callback(f"❌ Error autoenrute DICOM (SQL): {e}")
    else:
        reporte["software_monitoring"]["dicom_routing_queues"] = []

    # --- 6. Software Monitoring: Mirth Connect ---
    if config.get("enabled_mirth") and config.get("mirth_servers"):
        # Llamamos a mirth_collector en lugar de la función local
        mirth_data, m_status, m_errors = mirth_collector.recolectar_mirth(config["mirth_servers"], log_callback)
        reporte["software_monitoring"]["mirth"] = mirth_data
        collection_meta["mirth"]["status"] = m_status
        collection_meta["mirth"]["total"]  = len(config["mirth_servers"])
        collection_meta["mirth"]["errors"] = m_errors

    # --- 6.5. Software Monitoring: Certificados SSL ---
    if config.get("enabled_ssl") and config.get("ssl_urls"):
        ssl_data, ssl_status, ssl_errors = obtener_certificados_ssl(config["ssl_urls"], log_callback)
        reporte["software_monitoring"]["ssl_certificates"] = ssl_data
        collection_meta["ssl_monitoring"]["status"] = ssl_status
        collection_meta["ssl_monitoring"]["total"] = len(config["ssl_urls"])
        collection_meta["ssl_monitoring"]["errors"] = ssl_errors

    # --- 6.8. Software Monitoring: Logs de Suitestensa (ElasticSearch) ---
    if _logs_suitestensa_habilitado(config):
        # Ver docs/PLAN_MEJORAS_V4.5.md §3.3: HTTPS es opcional (retrocompatible,
        # default apagado), pero eso no debe ser un riesgo silencioso — se
        # avisa en cada ciclo mientras el hospital siga en HTTP plano.
        if not config["elastic"].get("use_https") and log_callback:
            log_callback("⚠️ ElasticSearch configurado sin HTTPS (texto plano) — "
                         "considerar activar \"Usar HTTPS\" en la tarjeta de Elastic si el clúster lo soporta.")
        try:
            # 1. Extraemos el intervalo de configuración (o usamos 5 por defecto)
            intervalo_global = int(config.get("interval_minutes", 5))
            
            # 2. Pasamos el intervalo_global a la función
            elastic_data = recolectar_logs_elastic(config["elastic"], config.get("hospital_id"), intervalo_global, log_callback)
            
            # Guardamos el checkpoint en memoria para persistirlo post-envío
            if "_checkpoint_to_save" in elastic_data:
                config["_elastic_checkpoint_to_save"] = elastic_data.pop("_checkpoint_to_save")
            
            # Formamos el JSON final
            reporte["software_monitoring"]["suitestensa_logs"] = {
                "scan_time": elastic_data["meta"]["scan_time"],
                "events": elastic_data["events"]
            }
            collection_meta["suitestensa_logs"]["status"] = "ok"
            collection_meta["suitestensa_logs"]["new_alerts"] = elastic_data["meta"]["new_alerts"]
            collection_meta["suitestensa_logs"]["errors"] = 0
            
        except Exception as e:
            collection_meta["suitestensa_logs"]["status"] = "error"
            collection_meta["suitestensa_logs"]["error"] = str(e)
            
    # --- 7. Envío al servidor central ---
    try:
        r = requests.post(
            config.get("central_url"),
            json=reporte,
            timeout=25,
            verify=False,
            headers={"Authorization": f"Bearer {config.get('auth_token', '')}"},
        )
        r.raise_for_status()

        # Checkpoint guardado SOLO aquí, tras confirmación de envío exitoso
        if "_sql_data_payload" in config:
            checkpoint_dt = config["_sql_data_payload"].get("_checkpoint_to_save")
            if checkpoint_dt:
                save_checkpoint(config.get("hospital_id"), checkpoint_dt, log_func=log_callback)
                if log_callback:
                    log_callback(f"💾 Checkpoint SQL guardado: {checkpoint_dt.strftime('%Y-%m-%d %H:%M:%S')}")

        # --- NUEVO v4.3: Checkpoint de ElasticSearch ---
        if "_elastic_checkpoint_to_save" in config:
            el_ts = config.pop("_elastic_checkpoint_to_save")
            try:
                checkpoint_path = _ruta_checkpoint_elastic(config.get("hospital_id"))
                tmp = checkpoint_path + ".tmp"
                with open(tmp, 'w') as f:
                    f.write(el_ts)
                os.replace(tmp, checkpoint_path)
                if log_callback: log_callback(f"💾 Checkpoint Elastic guardado: {el_ts}")
            except Exception as e:
                if log_callback:
                    log_callback(f"⚠️ No se pudo guardar el checkpoint Elastic de [{config.get('hospital_id')}]: {e}")
        # -----------------------------------------------

        return {"status": "OK", "timestamp": datetime.now().strftime("%H:%M:%S")}

    except requests.exceptions.HTTPError as e:
        status_code = e.response.status_code if e.response is not None else None
        if status_code == 401:
            # Ver docs/PLAN_MEJORAS_V4.5.md §2, riesgo 4: antes esto caía en
            # el mismo bloque genérico que una caída de red, indistinguible
            # en el log de un problema de conectividad.
            mensaje = ("🔒 401 No autorizado: el token fue rechazado o no corresponde al "
                       "hospital_id configurado. Revisar auth_token en la GUI, no un problema de red.")
            if log_callback:
                log_callback(mensaje)
            return {
                "status":      "Error",
                "error":       mensaje,
                "http_status": 401,
                "timestamp":   datetime.now().strftime("%H:%M:%S"),
            }
        if log_callback:
            log_callback(f"❌ Error HTTP {status_code} al enviar el reporte: {e}")
        return {
            "status":      "Error",
            "error":       str(e),
            "http_status": status_code,
            "timestamp":   datetime.now().strftime("%H:%M:%S"),
        }

    except Exception as e:
        return {
            "status":    "Error",
            "error":     str(e),
            "timestamp": datetime.now().strftime("%H:%M:%S"),
        }

# ---------------------------------------------------------------------------
# MIRTH CONNECT
# ---------------------------------------------------------------------------
def test_connection_mirth(config):
    url = config.get("url", "").rstrip('/')
    try:
        s = requests.Session()
        s.headers.update({'X-Requested-With': 'OpenAPI', 'Accept': 'application/json'})
        r = s.post(f"{url}/api/users/_login", data={'username': config.get("user", ""), 'password': config.get("pass", "")}, verify=False, timeout=5)
        
        if r.status_code == 200:
            s.post(f"{url}/api/users/_logout", verify=False, timeout=2)
            return {"success": True, "msg": "Conexión a Mirth Connect OK"}
        return {"success": False, "msg": f"Credenciales inválidas (HTTP {r.status_code})"}
    except Exception as e:
        return {"success": False, "msg": str(e)}