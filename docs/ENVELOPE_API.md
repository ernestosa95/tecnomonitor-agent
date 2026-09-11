# Envelope enviado al servidor central

Al final de cada ciclo, `ejecutar_ciclo_agente()` hace:

```
POST <central_url>
Authorization: Bearer <auth_token>
Content-Type: application/json
(verify=False, timeout=25s)

<reporte JSON completo>
```

Un `raise_for_status()` determina éxito/fracaso del envío; solo en caso de éxito se persisten
los checkpoints de SQL y Elastic (ver [MODULOS.md](./MODULOS.md)).

## Forma general del reporte

```jsonc
{
  "envelope": {
    "schema_version": "4.3",
    "agent_version": "4.4.0",
    "hospital_id": "HOSP-001",
    "timestamp": "2026-09-11T10:05:00.123456"
  },
  "collection_meta": { /* ver abajo — un bloque por módulo */ },
  "software_monitoring": { /* mirth, ssl_certificates, suitestensa_logs, dicom_routing_queues */ },
  "physical_layer": { /* host físico: hipervisor + iDRAC + red */ },
  "virtual_layer": [ /* array de VMs/workstations/equipos vía WMI */ ],
  "application_metrics": { /* solo presente si el módulo SQL produjo datos este ciclo */ }
}
```

