-- pgvector 확장 (Week 2: 공시 청크 임베딩 저장)
CREATE EXTENSION IF NOT EXISTS vector;

-- Airflow 메타데이터 DB 분리
CREATE DATABASE airflow OWNER kograph;
