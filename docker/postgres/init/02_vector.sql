-- 공시 본문 청크 + 임베딩. vanilla RAG 경로이자 하이브리드 리트리버의 벡터 축.
-- 차원 1024는 기본 임베딩 모델(BAAI/bge-m3) 기준 — 모델 교체 시 함께 변경해야 한다.

CREATE TABLE IF NOT EXISTS doc_chunk (
    chunk_id    BIGSERIAL PRIMARY KEY,
    rcept_no    CHAR(14)     NOT NULL,   -- 근거 공시 (Oracle filing과 조인 키)
    corp_name   VARCHAR(200),
    report_nm   VARCHAR(400),
    rcept_dt    CHAR(8),                 -- YYYYMMDD
    chunk_idx   INT          NOT NULL,   -- 공시 내 청크 순번
    content     TEXT         NOT NULL,
    embedding   vector(1024),
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uq_chunk UNIQUE (rcept_no, chunk_idx)
);

CREATE INDEX IF NOT EXISTS ix_chunk_rcept ON doc_chunk (rcept_no);

-- HNSW(코사인). 적재 후 생성해야 빌드가 빠르지만, 초기화 시점엔 테이블이 비어
-- 있어 비용이 없다.
CREATE INDEX IF NOT EXISTS ix_chunk_embedding
    ON doc_chunk USING hnsw (embedding vector_cosine_ops);