> Nota: desde v4.5.0, `schema_version` y `agent_version` (ambos `"4.5.0"`/`"4.5"`, hardcodeados
> como literales en `ejecutar_ciclo_agente()`) están alineados con el `AppVersion` del
> instalador — ver [BUILD.md](./BUILD.md#versionado). Siguen sin derivarse automáticamente de
> un único lugar: si se cambia uno hay que actualizar los tres a mano. `schema_version` no es
> cosmético — el servidor central lo usa para decidir cómo parsear el payload y, desde `"4.5"`,
> para exigir el token de autenticación (ver el contrato de ingesta del servidor).

## `collection_meta`

Un objeto por módulo, presente **siempre** (incluso deshabilitado), para que el servidor
central pueda distinguir "módulo apagado" de "módulo activo sin datos" de "módulo con error".

| Clave | Corresponde a |
|---|---|
| `proxmox` | Hipervisor (Proxmox o VMware) |
| `idrac` | Sensores + storage Dell |
| `wmi` | Equipos Windows |
| `sql` | KPIs de negocio |
| `mirth` | Mirth Connect |
| `ssl_monitoring` | Certificados SSL |
| `suitestensa_logs` | Logs de Elastic |
| `dicom_routing` | Autoenrute DICOM |

Campos comunes: `enabled` (bool, refleja la config), `status`. Valores posibles de `status`
según el módulo:

- `"disabled"` — módulo no habilitado en config (valor inicial, antes de intentar nada).
- `"ok"` — recolección exitosa.
- `"error"` — falló completamente; suele venir acompañado de `error` (string).
- `"partial"` — (`wmi`, `mirth`, `ssl_monitoring`) algunos elementos de una lista fallaron y
  otros no; viene con `total` y `errors` (conteos).
- `"stale"` / `"empty"` — específicos de `dicom_routing` (ver [MODULOS.md](./MODULOS.md#autoenrute-dicom-vía-elasticsearch)).

Campos adicionales por módulo:

```jsonc
"sql":   { "enabled": true, "status": "ok", "block_start": "2026-09-11T02:00:00", "block_end": "2026-09-11T10:00:00" }
"wmi":   { "enabled": true, "status": "partial", "total": 12, "errors": 2 }
"mirth": { "enabled": true, "status": "ok", "total": 2, "errors": 0 }
"ssl_monitoring":   { "enabled": true, "status": "ok", "total": 5, "errors": 0 }
"suitestensa_logs": { "enabled": true, "status": "ok", "new_alerts": 3 }
"dicom_routing":    { "enabled": true, "status": "ok", "total": 8, "errors": 0 }
```

## `physical_layer`

Se va **acumulando** en el mismo objeto entre los pasos de hipervisor + iDRAC + red — no se
pisan entre sí (el código explícitamente preserva lo que ya escribió un módulo anterior antes
de que el siguiente agregue sus claves).

```jsonc
{
  "host_info": { "hostname": "...", "type": "proxmox"|"vmware", "model": "...", "uptime_seconds": 123456, "vendor": "..." /* solo vmware */ },
  "telemetry": { "cpu": { "usage_percent": 12.3 }, "ram": { "total_gb": 64.0, "used_gb": 20.1, "usage_percent": 31.4 } },
  "sensors": { "status": "OK", "temperatures": [...], "fans": [...], "power": { "watts_current": 210, "supplies": [...] } },
  "storage_layer": { "controllers": [...], "logical_volumes": [...], "physical_drives": [...], "collection_complete": true },
  "network_health": { "status": "ok", "upload_usage_mbps": 4.2, "download_usage_mbps": 1.1, "cloud_latency_ms": 38.5, "cloud_status": "conectado", "last_check": "2026-09-11T10:05:00" },
  "vms": [ /* solo si proxmox.type == "vmware": lista de VMs del hipervisor, distinta de virtual_layer */ ]
}
```

`sensors` y `storage_layer` solo se agregan si `enabled_idrac` está activo. `network_health` mide
tráfico real de la placa de red durante 1 s (`psutil.net_io_counters`, muestreo antes/después)
y la latencia TCP hacia `tecnomonitor.tecnoimagen.com.ar:443` — **siempre se recolecta**,
independientemente de qué otros módulos estén habilitados.

## `virtual_layer`

Array de objetos, uno por equipo configurado en `vms[]` (WMI), **no** por VM del hipervisor
(esas van dentro de `physical_layer.vms` cuando el hipervisor es VMware):

```jsonc
{
  "id": "PACSWKS01",
  "type": "vm" | "ws" | "eq",
  "state": "Online" | "Offline",
  "state_reason": "ok" | "port_closed" | "wmi_error" | "wmi_timeout" | "unknown",
  "telemetry": {
    "cpu": { "usage_percent": 8.5 },
    "ram": { "total_gb": 16.0, "used_gb": 9.2, "usage_percent": 57.5 },
    "uptime_seconds": 302400
  },
  "storage": [
    { "mount_point": "C:", "total_gb": 238.0, "free_gb": 54.3, "usage_percent": 77.2,
      "performance": { "latency_ms": 4.1, "status": "OK" } }
  ],
  "application_layer": {
    "services": [
      { "name": "MSSQLSERVER", "display_name": "SQL Server (MSSQLSERVER)", "state": "Running",
        "vital_signs": { "pid": 4321, "health": "OK", "cpu_percent": 2.1, "ram_mb": 812.4, "threads": 45, "handles": 980 } }
    ]
  },
  "wmi_error": "..." /* solo presente si state_reason == "wmi_error" */
}
```

## `software_monitoring`

```jsonc
{
  "dicom_routing_queues": [
    { "id_rule": "R001",
      "from_node": { "key": "...", "nickname": "...", "hostname": "..." },
      "to_node":   { "key": "...", "nickname": "...", "hostname": "..." },
      "pending_instances": 42,
      "snapshot_age_minutes": 2.3 }
  ],
  "mirth": {
    "Produccion_Principal": [
      { "channel": "ADT_IN", "status": "RUNNING", "queued": 0, "received": 15234, "sent": 15234, "last_error": "" }
    ]
  },
  "ssl_certificates": [
    { "url": "https://pacs.hospital.com", "status": "OK", "expiration_date": "2027-03-01T00:00:00Z", "days_remaining": 180, "issuer": "DigiCert" }
  ],
  "suitestensa_logs": {
    "scan_time": "2026-09-11T10:05:00Z",
    "events": [ { "rule_id": "DCM-COM-01", "count": 3 } ]
  }
}
```

`dicom_routing_queues` es siempre un array (vacío si el módulo está apagado, con error, o
`stale`/`empty` — ver [MODULOS.md](./MODULOS.md)). Las demás claves solo aparecen si su
módulo respectivo está habilitado y tiene servidores/URLs configurados.

## `application_metrics`

Solo presente cuando el módulo SQL produjo un bloque nuevo en este ciclo (ver checkpointing en
[MODULOS.md](./MODULOS.md#sql-server-kpis-de-negocio)). Estructura tal como sale de
`SQL_QUERY` (`FOR JSON PATH`), más tres campos que agrega el propio agente:

```jsonc
{
  "ris":  [ { "equipo": "...", "aet": "...", "mod": "CT", "totales": 120, "citados": 100, "admitidos": 95, "ejecutados": 90, "con_imagen": 88, "borradores": 80, "definitivos": 75, "suspendidos": 5 } ],
  "pacs": [ { "aet": "...", "mod": "CT", "almacenados": 90 } ],
  "users": [ { "rol": "Tecnico", "usuarios_unicos": 4, "inicios_sesion": 11 } ],
  "extraction_interval_hours": 8.0,
  "start_time_extraction": "2026-09-11T02:00:00",
  "end_time_extraction": "2026-09-11T10:00:00"
}
```

## Errores de envío

Si el `POST` falla (timeout, DNS, TLS, HTTP ≥ 400), `ejecutar_ciclo_agente()` **no** lanza la
excepción hacia el bucle del servicio: la captura y devuelve
`{"status": "Error", "error": "<detalle>", "timestamp": "HH:MM:SS"}`, que el servicio solo
registra en el log. No hay cola/reintento local para ese reporte puntual — el próximo ciclo
generará un reporte nuevo con el estado *actual* de la infraestructura (correcto para métricas
tipo snapshot como WMI/iDRAC/Proxmox; para SQL/Elastic no hay pérdida porque el checkpoint no
avanzó, así que el mismo bloque se reintenta en el siguiente ciclo).
