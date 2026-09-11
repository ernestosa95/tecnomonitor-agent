# Reglas de clasificación de logs — `rules.json`

Usado por el módulo de logs de Suitestensa (`recolectar_logs_elastic`, ver
[MODULOS.md](./MODULOS.md#elasticsearch--logs-de-suitestensa)). Es un archivo estático que
viaja empaquetado junto al ejecutable del servicio, **no** es parte de la configuración por
hospital ni se edita desde la GUI.

## Ubicación y ciclo de vida

- En desarrollo: raíz del repo, junto a `agent_logic.py`.
- Empaquetado: `build.bat` lo agrega al build del servicio con
  `pyinstaller --add-data "rules.json;."`, así que termina junto al `.exe` en
  `dist\TecnoMonitorService\`.
- Se resuelve en runtime como
  `os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules.json")` — es decir, relativo
  al ejecutable, no a `ProgramData`. **Actualizar las reglas requiere redistribuir este archivo
  junto con (o en reemplazo de) el del agente instalado**, no un simple guardado desde la GUI.
- Se carga y **precompila una vez por ciclo** (no una vez al arrancar el servicio): cada
  llamada a `recolectar_logs_elastic` vuelve a leer y compilar `rules.json`, así que un cambio
  en el archivo se aplica en el próximo ciclo sin reiniciar el servicio.

## Formato

Array de objetos:

```json
{
  "id": "DCM-COM-01",
  "regex": "(?i)0x80040221|Interop\\.DCMProxy|MPSClientEngine.*Exception",
  "service_target": "*",
  "severidad": "HIGH"
}
```

| Campo | Tipo | Descripción |
|---|---|---|
| `id` | string | Identificador de la regla. Viaja tal cual como `rule_id` en el envelope enviado al servidor central |
| `regex` | string | Expresión regular Python, compilada con flags `re.I \| re.S` (case-insensitive, `.` matchea saltos de línea) |
| `service_target` | string | `"*"` para aplicar a cualquier proceso/servicio, o un fragmento de nombre de proceso (ej. `"RADIO"`, `"IIS"`, `"SUITESTENSAPACSSRV"`) que debe estar contenido en el campo `Process` del log |
| `severidad` | string | Metadato informativo (`CRITICAL`/`HIGH`/`MEDIUM`/`LOW`) — **no se envía al servidor central**; solo se usa localmente/para lectura humana del archivo. El envelope solo lleva `{rule_id, count}` |

## Estado actual del archivo (45 reglas)

| Severidad | Cantidad |
|---|---|
| CRITICAL | 3 |
| HIGH | 17 |
| MEDIUM | 15 |
| LOW | 10 |

La gran mayoría (39 de 45) usa `service_target: "*"`; solo un puñado apunta a un proceso
específico (`RADIO`, `IIS`, `w3wp`, `SUITESTENSAPACSSRV`).

## Cómo se evalúa cada log

Por cada documento devuelto por Elastic (`level` en `Error`/`Fatal`/`Critical`), en
`recolectar_logs_elastic`:

1. Se recorren las reglas **en el orden del archivo** y se usa la **primera que matchea**
   (`target_match` por `service_target` Y `compiled_regex.search(msg)`) — el orden de
   `rules.json` importa: una regla genérica (`"*"`) puesta antes que una específica puede
   "tapar" a la específica si su regex también matchea el mismo mensaje.
2. Si ninguna regla matchea, el log se clasifica como `UNKNOWN-ERR-99`.

## Laboratorio de patrones desconocidos — `unknowns_lab.json`

Cuando un log cae en `UNKNOWN-ERR-99`, además de contarlo para el envelope, el agente lo
registra localmente en `%PROGRAMDATA%\TecnoMonitor\unknowns_lab.json`, agrupando por los
primeros 100 caracteres del mensaje como clave:

```json
{
  "patterns": {
    "SqlException: Timeout expired. The timeout period elapsed prior": {
      "first_detected": "2026-09-10T08:12:00Z",
      "last_detected": "2026-09-11T09:40:00Z",
      "total_hits": 14,
      "services": ["SUITESTENSAPACSSRV", "w3wp"],
      "full_sample": "SqlException: Timeout expired. The timeout period elapsed prior to completion of the operation..."
    }
  }
}
```

Esto le da a un técnico en el sitio (o a quien mantiene `rules.json`) evidencia completa para
decidir si vale la pena dar de alta una regla nueva, sin tener que ir a buscar el log crudo en
Elastic. Este archivo es puramente local — su contenido **no** se envía al servidor central
(el envelope solo incluye el conteo agregado bajo `UNKNOWN-ERR-99`, ver
[ENVELOPE_API.md](./ENVELOPE_API.md)).

## Agregar una regla nueva

1. Revisar `unknowns_lab.json` en el equipo del hospital (o los logs crudos de Elastic) para
   confirmar el patrón exacto del mensaje.
2. Agregar una entrada a `rules.json` con un `id` único, `regex` lo más específica posible
   (evitar que una regex nueva "capture" mensajes que ya cubre una regla existente, dado el
   matching por orden-de-lista-primera-que-matchea), `service_target` acotado si aplica, y
   `severidad` según el impacto real observado.
3. Redistribuir `rules.json` junto al agente (no hay mecanismo de push remoto de reglas —
   viaja empaquetado con el ejecutable del servicio, ver [BUILD.md](./BUILD.md)).
