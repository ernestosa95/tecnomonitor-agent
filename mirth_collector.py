import requests
import urllib3

# Desactivamos advertencias SSL.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def recolectar_mirth(mirth_configs, log_func=None):
    """
    Extrae la telemetría de canales HL7 desde la API REST de Mirth Connect.
    Actualizado para cruzar estados (statuses) con transacciones (statistics).
    """
    resultados = {}
    meta_status = "ok"
    errores_globales = 0

    if not mirth_configs:
        return resultados, "disabled", 0

    for m_cfg in mirth_configs:
        alias = m_cfg.get("alias", "Mirth_Desconocido")
        url   = m_cfg.get("url", "").rstrip('/')
        user  = m_cfg.get("user", "")
        pwd   = m_cfg.get("pass", "")
        
        canales_data = []
        try:
            s = requests.Session()
            # Encabezados requeridos por Mirth para evitar rechazos por CSRF
            s.headers.update({
                'X-Requested-With': 'OpenAPI', 
                'Accept': 'application/json'
            })
            
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
            
            for status in dash_status:
                channel_id = status.get('channelId')
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
                    "status": state,
                    "queued": queued,
                    "received": metricas_tx["received"],
                    "sent": metricas_tx["sent"],
                    "last_error": f"Errores acumulados: {errors_count}" if errors_count > 0 else ""
                })
            
            # 4. Logout
            s.post(f"{url}/api/users/_logout", verify=False, timeout=5)
            resultados[alias] = canales_data
            
        except Exception as e:
            errores_globales += 1
            meta_status = "partial"
            if log_func: 
                log_func(f"⚠️ Error Mirth ({alias}): {str(e)}")
            
            resultados[alias] = [{
                "channel": "SYSTEM_ERROR", 
                "status": "ERROR", 
                "queued": 0, 
                "received": 0,
                "sent": 0,
                "last_error": str(e)[:100]
            }]
    
    if errores_globales == len(mirth_configs) and len(mirth_configs) > 0:
        meta_status = "error"
        
    return resultados, meta_status, errores_globales