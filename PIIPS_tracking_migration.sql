/* ============================================================================
   PIIPS 2.3+ - who / when tracking: tables and columns
   Run once on the target database (e.g. PIIPS_UAT). Safe to re-run: every step
   checks first. Nothing is dropped or changed on existing data.
   ============================================================================ */

-- USE PIIPS_UAT;
-- GO

/* 1. History table: one row per action on a file / batch / user ------------- */
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'tbl_Audit_Event')
BEGIN
    CREATE TABLE dbo.tbl_Audit_Event (
        Id                 BIGINT IDENTITY(1,1) PRIMARY KEY,
        EventDatetime      DATETIME      NOT NULL DEFAULT GETDATE(),
        UserId             INT           NULL,
        Entity             NVARCHAR(10)  NOT NULL,          -- INVOICE | BATCH | USER
        BatchName          NVARCHAR(200) NULL,
        Purchase_Header_ID INT           NULL,
        FileName           NVARCHAR(400) NULL,
        InvoiceNo          NVARCHAR(100) NULL,
        Action             NVARCHAR(40)  NOT NULL,
        FromStatus         NVARCHAR(80)  NULL,
        ToStatus           NVARCHAR(80)  NULL,
        Detail             NVARCHAR(600) NULL
    );
    CREATE INDEX IX_Audit_Header ON dbo.tbl_Audit_Event (Purchase_Header_ID, EventDatetime);
    CREATE INDEX IX_Audit_Batch  ON dbo.tbl_Audit_Event (BatchName, EventDatetime);
    CREATE INDEX IX_Audit_Time   ON dbo.tbl_Audit_Event (EventDatetime);
END
GO

/* 2. tbl_Purchase_Tracker: current who / when per file ---------------------- */
IF OBJECT_ID('dbo.tbl_Purchase_Tracker') IS NOT NULL
BEGIN
    IF COL_LENGTH('dbo.tbl_Purchase_Tracker', 'BuyerOrderByID')     IS NULL ALTER TABLE dbo.tbl_Purchase_Tracker ADD BuyerOrderByID     INT          NULL;
    IF COL_LENGTH('dbo.tbl_Purchase_Tracker', 'BuyerOrderDatetime') IS NULL ALTER TABLE dbo.tbl_Purchase_Tracker ADD BuyerOrderDatetime DATETIME     NULL;
    IF COL_LENGTH('dbo.tbl_Purchase_Tracker', 'BuyerOrderSource')   IS NULL ALTER TABLE dbo.tbl_Purchase_Tracker ADD BuyerOrderSource   NVARCHAR(30) NULL;  -- PDF | MANUAL
    IF COL_LENGTH('dbo.tbl_Purchase_Tracker', 'VendorCodeByID')     IS NULL ALTER TABLE dbo.tbl_Purchase_Tracker ADD VendorCodeByID     INT          NULL;
    IF COL_LENGTH('dbo.tbl_Purchase_Tracker', 'VendorCodeDatetime') IS NULL ALTER TABLE dbo.tbl_Purchase_Tracker ADD VendorCodeDatetime DATETIME     NULL;
    IF COL_LENGTH('dbo.tbl_Purchase_Tracker', 'VendorCodeSource')   IS NULL ALTER TABLE dbo.tbl_Purchase_Tracker ADD VendorCodeSource   NVARCHAR(30) NULL;  -- PDF | Service First | MANUAL
    IF COL_LENGTH('dbo.tbl_Purchase_Tracker', 'LoadedByID')         IS NULL ALTER TABLE dbo.tbl_Purchase_Tracker ADD LoadedByID         INT          NULL;
    IF COL_LENGTH('dbo.tbl_Purchase_Tracker', 'LoadedDatetime')     IS NULL ALTER TABLE dbo.tbl_Purchase_Tracker ADD LoadedDatetime     DATETIME     NULL;
    IF COL_LENGTH('dbo.tbl_Purchase_Tracker', 'PostedByID')         IS NULL ALTER TABLE dbo.tbl_Purchase_Tracker ADD PostedByID         INT          NULL;
    IF COL_LENGTH('dbo.tbl_Purchase_Tracker', 'PostedDatetime')     IS NULL ALTER TABLE dbo.tbl_Purchase_Tracker ADD PostedDatetime     DATETIME     NULL;
    IF COL_LENGTH('dbo.tbl_Purchase_Tracker', 'CompletedByID')      IS NULL ALTER TABLE dbo.tbl_Purchase_Tracker ADD CompletedByID      INT          NULL;
    IF COL_LENGTH('dbo.tbl_Purchase_Tracker', 'CompletedDatetime')  IS NULL ALTER TABLE dbo.tbl_Purchase_Tracker ADD CompletedDatetime  DATETIME     NULL;
    IF COL_LENGTH('dbo.tbl_Purchase_Tracker', 'DownloadedByID')     IS NULL ALTER TABLE dbo.tbl_Purchase_Tracker ADD DownloadedByID     INT          NULL;
    IF COL_LENGTH('dbo.tbl_Purchase_Tracker', 'DownloadedDatetime') IS NULL ALTER TABLE dbo.tbl_Purchase_Tracker ADD DownloadedDatetime DATETIME     NULL;
    IF COL_LENGTH('dbo.tbl_Purchase_Tracker', 'LastStatusByID')     IS NULL ALTER TABLE dbo.tbl_Purchase_Tracker ADD LastStatusByID     INT          NULL;
    IF COL_LENGTH('dbo.tbl_Purchase_Tracker', 'LastStatusDatetime') IS NULL ALTER TABLE dbo.tbl_Purchase_Tracker ADD LastStatusDatetime DATETIME     NULL;
END
GO

