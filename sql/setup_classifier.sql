-- ============================================================
-- Email Classifier: Training data + feedback tables + RPC
-- Run this in the Supabase SQL Editor
-- ============================================================

-- 1. Ensure pgvector is enabled
create extension if not exists vector;

-- 2. Training data table for labeled email embeddings
create table if not exists email_training_data (
  id          bigint primary key generated always as identity,
  content     text not null,
  subject     text default '',
  sender      text default '',
  label       text not null,
  embedding   vector(1536) not null,
  created_at  timestamptz default now()
);

create index if not exists idx_training_embedding_cosine
  on email_training_data
  using hnsw (embedding vector_cosine_ops)
  with (m = 16, ef_construction = 64);

create index if not exists idx_training_label
  on email_training_data (label);

-- 3. Feedback table: human corrections stored here and fed back into training
create table if not exists classification_feedback (
  id                bigint primary key generated always as identity,
  email_subject     text default '',
  email_body        text not null,
  email_sender      text default '',
  predicted_label   text not null,
  corrected_label   text not null,
  confidence        float default 0,
  added_to_training boolean default false,
  created_at        timestamptz default now()
);

-- 4. KNN classification function (weighted vote)
create or replace function classify_email(
  query_embedding vector(1536),
  k int default 7
)
returns table (
  predicted_label text,
  confidence      float,
  vote_count      bigint,
  avg_similarity  float
)
language plpgsql
as $$
declare
  total_neighbors int;
begin
  total_neighbors := k;
  return query
  select
    sub.label                                    as predicted_label,
    (count(*)::float / total_neighbors)          as confidence,
    count(*)                                     as vote_count,
    avg(sub.similarity)::float                   as avg_similarity
  from (
    select
      li.label,
      1 - (li.embedding <=> query_embedding) as similarity
    from email_training_data li
    order by li.embedding <=> query_embedding
    limit k
  ) sub
  group by sub.label
  order by vote_count desc, avg_similarity desc
  limit 1;
end;
$$;
