"""
Chequeo de integridad de bases SQL Server (DBCC CHECKDB) tras un reinicio del servicio SQL.

Es una consulta costosa (minutos u horas), por eso solo corre ante un reinicio -- típicamente un
corte de energía abrupto -- y el resultado se manda UNA sola vez por reinicio, en
`software_monitoring.sql_integrity`. Plan y decisiones: docs/PLAN_CHECKDB_POST_REINICIO.md.

Dos caminos independientes (mismo criterio que autoenrute DICOM y los KPIs de RIS):

  * Elastic (principal): Logstash corre el CHECKDB (elk/ext_checkdb.conf, con el T-SQL inline) y decide del
    lado SQL si hubo un reinicio; el agente solo lee el índice y reenvía el último resultado.
  * SQL directo (excepción, hospitales sin Elastic): el agente detecta el reinicio y lanza un
    PROCESO TRABAJADOR desacoplado, porque el CHECKDB dura mucho más que un ciclo y en modo
    "tarea programada" cada ciclo es un proceso efímero que mataría cualquier hilo.

Si ambos caminos están activos gana Elastic.

Este módulo NO importa agent_logic (agent_logic lo importa a él): las rutas y helpers chicos se
replican acá para evitar el ciclo de imports. Nada de lo que hace acá puede tumbar el ciclo del
agente: `recolectar` atrapa cualquier excepción y devuelve un estado "error".

Diseño de un único escritor por archivo, para no necesitar locks entre procesos:
  * `.sql_integrity_state_<hospital>`   -> lo escribe SOLO el agente (línea base, corrida, enviado).
  * `.sql_integrity_results_<hospital>` -> lo escribe SOLO el trabajador (resultado por base).
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

import psutil
import pyodbc
import requests
from requests.auth import HTTPBasicAuth

import security

DATA_DIR = security.get_app_data_path()

TIPO_COMPLETO = "full"
TIPO_FISICO = "physical_only"
ESTADO_OK, ESTADO_ERROR, ESTADO_NO_ONLINE = "OK", "ERROR", "NOT_ONLINE"

# Las 26 bases de Extensa de la consulta manual del equipo. Editable por hospital
# (`sql.checkdb_databases`): las que no existan en el servidor se ignoran.
BASES_POR_DEFECTO = [
    "AspnetDB", "DBScheduler", "DicomedBroker", "DicomedBrokerStorico", "DICOMedP@CS",
    "ExtensaAnalytics", "ExtensaCardio", "ExtensaConnect", "ExtensaCustomPage",
    "ExtensaDataExport", "ExtensaGeneric", "ExtensaHistory", "ExtensaIntegration",
    "ExtensaIntegrationGateway", "ExtensaMPS", "ExtensaPACS", "ExtensaPatient",
    "ExtensaPublication", "ExtensaRadio", "ExtensaRT", "ExtensaVNA", "ExtensaWarehouse",
    "eXtensaWRK", "MediaProducerDB", "SL_UserAndConfig", "support",
]

MAX_DETALLE = 500              # largo máximo de `detail` por base (el servidor también lo recorta)
ESPERA_ASENTARSE_MIN = 10      # minutos tras el arranque de SQL antes de chequear
ESPERA_MAXIMA_MIN = 60         # hasta cuándo esperar que todas las bases estén ONLINE
MAX_LANZAMIENTOS = 4           # lanzamientos totales del trabajador por corrida (1 inicial + 3 reintentos)
ESPERA_RECONEXION_SEG = 30     # el trabajador espera esto entre reintentos de conexión
INTENTOS_RECONEXION = 10

_ARRANQUE_SQL = "SELECT create_date, GETDATE() FROM sys.databases WHERE name = N'tempdb'"
_BASES_SQL = "SELECT name, state_desc FROM sys.databases"
_RESUMEN_CHECKDB = re.compile(r"CHECKDB found (\d+) allocation errors and (\d+) consistency errors", re.I)


# ---------------------------------------------------------------------------
# CONFIGURACIÓN
# ---------------------------------------------------------------------------
def habilitado_elastic(config):
    el = config.get("elastic") or {}
    return bool(config.get("enabled_elastic") and el.get("enabled_checkdb") and el.get("host"))


def habilitado_sql(config):
    sq = config.get("sql") or {}
    return bool(config.get("enabled_sql") and sq.get("enabled_checkdb") and sq.get("host"))


def habilitado(config):
    """True si cualquiera de los dos caminos está activo (usado para `collection_meta`)."""
    return habilitado_elastic(config) or habilitado_sql(config)


def bases_configuradas(sql_cfg):
    """Lista de bases a chequear: la configurada (sin vacíos ni repetidas) o la de por defecto."""
    crudas = (sql_cfg or {}).get("checkdb_databases")
    if isinstance(crudas, str):
        crudas = re.split(r"[,;\n]", crudas)
    limpias, vistas = [], set()
    for b in crudas or []:
        b = str(b).strip()
        if b and b.lower() not in vistas:
            vistas.add(b.lower())
            limpias.append(b)
    return limpias or list(BASES_POR_DEFECTO)


def tipo_chequeo(sql_cfg):
    return TIPO_FISICO if str((sql_cfg or {}).get("checkdb_type", "")).lower() == TIPO_FISICO else TIPO_COMPLETO


def _entero(valor, por_defecto, minimo=0):
    try:
        return max(minimo, int(valor))
    except (TypeError, ValueError):
        return por_defecto


# ---------------------------------------------------------------------------
# ESTADO EN DISCO (un escritor por archivo)
# ---------------------------------------------------------------------------
def _sufijo_seguro(hospital_id):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(hospital_id or "default"))


def _ruta(prefijo, hospital_id):
    return os.path.join(DATA_DIR, f"{prefijo}_{_sufijo_seguro(hospital_id)}")


def ruta_estado(hospital_id):
    return _ruta(".sql_integrity_state", hospital_id)


def ruta_resultados(hospital_id):
    return _ruta(".sql_integrity_results", hospital_id)


def _leer_json(ruta):
    try:
        with open(ruta, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _escribir_json(ruta, data):
    tmp = ruta + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, ruta)


def cargar_estado(hospital_id):
    return _leer_json(ruta_estado(hospital_id))


def guardar_estado(hospital_id, estado):
    _escribir_json(ruta_estado(hospital_id), estado)


def cargar_resultados(hospital_id):
    return _leer_json(ruta_resultados(hospital_id))


def guardar_resultados(hospital_id, resultados):
    _escribir_json(ruta_resultados(hospital_id), resultados)


def _ahora_iso():
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# CONEXIÓN A SQL SERVER
# ---------------------------------------------------------------------------
def conectar(sql_cfg, base="master", timeout=10):
    """Misma estrategia de driver que obtener_dicom_routing_sql (driver 17 y, si falta, el viejo)."""
    def _cadena(driver):
        return (f"DRIVER={{{driver}}};SERVER={sql_cfg['host']};DATABASE={base};"
                f"UID={sql_cfg.get('user', '')};PWD={sql_cfg.get('pass', '')}")
    try:
        conn = pyodbc.connect(_cadena("ODBC Driver 17 for SQL Server"), timeout=timeout, autocommit=True)
    except pyodbc.Error:
        conn = pyodbc.connect(_cadena("SQL Server"), timeout=timeout, autocommit=True)
    return conn


def leer_servidor(conn):
    """(arranque_de_SQL, hora_actual_del_servidor, {base: state_desc}). El arranque sale de la
    fecha de creación de tempdb (se recrea en cada inicio del servicio) y no pide permisos
    especiales, a diferencia de sys.dm_os_sys_info."""
    cur = conn.cursor()
    cur.execute(_ARRANQUE_SQL)
    fila = cur.fetchone()
    arranque, ahora = fila[0], fila[1]
    cur.execute(_BASES_SQL)
    bases = {str(n): str(s) for n, s in cur.fetchall()}
    return arranque, ahora, bases


# ---------------------------------------------------------------------------
# CHECKDB (una base) -- usado por el trabajador
# ---------------------------------------------------------------------------
def _mensaje(fila):
    """MessageText de una fila de DBCC ... WITH TABLERESULTS (columna 4, índice 3)."""
    texto = getattr(fila, "MessageText", None)
    if texto is None:
        try:
            texto = fila[3]
        except (IndexError, TypeError):
            texto = ""
    return str(texto or "").strip()


def contar_errores(filas):
    """Cantidad de errores del CHECKDB. Si vino el resumen ("CHECKDB found N allocation errors and
    M consistency errors") se usa esa suma; si no, la cantidad de filas."""
    for fila in filas:
        m = _RESUMEN_CHECKDB.search(_mensaje(fila))
        if m:
            return int(m.group(1)) + int(m.group(2))
    return len(filas)


def armar_detalle(filas):
    """Resumen de CHECKDB (si vino) primero, y después los primeros mensajes, unidos y recortados a
    MAX_DETALLE: así el recorte nunca se lleva el resumen, que es lo más informativo."""
    mensajes = [m for m in (_mensaje(f) for f in filas) if m]
    resumen = next((m for m in mensajes if _RESUMEN_CHECKDB.search(m)), None)
    otros = [m for m in mensajes if m is not resumen][:2 if resumen else 3]
    return " | ".join(([resumen] if resumen else []) + otros)[:MAX_DETALLE]


def ejecutar_checkdb(conn, base, tipo):
    """Corre DBCC CHECKDB sobre una base. Devuelve el dict de resultado para el contrato.
    Sin tablas temporales ni columnas que trunquen (los problemas de la consulta manual): un
    fallo de una base queda como ERROR de esa base y no afecta a las demás."""
    consulta = (f"DBCC CHECKDB ([{base.replace(']', ']]')}]) WITH NO_INFOMSGS, ALL_ERRORMSGS, TABLERESULTS"
                + (", PHYSICAL_ONLY" if tipo == TIPO_FISICO else ""))
    t0 = time.time()
    estado, errores, detalle = ESTADO_OK, 0, ""
    try:
        cur = conn.cursor()
        cur.execute(consulta)
        try:
            filas = cur.fetchall()
        except pyodbc.ProgrammingError:      # sin result set: la base salió limpia
            filas = []
        if filas:
            estado, errores, detalle = ESTADO_ERROR, contar_errores(filas), armar_detalle(filas)
    except Exception as e:
        estado, errores, detalle = ESTADO_ERROR, 0, str(e)[:MAX_DETALLE]
    return {
        "db": base, "status": estado, "error_count": errores, "detail": detalle,
        "duration_s": int(time.time() - t0), "checked_at": _ahora_iso(),
    }


# ---------------------------------------------------------------------------
# TRABAJADOR (proceso desacoplado, camino SQL directo)
# ---------------------------------------------------------------------------
def comando_trabajador(hospital_id):
    if getattr(sys, "frozen", False):
        return [sys.executable, "--sql-integrity-worker", hospital_id]
    return [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "headless_service.py"),
            "--sql-integrity-worker", hospital_id]


