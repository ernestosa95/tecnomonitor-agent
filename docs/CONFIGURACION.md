# Configuración — `monitor_config.json`

Ubicación: `%PROGRAMDATA%\TecnoMonitor\monitor_config.json`. Se lee/escribe únicamente a
través de la GUI (`cargar_config` / `guardar_config` en `main_gui.py`) y se lee en cada ciclo
por el servicio (`cargar_config_segura` en `headless_service.py`). Nunca se debería editar a
mano estando el servicio corriendo (los cambios se pisan al guardar desde la GUI, y las
contraseñas en texto plano quedarían así hasta el próximo guardado desde la GUI, que las
vuelve a cifrar).

Los campos marcados **🔒 cifrado** se guardan pasados por `security.encriptar()` (Fernet) y se
descifran automáticamente al cargar la configuración (`security.desencriptar()`).

## Nivel raíz

| Campo | Tipo | Default | Descripción |
|---|---|---|---|
| `hospital_id` | string | `"UNKNOWN"` | Identificador del hospital, viaja en `envelope.hospital_id` de cada reporte |
| `auth_token` | string 🔒 | `""` | Token enviado como `Authorization: Bearer <token>` al servidor central |
| `central_url` | string | — | Endpoint HTTPS al que se hace `POST` con el envelope completo |
| `interval_minutes` | int | `5` | Cada cuánto corre un ciclo completo de recolección + envío |

### `auth_token` — de dónde sale y por qué tiene que coincidir con `hospital_id`

Desde `schema_version 4.5` (vigente, confirmado por el contrato de ingesta del servidor —
`2026-09-11`), el servidor **exige** este header y rechaza el reporte completo (401, sin
guardar nada) si falta, no existe, o no corresponde al `hospital_id` declarado en el envelope.
El mensaje de rechazo es intencionalmente genérico — no dice cuál de los tres motivos fue.

- El token es **único por hospital** y se genera desde el **panel de administración del
  servidor** (no algo que el agente genere): al dar de alta el hospital, o mediante
  `POST /api/hospitales-metadata/{hid}/regenerar-token` para uno ya existente.
- Se copia **una sola vez** al campo `Auth Token` de la GUI al configurar el agente — el
  servidor no lo vuelve a mostrar después de generarlo.
- El servidor identifica al hospital **por el token**, no por el `hospital_id` del JSON.
  Asegurarse de que ambos correspondan al mismo hospital antes de guardar la configuración:
  un token de otro hospital pegado por error rechaza todos los reportes en silencio.
- Antes de instalar un agente nuevo en `schema_version 4.5` (el default desde v4.5.0, ver
  `agent_logic.ejecutar_ciclo_agente`), generar el token de ese hospital específico desde el
  panel — no reusar un token de otro hospital ni dejarlo vacío.

## Proxmox / VMware — `enabled_proxmox` + `proxmox`

Un único bloque cubre ambos hipervisores; `proxmox.type` decide cuál se usa.

| Campo | Tipo | Descripción |
|---|---|---|
| `proxmox.type` | `"proxmox"` \| `"vmware"` | Selector de hipervisor |
| `proxmox.host` | string | IP/hostname del nodo Proxmox o del vCenter/ESXi |
| `proxmox.node` | string | Nombre del nodo dentro del cluster (solo Proxmox; no aplica a VMware) |
| `proxmox.user` | string | Usuario |
| `proxmox.pass` | string 🔒 | Contraseña |

