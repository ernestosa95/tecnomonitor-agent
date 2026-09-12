# Contrato de datos — qué envía el agente al servidor

Contraparte de `10-contratoingestaagente.md` (contrato de ingesta del servidor, provisto por
el usuario, `2026-09-10`/`2026-09-11`): mientras aquel documenta qué exige y consume
`alerts_engine`, este documenta qué **efectivamente arma y manda** `TecnoMonitor Agent` hoy.
Basado en lectura exacta de `agent_logic.py` (función `ejecutar_ciclo_agente` y las funciones
de recolección que llama) — no es una interpretación, es lo que el código hace, `2026-09-12`.

Pensado para pasarle al equipo de servidor como referencia de sincronización: si algo de acá
no coincide con lo que ese equipo entiende que el agente manda, es una señal de que uno de los
dos documentos quedó desactualizado.

**Versión del agente al momento de este documento:** `agent_version: "4.5.0"`,
`schema_version: "4.5"` (`agent_logic.py`, dentro de `ejecutar_ciclo_agente`). El rediseño de
GUI y el soporte multi-hospital por agente (pywebview + `instalaciones[]`, ver
[PLAN_MEJORAS_V4.5.md](./PLAN_MEJORAS_V4.5.md#91-un-agente-múltiples-sistemas-monitoreados-en-la-misma-red))
**no cambian nada de lo que va acá** — son cambios de cómo el mismo proceso agente arma la
config y cuántas veces por ciclo repite este mismo envío, no de la forma del reporte en sí (ver
§9 más abajo para el detalle de por qué).

## 1. El endpoint

```
POST <central_url>
Content-Type: application/json
Authorization: Bearer <auth_token>
timeout=25s, verify=False (TLS no validado del lado agente — ver SEGURIDAD.md, pendiente)
```

`central_url` y `auth_token` son valores de configuración por hospital (no hardcodeados en el
agente); en la práctica hoy todos los hospitales apuntan al mismo host
(`tecnomonitor.tecnoimagen.com.ar/v1/hospital-status`), pero el agente no lo asume, es un campo
de configuración libre.

**Manejo de la respuesta:** el agente solo llama `r.raise_for_status()` — no inspecciona el
body de la respuesta más allá de eso. Si el servidor responde `201` con un body distinto al
documentado en el contrato de ingesta, o cambia el status code de éxito, el agente no se entera
de la diferencia; simplemente trata cualquier `2xx` como éxito (persiste checkpoints) y
cualquier código de error como fracaso (los reintenta según corresponda — ver §8).

## 2. `schema_version` / `agent_version` — valores actuales

Ambos son **literales hardcodeados** en `ejecutar_ciclo_agente()`, no se derivan de un único
lugar (ver deuda técnica anotada en [PLAN_MEJORAS_V4.5.md §6](./PLAN_MEJORAS_V4.5.md#6-deuda-técnica--higiene-de-versión)):

```json
"schema_version": "4.5",
"agent_version": "4.5.0"
```

Un bump de cualquiera de los dos es un cambio de código explícito (`agent_logic.py`), nunca
algo que varíe reporte a reporte ni hospital a hospital.

## 2bis. Autenticación por token — implementado del lado agente

- El agente manda `Authorization: Bearer <auth_token>` en **cada** request, siempre (no solo a
  partir de cierto `schema_version` — el header se arma incondicionalmente en
  `ejecutar_ciclo_agente`).
- `auth_token` se guarda cifrado en disco (Fernet, `security.py`) y se desencripta recién en
  memoria antes de armar el header. Desde v4.6, es un campo **por perfil/hospital**
  (`instalaciones[].auth_token`), no uno solo por instalación — cada perfil configurado en el
  mismo equipo agente manda su propio token, para su propio `hospital_id`, en su propio POST
  independiente (ver §9).
- **Pendiente del lado agente** (anotado en
  [PLAN_MEJORAS_V4.5.md §2](./PLAN_MEJORAS_V4.5.md#2--hecho-preparación-para-autenticación-obligatoria-schema_version-45)):
  un `401` hoy cae al mismo manejo genérico de error que cualquier otro fallo de red (ver §8) —
  no se distingue en el log "token rechazado" de "servidor caído". No afecta lo que se manda,
  sí la capacidad de diagnosticar un rechazo de auth desde el lado del agente.

## 3. `envelope`

```json
"envelope": {
  "schema_version": "4.5",
  "agent_version": "4.5.0",
  "hospital_id": "H42",
  "timestamp": "2026-09-12T10:05:00.123456"
}
```

| Campo | De dónde sale en el agente |
|---|---|
| `hospital_id` | `config.get("hospital_id", "UNKNOWN")` — si no está configurado, manda literalmente el string `"UNKNOWN"`, no omite el campo. |
| `timestamp` | `datetime.now().isoformat()` — **hora local del equipo donde corre el agente, sin offset de timezone explícito** (no es UTC, no lleva `Z` ni `+00:00`). Si el servidor asume UTC o normaliza por su cuenta, confirmar que esto no genera un desfasaje sistemático hospital por hospital según su huso horario. |

## 4. `physical_layer`

Se arma acumulando entre hipervisor + iDRAC + red (ninguno pisa lo que escribió el anterior):

```json
{
  "host_info": { "hostname": "...", "type": "proxmox|vmware", "model": "...", "uptime_seconds": 123456 },
  "telemetry": { "cpu": { "usage_percent": 12.3 }, "ram": { "total_gb": 64.0, "used_gb": 20.1, "usage_percent": 31.4 } },
  "sensors": { "status": "OK", "temperatures": [...], "fans": [...], "power": { "watts_current": 210, "supplies": [...] } },
  "storage_layer": { "controllers": [...], "logical_volumes": [...], "physical_drives": [...], "collection_complete": true },
  "network_health": { "status": "ok", "upload_usage_mbps": 4.2, "download_usage_mbps": 1.1, "cloud_latency_ms": 38.5, "cloud_status": "conectado", "last_check": "2026-09-12T10:05:00" },
  "vms": [ "/* solo si proxmox.type == vmware */" ]
}
```

- `sensors`/`storage_layer`: solo presentes si el módulo iDRAC está habilitado para ese perfil.
  Ambos leen `Status.Health` real de Redfish desde el fix de
  [PLAN_MEJORAS_V4.5.md §1.1/§1.2](./PLAN_MEJORAS_V4.5.md#1-corrección-funcional--paridad-agente--contrato-del-servidor)
  — antes de ese fix, `sensors.temperatures[].status`/`.fans[].status`/`.power.supplies[].status`
  viajaban siempre como `"OK"` literal, sin importar el estado real del hardware. **Pendiente
  de validar contra un iDRAC real con una falla real** (ver checklist del plan de mejoras).
- `storage_layer` (antes `storage`, renombrado por el mismo fix): `logical_volumes[].status` y
  `physical_drives[].status` reportan lo que devuelve Redfish tal cual (`"Online"`, `"OK"`,
  `"Predictive Failure"`, etc.), sin normalizar a un vocabulario fijo del lado agente.
- `network_health`: se recolecta **siempre**, independientemente de qué otro módulo esté
  habilitado (mide tráfico real de 1s y latencia TCP hacia
  `tecnomonitor.tecnoimagen.com.ar:443`, no hacia `central_url` del hospital).

## 5. `virtual_layer`

Array de objetos, uno por equipo en `vms[]` — **no** incluye las VMs del hipervisor cuando es
VMware (esas van en `physical_layer.vms`, ver §4). Desde v4.6 cada equipo se recolecta por WMI
(Windows) o por SSH (Linux) según cómo esté configurado, pero **ambos caminos arman el mismo
`vm_obj`** — no hay una clave nueva por SO a nivel de `virtual_layer`, solo un campo `os` extra
dentro de cada objeto:

```json
[
  {
    "id": "PACSWKS01",
    "type": "vm" | "ws" | "eq",
    "os": "windows" | "linux",
    "state": "Online" | "Offline",
    "telemetry": { "cpu": { "usage_percent": 8.5 }, "ram": { "total_gb": 16.0, "used_gb": 9.2, "usage_percent": 57.5 }, "uptime_seconds": 302400 },
    "storage": [ { "mount_point": "C:", "total_gb": 238.0, "free_gb": 54.3, "usage_percent": 77.2 } ],
    "application_layer": { "services": [ { "name": "MSSQLSERVER", "state": "Running" } ] }
  }
]
```

⚠️ **Dos cambios de forma para el equipo de servidor, ambos introducidos en esta misma
versión:**

- `os` (`"windows"|"linux"`) es un campo **nuevo** en cada objeto de `virtual_layer`. No lo
  consume nada hoy — es información agregada, no debería romper nada dado que este array no
  tiene schema Pydantic estricto.
- El campo de error por objeto pasa a llamarse **`collection_error`** en vez de `wmi_error`
  (antes solo existía el camino WMI; con dos mecanismos posibles, un campo llamado `wmi_error`
  en una entrada Linux quedaba confuso). Igual que `os`, no está descrito como consumido por
  ninguna alerta en el contrato de ingesta — de todos modos, si algo del lado servidor buscaba
  literalmente la clave `wmi_error`, esto es lo que cambió.

Para un equipo con `os: "linux"`, dos campos de las entradas Windows no tienen equivalente
exacto y quedan resueltos así (no es un bug, es una limitación real de qué se puede leer de un
Linux por SSH sin instalar nada en el destino):

- `storage[].performance` (latencia de disco): **no se manda** en equipos Linux.
- `vital_signs.handles`: en Linux es la cantidad de file descriptors abiertos del proceso, no
  el mismo concepto que "handles" de Windows — mismo propósito (detectar fugas de recursos),
  número no comparable 1:1 contra una entrada Windows.

## 6. `application_metrics`

Solo presente si el módulo de KPIs de negocio (SQL directo o vía Elastic — mismo checkpoint,
ver [ELK_RIS_METRICS.md](./ELK_RIS_METRICS.md)) produjo un bloque nuevo en este ciclo.

```json
{
  "ris":  [ { "equipo": "...", "aet": "...", "mod": "CT", "totales": 120, "citados": 100, "admitidos": 95, "ejecutados": 90, "con_imagen": 88, "borradores": 80, "definitivos": 75, "suspendidos": 5 } ],
  "pacs": [ { "aet": "...", "mod": "CT", "almacenados": 90 } ],
  "users": [ { "rol": "Tecnico", "usuarios_unicos": 4, "inicios_sesion": 11 } ],
  "extraction_interval_hours": 8.0,
  "start_time_extraction": "2026-09-12T02:00:00",
  "end_time_extraction": "2026-09-12T10:00:00"
}
```

**El agente valida esto localmente antes de adjuntarlo** (fix de
[PLAN_MEJORAS_V4.5.md §1.3](./PLAN_MEJORAS_V4.5.md#13--resuelto-alternativa-de-bajo-esfuerzo--reporte-todo-o-nada-un-dato-de-negocio-malo-tumba-la-telemetría-de-infraestructura)):
si a un ítem de `ris`/`pacs`/`users` le falta un campo o tiene un tipo incorrecto, el agente
**omite `application_metrics` de ese ciclo entero** (loguea el detalle exacto) en vez de mandar
un bloque que el servidor rechazaría con un 500 genérico arrastrando el resto del reporte. Si el
servidor recibe `application_metrics`, todos los campos de todos los ítems ya pasaron esta
validación local — no debería rebotar por forma, solo por reglas de negocio que el agente no
conoce.

## 7. `software_monitoring`

Cuatro sub-claves, cada una presente solo si su módulo está habilitado y tiene datos:

```json
{
  "dicom_routing_queues": [ { "id_rule": "R001", "from_node": {"key":"...","nickname":"...","hostname":"..."}, "to_node": {"key":"...","nickname":"...","hostname":"..."}, "pending_instances": 42 } ],
  "mirth": { "Produccion_Principal": [ { "channel": "ADT_IN", "status": "RUNNING", "queued": 0, "received": 15234, "sent": 15234, "last_error": "" } ] },
  "ssl_certificates": [ { "url": "https://pacs.hospital.com", "status": "OK", "expiration_date": "2027-03-01T00:00:00Z", "days_remaining": 180, "issuer": "DigiCert" } ],
  "suitestensa_logs": { "scan_time": "2026-09-12T10:05:00Z", "events": [ { "rule_id": "DCM-COM-01", "count": 3 } ] }
}
```

`dicom_routing_queues` es siempre un array (vacío si el módulo está apagado, con error, o los
datos están obsoletos) — se manda en **cada** ciclo periódico cuando el módulo está activo, tal
como pide el contrato de ingesta (§7.4 de ese documento: el detector necesita varios puntos en
el tiempo, no solo cuando cambia).

## 8. `collection_meta` — clave que el agente manda y no está en el contrato de ingesta

```json
{
  "proxmox": { "enabled": true, "status": "ok" },
  "idrac":   { "enabled": true, "status": "ok" },
  "wmi":     { "enabled": true, "status": "partial", "total": 12, "errors": 2 },
  "sql":     { "enabled": true, "status": "ok", "block_start": "...", "block_end": "..." },
  "mirth":   { "enabled": true, "status": "ok", "total": 2, "errors": 0 },
  "ssl_monitoring":   { "enabled": true, "status": "ok", "total": 5, "errors": 0 },
  "suitestensa_logs": { "enabled": true, "status": "ok", "new_alerts": 3 },
  "dicom_routing":    { "enabled": true, "status": "ok", "total": 8, "errors": 0 }
}
```

⚠️ **Señal para el equipo de servidor:** el agente manda esta clave de nivel superior en
**todos** los reportes desde hace varias versiones, pero `10-contratoingestaagente.md` no la
menciona en absoluto. Como el envelope no tiene schema Pydantic estricto (`Dict[str, Any]`
según ese mismo contrato), probablemente se guarda sin validarse y sin usarse — pero vale la
pena confirmarlo explícitamente: si en algún momento se agrega valor mostrando "módulo apagado"
vs. "módulo activo sin datos" vs. "módulo con error" en el dashboard, esta clave ya trae esa
distinción hecha del lado agente, sin necesitar inferirla de otra forma.

## 9. Qué cambia con el soporte multi-hospital por agente (v4.6) — y qué NO cambia

Desde la migración a `instalaciones[]` (ver
[PLAN_MEJORAS_V4.5.md §9.1](./PLAN_MEJORAS_V4.5.md#91-un-agente-múltiples-sistemas-monitoreados-en-la-misma-red)),
un mismo proceso agente puede tener configurados varios perfiles (ej. el hospital principal +
una cache DICOM), cada uno con su propio `hospital_id`/`auth_token`/config de conexión.

- **Lo que NO cambia:** cada perfil arma y envía su **propio** reporte, con exactamente la
  misma forma descripta en este documento (§1 a §8) — el servidor sigue recibiendo reportes
  individuales indistinguibles de los que mandaría un agente físico separado por hospital. No
  hay ninguna clave nueva a nivel de envelope para "agrupar" reportes de un mismo equipo.
- **Lo que sí cambia (operativo, no de payload):** un mismo equipo Windows puede ahora generar
  N reportes por ciclo en vez de uno — si el equipo de servidor tiene alguna lógica que asuma
  "una IP de origen = un hospital" (ej. rate limiting, listas de IPs permitidas), esta es la
  señal de que eso dejó de ser cierto: la misma IP puede legítimamente mandar varios
  `hospital_id` distintos.

## 10. Errores de envío — comportamiento del agente

Si el `POST` falla (timeout, DNS, TLS, HTTP ≥ 400 vía `raise_for_status()`), el agente:

- **No** reintenta ese envío puntual dentro del mismo ciclo — no hay cola local.
- **No** pierde datos de `application_metrics`/logs de Elastic: el checkpoint solo avanza tras
  un envío exitoso, así que el mismo bloque se reintenta en el ciclo siguiente.
- Para telemetría tipo snapshot (WMI, iDRAC, Proxmox, red), no hay "pérdida" per se: el
  siguiente ciclo manda el estado *actual*, no arrastra el que falló.
- El error se loguea localmente (`activity.log`) pero no se le informa nada al servidor sobre
  el intento fallido — desde la perspectiva del servidor, un reporte que nunca llegó es
  indistinguible de un ciclo en el que el agente estuvo apagado.

## 11. Payload mínimo real

Lo mínimo que arma el agente con todos los módulos deshabilitados (sirve para probar
conectividad/auth sin infraestructura configurada):

```json
{
  "envelope": {
    "schema_version": "4.5",
    "agent_version": "4.5.0",
    "hospital_id": "H42",
    "timestamp": "2026-09-12T10:05:00.123456"
  },
  "collection_meta": {
    "proxmox": {"enabled": false, "status": "disabled"},
    "idrac":   {"enabled": false, "status": "disabled"},
    "wmi":     {"enabled": false, "status": "disabled"},
    "sql":     {"enabled": false, "status": "disabled"},
    "mirth":   {"enabled": false, "status": "disabled"},
    "ssl_monitoring":    {"enabled": false, "status": "disabled"},
    "suitestensa_logs":  {"enabled": false, "status": "disabled"},
    "dicom_routing":     {"enabled": false, "status": "disabled"}
  },
  "software_monitoring": { "dicom_routing_queues": [] },
  "physical_layer": { "network_health": { "...": "siempre se recolecta, ver §4" } },
  "virtual_layer": []
}
```

Nótese que `physical_layer` **nunca** viaja vacío (`{}`) como acepta el contrato de ingesta —
siempre trae al menos `network_health`, porque el agente la recolecta incondicionalmente.
