import re
import time
from datetime import datetime

import requests
import urllib3

# Desactivamos advertencias SSL.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Cache de topología (definición de canales: origen/destino/routing interno).
# Vive a nivel de módulo porque cambia rarísima vez -- no vale la pena pegarle
# a /api/channels (puede pesar varios MB, trae transformers) en cada ciclo.
# Se refresca antes de tiempo si el set de channel_id que devolvió
# /statuses este ciclo difiere del cacheado (canal nuevo o borrado).
# ---------------------------------------------------------------------------
CACHE_TOPO_TTL_SEG = 3600
_topo_cache = {}  # alias -> {"fetched_at": epoch, "ids": set(channel_id), "canales": [...]}

# Nunca se manda el jdbc/URL de un conector tal cual: puede traer
# credenciales embebidas (Database Reader/Writer, HTTP Sender con auth).
_PATRON_USERINFO = re.compile(r'(?i)(https?://)[^/@\s]+@')
_PATRON_CREDENCIAL = re.compile(r'(?i)(password|pwd|user|uid)\s*=\s*[^;&\s]*')


def _sanear_endpoint(txt):
    if not txt:
        return None
    txt = str(txt)
    txt = _PATRON_USERINFO.sub(r'\1***@', txt)
    txt = _PATRON_CREDENCIAL.sub(lambda m: m.group(1) + '=***', txt)
    return txt[:160]


def _safe_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _as_bool(v, default=True):
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() == "true"
    return default


def _endpoint_de_conector(conn):
    """
    Distila un conector (origen o destino) de la definición de canal de
    Mirth a {transport, endpoint, host, port, target_channel_id}. Nunca
    tira excepción -- un conector no reconocido o con properties en una
    forma inesperada sale con lo que se pudo leer en vez de romper la
    recolección de topología entera. Nunca serializa `properties` completo:
    ahí adentro viven credenciales de Database Reader/Writer y HTTP Sender.
    """
    out = {"transport": None, "endpoint": None, "host": None, "port": None, "target_channel_id": None}
    if not isinstance(conn, dict):
        return out
    try:
        transport = conn.get("transportName") or conn.get("name") or "Desconocido"
        out["transport"] = transport
        props = conn.get("properties") or {}

        if transport == "Channel Writer":
            cid = props.get("channelId")
            out["target_channel_id"] = cid if cid and cid != "none" else None
            return out

        if transport in ("TCP Listener", "HTTP Listener", "Web Service Listener", "DICOM Listener"):
            lp = props.get("listenerConnectorProperties") or {}
            host, port = lp.get("host"), _safe_int(lp.get("port"))
            out["host"], out["port"] = host, port
            out["endpoint"] = _sanear_endpoint(f"{host}:{port}" if host and port else (host or lp.get("port")))
            return out

        if transport in ("TCP Sender", "DICOM Sender"):
            host = props.get("remoteAddress") or props.get("host")
            port = _safe_int(props.get("remotePort") or props.get("port"))
            out["host"], out["port"] = host, port
            out["endpoint"] = _sanear_endpoint(f"{host}:{port}" if host and port else host)
            return out

        if transport in ("Database Reader", "Database Writer"):
            out["endpoint"] = _sanear_endpoint(props.get("url"))
            return out

        if transport in ("File Reader", "File Writer"):
            partes = [props.get("scheme"), props.get("host"), props.get("directory")]
            out["endpoint"] = _sanear_endpoint(" ".join(p for p in partes if p))
            return out

        if transport in ("HTTP Sender", "Web Service Sender"):
            out["endpoint"] = _sanear_endpoint(props.get("host") or props.get("url"))
            return out

        # Conector no mapeado (custom, o tipo no contemplado): se manda solo
        # el transport, sin endpoint -- mejor información parcial que nada.
        return out
    except Exception:
        return out