/* 3. tbl_Purchase_Header: Buyer Order No / NAV Vendor Code who + when -------- */
IF OBJECT_ID('dbo.tbl_Purchase_Header') IS NOT NULL
BEGIN
    IF COL_LENGTH('dbo.tbl_Purchase_Header', 'BuyerOrderNoUpdatedByID')      IS NULL ALTER TABLE dbo.tbl_Purchase_Header ADD BuyerOrderNoUpdatedByID      INT      NULL;
    IF COL_LENGTH('dbo.tbl_Purchase_Header', 'BuyerOrderNoUpdatedDatetime')  IS NULL ALTER TABLE dbo.tbl_Purchase_Header ADD BuyerOrderNoUpdatedDatetime  DATETIME NULL;
    IF COL_LENGTH('dbo.tbl_Purchase_Header', 'NavVendorCodeUpdatedByID')     IS NULL ALTER TABLE dbo.tbl_Purchase_Header ADD NavVendorCodeUpdatedByID     INT      NULL;
    IF COL_LENGTH('dbo.tbl_Purchase_Header', 'NavVendorCodeUpdatedDatetime') IS NULL ALTER TABLE dbo.tbl_Purchase_Header ADD NavVendorCodeUpdatedDatetime DATETIME NULL;
END
GO

/* 4. tbl_Purchase_Line: part description who + when ------------------------- */
IF OBJECT_ID('dbo.tbl_Purchase_Line') IS NOT NULL
BEGIN
    IF COL_LENGTH('dbo.tbl_Purchase_Line', 'PartDescriptionUpdatedByID')     IS NULL ALTER TABLE dbo.tbl_Purchase_Line ADD PartDescriptionUpdatedByID     INT      NULL;
    IF COL_LENGTH('dbo.tbl_Purchase_Line', 'PartDescriptionUpdatedDatetime') IS NULL ALTER TABLE dbo.tbl_Purchase_Line ADD PartDescriptionUpdatedDatetime DATETIME NULL;
END
GO

/* 5. tbl_BatchDownload: who downloaded (date + count already exist) --------- */
IF OBJECT_ID('dbo.tbl_BatchDownload') IS NOT NULL
   AND COL_LENGTH('dbo.tbl_BatchDownload', 'LastDownloadedByID') IS NULL
    ALTER TABLE dbo.tbl_BatchDownload ADD LastDownloadedByID INT NULL;
GO

/* 6. tbl_User: login tracking ---------------------------------------------- */
IF OBJECT_ID('dbo.tbl_User') IS NOT NULL
BEGIN
    IF COL_LENGTH('dbo.tbl_User', 'LastLoginDatetime')  IS NULL ALTER TABLE dbo.tbl_User ADD LastLoginDatetime  DATETIME NULL;
    IF COL_LENGTH('dbo.tbl_User', 'LastLogoutDatetime') IS NULL ALTER TABLE dbo.tbl_User ADD LastLogoutDatetime DATETIME NULL;
    IF COL_LENGTH('dbo.tbl_User', 'IsLoggedIn')         IS NULL ALTER TABLE dbo.tbl_User ADD IsLoggedIn         BIT      NOT NULL CONSTRAINT DF_tbl_User_IsLoggedIn DEFAULT 0;
    IF COL_LENGTH('dbo.tbl_User', 'LastSeenDatetime')   IS NULL ALTER TABLE dbo.tbl_User ADD LastSeenDatetime   DATETIME NULL;
END
GO

/* 7. Check: every line should show 1 (present) --------------------------------- */
SELECT 'tbl_Audit_Event'                          AS Item, CASE WHEN OBJECT_ID('dbo.tbl_Audit_Event') IS NOT NULL THEN 1 ELSE 0 END AS Present
UNION ALL SELECT 'tbl_Purchase_Tracker (16 cols)',  CASE WHEN (SELECT COUNT(*) FROM sys.columns WHERE object_id = OBJECT_ID('dbo.tbl_Purchase_Tracker') AND name IN
        ('BuyerOrderByID','BuyerOrderDatetime','BuyerOrderSource','VendorCodeByID','VendorCodeDatetime','VendorCodeSource','LoadedByID','LoadedDatetime',
         'PostedByID','PostedDatetime','CompletedByID','CompletedDatetime','DownloadedByID','DownloadedDatetime','LastStatusByID','LastStatusDatetime')) = 16 THEN 1 ELSE 0 END
UNION ALL SELECT 'tbl_Purchase_Header (4 cols)',    CASE WHEN (SELECT COUNT(*) FROM sys.columns WHERE object_id = OBJECT_ID('dbo.tbl_Purchase_Header') AND name IN
        ('BuyerOrderNoUpdatedByID','BuyerOrderNoUpdatedDatetime','NavVendorCodeUpdatedByID','NavVendorCodeUpdatedDatetime')) = 4 THEN 1 ELSE 0 END
UNION ALL SELECT 'tbl_Purchase_Line (2 cols)',      CASE WHEN (SELECT COUNT(*) FROM sys.columns WHERE object_id = OBJECT_ID('dbo.tbl_Purchase_Line') AND name IN
        ('PartDescriptionUpdatedByID','PartDescriptionUpdatedDatetime')) = 2 THEN 1 ELSE 0 END
UNION ALL SELECT 'tbl_BatchDownload.LastDownloadedByID', CASE WHEN COL_LENGTH('dbo.tbl_BatchDownload', 'LastDownloadedByID') IS NOT NULL THEN 1 ELSE 0 END
UNION ALL SELECT 'tbl_User (4 cols)',               CASE WHEN (SELECT COUNT(*) FROM sys.columns WHERE object_id = OBJECT_ID('dbo.tbl_User') AND name IN
        ('LastLoginDatetime','LastLogoutDatetime','IsLoggedIn','LastSeenDatetime')) = 4 THEN 1 ELSE 0 END;
