-- ---------------------------------------------------------------------------
-- ext_checkdb.sql
--
-- Chequeo de integridad de bases SQL Server (DBCC CHECKDB) tras un reinicio del servicio SQL.
-- Lo ejecuta ext_checkdb.conf (statement_filepath). El agente lee despues el indice ext_checkdb
-- (ver docs/PLAN_CHECKDB_POST_REINICIO.md y agent sql_integrity.py).
--
-- COMO DECIDE SI HAY QUE CORRER (sin depender de en que VM corra Logstash)
--   El pipeline se invoca cada 15 min. Esta consulta compara el arranque de SQL Server (fecha de
--   creacion de tempdb, se recrea en cada inicio del servicio) contra el ultimo arranque ya
--   procesado, que Logstash mantiene en sql_last_value (columna de seguimiento
--   sqlserver_start_epoch, numerica). Solo si el arranque es nuevo Y ya paso el tiempo de espera
--   corre el CHECKDB; si no, devuelve 0 filas y el valor no avanza, asi que se reintenta en la
--   proxima invocacion. Eso cubre un SQL que tarda en levantar tras un corte de energia.
--
-- PRIMERA CORRIDA
--   sql_last_value arranca en 0. Devuelve UNA fila BASELINE (que ext_checkdb.conf no indexa) para
--   sembrar el valor. NO corre ningun CHECKDB al instalar el pipeline.
--
-- IMPORTANTE - NO USAR DOS PUNTOS EN ESTE ARCHIVO
--   Logstash reemplaza el marcador de sql_last_value antes de enviar el texto a SQL Server.
--   Para que no confunda otros textos con marcadores, este archivo no usa dos puntos en ningun
--   otro lado (ni en comentarios ni en literales de hora). Mantener asi al editarlo.
--
-- COMO PROBARLO A MANO EN SSMS (sin Logstash)
--   1) Copiar este script a una ventana de SSMS.
--   2) Reemplazar el marcador del ultimo valor procesado (la linea DECLARE @UltimoEpoch) por 0
--      para ver la fila BASELINE, o por 1 para forzar el chequeo. Para probar rapido, dejar en
--      @Bases una o dos bases chicas y poner @SoloFisico = 1 y @EsperaMin = 0.
--
-- EDITABLE POR HOSPITAL - la seccion CONFIGURACION (tipo de chequeo, esperas y lista de bases).
-- ---------------------------------------------------------------------------
SET NOCOUNT ON;

-- ============================ CONFIGURACION ============================
DECLARE @SoloFisico  BIT = 0;    -- 0 = chequeo completo (como la consulta manual), 1 = PHYSICAL_ONLY (liviano)
DECLARE @EsperaMin   INT = 10;   -- minutos tras el arranque de SQL antes de chequear
DECLARE @EsperaMaxMin INT = 60;  -- hasta cuando esperar que todas las bases esten ONLINE

DECLARE @Bases TABLE (nombre SYSNAME);
INSERT INTO @Bases (nombre) VALUES
    (N'AspnetDB'), (N'DBScheduler'), (N'DicomedBroker'), (N'DicomedBrokerStorico'), (N'DICOMedP@CS'),
    (N'ExtensaAnalytics'), (N'ExtensaCardio'), (N'ExtensaConnect'), (N'ExtensaCustomPage'),
    (N'ExtensaDataExport'), (N'ExtensaGeneric'), (N'ExtensaHistory'), (N'ExtensaIntegration'),
    (N'ExtensaIntegrationGateway'), (N'ExtensaMPS'), (N'ExtensaPACS'), (N'ExtensaPatient'),
    (N'ExtensaPublication'), (N'ExtensaRadio'), (N'ExtensaRT'), (N'ExtensaVNA'), (N'ExtensaWarehouse'),
    (N'eXtensaWRK'), (N'MediaProducerDB'), (N'SL_UserAndConfig'), (N'support');
-- Las bases de esta lista que no existan en el servidor se ignoran. Agregar aca las que falten.
-- =======================================================================

DECLARE @UltimoEpoch BIGINT = :sql_last_value;
DECLARE @Ahora DATETIME = GETDATE();
DECLARE @Arranque DATETIME;
SELECT @Arranque = create_date FROM sys.databases WHERE name = N'tempdb';
DECLARE @ArranqueEpoch BIGINT = DATEDIFF(SECOND, '19700101', @Arranque);
DECLARE @MinDesdeArranque INT = DATEDIFF(MINUTE, @Arranque, @Ahora);
DECLARE @Tipo VARCHAR(20) = CASE WHEN @SoloFisico = 1 THEN 'physical_only' ELSE 'full' END;

DECLARE @Salida TABLE (
    dbname      NVARCHAR(128),
    estado      VARCHAR(12),
    error_count INT,
    detalle     NVARCHAR(500),
    duration_s  INT,
    check_type  VARCHAR(20),
    checked_at  VARCHAR(19)
);

IF @UltimoEpoch = 0
BEGIN
    -- Primera corrida - solo siembra el ultimo arranque conocido. No chequea nada.
    INSERT INTO @Salida VALUES (N'', 'BASELINE', 0, N'', 0, @Tipo, CONVERT(VARCHAR(19), @Ahora, 126));
END
ELSE IF @ArranqueEpoch > @UltimoEpoch
        AND @MinDesdeArranque >= @EsperaMin
        AND (
            @MinDesdeArranque >= @EsperaMaxMin
            OR NOT EXISTS (
                SELECT 1
                FROM sys.databases d
                JOIN @Bases b ON b.nombre COLLATE DATABASE_DEFAULT = d.name COLLATE DATABASE_DEFAULT
                WHERE d.state_desc <> 'ONLINE'
            )
        )
