REM Metricas "tiempo real" via Elastic -- cadencia cada 5 min. Cajon para
REM todo lo que necesite esa frecuencia (hoy solo autoenrute DICOM; sumar
REM otro CALL+timeout aca si aparece otra medicion con esta misma cadencia,
REM en vez de crear una Tarea Programada nueva -- mismo criterio que
REM ext_kpis_negocio-all-sito.bat con la cadencia de 1 hora).
REM
REM JAVA_HOME externo (ej. una JDK 19+) rompe esta version de Logstash/JRuby
REM ("Unrecognized VM option 'UseConcMarkSweepGC'", o despues
REM InaccessibleObjectException en java.security) -- se limpia aca para que
REM use la JDK que Logstash trae empaquetada, sin tocar la variable a nivel
REM sistema (por si algo mas en el servidor si la necesita).
set JAVA_HOME=
CALL C:\Estensa\ELK\L\bin\logstash.bat -f C:\Estensa\ELK\Configfile\ext_dicom_queues.conf
timeout /t 30 /nobreak
