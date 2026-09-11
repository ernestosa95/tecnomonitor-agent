REM Metricas "al reinicio" via Elastic -- NO tiene cadencia de repeticion.
REM Se dispara una sola vez por corrida, atado a un disparador "Al iniciar
REM el equipo" (no "Repetir cada...") en la Tarea Programada -- pensado para
REM datos que solo tiene sentido recalcular cuando la VM/servidor arranca de
REM nuevo (ej. inventario/estado que no cambia salvo por un reinicio), no
REM para series de tiempo continuas.
REM
REM TODO: sin CALL todavia -- pendiente definir que .conf(s) corresponden a
REM este cajon. Agregar aca el mismo patron que los otros dos .bat:
REM   CALL C:\Estensa\ELK\L\bin\logstash.bat -f C:\Estensa\ELK\Configfile\<archivo>.conf
REM   timeout /t 10 /nobreak
REM (uno por cada .conf que se decida agrupar con esta cadencia).
REM
REM JAVA_HOME externo (ej. una JDK 19+) rompe esta version de Logstash/JRuby
REM ("Unrecognized VM option 'UseConcMarkSweepGC'", o despues
REM InaccessibleObjectException en java.security) -- limpiar aca antes de
REM cualquier CALL que se agregue, para que use la JDK empaquetada.
set JAVA_HOME=
