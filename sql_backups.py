"""
Último backup completo de las bases SQL Server (REQ-06, v4.5.3).

Cada hospital hace el backup con el propio SQL Server, así que la fecha sale de
`msdb.dbo.backupset` (solo backups completos, `type = 'D'`, sin los "solo copia"). Es una consulta
liviana: se manda en cada ciclo el estado actual (una entrada por base) en
`software_monitoring.sql_backups`, y el servidor decide si hay que alertar (umbral configurable,
24 h por defecto). Las bases son las mismas del chequeo de integridad (`sql.checkdb_databases`,
26 de Extensa por defecto).

Dos caminos independientes (mismo criterio que sql_integrity):

  * Elastic (principal): Logstash corre la consulta cada hora (elk/ext_sql_backups.conf, en el
    cajón ext_kpis_negocio-all-sito.bat) y escribe un documento por base en `ext_sql_backups`; el
    agente lee el índice y reenvía.
  * SQL directo (excepción, hospitales sin Elastic): el agente corre la misma consulta.

Si ambos caminos están activos gana Elastic. Las fechas viajan como texto en hora local del
servidor SQL (sin zona), igual que en el resto de las mediciones SQL: Logstash las convierte a
texto en el propio T-SQL para que no las pase a UTC.
"""
from datetime import datetime

import requests
from requests.auth import HTTPBasicAuth

import sql_integrity

# Documentos de Elastic más viejos que esto respecto del más reciente se descartan: son de bases
# que se sacaron de la lista del .conf (su documento queda en el índice con la última fecha vista).
MARGEN_CORRIDA_HORAS = 3

_BACKUPS_SQL = """
SELECT d.name,
       CONVERT(VARCHAR(19), MAX(b.backup_finish_date), 126) AS ultimo_full,
       CONVERT(VARCHAR(19), GETDATE(), 126) AS ahora
FROM sys.databases d
LEFT JOIN msdb.dbo.backupset b
       ON b.database_name = d.name AND b.type = 'D' AND b.is_copy_only = 0
GROUP BY d.name
"""


# ---------------------------------------------------------------------------
# CONFIGURACIÓN
# ---------------------------------------------------------------------------
def habilitado_elastic(config):
    el = config.get("elastic") or {}
    return bool(config.get("enabled_elastic") and el.get("enabled_backups") and el.get("host"))


def habilitado_sql(config):
    sq = config.get("sql") or {}
    return bool(config.get("enabled_sql") and sq.get("enabled_backups") and sq.get("host"))


def habilitado(config):
    """True si cualquiera de los dos caminos está activo (usado para `collection_meta`)."""
    return habilitado_elastic(config) or habilitado_sql(config)


def _respuesta(payload=None, status="ok", extra=None, origen=None):
    return {"payload": payload, "status": status, "extra": extra or {}, "source": origen}


def _payload(origen, collected_at, bases):
    return {"source": origen, "collected_at": collected_at, "databases": bases}


def _fecha_texto(valor):
    """Normaliza una fecha (texto ISO o datetime) a 'YYYY-MM-DDTHH:MM:SS'; None si no hay o no se entiende."""
    if valor is None or valor == "":
        return None
    if isinstance(valor, datetime):
        return valor.replace(tzinfo=None).isoformat(timespec="seconds")
    try:
        return datetime.fromisoformat(str(valor).strip()[:19]).isoformat(timespec="seconds")
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# CAMINO SQL DIRECTO
# ---------------------------------------------------------------------------
def leer_backups(conn):
    """{nombre_en_minusculas: (nombre_real, ultimo_full|None)} y la hora actual del SQL."""
    cur = conn.cursor()
    cur.execute(_BACKUPS_SQL)
    por_base, ahora = {}, None
    for nombre, ultimo, ahora_srv in cur.fetchall():
        por_base[str(nombre).lower()] = (str(nombre), _fecha_texto(ultimo))
        ahora = ahora or _fecha_texto(ahora_srv)
    return por_base, ahora


def _bases_pedidas(por_base, sql_cfg):
    """Las bases configuradas que existen en el servidor, con su último backup completo."""
    salida = []
    for pedida in sql_integrity.bases_configuradas(sql_cfg):
        encontrada = por_base.get(pedida.lower())
        if encontrada:
            salida.append({"db": encontrada[0], "last_full": encontrada[1]})
    return salida


def recolectar_sql(config, log_func=None):
    log = log_func or (lambda m: None)
    sql_cfg = config.get("sql") or {}
    try:
        conn = sql_integrity.conectar(sql_cfg)
        try:
            por_base, ahora = leer_backups(conn)
        finally:
            conn.close()
    except Exception as e:
        log(f"⚠️ Backups SQL: no se pudo consultar SQL Server ({e}).")
        return _respuesta(status="error", extra={"error": str(e)[:200]}, origen="sql")

    bases = _bases_pedidas(por_base, sql_cfg)
    if not bases:
        return _respuesta(status="empty", origen="sql")
    collected = ahora or datetime.now().isoformat(timespec="seconds")
    return _respuesta(_payload("sql", collected, bases), "ok", {"total": len(bases)}, "sql")


