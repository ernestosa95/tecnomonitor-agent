# Documentación — TecnoMonitor Agent

Índice de documentación técnica del agente de monitoreo TecnoMonitor (v4.5.0).
Esta carpeta describe el sistema **tal como está implementado hoy** en el repositorio;
no incluye cambios ni propuestas de código, solo documentación de referencia.

## Contenido

| Documento | Contenido |
|---|---|
| [ARQUITECTURA.md](./ARQUITECTURA.md) | Componentes del sistema, cómo se relacionan, ciclo de vida del servicio |
| [INSTALACION.md](./INSTALACION.md) | Requisitos, instalador Inno Setup, qué hace en el equipo destino |
| [CONFIGURACION.md](./CONFIGURACION.md) | Esquema completo de `monitor_config.json`, valores por defecto |
| [MODULOS.md](./MODULOS.md) | Qué recolecta cada módulo (Proxmox/VMware, iDRAC, WMI, SQL, Mirth, SSL, Elastic) |
| [ENVELOPE_API.md](./ENVELOPE_API.md) | Estructura del JSON que el agente envía al servidor central |
| [REGLAS_LOGS.md](./REGLAS_LOGS.md) | Formato de `rules.json` y motor de clasificación de errores de Suitestensa |
| [OPERACION.md](./OPERACION.md) | Runbook: logs, checkpoints, control del servicio, diagnóstico de problemas comunes |
| [BUILD.md](./BUILD.md) | Cómo se compila y empaqueta (`build.bat`, PyInstaller, Inno Setup) |
| [SEGURIDAD.md](./SEGURIDAD.md) | Modelo de seguridad actual: qué protege, qué no, y limitaciones conocidas |
| [CHANGELOG.md](./CHANGELOG.md) | Historial de versiones reconstruido a partir de comentarios en el código |
| [PLAN_MEJORAS_V4.5.md](./PLAN_MEJORAS_V4.5.md) | Plan de mejoras para v4.5: bugs de paridad con el contrato del servidor, seguridad, robustez, performance y secuencia de release |
| [ELK_RIS_METRICS.md](./ELK_RIS_METRICS.md) | Migración de KPIs de RIS de SQL directo a ElasticSearch (Logstash), mismo patrón que el autoenrute DICOM |

## Resumen de una línea

TecnoMonitor Agent es un **servicio de Windows** que cada N minutos recolecta telemetría de
infraestructura (hipervisor, servidor Dell/iDRAC, VMs/workstations vía WMI), KPIs de negocio
(SQL Server de Extensa RIS/PACS), estado de integraciones HL7 (Mirth Connect), vigencia de
certificados SSL y errores operativos (ElasticSearch/Suitestensa), arma un único reporte JSON
y lo envía por HTTPS a un servidor central. Una GUI local (Eel + HTML/JS) permite configurarlo
y controlar el servicio sin editar archivos a mano.

## Componentes principales del repositorio

```
agent_logic.py        Toda la lógica de recolección (Proxmox, VMware, iDRAC, WMI, SQL, SSL, Elastic) y el ciclo principal
mirth_collector.py    Recolección específica de Mirth Connect (API REST)
security.py           Cifrado simétrico (Fernet) de credenciales guardadas en disco
headless_service.py   Entry point del servicio de Windows (bucle infinito, logging, mutex anti-duplicados)
service_control.py    Wrapper sobre el SCM de Windows (start/stop/status) usado por la GUI
main_gui.py           Entry point de la GUI (Eel), expone funciones Python al frontend
web/                  Frontend de la GUI (index.html, script.js, style.css)
rules.json            Reglas de clasificación de errores para el módulo de logs Elastic
elk/                  Referencia de pipelines de Logstash (.conf/pipelines.yml) para KPIs de RIS vía Elastic — no se empaqueta ni se instala, es para aplicar a mano en el servidor ELK del hospital
debug_disk.py         Script suelto de diagnóstico manual de latencia de disco (no se empaqueta)
TecnoMonitor.iss       Script de Inno Setup para el instalador
build.bat              Pipeline de compilación (PyInstaller + Inno Setup)
```
