REM Integridad de bases SQL Server (DBCC CHECKDB) tras un reinicio -- cadencia cada 30 min.
REM Este cajon SOLO DECIDE si hubo un reinicio nuevo (consulta barata); el CHECKDB, que puede
REM durar horas, corre unicamente si lo hubo -- ver ext_checkdb.conf y
REM docs/PLAN_CHECKDB_POST_REINICIO.md.
REM
REM CAJON PROPIO, NO sumarlo a ext_tiempo_real-all-sito.bat: un CHECKDB largo dentro de ese cajon
REM bloquearia todos los pipelines de 5 minutos (autoenrute DICOM, etc.) durante horas y sus
REM datos quedarian obsoletos.
REM
REM CONFIGURACION OBLIGATORIA DE LA TAREA PROGRAMADA de este cajon:
REM   - Repetir cada 30 minutos.
REM   - "Si la tarea ya se esta ejecutando" = "No iniciar una instancia nueva" (evita que se
REM     encimen dos corridas mientras un CHECKDB largo sigue en curso).
REM
REM --path.data PROPIO: dos Logstash a la vez con el mismo data dir se bloquean entre si, y este
REM cajon puede encimarse con los de 5 minutos. Crear la carpeta antes (o dejar que Logstash la cree).
REM
REM JAVA_HOME externo (ej. una JDK 19+) rompe esta version de Logstash/JRuby
REM ("Unrecognized VM option 'UseConcMarkSweepGC'", o despues
REM InaccessibleObjectException en java.security) -- se limpia aca para que
REM use la JDK que Logstash trae empaquetada.
set JAVA_HOME=
CALL C:\Estensa\ELK\L\bin\logstash.bat --path.data C:\Estensa\ELK\data_checkdb -f C:\Estensa\ELK\Configfile\ext_checkdb.conf
timeout /t 30 /nobreak