Ver [MODULOS.md](./MODULOS.md#proxmox--vmware) para qué se recolecta según el tipo.

## iDRAC (Dell) — `enabled_idrac` + `idrac`

| Campo | Tipo | Descripción |
|---|---|---|
| `idrac.ip` | string | IP del iDRAC |
| `idrac.user` | string | Usuario Redfish |
| `idrac.pass` | string 🔒 | Contraseña |

## SQL Server (KPIs de negocio) — `enabled_sql` + `sql`

| Campo | Tipo | Default | Descripción |
|---|---|---|---|
| `sql.host` | string | — | Instancia SQL Server (`ExtensaRadio`/`ExtensaPACS`/`SL_UserAndConfig`) |
| `sql.db` | string | `"ExtensaRadio"` (sugerido en UI) | Base de datos inicial de conexión |
| `sql.user` | string | — | Usuario SQL |
| `sql.pass` | string 🔒 | — | Contraseña |
| `sql.executions_per_day` | int | `3` | Cuántos bloques por día se extraen (define `interval_hours = 24 / executions_per_day`); si es `<= 0` se fuerza a `3` |
| `sql.historical_start_date` | string `YYYY-MM-DD` | — | Fecha desde la que arrancar el backfill histórico si no hay checkpoint previo. Si falta o es inválida, se usa el inicio del día actual |
| `sql.enabled_dicom_routing` | bool | — | **Obsoleto desde v4.4** (ver más abajo) |

Detalle de la extracción (checkpoint, backfill, bloques) en [MODULOS.md](./MODULOS.md#sql-server-kpis-de-negocio).

## Equipos Windows/Linux — `enabled_vms` + `vms[]`

Lista de equipos monitoreados vía WMI (Windows) o SSH (Linux, desde v4.6 — ver
[PLAN_MEJORAS_V4.5.md §9.2](./PLAN_MEJORAS_V4.5.md#92-monitoreo-de-equipos-linux-en-vms-hoy-solo-wmiwindows)):
VMs, workstations físicas o equipos médicos.

| Campo | Tipo | Descripción |
|---|---|---|
| `nombre` | string | Nombre manual opcional. Si se deja vacío, se usa el hostname real detectado (o la IP si falla) |
| `type` | `"vm"` \| `"ws"` \| `"eq"` | Clasificación del equipo (VM, workstation física, equipo médico) |
| `os` | `"windows"` \| `"linux"` | Qué mecanismo de recolección usar (WMI o SSH). **Si no está presente, se asume `"windows"`** — configs guardadas antes de v4.6 no tienen este campo y siguen funcionando igual que siempre |
| `ip` | string | IP/hostname del equipo |
| `user` | string | Usuario con permisos WMI remoto (`os: "windows"`) o usuario SSH (`os: "linux"`) |
| `pass` | string 🔒 | Contraseña (WMI o SSH según `os`; SSH solo soporta usuario/contraseña por ahora, no clave privada) |
| `servicios` | string (CSV) o lista | Nombres de servicios de Windows (`os: "windows"`, ej. `"MSSQLSERVER, Spooler"`) o de unidades `systemd` (`os: "linux"`, ej. `"postgresql, logstash"`) a monitorear en ese equipo |

**Diferencias de Linux/SSH contra Windows/WMI** (ver
[ENVELOPE_API.md](./ENVELOPE_API.md#virtual_layer) para el detalle del JSON resultante): no se
recolecta latencia de disco (`storage[].performance`) por no haber una forma confiable de
mapear punto de montaje a dispositivo real en LVM/RAID; `vital_signs.handles` en Linux es la
cantidad de file descriptors abiertos del proceso, no el mismo concepto que en Windows.

## Mirth Connect — `enabled_mirth` + `mirth_servers[]`

| Campo | Tipo | Descripción |
|---|---|---|
| `alias` | string | Etiqueta libre para identificar el entorno (aparece como clave en `software_monitoring.mirth`) |
| `url` | string | URL base de la API REST de Mirth (`https://host:8443`, sin barra final) |
| `user` | string | Usuario de Mirth |
| `pass` | string 🔒 | Contraseña |

## Certificados SSL — `enabled_ssl` + `ssl_urls[]`

| Campo | Tipo | Descripción |
|---|---|---|
| `url` | string | URL a monitorear (se usa hostname + puerto, default 443) |

## ElasticSearch (logs + autoenrute DICOM + KPIs de RIS) — `enabled_elastic` + `elastic`

| Campo | Tipo | Default | Descripción |
|---|---|---|---|
| `elastic.host` | string | — | Host de ElasticSearch |
| `elastic.port` | int | `9200` en la GUI; el código de logs/autoenrute usa `29200` como fallback interno si la clave faltara | Puerto HTTP de Elastic |
| `elastic.user` | string | — | Usuario (Basic Auth) |
| `elastic.pass` | string 🔒 | — | Contraseña |
| `elastic.index_pattern` | string | `"se-es-logging-*"` | Patrón de índices para el módulo de logs de Suitestensa |
| `elastic.enabled_dicom_routing` | bool | `false` | Habilita el sub-módulo de autoenrute DICOM (ver más abajo) |
| `elastic.dicom_index` | string | `"ext_dicom_queues"` | Índice donde Logstash publica el estado de las colas de autoenrute |
| `elastic.dicom_max_age_minutes` | int | `15` | Antigüedad máxima aceptada de un documento del índice de autoenrute antes de considerarlo obsoleto |
| `elastic.enabled_ris_metrics` | bool | `false` | **v4.5** — habilita la extracción de KPIs de RIS/PACS/usuarios vía Elastic en vez de SQL directo (ver [ELK_RIS_METRICS.md](./ELK_RIS_METRICS.md)). Convive con `enabled_sql`: si está en `true` y `elastic.host` está configurado, tiene prioridad sobre el módulo SQL directo para ese hospital |
| `elastic.ris_executions_per_day` | int | `3` | Igual semántica que `sql.executions_per_day`, pero para el camino vía Elastic — define el tamaño de bloque (`24 / ris_executions_per_day` horas) que el agente reconstruye sumando buckets horarios |
| `elastic.ris_historical_start_date` | string `YYYY-MM-DD` | — | Igual semántica que `sql.historical_start_date`, para el backfill del camino vía Elastic |
| `elastic.ris_index_ris` | string | `"ext_ris_metrics_hourly"` | Índice con los buckets horarios de KPIs de RIS |
| `elastic.ris_index_pacs` | string | `"ext_pacs_metrics_hourly"` | Índice con los buckets horarios de almacenamiento PACS |
| `elastic.ris_index_users` | string | `"ext_users_metrics_hourly"` | Índice con los buckets horarios de actividad de usuarios |

> ⚠️ **Nota de consistencia interna:** el valor por defecto de `elastic.port` difiere según la
> función: `test_connection_elastic` (botón "Test" genérico) usa `9200` si la clave no está
> presente, mientras que `get_dicom_routing_queues` y `recolectar_logs_elastic` usan `29200`.
> En la práctica esto no suele notarse porque la GUI siempre guarda `port` explícitamente
> (default `9200`), pero es relevante si se edita `monitor_config.json` a mano o se omite el
> campo.

### Compatibilidad del flag de autoenrute DICOM (v4.3 → v4.4)

Hasta la v4.3, el flag vivía en `sql.enabled_dicom_routing` (el colector leía SQL Server
directo). Desde v4.4 el autoenrute se lee de ElasticSearch y el flag se movió a
`elastic.enabled_dicom_routing`. Tanto el backend (`_dicom_routing_habilitado` en
`agent_logic.py`) como el frontend (`cargarConfiguracion` en `script.js`) tienen un fallback:
si `elastic.enabled_dicom_routing` no está presente, se usa el valor viejo de
`sql.enabled_dicom_routing`. Esto evita que agentes ya desplegados con la configuración vieja
dejen de reportar en silencio hasta que alguien reguarde la configuración. Se puede retirar
este fallback una vez que todos los hospitales estén confirmados en v4.4 o superior.

### KPIs de RIS vía Elastic — coexistencia con `enabled_sql` (v4.5)

`elastic.enabled_ris_metrics` no reemplaza a `enabled_sql`/`sql.*`: es una migración
hospital por hospital. `headless_service.py` decide con un `elif` (no ambos a la vez):

1. Si `elastic.enabled_ris_metrics` es `true` y `elastic.host` está configurado → usa
   `agent_logic.extraer_metricas_ris_elastic` (requiere que el Logstash de ese hospital ya
   publique a los tres índices horarios — ver [ELK_RIS_METRICS.md](./ELK_RIS_METRICS.md)).
2. Si no, y `enabled_sql` + `sql.host` están configurados → sigue usando
   `agent_logic.extraer_metricas_sql` (SQL Server directo), exactamente como hoy.

Ambos caminos comparten el mismo checkpoint (`.sql_checkpoint`) y producen el mismo
`application_metrics` en el envelope — no hay diferencia visible para el servidor central según
cuál esté activo. La GUI expone este sub-ítem dentro de la tarjeta 8 (ElasticSearch), como
"KPIs de RIS vía Elastic" — mismo patrón visual que el sub-ítem de autoenrute DICOM, con su
propio botón de test (valida lectura sobre los tres índices en un solo llamado).

## Claves internas transitorias (no se guardan en disco)

`headless_service.py` inyecta estas claves en el diccionario de configuración **en memoria**,
antes de llamar a `ejecutar_ciclo_agente`, y `agent_logic.py` las consume/limpia durante el
ciclo. No forman parte del `monitor_config.json` persistido:

| Clave | Origen | Uso |
|---|---|---|
| `_sql_data_payload` | `extraer_metricas_sql()` | Payload ya extraído de SQL para el ciclo actual |
| `_elastic_checkpoint_to_save` | `recolectar_logs_elastic()` | Timestamp a persistir en `.elastic_checkpoint` tras el envío exitoso |

## Ejemplo de estructura completa (valores ilustrativos)

```json
{
  "hospital_id": "HOSP-001",
  "auth_token": "<cifrado>",
  "central_url": "https://tecnomonitor.tecnoimagen.com.ar/api/ingest",
  "interval_minutes": 5,

  "enabled_proxmox": true,
  "proxmox": { "type": "proxmox", "host": "10.0.0.10", "node": "pve", "user": "root@pam", "pass": "<cifrado>" },

  "enabled_idrac": true,
  "idrac": { "ip": "10.0.0.11", "user": "root", "pass": "<cifrado>" },

  "enabled_sql": true,
  "sql": {
    "host": "10.0.0.20", "db": "ExtensaRadio", "user": "sa", "pass": "<cifrado>",
    "executions_per_day": 3, "historical_start_date": "2026-01-01"
  },

  "enabled_vms": true,
  "vms": [
    { "nombre": "", "type": "vm", "ip": "10.0.0.30", "user": "administrador", "pass": "<cifrado>", "servicios": "MSSQLSERVER, Spooler" }
  ],

  "enabled_mirth": true,
  "mirth_servers": [
    { "alias": "Produccion_Principal", "url": "https://10.0.0.40:8443", "user": "admin", "pass": "<cifrado>" }
  ],

  "enabled_ssl": true,
  "ssl_urls": [ { "url": "https://pacs.hospital.com" } ],

  "enabled_elastic": true,
  "elastic": {
    "host": "10.0.0.50", "port": 9200, "user": "elastic", "pass": "<cifrado>",
    "index_pattern": "se-es-logging-*",
    "enabled_dicom_routing": true, "dicom_index": "ext_dicom_queues", "dicom_max_age_minutes": 15
  }
}
```
