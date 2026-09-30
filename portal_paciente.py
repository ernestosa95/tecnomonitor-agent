"""
Portal paciente: estado de la cola de publicación (REQ-07, v4.5.4).

Circuito en las instalaciones con portal: el RIS marca el examen para publicar cuando el informe
pasa a definitivo (`tbExamination.PublicationState`), el MPS lo toma en su cola (`ExtMPS.QUEUE`),
genera una ISO (`JOBS.MEDIA_ACTUAL_SIZE` deja de ser nulo) y la manda por FTP a la VM que publica.

En cada ciclo se manda, por estado, cuántos estudios hay en el RIS y en la cola del MPS
(`software_monitoring.patient_portal`), con el más antiguo de cada uno. El agente no clasifica los
estados (pendiente / error / final): lo decide el servidor, así un código nuevo del MPS no obliga a
recompilar el agente. El servidor arma con esto la serie temporal por estado y las alertas.

Se cuenta sobre los últimos VENTANA_DIAS días y sin cortar por estado: un estudio trabado aparece
hasta que pasan esos días, en vez de desaparecer a la semana. `last_24h` es el subconjunto de las
últimas 24 h (para distinguir bloqueos nuevos de los viejos). Los estados del catálogo sin estudios
salen igual, con 0, para que la serie baje a cero y no quede el último valor visto.

Dos caminos independientes (mismo criterio que sql_backups):

  * Elastic (principal): Logstash corre las consultas cada 5 minutos (elk/ext_portal_paciente.conf,
    en el cajón ext_tiempo_real-all-sito.bat) y escribe un documento por estado en
    `ext_portal_paciente`; el agente lee el índice y reenvía.
  * SQL directo (excepción, hospitales sin Elastic): el agente corre las mismas consultas.

Si ambos caminos están activos gana Elastic. Las fechas viajan como texto en hora local del
servidor SQL (sin zona), convertidas en el propio T-SQL para que Logstash no las pase a UTC.
"""
from datetime import datetime

import requests
from requests.auth import HTTPBasicAuth

import sql_integrity

VENTANA_DIAS = 30

# Documentos de Elastic más viejos que esto respecto de la corrida más reciente se descartan: son de
# estados que dejaron de salir (por ejemplo, un código que ya no existe en el catálogo) o de una de
# las dos consultas que dejó de correr.
MARGEN_CORRIDA_MINUTOS = 30

# Las mismas consultas que elk/ext_portal_paciente.conf: si se cambia una, cambiar la otra.
_RIS_SQL = """
SELECT 'RIS' AS origin,
       ISNULL(CAST(COALESCE(ps.PublicationState, x.PublicationState) AS VARCHAR(10)), 'NULL') AS code,
       COALESCE(ps.Description,
                CASE WHEN x.PublicationState IS NULL THEN 'Not Ready / In Progress' ELSE 'UNKNOWN' END) AS state,
       ISNULL(x.total, 0) AS total,
       ISNULL(x.last_24h, 0) AS last_24h,
       0 AS pending_iso,
       0 AS with_iso,
       CONVERT(VARCHAR(19), x.oldest, 126) AS oldest,
       CONVERT(VARCHAR(19), GETDATE(), 126) AS checked_at
FROM [ExtensaRadio].[ExtRadio].[dsPublicationState] ps WITH (NOLOCK)
FULL OUTER JOIN (
    SELECT e.PublicationState,
           COUNT(*) AS total,
           COUNT(CASE WHEN e.AdmissionDate >= DATEADD(HOUR, -24, GETDATE()) THEN 1 END) AS last_24h,
           MIN(e.AdmissionDate) AS oldest
    FROM [ExtensaRadio].[ExtRadio].[tbExamination] e WITH (NOLOCK)
    WHERE e.AdmissionDate >= DATEADD(DAY, -30, GETDATE())
    GROUP BY e.PublicationState
) x ON x.PublicationState = ps.PublicationState
"""

