-- Enable the pgvector extension to work with OpenAI embeddings
create extension if not exists vector;

-- Create the shipments table for historical data
create table if not exists shipments (
    id bigint primary key generated always as identity,
    origin text not null,
    destination text not null,
    mode text not null,                -- 'sea_freight', 'air_freight', 'road'
    weight_kg numeric not null,
    commodity text not null,
    cargo_embedding vector(1536),      -- commodity-only embedding (short, ~10 words)
    full_embedding vector(1536),       -- full shipment text embedding (route+equipment+incoterm+rate)
    agent_used text,                   -- the vendor we used
    rate_paid numeric,                 -- historical price we paid
    transit_time_days integer          -- historical transit time
);

-- Add full_embedding to existing tables (safe to run on existing DB)
alter table shipments add column if not exists full_embedding vector(1536);

-- ── RPC 1: Commodity embedding search ────────────────────────────────────────
-- Searches using the short commodity-only embedding.
-- Filters by origin/destination/mode, ranks by commodity vector similarity.
create or replace function match_shipments(
    p_origin text,
    p_destination text,
    p_mode text,
    p_embedding vector(1536),
    match_count int DEFAULT 5
)
returns table (
    id bigint,
    origin text,
    destination text,
    mode text,
    weight_kg numeric,
    commodity text,
    agent_used text,
    rate_paid numeric,
    transit_time_days integer,
    similarity float
)
language plpgsql
as $$
begin
  return query
  select
    s.id,
    s.origin,
    s.destination,
    s.mode,
    s.weight_kg,
    s.commodity,
    s.agent_used,
    s.rate_paid,
    s.transit_time_days,
    1 - (s.cargo_embedding <=> p_embedding) as similarity
  from shipments s
  where
    s.cargo_embedding is not null and
    (p_origin is null or lower(s.origin) = lower(p_origin)) and
    (p_destination is null or lower(s.destination) = lower(p_destination)) and
    (p_mode is null or lower(s.mode) = lower(p_mode))
  order by s.cargo_embedding <=> p_embedding
  limit match_count;
end;
$$;

-- ── RPC 2: Full shipment embedding search ────────────────────────────────────
-- Searches using the rich full-text embedding (route+equipment+incoterm+rate).
-- No structured filters — the embedding itself encodes route context.
create or replace function match_shipments_full(
    p_embedding vector(1536),
    match_count int DEFAULT 5
)
returns table (
    id bigint,
    origin text,
    destination text,
    mode text,
    weight_kg numeric,
    commodity text,
    agent_used text,
    rate_paid numeric,
    transit_time_days integer,
    similarity float
)
language plpgsql
as $$
begin
  return query
  select
    s.id,
    s.origin,
    s.destination,
    s.mode,
    s.weight_kg,
    s.commodity,
    s.agent_used,
    s.rate_paid,
    s.transit_time_days,
    1 - (s.full_embedding <=> p_embedding) as similarity
  from shipments s
  where s.full_embedding is not null
  order by s.full_embedding <=> p_embedding
  limit match_count;
end;
$$;