BEGIN
    -- Estructura de la salida de DBCC CHECKDB WITH TABLERESULTS (la misma de la consulta manual).
    IF OBJECT_ID('tempdb..#DBCC_OUTPUT') IS NOT NULL DROP TABLE #DBCC_OUTPUT;
    CREATE TABLE #DBCC_OUTPUT (
        Error INT NULL, Level INT NULL, State INT NULL, MessageText NVARCHAR(4000) NULL,
        RepairLevel NVARCHAR(60) NULL, Status INT NULL, DbId INT NULL, DbFragId INT NULL,
        ObjectId BIGINT NULL, IndexId BIGINT NULL, PartitionId BIGINT NULL, AllocUnitId BIGINT NULL,
        RidDbId INT NULL, RidPgid INT NULL, RidRecId INT NULL
    );

    DECLARE @Db SYSNAME;
    DECLARE @Estado NVARCHAR(60);
    DECLARE @Sql NVARCHAR(MAX);
    DECLARE @Ini DATETIME;
    DECLARE @Errores INT;
    DECLARE @Resumen NVARCHAR(4000);
    DECLARE @Detalle NVARCHAR(4000);

    DECLARE cur CURSOR LOCAL FAST_FORWARD FOR
        SELECT d.name, d.state_desc
        FROM sys.databases d
        JOIN @Bases b ON b.nombre COLLATE DATABASE_DEFAULT = d.name COLLATE DATABASE_DEFAULT
        ORDER BY d.name;

    OPEN cur;
    FETCH NEXT FROM cur INTO @Db, @Estado;
    WHILE @@FETCH_STATUS = 0
    BEGIN
        SET @Ini = GETDATE();

        IF @Estado <> 'ONLINE'
        BEGIN
            INSERT INTO @Salida VALUES (@Db, 'NOT_ONLINE', 0, LEFT(@Estado, 500), 0, @Tipo, CONVERT(VARCHAR(19), GETDATE(), 126));
        END
        ELSE
        BEGIN
            BEGIN TRY
                TRUNCATE TABLE #DBCC_OUTPUT;
                SET @Sql = N'DBCC CHECKDB (' + QUOTENAME(@Db) + N') WITH NO_INFOMSGS, ALL_ERRORMSGS, TABLERESULTS'
                         + CASE WHEN @SoloFisico = 1 THEN N', PHYSICAL_ONLY' ELSE N'' END + N';';
                INSERT INTO #DBCC_OUTPUT EXEC sp_executesql @Sql;

                SET @Errores = (SELECT COUNT(*) FROM #DBCC_OUTPUT);

                IF @Errores = 0
                BEGIN
                    INSERT INTO @Salida VALUES (@Db, 'OK', 0, N'', DATEDIFF(SECOND, @Ini, GETDATE()), @Tipo, CONVERT(VARCHAR(19), GETDATE(), 126));
                END
                ELSE
                BEGIN
                    -- Si vino el resumen de CHECKDB ("CHECKDB found N allocation errors and M consistency errors")
                    -- se usa esa suma como cantidad de errores; si no, la cantidad de filas.
                    SET @Resumen = NULL;
                    SELECT TOP 1 @Resumen = MessageText FROM #DBCC_OUTPUT WHERE MessageText LIKE N'CHECKDB found%';
                    IF @Resumen IS NOT NULL
                    BEGIN
                        BEGIN TRY
                            SET @Errores =
                                  CAST(SUBSTRING(@Resumen, 15, CHARINDEX(N' allocation', @Resumen) - 15) AS INT)
                                + CAST(SUBSTRING(@Resumen, CHARINDEX(N'and ', @Resumen) + 4,
                                                 CHARINDEX(N' consistency', @Resumen) - CHARINDEX(N'and ', @Resumen) - 4) AS INT);
                        END TRY
                        BEGIN CATCH
                            SET @Resumen = @Resumen;   -- no se pudo interpretar el resumen, queda la cantidad de filas
                        END CATCH
                    END

                    -- Detalle - el resumen primero (para que el recorte no lo pierda) y hasta 2 mensajes mas.
                    SET @Detalle = ISNULL(@Resumen, N'');
                    SELECT TOP 2 @Detalle = @Detalle + CASE WHEN LEN(@Detalle) > 0 THEN N' | ' ELSE N'' END + LEFT(MessageText, 200)
                    FROM #DBCC_OUTPUT
                    WHERE MessageText IS NOT NULL AND MessageText NOT LIKE N'CHECKDB found%';

                    INSERT INTO @Salida VALUES (@Db, 'ERROR', @Errores, LEFT(@Detalle, 500), DATEDIFF(SECOND, @Ini, GETDATE()), @Tipo, CONVERT(VARCHAR(19), GETDATE(), 126));
                END
            END TRY
            BEGIN CATCH
                INSERT INTO @Salida VALUES (@Db, 'ERROR', 0, LEFT(ERROR_MESSAGE(), 500), DATEDIFF(SECOND, @Ini, GETDATE()), @Tipo, CONVERT(VARCHAR(19), GETDATE(), 126));
            END CATCH
        END

        FETCH NEXT FROM cur INTO @Db, @Estado;
    END

    CLOSE cur;
    DEALLOCATE cur;
    DROP TABLE #DBCC_OUTPUT;
END

-- Siempre devuelve un result set (0 filas si no hay nada que hacer). Cada fila lleva el arranque de SQL
-- como columna de seguimiento para Logstash.
SELECT
    dbname, estado, error_count, detalle, duration_s, check_type, checked_at,
    CONVERT(VARCHAR(19), @Arranque, 126) AS sqlserver_start_time,
    @ArranqueEpoch                       AS sqlserver_start_epoch
FROM @Salida;
