REM JAVA_HOME externo (ej. una JDK 19+) rompe esta version de Logstash/JRuby
REM ("Unrecognized VM option 'UseConcMarkSweepGC'", o despues
REM InaccessibleObjectException en java.security) -- se limpia aca para que
REM use la JDK que Logstash trae empaquetada, sin tocar la variable a nivel
REM sistema (por si algo mas en el servidor si la necesita).
set JAVA_HOME=
CALL C:\Estensa\ELK\L\bin\logstash.bat -f C:\Estensa\ELK\Configfile\ext_dicom_queues.conf
timeout /t 30 /nobreak