_MPS_SQL = """
SELECT 'MPS' AS origin,
       ISNULL(CAST(COALESCE(s.STATUS_ID, x.STATUS_ID) AS VARCHAR(10)), 'NULL') AS code,
       ISNULL(s.DESCRIPTION, 'UNKNOWN') AS state,
       ISNULL(x.total, 0) AS total,
       ISNULL(x.last_24h, 0) AS last_24h,
       ISNULL(x.pending_iso, 0) AS pending_iso,
       ISNULL(x.with_iso, 0) AS with_iso,
       CONVERT(VARCHAR(19), x.oldest, 126) AS oldest,
       CONVERT(VARCHAR(19), GETDATE(), 126) AS checked_at
FROM [ExtensaMPS].[ExtMPS].[LS_STATUS_CODES] s WITH (NOLOCK)
FULL OUTER JOIN (
    SELECT q.STATUS_ID,
           COUNT(*) AS total,
           COUNT(CASE WHEN q.IN_TIME >= DATEADD(HOUR, -24, GETDATE()) THEN 1 END) AS last_24h,
           COUNT(CASE WHEN j.MEDIA_ACTUAL_SIZE IS NULL THEN 1 END) AS pending_iso,
           COUNT(CASE WHEN j.MEDIA_ACTUAL_SIZE IS NOT NULL THEN 1 END) AS with_iso,
           MIN(q.IN_TIME) AS oldest
    FROM [ExtensaMPS].[ExtMPS].[QUEUE] q WITH (NOLOCK)
    INNER JOIN [ExtensaMPS].[ExtMPS].[JOBS] j WITH (NOLOCK) ON j.ID = q.JOB_ID
    WHERE q.IN_TIME >= DATEADD(DAY, -30, GETDATE())
    GROUP BY q.STATUS_ID
) x ON x.STATUS_ID = s.STATUS_ID
"""

_CAMPOS = ("origin", "code", "state", "total", "last_24h", "pending_iso", "with_iso", "oldest", "checked_at")


# ---------------------------------------------------------------------------
# CONFIGURACIÓN
# ---------------------------------------------------------------------------
def habilitado_elastic(config):
    el = config.get("elastic") or {}
    return bool(config.get("enabled_elastic") and el.get("enabled_portal") and el.get("host"))


def habilitado_sql(config):
    sq = config.get("sql") or {}
    return bool(config.get("enabled_sql") and sq.get("enabled_portal") and sq.get("host"))


def habilitado(config):
    """True si cualquiera de los dos caminos está activo (usado para `collection_meta`)."""
    return habilitado_elastic(config) or habilitado_sql(config)


def _respuesta(payload=None, status="ok", extra=None, origen=None):
    return {"payload": payload, "status": status, "extra": extra or {}, "source": origen}


def _payload(origen, collected_at, estados):
    return {"source": origen, "collected_at": collected_at, "window_days": VENTANA_DIAS, "states": estados}


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


def _entero(valor):
    try:
        return max(0, int(valor or 0))
    except (TypeError, ValueError):
        return 0


def _estado(fila):
    """Normaliza una fila (dict con los nombres de _CAMPOS) al formato del payload; None si no sirve."""
    origen = str(fila.get("origin") or "").strip().upper()
    if origen not in ("RIS", "MPS"):
        return None
    codigo = str(fila.get("code") if fila.get("code") is not None else "NULL").strip() or "NULL"
    return {
        "origin": origen,
        "code": codigo[:20],
        "state": str(fila.get("state") or "UNKNOWN").strip()[:100],
        "total": _entero(fila.get("total")),
        "last_24h": _entero(fila.get("last_24h")),
        "pending_iso": _entero(fila.get("pending_iso")),
        "with_iso": _entero(fila.get("with_iso")),
        "oldest": _fecha_texto(fila.get("oldest")),
    }


def _ordenar(estados):
    return sorted(estados, key=lambda e: (e["origin"] != "RIS", e["code"]))


# ---------------------------------------------------------------------------
# CAMINO SQL DIRECTO
# ---------------------------------------------------------------------------
def leer_estados(conn):
    """
    Corre las dos consultas por separado (si una falla, la otra igual se informa).
    Devuelve (estados, hora_del_sql, {origen: error}).
    """
    estados, ahora, errores = [], None, {}
    for origen, consulta in (("RIS", _RIS_SQL), ("MPS", _MPS_SQL)):
        try:
            cur = conn.cursor()
            cur.execute(consulta)
            for tupla in cur.fetchall():
                fila = dict(zip(_CAMPOS, tupla))
                est = _estado(fila)
                if est:
                    estados.append(est)
                ahora = ahora or _fecha_texto(fila.get("checked_at"))
        except Exception as e:
            errores[origen] = str(e)[:200]
    return _ordenar(estados), ahora, errores