# ---------------------------------------------------------------------------
# CAMINO ELASTIC (Logstash corre la consulta cada hora; el agente lee el índice)
# ---------------------------------------------------------------------------
def recolectar_elastic(config, log_func=None):
    log = log_func or (lambda m: None)
    el = config.get("elastic") or {}
    indice = el.get("backups_index") or "ext_sql_backups"
    esquema = "https" if el.get("use_https") else "http"
    url = f"{esquema}://{el.get('host', '').strip()}:{el.get('port', 29200)}/{indice}/_search"
    auth = HTTPBasicAuth(el.get("user", ""), el.get("pass", "")) if el.get("user") else None
    try:
        resp = requests.post(url, json={"size": 1000, "query": {"match_all": {}}},
                             auth=auth, timeout=15, verify=False)
        if resp.status_code == 404:      # el pipeline todavía no corrió nunca
            return _respuesta(status="empty", origen="elastic")
        resp.raise_for_status()
        hits = resp.json().get("hits", {}).get("hits", [])
    except Exception as e:
        log(f"⚠️ Backups SQL (Elastic): error leyendo '{indice}': {e}")
        return _respuesta(status="error", extra={"error": str(e)[:200]}, origen="elastic")

    docs = []
    for hit in hits:
        src = {str(k).lower(): v for k, v in (hit.get("_source") or {}).items()}
        nombre = str(src.get("dbname") or "").strip()
        corrida = _fecha_texto(src.get("checked_at"))
        if nombre and corrida:
            docs.append((nombre, _fecha_texto(src.get("last_full")), corrida))
    if not docs:
        return _respuesta(status="empty", origen="elastic")

    ultima = max(d[2] for d in docs)
    tope = datetime.fromisoformat(ultima).timestamp() - MARGEN_CORRIDA_HORAS * 3600
    bases = [{"db": n, "last_full": f} for n, f, c in sorted(docs)
             if datetime.fromisoformat(c).timestamp() >= tope]
    return _respuesta(_payload("elastic", ultima, bases), "ok", {"total": len(bases)}, "elastic")


# ---------------------------------------------------------------------------
# ENTRADA PARA EL CICLO
# ---------------------------------------------------------------------------
def recolectar(config, log_func=None):
    """Devuelve {"payload", "status", "extra", "source"}. Nunca lanza."""
    try:
        if habilitado_elastic(config):
            return recolectar_elastic(config, log_func)
        if habilitado_sql(config):
            return recolectar_sql(config, log_func)
        return _respuesta(status="disabled")
    except Exception as e:
        if log_func:
            log_func(f"❌ Backups SQL: error inesperado: {e}")
        return _respuesta(status="error", extra={"error": str(e)[:200]})


# ---------------------------------------------------------------------------
# TESTS DE CONEXIÓN (botones de la GUI)
# ---------------------------------------------------------------------------
def _resumen(bases, ahora_txt, horas=24):
    """Texto corto: cuántas bases, cuántas sin backup completo y cuántas con más de `horas`."""
    nunca = [b["db"] for b in bases if not b["last_full"]]
    try:
        ahora = datetime.fromisoformat(ahora_txt)
    except (TypeError, ValueError):
        ahora = datetime.now()
    viejas = [b["db"] for b in bases if b["last_full"]
              and (ahora - datetime.fromisoformat(b["last_full"])).total_seconds() > horas * 3600]
    partes = [f"{len(bases)} bases"]
    if nunca:
        partes.append(f"{len(nunca)} sin ningún backup completo ({', '.join(nunca[:3])}{'…' if len(nunca) > 3 else ''})")
    if viejas:
        partes.append(f"{len(viejas)} con el último backup completo de hace más de {horas} h")
    if not nunca and not viejas:
        partes.append(f"todas con backup completo en las últimas {horas} h")
    return ", ".join(partes)


def test_conexion_sql(sql_cfg):
    """Verifica conexión, lectura de msdb.dbo.backupset y qué bases configuradas existen."""
    if not sql_cfg or not sql_cfg.get("host"):
        return {"success": False, "msg": "Falta el host de SQL Server."}
    try:
        conn = sql_integrity.conectar(sql_cfg)
    except Exception as e:
        return {"success": False, "msg": f"No se pudo conectar: {e}"}
    try:
        por_base, ahora = leer_backups(conn)
    except Exception as e:
        return {"success": False, "msg": f"Conexión OK, pero no se pudo leer msdb.dbo.backupset: {e}"}
    finally:
        try:
            conn.close()
        except Exception:
            pass
    bases = _bases_pedidas(por_base, sql_cfg)
    if not bases:
        return {"success": False, "msg": "Conexión OK, pero ninguna de las bases configuradas existe en el servidor."}
    return {"success": True, "msg": f"OK: {_resumen(bases, ahora)}."}


def test_conexion_indice(elastic_cfg):
    """Verifica que se pueda leer el índice de backups y resume el último estado disponible."""
    if not elastic_cfg or not elastic_cfg.get("host"):
        return {"success": False, "msg": "Falta el host de Elastic."}
    resultado = recolectar_elastic({"elastic": elastic_cfg})
    indice = elastic_cfg.get("backups_index") or "ext_sql_backups"
    if resultado["status"] == "error":
        return {"success": False, "msg": f"No se pudo leer '{indice}': {resultado['extra'].get('error', '')}"}
    if resultado["status"] == "empty":
        return {"success": True, "msg": f"Índice '{indice}' accesible, todavía sin datos (¿corrió el pipeline ext_sql_backups?)."}
    p = resultado["payload"]
    return {"success": True, "msg": f"OK (lectura de Logstash del {p['collected_at'].replace('T', ' ')}): "
                                    f"{_resumen(p['databases'], p['collected_at'])}."}
