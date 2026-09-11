# Modelo de seguridad actual

Este documento describe **cómo funciona hoy** la seguridad del agente — qué protege, cómo, y
qué límites tiene ese diseño — a modo de referencia para quien opere o audite el sistema. No es
un plan de remediación; las limitaciones se listan como hechos a tener en cuenta, no como tareas
pendientes de esta documentación.

## Cifrado de credenciales en reposo (`security.py`)

- Todas las contraseñas de `monitor_config.json` (Proxmox, iDRAC, SQL, VMs, Mirth, Elastic) y el
  `auth_token` del servidor central se cifran con **Fernet** (AES-128-CBC + HMAC-SHA256,
  simétrico) antes de escribirse a disco, y se descifran solo en memoria al cargar la
  configuración.
- La clave (`secret.key`) se autogenera al primer uso y se guarda en
  `%PROGRAMDATA%\TecnoMonitor\secret.key`, en el mismo equipo y la misma carpeta que el archivo
  que cifra. Esto protege contra:
  - Lectura casual del `monitor_config.json` (por ejemplo, si se comparte por error un backup
    de configuración o se lo adjunta a un ticket de soporte sin las contraseñas en claro).
  - Que el archivo sea legible pero la clave no lo sea (permisos de carpeta distintos, o si el
    archivo de config se copia a otro medio sin la clave).
- **No protege** contra un atacante con acceso al mismo equipo y a la misma carpeta
  `ProgramData` con permisos de lectura: al estar la clave y el archivo cifrado juntos, quien
  pueda leer ambos puede descifrar todas las credenciales. La carpeta se crea con
  `os.makedirs()` sin ACL explícita — su nivel real de protección depende de los permisos por
  defecto de `%PROGRAMDATA%` en cada instalación de Windows (habitualmente escribible/legible
  por el grupo `Users`, no solo por administradores).
- No hay rotación de clave: es la misma desde que se genera hasta que se borra manualmente el
  archivo (lo que además invalidaría el descifrado de la configuración existente).

## Acceso a la GUI (`main_gui.py` / `web/index.html`)

- Al abrir la GUI aparece un overlay ("Acceso Restringido") que pide un código. La validación
  (`verificar_clave`) ocurre en Python: el frontend nunca conoce ni transmite la clave real, solo
  su hash se compara del lado servidor (`hashlib.sha256`).
- El hash de referencia (`_ADMIN_HASH`) está **fijo en el código fuente**, con el mismo valor en
  todas las instalaciones distribuidas (no se deriva de nada específico de cada hospital ni se
  configura durante la instalación).
- El mecanismo de Eel expone las funciones marcadas `@eel.expose` (`cargar_config`,
  `guardar_config`, `toggle_monitoreo`, los `test_*_gui`, etc.) a través de un servidor
  WebSocket local en cuanto el proceso `TecnoMonitorConfig.exe` arranca — antes de que el
  overlay de la interfaz se resuelva. El overlay controla qué se **muestra** en la página, no
  qué funciones Python quedan disponibles para invocar sobre ese WebSocket.
- Por defecto, Eel sirve en `localhost`, así que el WebSocket no es alcanzable desde otro equipo
  de la red — el alcance de este punto es "otro proceso en el mismo equipo Windows", no la red
  del hospital en general.

## Transporte de red

- **Servidor central:** el envío del envelope (`requests.post(config["central_url"], ...,
  verify=False)`) usa HTTPS pero **sin validar el certificado del servidor**
  (`verify=False`, y `urllib3.disable_warnings(InsecureRequestWarning)` deshabilita el aviso
  correspondiente a nivel global del proceso). Igual tratamiento reciben las conexiones a
  Proxmox, iDRAC y Mirth Connect.
- **ElasticSearch:** las URLs se arman explícitamente con esquema `http://` (no `https://`), y
  la autenticación (usuario/contraseña) viaja como HTTP Basic Auth sobre ese transporte sin
  cifrar, tanto para el módulo de logs de Suitestensa como para el de autoenrute DICOM.
- **Certificados SSL monitoreados** (módulo `obtener_certificados_ssl` / `test_ssl_gui`): el
  contexto TLS se crea deliberadamente con `verify_mode = ssl.CERT_NONE` — es intencional, ya
  que el propósito de ese módulo es *leer* el certificado para auditar su vigencia (incluso si
  está vencido o es autofirmado), no establecer una conexión de confianza para intercambiar
  datos de negocio.
- Efecto combinado de lo anterior: en una red donde un tercero pueda interponerse (LAN
  compartida, switch comprometido, ARP spoofing), es posible interceptar o alterar tanto el
  envelope de telemetría en tránsito hacia el servidor central como las credenciales de
  ElasticSearch.

## Contraseñas de terceros usadas por el agente

- El agente necesita credenciales con permisos de lectura/consulta sobre cada sistema integrado
  (SQL Server, WMI remoto, Redfish de iDRAC, API de Proxmox/vCenter, API de Mirth,
  ElasticSearch). Todas quedan almacenadas cifradas localmente según lo descripto arriba.
- Los campos de contraseña en la GUI (`web/index.html`) tienen `oncopy="return false"` /
  `oncut="return false"` — impiden copiar/cortar el valor desde el campo mediante el mouse o
  atajos estándar del navegador. Es una fricción de UI, no un control de seguridad: el valor
  sigue siendo recuperable con las herramientas de desarrollo del navegador embebido, o leyendo
  directamente `monitor_config.json` + `secret.key` como se describe arriba.

## Superficie expuesta por los "Test conexión"

Las funciones `test_*_gui` (Proxmox, VMware, iDRAC, WMI, Mirth, SSL, ElasticSearch,
`probar_conexion_central`) hacen que el proceso del agente contacte, con las credenciales que
se le pasen, el host/puerto/URL indicados en el formulario — son puntos de conectividad interna
"a demanda" alcanzables por cualquiera que pueda invocar el WebSocket local de Eel (ver más
arriba). En el contexto de uso previsto (agente interno de infraestructura, un administrador
operando su propia GUI) el riesgo práctico es bajo, pero es relevante tenerlo presente si
`TecnoMonitorConfig.exe` llegara a correr en un equipo compartido con usuarios no confiables.

## Origen de las reglas de clasificación de logs

`rules.json` se distribuye junto al ejecutable del servicio y se recarga desde disco en cada
ciclo (ver [REGLAS_LOGS.md](./REGLAS_LOGS.md)). No hay verificación de integridad (firma,
checksum) sobre ese archivo: cualquiera con permisos de escritura sobre la carpeta de
instalación del servicio podría alterar las reglas de clasificación de errores operativos.

## Resumen por área

| Área | Mecanismo actual | Protege contra | No protege contra |
|---|---|---|---|
| Credenciales en `monitor_config.json` | Cifrado Fernet con clave local | Lectura casual del archivo fuera de su equipo de origen | Acceso local con lectura de `secret.key` + config juntos |
| Acceso a la GUI | Código fijo, verificado en Python | Que alguien abra la GUI y navegue el formulario sin conocer el código | Invocación directa de funciones expuestas por Eel en el mismo equipo |
| Envío al servidor central | HTTPS, token Bearer | Lectura pasiva simple en tránsito | MITM activo (no valida certificado del servidor) |
| Envío a ElasticSearch | HTTP + Basic Auth | Nada frente a un observador de la red | Sniffing de credenciales en la LAN |
| Certificados SSL monitoreados | Lectura sin validar cadena (por diseño) | — (no es su función proteger, es auditar vigencia) | — |
