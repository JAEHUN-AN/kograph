-- kograph raw-layer schema (Oracle 23ai Free / FREEPDB1, APP_USER=kograph)
-- gvenzl 이미지는 init 스크립트를 SYSDBA로 CDB 루트에서 실행하므로
-- 반드시 PDB와 스키마를 먼저 전환해야 한다.
ALTER SESSION SET CONTAINER = FREEPDB1;
ALTER SESSION SET CURRENT_SCHEMA = KOGRAPH;

-- 기업 마스터 (DART corpCode.xml 기반)
CREATE TABLE corp (
    corp_code    CHAR(8)       NOT NULL,   -- DART 고유번호
    stock_code   CHAR(6),                  -- 상장 종목코드 (비상장 NULL)
    corp_name    VARCHAR2(200) NOT NULL,
    modify_date  CHAR(8),                  -- YYYYMMDD
    in_universe  NUMBER(1)     DEFAULT 0 NOT NULL,  -- 분석 대상 여부
    loaded_at    TIMESTAMP     DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT pk_corp PRIMARY KEY (corp_code)
);
CREATE INDEX ix_corp_stock ON corp (stock_code);

-- 공시 목록 (DART list.json 응답 원형 보존)
CREATE TABLE filing (
    rcept_no     CHAR(14)      NOT NULL,   -- 접수번호
    corp_code    CHAR(8)       NOT NULL,
    corp_name    VARCHAR2(200),
    stock_code   CHAR(6),
    report_nm    VARCHAR2(400) NOT NULL,   -- 보고서명
    rcept_dt     CHAR(8)       NOT NULL,   -- 접수일자 YYYYMMDD
    flr_nm       VARCHAR2(200),            -- 공시 제출인
    rm           VARCHAR2(50),             -- 비고 (유/코 등)
    raw_json     CLOB,                     -- 응답 원문 (재처리용)
    doc_text     CLOB,                     -- 본문 텍스트 (Week 2에서 적재)
    loaded_at    TIMESTAMP     DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT pk_filing PRIMARY KEY (rcept_no),
    CONSTRAINT fk_filing_corp FOREIGN KEY (corp_code) REFERENCES corp (corp_code)
);
CREATE INDEX ix_filing_corp_dt ON filing (corp_code, rcept_dt);
CREATE INDEX ix_filing_dt ON filing (rcept_dt);

-- 일별 시세 (pykrx OHLCV)
CREATE TABLE price_daily (
    stock_code   CHAR(6)       NOT NULL,
    trade_date   DATE          NOT NULL,
    open_price   NUMBER(15)    NOT NULL,
    high_price   NUMBER(15)    NOT NULL,
    low_price    NUMBER(15)    NOT NULL,
    close_price  NUMBER(15)    NOT NULL,
    volume       NUMBER(18)    NOT NULL,
    trade_value  NUMBER(20),               -- 거래대금
    change_rate  NUMBER(8, 4),             -- 등락률(%)
    loaded_at    TIMESTAMP     DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT pk_price_daily PRIMARY KEY (stock_code, trade_date)
);
CREATE INDEX ix_price_date ON price_daily (trade_date);

-- ETL 실행 이력 (증분 수집 워터마크)
CREATE TABLE etl_run (
    run_id       NUMBER GENERATED ALWAYS AS IDENTITY,
    job_name     VARCHAR2(100) NOT NULL,   -- 'dart_filings' | 'krx_prices'
    watermark    VARCHAR2(20),             -- 마지막 처리 기준값 (YYYYMMDD 등)
    row_count    NUMBER,
    status       VARCHAR2(20)  NOT NULL,   -- SUCCESS | FAILED
    started_at   TIMESTAMP     NOT NULL,
    finished_at  TIMESTAMP,
    error_msg    VARCHAR2(4000),
    CONSTRAINT pk_etl_run PRIMARY KEY (run_id)
);
CREATE INDEX ix_etl_job ON etl_run (job_name, started_at);
