REM KPIs de negocio via Elastic (RIS/PACS/usuarios) - una sola Tarea Programada
REM agrupa los 3 (misma cadencia: cada 1 hora). Para sumar una medicion nueva
REM con esta misma cadencia, agregar otro bloque CALL+timeout aca en vez de
REM crear una tarea nueva.
REM
REM JAVA_HOME externo (ej. una JDK 19+) rompe esta version de Logstash/JRuby
REM ("Unrecognized VM option 'UseConcMarkSweepGC'", o despues
REM InaccessibleObjectException en java.security) -- se limpia aca (una sola
REM vez, vale para los 3 CALL de abajo) para que use la JDK empaquetada.
set JAVA_HOME=
CALL C:\Estensa\ELK\L\bin\logstash.bat -f C:\Estensa\ELK\Configfile\ext_ris_metrics.conf
timeout /t 10 /nobreak
CALL C:\Estensa\ELK\L\bin\logstash.bat -f C:\Estensa\ELK\Configfile\ext_pacs_metrics.conf
timeout /t 10 /nobreak
CALL C:\Estensa\ELK\L\bin\logstash.bat -f C:\Estensa\ELK\Configfile\ext_users_metrics.conf
timeout /t 30 /nobreak
