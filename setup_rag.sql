-- ============================================================
-- RAG Classification Store
-- Run this in Supabase SQL Editor ONCE before ingesting data.
-- ============================================================

-- Enable pgvector if not already enabled
CREATE EXTENSION IF NOT EXISTS vector;

-- ── Training data table ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS email_training_data (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    content     text        NOT NULL,
    subject     text        DEFAULT '',
    sender      text        DEFAULT '',
    label       text        NOT NULL CHECK (label IN ('customer_requirement', 'quotation_rate_card', 'general')),
    source      text        DEFAULT 'manual',   -- 'manual' | 'screenshot' | 'feedback' | 'auto'
    embedding   vector(1536),
    created_at  timestamptz DEFAULT now()
);

-- Index for fast KNN search
CREATE INDEX IF NOT EXISTS email_training_data_embedding_idx
    ON email_training_data
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 50);

-- ── KNN classification function ─────────────────────────────
-- Returns the majority-voted label from the k nearest neighbours.
CREATE OR REPLACE FUNCTION classify_email(
    query_embedding vector(1536),
    k               int DEFAULT 7
)
RETURNS TABLE (
    predicted_label text,
    confidence      float,
    vote_count      int,
    avg_similarity  float
)
LANGUAGE sql STABLE AS $$
    WITH neighbours AS (
        SELECT
            label,
            1 - (embedding <=> query_embedding) AS similarity
        FROM email_training_data
        WHERE embedding IS NOT NULL
        ORDER BY embedding <=> query_embedding
        LIMIT k
    ),
    votes AS (
        SELECT
            label,
            COUNT(*)                    AS cnt,
            AVG(similarity)             AS avg_sim
        FROM neighbours
        GROUP BY label
        ORDER BY cnt DESC, avg_sim DESC
        LIMIT 1
    )
    SELECT
        label                           AS predicted_label,
        cnt::float / k                  AS confidence,
        cnt::int                        AS vote_count,
        avg_sim                         AS avg_similarity
    FROM votes;
$$;