def _lista_destinos(ch):
    """
    La forma exacta de los conectores destino en el JSON de /api/channels
    varía entre versiones de Mirth (3.x vs 4.x, con o sin grupos de
    destino). Se intentan las formas conocidas sin tirar excepción si
    ninguna aplica -- un canal sin destinos legibles sale con lista vacía,
    no rompe la topología completa.
    """
    try:
        bloque = ch.get("destinationConnectors")
        if isinstance(bloque, dict):
            lst = bloque.get("connector", [])
            if isinstance(lst, dict):
                lst = [lst]
            if lst:
                return lst
    except Exception:
        pass
    try:
        grupos = (ch.get("destinationConnectorGroups") or {}).get("destinationConnectorGroup", [])
        if isinstance(grupos, dict):
            grupos = [grupos]
        destinos = []
        for g in grupos:
            conns = (g.get("destinationConnectors") or {}).get("connector", [])
            if isinstance(conns, dict):
                conns = [conns]
            destinos.extend(conns)
        return destinos
    except Exception:
        return []


def _distilar_canal(ch):
    if not isinstance(ch, dict):
        return None
    cid = ch.get("id")
    if not cid:
        return None

    revision = _safe_int(ch.get("revision"))
    origen = _endpoint_de_conector(ch.get("sourceConnector") or {})
    origen.pop("target_channel_id", None)  # el origen nunca es un Channel Writer

    destinos = []
    for i, conn in enumerate(_lista_destinos(ch)):
        try:
            d = _endpoint_de_conector(conn)
            d["metadata_id"] = conn.get("metaDataId", i + 1)
            d["name"] = (conn.get("name") or f"Destino {i + 1}")[:120]
            d["enabled"] = _as_bool(conn.get("enabled"), default=True)
            destinos.append(d)
        except Exception:
            continue

    return {
        "channel_id": cid,
        "name": (ch.get("name") or "Sin nombre")[:200],
        "revision": revision,
        "source": origen,
        "destinations": destinos,
    }


def _recolectar_topologia(s, url, alias, ids_actuales, log_func=None):
    """
    GET /api/channels -- definición completa de los canales (para derivar
    origen/destino/routing interno vía Channel Writer). Se cachea
    CACHE_TOPO_TTL_SEG (1h) por alias. Si el fetch falla y hay una copia
    cacheada, se devuelve esa (mejor topología vieja que ninguna); si no
    hay cache, se devuelve None y el ciclo simplemente no manda
    `mirth_topology` para esta instancia.
    """
    cache = _topo_cache.get(alias)
    ahora = time.time()
    if cache and (ahora - cache["fetched_at"] < CACHE_TOPO_TTL_SEG) and cache["ids"] == set(ids_actuales):
        return cache["canales"]

    try:
        r = s.get(f"{url}/api/channels", verify=False, timeout=30)
        r.raise_for_status()
        data = r.json()
        lista = data.get('list', {}).get('channel', [])
        if isinstance(lista, dict):
            lista = [lista]

        canales = []
        for ch in lista:
            try:
                d = _distilar_canal(ch)
                if d:
                    canales.append(d)
            except Exception:
                continue  # un canal mal formado no tira abajo el resto

        _topo_cache[alias] = {
            "fetched_at": ahora,
            "ids": {c["channel_id"] for c in canales},
            "canales": canales,
        }
        return canales

    except Exception as e:
        if log_func:
            log_func(f"⚠️ Topología Mirth ({alias}) no disponible este ciclo: {str(e)[:120]}")
        return cache["canales"] if cache else None


