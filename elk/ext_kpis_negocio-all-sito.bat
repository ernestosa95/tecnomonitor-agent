REM KPIs de negocio via Elastic (RIS/PACS/usuarios) - una sola Tarea Programada
REM agrupa los 3 (misma cadencia: cada 1 hora). Para sumar una medicion nueva
REM con esta misma cadencia, agregar otro bloque CALL+timeout aca en vez de
REM crear una tarea nueva.
CALL C:\Estensa\ELK\L\bin\logstash.bat -f C:\Estensa\ELK\Configfile\ext_ris_metrics.conf
timeout /t 10 /nobreak
CALL C:\Estensa\ELK\L\bin\logstash.bat -f C:\Estensa\ELK\Configfile\ext_pacs_metrics.conf
timeout /t 10 /nobreak
CALL C:\Estensa\ELK\L\bin\logstash.bat -f C:\Estensa\ELK\Configfile\ext_users_metrics.conf
timeout /t 30 /nobreak