def lanzar_trabajador(hospital_id):
    """Lanza el trabajador desacoplado del ciclo actual (sobrevive a que el proceso del ciclo
    termine, como en modo tarea programada). Devuelve el PID."""
    flags = 0
    if os.name == "nt":
        flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                 | subprocess.CREATE_NO_WINDOW)
    proc = subprocess.Popen(comando_trabajador(hospital_id), creationflags=flags, close_fds=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc.pid


def trabajador_vivo(resultados):
    """True si el trabajador anotado en el archivo de resultados sigue corriendo. Se compara también
    la hora de creación del proceso para no confundirlo con otro que reusó el mismo PID."""
    pid = resultados.get("pid")
    if not pid:
        return False
    try:
        proc = psutil.Process(int(pid))
        if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
            return False
        guardada = resultados.get("pid_create_time")
        return guardada is None or abs(proc.create_time() - float(guardada)) < 2
    except (psutil.Error, ValueError, TypeError):
        return False


def _log_worker(mensaje):
    """El trabajador escribe en su propio log (no en activity.log: dos procesos escribiendo el mismo
    archivo con rotación se pisan en Windows). Se rota a mano al pasar 1 MB."""
    ruta = os.path.join(DATA_DIR, "sql_integrity_worker.log")
    try:
        if os.path.exists(ruta) and os.path.getsize(ruta) > 1_000_000:
            os.replace(ruta, ruta + ".1")
        with open(ruta, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {mensaje}\n")
    except OSError:
        pass


def _reconectar(sql_cfg, log):
    ultimo = None
    for _ in range(INTENTOS_RECONEXION):
        try:
            return conectar(sql_cfg)
        except Exception as e:
            ultimo = e
            log(f"⚠️ Sin conexión a SQL ({e}); reintento en {ESPERA_RECONEXION_SEG}s.")
            time.sleep(ESPERA_RECONEXION_SEG)
    raise ultimo


def _leer_con_reconexion(sql_cfg, conn, log):
    """Lee arranque/bases; si la conexión se cortó (típico tras un CHECKDB de horas), reconecta.
    Devuelve (conn, arranque, ahora, bases)."""
    if conn is not None:
        try:
            return (conn,) + leer_servidor(conn)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
    conn = _reconectar(sql_cfg, log)
    return (conn,) + leer_servidor(conn)


def trabajador_main(perfil, log_func=None):
    """Cuerpo del proceso trabajador. Chequea base por base, guardando cada resultado apenas lo
    tiene, y se puede relanzar: retoma desde la primera base que falte. Se detiene si SQL se
    reinició de nuevo (el resultado de este arranque ya no importa)."""
    log = log_func or _log_worker
    hid = (perfil or {}).get("hospital_id")
    sql_cfg = (perfil or {}).get("sql") or {}
    estado = cargar_estado(hid)
    corrida = estado.get("run") or {}
    if corrida.get("status") != "running":
        log(f"[{hid}] No hay una corrida en curso; el trabajador termina.")
        return

    arranque_esperado = corrida["boot"]
    res = cargar_resultados(hid)
    if res.get("boot") != arranque_esperado:
        res = {"boot": arranque_esperado, "results": []}
    res.update(status="running", pid=os.getpid(), error=None)
    try:
        res["pid_create_time"] = psutil.Process(os.getpid()).create_time()
    except psutil.Error:
        res["pid_create_time"] = None
    guardar_resultados(hid, res)

    tipo = corrida.get("check_type") or TIPO_COMPLETO
    hechas = {r["db"] for r in res["results"]}
    log(f"[{hid}] Trabajador iniciado (arranque {arranque_esperado}, {tipo}): "
        f"{len(hechas)}/{len(corrida['databases'])} bases ya hechas.")
    conn = None
    try:
        for base in corrida["databases"]:
            if base in hechas:
                continue
            conn, arranque, _ahora, bases = _leer_con_reconexion(sql_cfg, conn, log)
            if arranque.isoformat(timespec="seconds") != arranque_esperado:
                res["status"] = "aborted"
                res["error"] = "SQL se reinició durante el chequeo"
                guardar_resultados(hid, res)
                log(f"[{hid}] SQL se reinició durante el chequeo; se abandona esta corrida.")
                return
            estado_base = bases.get(base)
            if estado_base != "ONLINE":
                resultado = {"db": base, "status": ESTADO_NO_ONLINE, "error_count": 0,
                             "detail": estado_base or "no encontrada", "duration_s": 0, "checked_at": _ahora_iso()}
            else:
                log(f"[{hid}] CHECKDB {base} ({tipo})...")
                # Sin timeout de consulta (pyodbc lo deja en 0): un CHECKDB puede durar horas.
                resultado = ejecutar_checkdb(conn, base, tipo)
                log(f"[{hid}] {base}: {resultado['status']} ({resultado['error_count']} errores, {resultado['duration_s']}s)")
            res["results"].append(resultado)
            guardar_resultados(hid, res)
        res["status"] = "done"
        guardar_resultados(hid, res)
        log(f"[{hid}] Chequeo terminado: {len(res['results'])} bases.")
    except Exception as e:
        res["status"] = "aborted"
        res["error"] = str(e)[:300]
        guardar_resultados(hid, res)
        log(f"[{hid}] ❌ El trabajador se detuvo: {e}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# RESULTADO PARA EL REPORTE
# ---------------------------------------------------------------------------
def _payload(boot, tipo, origen, bases):
    return {"sqlserver_start_time": boot, "check_type": tipo, "source": origen, "databases": bases}


def _respuesta(payload=None, status="ok", extra=None, commit=None, origen=None):
    return {"payload": payload, "status": status, "extra": extra or {}, "commit": commit, "source": origen}


# ---------------------------------------------------------------------------
# CAMINO SQL DIRECTO (agente detecta y lanza el trabajador)
# ---------------------------------------------------------------------------
def recolectar_sql(config, log_func=None):
    log = log_func or (lambda m: None)
    hid = config.get("hospital_id")
    sql_cfg = config.get("sql") or {}
    estado = cargar_estado(hid)
    corrida = estado.get("run") or {}

    # 1) Un resultado terminado y sin enviar va primero, sin necesidad de hablar con SQL.
    if corrida.get("status") == "done" and not corrida.get("sent"):
        return _enviar_resultado_sql(hid, corrida)

    # 2) Estado actual del servidor.
    try:
        conn = conectar(sql_cfg)
        try:
            arranque, ahora_srv, bases_srv = leer_servidor(conn)
        finally:
            conn.close()
    except Exception as e:
        log(f"⚠️ Integridad SQL: no se pudo consultar SQL Server ({e}).")
        vigente = corrida.get("status") in ("pending", "running")
        return _respuesta(status="running" if vigente else "error", extra={"error": str(e)[:200]}, origen="sql")

    boot = arranque.isoformat(timespec="seconds")

    # 3) Línea base: la primera vez (instalación o actualización del agente) solo se registra el
    # arranque actual. No se dispara ningún chequeo para no lanzar una carga enorme al actualizar.
    if estado.get("baseline_boot") is None:
        estado["baseline_boot"] = boot
        guardar_estado(hid, estado)
        log(f"ℹ️ Integridad SQL: línea base registrada (arranque de SQL: {boot}); sin chequeo.")
        return _respuesta(status="ok", extra={"baseline": True}, origen="sql")

    # 4) Reinicio nuevo -> corrida pendiente (reemplaza a la que hubiera en curso).
    if boot != estado["baseline_boot"]:
        if corrida.get("status") in ("pending", "running"):
            log("⚠️ Integridad SQL: SQL se reinició otra vez; se descarta la corrida anterior.")
        log(f"🔔 Integridad SQL: reinicio de SQL detectado (arranque {boot}).")
        estado["baseline_boot"] = boot
        estado["run"] = corrida = {
            "boot": boot, "status": "pending", "detected_at": _ahora_iso(),
            "check_type": tipo_chequeo(sql_cfg), "databases": [], "sent": False, "lanzamientos": 0,
        }
        guardar_estado(hid, estado)

    if corrida.get("status") not in ("pending", "running"):
        return _respuesta(status="ok", origen="sql")

    # 5) Pendiente -> esperar que SQL se asiente y que las bases estén ONLINE (o vencer la espera).
    if corrida["status"] == "pending":
        minutos = (ahora_srv - arranque).total_seconds() / 60.0
        objetivo = [b for b in bases_configuradas(sql_cfg) if b in bases_srv]
        todas_online = all(bases_srv[b] == "ONLINE" for b in objetivo)
        espera = _entero(sql_cfg.get("checkdb_settle_minutes"), ESPERA_ASENTARSE_MIN)
        espera_max = _entero(sql_cfg.get("checkdb_max_wait_minutes"), ESPERA_MAXIMA_MIN)
        if minutos < espera or (not todas_online and minutos < espera_max):
            return _respuesta(status="pending", extra={"minutes_since_boot": round(minutos, 1)}, origen="sql")
        corrida.update(status="running", databases=objetivo, check_type=tipo_chequeo(sql_cfg), started_at=_ahora_iso())
        guardar_estado(hid, estado)
        log(f"▶️ Integridad SQL: se inicia el chequeo de {len(objetivo)} bases ({corrida['check_type']}).")

    # 6) En curso -> ver el archivo de resultados del trabajador.
    res = cargar_resultados(hid)
    if res.get("boot") != corrida["boot"]:
        res = {}
    if res.get("status") == "done":
        corrida["status"] = "done"
        guardar_estado(hid, estado)
        return _enviar_resultado_sql(hid, corrida)

    hechas, total = len(res.get("results", [])), len(corrida.get("databases", []))
    if not trabajador_vivo(res):
        if corrida.get("lanzamientos", 0) >= MAX_LANZAMIENTOS:
            # El trabajador no logra terminar: se cierran las bases que faltan como ERROR y se informa.
            faltantes = [b for b in corrida["databases"] if b not in {r["db"] for r in res.get("results", [])}]
            cierre = list(res.get("results", [])) + [
                {"db": b, "status": ESTADO_ERROR, "error_count": 0, "detail": "El chequeo no pudo completarse",
                 "duration_s": 0, "checked_at": _ahora_iso()} for b in faltantes]
            guardar_resultados(hid, {"boot": corrida["boot"], "status": "done", "results": cierre})
            corrida["status"] = "done"
            guardar_estado(hid, estado)
            log("❌ Integridad SQL: el trabajador no logró terminar tras varios intentos; se informa lo hecho.")
            return _enviar_resultado_sql(hid, corrida)
        corrida["lanzamientos"] = corrida.get("lanzamientos", 0) + 1
        guardar_estado(hid, estado)
        try:
            pid = lanzar_trabajador(hid)
            log(f"▶️ Integridad SQL: trabajador lanzado (PID {pid}).")
        except Exception as e:
            log(f"❌ Integridad SQL: no se pudo lanzar el trabajador: {e}")
            return _respuesta(status="error", extra={"error": str(e)[:200]}, origen="sql")
    return _respuesta(status="running", extra={"done": hechas, "total": total}, origen="sql")


def _enviar_resultado_sql(hospital_id, corrida):
    res = cargar_resultados(hospital_id)
    bases = res.get("results", []) if res.get("boot") == corrida.get("boot") else []
    if not bases:                       # nada que informar (bases inexistentes): se da por enviado
        _marcar_enviado_sql(hospital_id)
        return _respuesta(status="ok", origen="sql")
    payload = _payload(corrida["boot"], corrida.get("check_type", TIPO_COMPLETO), "sql", bases)
    return _respuesta(payload, "ok", {"total": len(bases)}, lambda: _marcar_enviado_sql(hospital_id), "sql")


def _marcar_enviado_sql(hospital_id):
    estado = cargar_estado(hospital_id)
    if estado.get("run"):
        estado["run"]["sent"] = True
        estado["run"]["status"] = "done"
        guardar_estado(hospital_id, estado)


# ---------------------------------------------------------------------------
# CAMINO ELASTIC (Logstash corre el CHECKDB; el agente lee el índice)
# ---------------------------------------------------------------------------
def _esquema(elastic_cfg):
    return "https" if elastic_cfg.get("use_https") else "http"


def _entero_o_none(valor):
    try:
        return int(valor)
    except (TypeError, ValueError):
        return None


def recolectar_elastic(config, log_func=None):
    log = log_func or (lambda m: None)
    hid = config.get("hospital_id")
    el = config.get("elastic") or {}
    indice = el.get("checkdb_index") or "ext_checkdb"
    url = f"{_esquema(el)}://{el.get('host', '').strip()}:{el.get('port', 29200)}/{indice}/_search"
    auth = HTTPBasicAuth(el.get("user", ""), el.get("pass", "")) if el.get("user") else None
    consulta = {"size": 500, "sort": [{"sqlserver_start_epoch": {"order": "desc", "unmapped_type": "long"}}],
                "query": {"match_all": {}}}
    try:
        resp = requests.post(url, json=consulta, auth=auth, timeout=15, verify=False)
        if resp.status_code == 404:      # el índice todavía no existe: nunca hubo un reinicio chequeado
            return _respuesta(status="empty", origen="elastic")
        resp.raise_for_status()
        hits = resp.json().get("hits", {}).get("hits", [])
    except Exception as e:
        log(f"⚠️ Integridad SQL (Elastic): error leyendo '{indice}': {e}")
        return _respuesta(status="error", extra={"error": str(e)[:200]}, origen="elastic")

    docs = []
    for hit in hits:
        src = {str(k).lower(): v for k, v in (hit.get("_source") or {}).items()}
        if str(src.get("estado", "")).upper() == "BASELINE":
            continue
        docs.append(src)
    epocas = [e for e in (_entero_o_none(d.get("sqlserver_start_epoch")) for d in docs) if e is not None]
    if not epocas:
        return _respuesta(status="empty", origen="elastic")

    ultima = max(epocas)
    estado = cargar_estado(hid)
    if ultima <= (_entero_o_none(estado.get("last_sent_epoch")) or 0):
        return _respuesta(status="ok", origen="elastic")           # ese reinicio ya se informó

    del_arranque = [d for d in docs if _entero_o_none(d.get("sqlserver_start_epoch")) == ultima]
    bases = []
    for d in del_arranque:
        nombre = str(d.get("dbname") or "").strip()
        if not nombre:
            continue
        bases.append({
            "db": nombre, "status": str(d.get("estado") or "").upper(),
            "error_count": _entero_o_none(d.get("error_count")) or 0,
            "detail": str(d.get("detalle") or "")[:MAX_DETALLE],
            "duration_s": _entero_o_none(d.get("duration_s")) or 0,
            "checked_at": str(d.get("checked_at") or ""),
        })
    if not bases:
        return _respuesta(status="empty", origen="elastic")

    primero = del_arranque[0]
    payload = _payload(str(primero.get("sqlserver_start_time") or ""), str(primero.get("check_type") or TIPO_COMPLETO),
                       "elastic", bases)

    def _commit():
        e = cargar_estado(hid)
        e["last_sent_epoch"] = ultima
        guardar_estado(hid, e)

    log(f"🔔 Integridad SQL (Elastic): resultado de un reinicio con {len(bases)} bases listo para enviar.")
    return _respuesta(payload, "ok", {"total": len(bases)}, _commit, "elastic")


# ---------------------------------------------------------------------------
# ENTRADA PARA EL CICLO
# ---------------------------------------------------------------------------
def recolectar(config, log_func=None):
    """Devuelve {"payload", "status", "extra", "commit", "source"}. Nunca lanza.

    `commit` (callable o None) lo invoca el ciclo SOLO tras un POST exitoso, para marcar el resultado
    como enviado; si el envío falla, el mismo resultado se reintenta en el próximo ciclo."""
    try:
        if habilitado_elastic(config):
            return recolectar_elastic(config, log_func)
        if habilitado_sql(config):
            return recolectar_sql(config, log_func)
        return _respuesta(status="disabled")
    except Exception as e:
        if log_func:
            log_func(f"❌ Integridad SQL: error inesperado: {e}")
        return _respuesta(status="error", extra={"error": str(e)[:200]})


# ---------------------------------------------------------------------------
# TESTS DE CONEXIÓN (botones de la GUI)
# ---------------------------------------------------------------------------
def test_conexion_sql(sql_cfg):
    """Verifica conexión, qué bases configuradas existen/están ONLINE, y que el usuario pueda correr
    DBCC CHECKDB (sysadmin, o db_owner en cada base)."""
    if not sql_cfg or not sql_cfg.get("host"):
        return {"success": False, "msg": "Falta el host de SQL Server."}
    try:
        conn = conectar(sql_cfg)
    except Exception as e:
        return {"success": False, "msg": f"No se pudo conectar: {e}"}
    try:
        _arranque, _ahora, bases_srv = leer_servidor(conn)
        pedidas = bases_configuradas(sql_cfg)
        existentes = [b for b in pedidas if b in bases_srv]
        no_online = [b for b in existentes if bases_srv[b] != "ONLINE"]
        cur = conn.cursor()
        cur.execute("SELECT IS_SRVROLEMEMBER('sysadmin')")
        sysadmin = bool(cur.fetchone()[0])
        sin_permiso = []
        if not sysadmin:
            for b in existentes:
                try:
                    cur.execute(f"USE [{b.replace(']', ']]')}]; SELECT IS_MEMBER('db_owner')")
                    if not cur.fetchone()[0]:
                        sin_permiso.append(b)
                except Exception:
                    sin_permiso.append(b)
        if not existentes:
            return {"success": False, "msg": "Conexión OK, pero ninguna de las bases configuradas existe en el servidor."}
        if sin_permiso:
            return {"success": False, "msg": f"El usuario no es sysadmin ni db_owner en: {', '.join(sin_permiso[:5])}"
                                             f"{'…' if len(sin_permiso) > 5 else ''}. DBCC CHECKDB lo exige."}
        aviso = f" (no ONLINE: {', '.join(no_online)})" if no_online else ""
        return {"success": True,
                "msg": f"OK: {len(existentes)} de {len(pedidas)} bases encontradas, permisos suficientes{aviso}."}
    except Exception as e:
        return {"success": False, "msg": str(e)}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def test_conexion_indice(elastic_cfg):
    """Verifica que se pueda leer el índice de integridad y muestra el último resultado disponible."""
    if not elastic_cfg or not elastic_cfg.get("host"):
        return {"success": False, "msg": "Falta el host de Elastic."}
    resultado = recolectar_elastic({"hospital_id": "__test__", "elastic": elastic_cfg})
    indice = elastic_cfg.get("checkdb_index") or "ext_checkdb"
    if resultado["status"] == "error":
        return {"success": False, "msg": f"No se pudo leer '{indice}': {resultado['extra'].get('error', '')}"}
    if resultado["status"] == "empty":
        return {"success": True, "msg": f"Índice '{indice}' accesible, todavía sin resultados de ningún reinicio."}
    p = resultado["payload"]
    if p:
        errores = sum(1 for b in p["databases"] if b["status"] != ESTADO_OK)
        return {"success": True, "msg": f"OK: último resultado con {len(p['databases'])} bases ({errores} con problemas)."}
    return {"success": True, "msg": f"Índice '{indice}' accesible."}