def recolectar_mirth(mirth_configs, log_func=None):
    """
    Extrae la telemetría de canales HL7 desde la API REST de Mirth Connect,
    más la definición de topología (origen/destino/routing interno) para
    el mapa de integraciones. Cruza estados (statuses) con transacciones
    (statistics).

    Devuelve (resultados, meta_status, errores_globales, topologia).
    """
    resultados = {}
    topologia = {}
    meta_status = "ok"
    errores_globales = 0

    if not mirth_configs:
        return resultados, "disabled", 0, topologia

    for m_cfg in mirth_configs:
        alias = m_cfg.get("alias", "Mirth_Desconocido")
        url   = m_cfg.get("url", "").rstrip('/')
        user  = m_cfg.get("user", "")
        pwd   = m_cfg.get("pass", "")

        canales_data = []
        s = requests.Session()
        # Encabezados requeridos por Mirth para evitar rechazos por CSRF
        s.headers.update({
            'X-Requested-With': 'OpenAPI',
            'Accept': 'application/json'
        })

        try:
            # 1. Login
            login_req = s.post(f"{url}/api/users/_login", data={'username': user, 'password': pwd}, verify=False, timeout=10)
            login_req.raise_for_status()

            # 2. Obtener Estadísticas (Para Recibidos y Enviados)
            r_stats = s.get(f"{url}/api/channels/statistics", verify=False, timeout=15)
            r_stats.raise_for_status()
            stats_data = r_stats.json().get('list', {}).get('channelStatistics', [])
            if isinstance(stats_data, dict):
                stats_data = [stats_data]

            # Mapear estadísticas por channelId para un cruce eficiente O(1)
            mapa_estadisticas = {}
            for stat in stats_data:
                cid = stat.get('channelId')
                if cid:
                    mapa_estadisticas[cid] = {
                        "received": int(stat.get('received', 0)),
                        "sent": int(stat.get('sent', 0))
                    }

            # 3. Obtener Estados (Robustez ante XML-to-JSON)
            r_stat = s.get(f"{url}/api/channels/statuses", verify=False, timeout=15)
            r_stat.raise_for_status()
            data = r_stat.json()

            dash_status = data.get('list', {}).get('dashboardStatus', [])
            if isinstance(dash_status, dict):
                dash_status = [dash_status]

            ids_actuales = []
            for status in dash_status:
                channel_id = status.get('channelId')
                if channel_id:
                    ids_actuales.append(channel_id)
                name  = status.get('name', 'Unknown')
                state = status.get('state', 'UNKNOWN')

                stats_entries = status.get('statistics', {}).get('entry', [])
                if isinstance(stats_entries, dict):
                    stats_entries = [stats_entries]

                queued = 0
                errors_count = 0

                for entry in stats_entries:
                    st_type = entry.get('com.mirth.connect.donkey.model.message.Status')
                    st_val  = int(entry.get('long', 0))

                    if st_type == 'QUEUED':
                        queued = st_val
                    elif st_type == 'ERROR':
                        errors_count = st_val

                # Rescatamos received y sent del mapa usando el ID del canal
                metricas_tx = mapa_estadisticas.get(channel_id, {"received": 0, "sent": 0})

                # Payload con transacciones integradas para que Tecnomonitor las grafique
                canales_data.append({
                    "channel": name,
                    "channel_id": channel_id,
                    "status": state,
                    "queued": queued,
                    "received": metricas_tx["received"],
                    "sent": metricas_tx["sent"],
                    "errored": errors_count,
                    "last_error": f"Errores acumulados: {errors_count}" if errors_count > 0 else ""
                })

            # 4. Topología (definición de canales) -- en su propio try: si
            # falla, no invalida canales_data ya recolectado más arriba.
            try:
                topo_canales = _recolectar_topologia(s, url, alias, ids_actuales, log_func)
                if topo_canales:
                    topologia[alias] = {
                        "collected_at": datetime.now().isoformat(),
                        "full": True,
                        "channels": topo_canales,
                    }
            except Exception as e:
                if log_func:
                    log_func(f"⚠️ Topología Mirth ({alias}) omitida este ciclo: {str(e)[:120]}")

            resultados[alias] = canales_data

        except Exception as e:
            errores_globales += 1
            meta_status = "partial"
            if log_func:
                log_func(f"⚠️ Error Mirth ({alias}): {str(e)}")

            # Si ya se habían recolectado canales antes de que algo fallara,
            # no se descartan: se manda lo real y se marca "partial", en vez
            # de reemplazar todo por un canal sintético SYSTEM_ERROR.
            resultados[alias] = canales_data if canales_data else [{
                "channel": "SYSTEM_ERROR",
                "status": "ERROR",
                "queued": 0,
                "received": 0,
                "sent": 0,
                "errored": 0,
                "last_error": str(e)[:100]
            }]

        finally:
            # 5. Logout, best-effort de verdad: una falla acá (timeout,
            # sesión ya inválida) nunca debe invalidar lo que ya se juntó
            # arriba -- antes de este fix, sí lo hacía (mismo try que todo
            # lo demás), descartando telemetría real por un problema ajeno
            # a la recolección.
            try:
                s.post(f"{url}/api/users/_logout", verify=False, timeout=5)
            except Exception:
                pass

    if errores_globales == len(mirth_configs) and len(mirth_configs) > 0:
        meta_status = "error"

    return resultados, meta_status, errores_globales, topologia