def recolectar_sql(config, log_func=None):
    log = log_func or (lambda m: None)
    try:
        conn = sql_integrity.conectar(config.get("sql") or {})
        try:
            estados, ahora, errores = leer_estados(conn)
        finally:
            conn.close()
    except Exception as e:
        log(f"⚠️ Portal paciente: no se pudo consultar SQL Server ({e}).")
        return _respuesta(status="error", extra={"error": str(e)[:200]}, origen="sql")

    for origen, err in errores.items():
        log(f"⚠️ Portal paciente: falló la consulta de {origen} ({err}).")
    if not estados:
        if errores:
            return _respuesta(status="error", extra={"error": "; ".join(f"{o}: {e}" for o, e in errores.items())},
                              origen="sql")
        return _respuesta(status="empty", origen="sql")

    extra = {"total": len(estados)}
    if errores:
        extra["error"] = "; ".join(f"{o}: {e}" for o, e in errores.items())
    collected = ahora or datetime.now().isoformat(timespec="seconds")
    return _respuesta(_payload("sql", collected, estados), "partial" if errores else "ok", extra, "sql")


# ---------------------------------------------------------------------------
# CAMINO ELASTIC (Logstash corre las consultas cada 5 minutos; el agente lee el índice)
# ---------------------------------------------------------------------------
def recolectar_elastic(config, log_func=None):
    log = log_func or (lambda m: None)
    el = config.get("elastic") or {}
    indice = el.get("portal_index") or "ext_portal_paciente"
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
        log(f"⚠️ Portal paciente (Elastic): error leyendo '{indice}': {e}")
        return _respuesta(status="error", extra={"error": str(e)[:200]}, origen="elastic")

    docs = []
    for hit in hits:
        src = {str(k).lower(): v for k, v in (hit.get("_source") or {}).items()}
        est = _estado(src)
        corrida = _fecha_texto(src.get("checked_at"))
        if est and corrida:
            docs.append((est, corrida))
    if not docs:
        return _respuesta(status="empty", origen="elastic")

    ultima = max(c for _, c in docs)
    tope = datetime.fromisoformat(ultima).timestamp() - MARGEN_CORRIDA_MINUTOS * 60
    estados = _ordenar([e for e, c in docs if datetime.fromisoformat(c).timestamp() >= tope])
    return _respuesta(_payload("elastic", ultima, estados), "ok", {"total": len(estados)}, "elastic")


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
            log_func(f"❌ Portal paciente: error inesperado: {e}")
        return _respuesta(status="error", extra={"error": str(e)[:200]})


# ---------------------------------------------------------------------------
# TESTS DE CONEXIÓN (botones de la GUI)
# ---------------------------------------------------------------------------
def _resumen(estados):
    """Texto corto con el total por estado, separado RIS / MPS; omite los estados en 0."""
    partes = []
    for origen in ("RIS", "MPS"):
        items = [f"{e['state']} {e['total']}" for e in estados if e["origin"] == origen and e["total"]]
        if any(e["origin"] == origen for e in estados):
            partes.append(f"{origen}: {', '.join(items) if items else 'sin estudios'}")
    return " · ".join(partes) + f" (últimos {VENTANA_DIAS} días)"


def test_conexion_sql(sql_cfg):
    """Verifica conexión y que las dos consultas (RIS y MPS) corran."""
    if not sql_cfg or not sql_cfg.get("host"):
        return {"success": False, "msg": "Falta el host de SQL Server."}
    try:
        conn = sql_integrity.conectar(sql_cfg)
    except Exception as e:
        return {"success": False, "msg": f"No se pudo conectar: {e}"}
    try:
        estados, _, errores = leer_estados(conn)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if errores and not estados:
        return {"success": False, "msg": "Conexión OK, pero fallaron las consultas: "
                                         + "; ".join(f"{o}: {e}" for o, e in errores.items())}
    if errores:
        return {"success": False, "msg": f"Parcial: {_resumen(estados)}. Falló "
                                         + "; ".join(f"{o}: {e}" for o, e in errores.items())}
    return {"success": True, "msg": f"OK: {_resumen(estados)}."}


def test_conexion_indice(elastic_cfg):
    """Verifica que se pueda leer el índice del portal y resume el último estado disponible."""
    if not elastic_cfg or not elastic_cfg.get("host"):
        return {"success": False, "msg": "Falta el host de Elastic."}
    resultado = recolectar_elastic({"elastic": elastic_cfg})
    indice = elastic_cfg.get("portal_index") or "ext_portal_paciente"
    if resultado["status"] == "error":
        return {"success": False, "msg": f"No se pudo leer '{indice}': {resultado['extra'].get('error', '')}"}
    if resultado["status"] == "empty":
        return {"success": True, "msg": f"Índice '{indice}' accesible, todavía sin datos (¿corrió el pipeline ext_portal_paciente?)."}
    p = resultado["payload"]
    return {"success": True, "msg": f"OK (lectura de Logstash del {p['collected_at'].replace('T', ' ')}): "
                                    f"{_resumen(p['states'])}."}
