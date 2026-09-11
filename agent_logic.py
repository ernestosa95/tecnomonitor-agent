import requests
import wmi
import pythoncom
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

def parse_wmi_date(wmi_date):
    try:
        return datetime.strptime(wmi_date.split('.')[0], "%Y%m%d%H%M%S")
    except Exception:
        return datetime.now()

def _dicom_routing_habilitado(config):
    """
    Compatibilidad de configuración (v4.3 -> v4.4).

    Hasta v4.3 el flag de autoenrute vivía en config["sql"]["enabled_dicom_routing"],
    porque el colector leía SQL Server directo. Desde v4.4 lee ElasticSearch y el
    flag pasó a config["elastic"]["enabled_dicom_routing"].

    Los agentes ya desplegados tienen el valor viejo guardado; sin este fallback
    dejarían de reportar en silencio hasta que alguien reguarde la configuración.
    Se puede eliminar cuando todos los hospitales estén confirmados en v4.4.
    """
    elastic = config.get("elastic") or {}
    if "enabled_dicom_routing" in elastic:
        return bool(elastic.get("enabled_dicom_routing"))
    return bool((config.get("sql") or {}).get("enabled_dicom_routing"))

# ---------------------------------------------------------------------------
# CHECKPOINT SQL — escritura atómica, guardado solo al confirmar envío exitoso
# ---------------------------------------------------------------------------
def get_last_checkpoint():
    if os.path.exists(SQL_CHECKPOINT_FILE):
        try:
            with open(SQL_CHECKPOINT_FILE, 'r') as f:
                return datetime.strptime(f.read().strip(), "%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    return None

def save_checkpoint(dt):
    """Escritura atómica: escribe en .tmp y renombra. Nunca deja el archivo a medias."""
    try:
        tmp = SQL_CHECKPOINT_FILE + ".tmp"
        with open(tmp, 'w') as f:
            f.write(dt.strftime("%Y-%m-%d %H:%M:%S"))
        os.replace(tmp, SQL_CHECKPOINT_FILE)
    except Exception:
        pass

def reset_checkpoint():
    for path in [SQL_CHECKPOINT_FILE, SQL_CHECKPOINT_FILE + ".tmp"]:
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass

# ---------------------------------------------------------------------------
# MÉTRICAS SQL (NIVEL NEGOCIO - EJECUCIÓN LENTA)
# ---------------------------------------------------------------------------
def extraer_metricas_sql(sql_config, log_func=None):
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

        executions_per_day = int(sql_config.get("executions_per_day", 3))
        if executions_per_day <= 0:
            executions_per_day = 3
        interval_hours = 24.0 / executions_per_day

        ahora             = datetime.now()
        ultimo_checkpoint = get_last_checkpoint()

        if not ultimo_checkpoint:
            historical_start = sql_config.get("historical_start_date")
            if historical_start:
                try:
                    target_start_time = datetime.strptime(historical_start, "%Y-%m-%d")
                    if log_func:
                        log_func(f"🕰️ INICIANDO BACKFILL HISTÓRICO desde {historical_start}")
                except Exception:
                    target_start_time = ahora.replace(hour=0, minute=0, second=0, microsecond=0)
            else:
                target_start_time = ahora.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            target_start_time = ultimo_checkpoint

        target_end_time = target_start_time + timedelta(hours=interval_hours)

        if target_end_time > ahora:
            return None

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

        data_json["application_metrics"]["extraction_interval_hours"] = interval_hours
        data_json["application_metrics"]["start_time_extraction"]     = start_date_sql
        data_json["application_metrics"]["end_time_extraction"]       = end_date_sql

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

    url  = f"http://{host}:{port}/{index_name}/_search"
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
        resp = requests.post(url, json=payload, auth=auth, timeout=15)
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
    Exponer en main_gui.py:

        @eel.expose
        def test_dicom_index_gui(data):
            return agent_logic.test_connection_dicom_index(data)
    """
    host  = (data.get("host") or "").strip()
    port  = data.get("port", 29200)
    index = (data.get("dicom_index") or "ext_dicom_queues").strip()

    if not host:
        return {"success": False, "msg": "Host de ElasticSearch no configurado"}

    auth = HTTPBasicAuth(data.get("user", ""), data.get("pass", "")) if data.get("user") else None
    url  = f"http://{host}:{port}/{index}/_search"

    try:
        r = requests.post(
            url,
            json={"size": 1, "docvalue_fields": [{"field": "@timestamp", "format": "epoch_millis"}]},
            auth=auth, timeout=8
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
                sensors["temperatures"].append({"name": t.get("Name"), "value": t.get("ReadingCelsius"), "unit": "C", "status": "OK"})
            for f in d.get("Fans", []):
                sensors["fans"].append({"name": f.get("FanName") or f.get("Name"), "value": f.get("Reading"), "unit": "RPM", "status": "OK"})
        r_pw = requests.get(f"{base}/Power", auth=auth, verify=False, timeout=5)
        if r_pw.status_code == 200:
            d = r_pw.json()
            sensors["power"]["watts_current"] = safe_int(d.get("PowerControl", [{}])[0].get("PowerConsumedWatts"))
            for ps in d.get("PowerSupplies", []):
                sensors["power"]["supplies"].append({"name": ps.get("Name"), "watts": safe_float(ps.get("LastPowerOutputWatts")), "status": "OK"})
    except Exception as e:
        sensors["status"] = f"error: {str(e)}"
    return sensors

def obtener_storage_fisico_v3(config, log_func=None):
    """
    Recorre la API Redfish de iDRAC de forma PARALELA.
    Evita alcanzar el timeout global de 60s en servidores de PACS con múltiples arreglos RAID.
    """
    import concurrent.futures

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
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
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
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
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
        vm_obj["state"]        = "Offline"
        vm_obj["state_reason"] = "wmi_error"
        vm_obj["wmi_error"]    = str(e)
        if log_func:
            log_func(f"⚠️ Error WMI ({tipo_maquina.upper()}) {ip}: {e}")
    finally:
        pythoncom.CoUninitialize()

    return vm_obj


def obtener_vm_data(args):
    """
    Wrapper con timeout global de 90s por VM para evitar threads colgados.
    """
    vm_info, log_func = args
    ip            = vm_info.get("ip", "?")
    nombre_manual = vm_info.get("nombre", "").strip()

    resultado_holder = [None]

    def _worker():
        resultado_holder[0] = _recolectar_wmi_interno(vm_info, log_func)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=90)

    if resultado_holder[0] is None:
        if log_func:
            log_func(f"⏱️ Timeout WMI ({ip}): no respondió en 90s")
        return {
            "id":                nombre_manual if nombre_manual else ip,
            "type":              vm_info.get("type", "vm"),
            "state":             "Offline",
            "state_reason":      "wmi_timeout",
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
        url = f"http://{host}:{port}/"
        auth = HTTPBasicAuth(data.get("user", ""), data.get("pass", "")) if data.get("user") else None
        
        r = requests.get(url, auth=auth, timeout=5)
        if r.status_code == 200:
            return {"success": True, "msg": "Conexión a ElasticSearch OK"}
        return {"success": False, "msg": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"success": False, "msg": str(e)}

def recolectar_logs_elastic(elastic_cfg, global_interval_minutes=5, log_func=None):
    resultado = {"events": [], "meta": {"scan_time": datetime.now().isoformat() + "Z", "new_alerts": 0}}
    
    # 1. CARGAR Y PRE-COMPILAR REGLAS
    rules = []
    if os.path.exists(RULES_FILE):
        try:
            with open(RULES_FILE, 'r', encoding='utf-8') as f:
                raw_rules = json.load(f)
                for r in raw_rules:
                    if 'regex' in r and r['regex']:
                        r['compiled_regex'] = re.compile(r['regex'], re.I | re.S)
                        rules.append(r)
        except Exception as e:
            if log_func: log_func(f"⚠️ Error cargando rules.json: {e}")

    # 2. LEER CHECKPOINT
    last_ts = None
    if os.path.exists(ELASTIC_CHECKPOINT_FILE):
        try:
            with open(ELASTIC_CHECKPOINT_FILE, 'r') as f:
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
    url = f"http://{host}:{port}/{index_pattern}/_search"
    auth = HTTPBasicAuth(elastic_cfg.get("user", ""), elastic_cfg.get("pass", "")) if elastic_cfg.get("user") else None

    all_hits = []
    search_after = None
    batch_size = 1000 
    
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

            resp = requests.post(url, json=payload, auth=auth, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            hits = data.get('hits', {}).get('hits', [])
            
            if not hits:
                break 
                
            all_hits.extend(hits)
            
            if len(hits) < batch_size:
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
        # --- NUEVO v4.3 ---
        "suitestensa_logs": {"enabled": config.get("enabled_elastic", False), "status": "disabled"},
        # --- NUEVO v4.4: permite distinguir "apagado" de "activo sin datos" ---
        "dicom_routing": {"enabled": _dicom_routing_habilitado(config), "status": "disabled"},
    }

    reporte = {
        "envelope": {
            "schema_version": "4.3",
            "agent_version":  "4.4.0",
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
            reporte["physical_layer"]["storage"] = obtener_storage_fisico_v3(idrac_cfg, log_callback)
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

    # --- 5.5. Software Monitoring: Autoenrute DICOM (vía ElasticSearch) ---
    # El gate depende de la tarjeta Elastic, que es de donde sale la conexión.
    if (config.get("enabled_elastic")
            and _dicom_routing_habilitado(config)
            and config.get("elastic", {}).get("host")):
        try:
            dicom_data, d_status, d_errors = get_dicom_routing_queues(
                config.get("elastic"), log_callback
            )
            reporte["software_monitoring"]["dicom_routing_queues"] = dicom_data
            collection_meta["dicom_routing"]["status"] = d_status
            collection_meta["dicom_routing"]["total"]  = len(dicom_data)
            collection_meta["dicom_routing"]["errors"] = d_errors
        except Exception as e:
            reporte["software_monitoring"]["dicom_routing_queues"] = []
            collection_meta["dicom_routing"]["status"] = "error"
            collection_meta["dicom_routing"]["error"]  = str(e)
            if log_callback:
                log_callback(f"❌ Error autoenrute DICOM: {e}")
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
    if config.get("enabled_elastic") and config.get("elastic", {}).get("host"):
        try:
            # 1. Extraemos el intervalo de configuración (o usamos 5 por defecto)
            intervalo_global = int(config.get("interval_minutes", 5))
            
            # 2. Pasamos el intervalo_global a la función
            elastic_data = recolectar_logs_elastic(config["elastic"], intervalo_global, log_callback)
            
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
                save_checkpoint(checkpoint_dt)
                if log_callback:
                    log_callback(f"💾 Checkpoint SQL guardado: {checkpoint_dt.strftime('%Y-%m-%d %H:%M:%S')}")

        # --- NUEVO v4.3: Checkpoint de ElasticSearch ---
        if "_elastic_checkpoint_to_save" in config:
            el_ts = config.pop("_elastic_checkpoint_to_save")
            try:
                tmp = ELASTIC_CHECKPOINT_FILE + ".tmp"
                with open(tmp, 'w') as f:
                    f.write(el_ts)
                os.replace(tmp, ELASTIC_CHECKPOINT_FILE)
                if log_callback: log_callback(f"💾 Checkpoint Elastic guardado: {el_ts}")
            except Exception: pass
        # -----------------------------------------------

        return {"status": "OK", "timestamp": datetime.now().strftime("%H:%M:%S")}

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