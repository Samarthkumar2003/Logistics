-- =============================================================================
-- Migration: agents table + shipment_size column on rfq_jobs
-- Target: Supabase (PostgreSQL)
-- Run in Supabase Dashboard -> SQL Editor.
-- Seed data: python -m backend.scripts.seed_agents_table
-- =============================================================================

-- Agents table: replaces data/agents_database.csv as the source of truth
-- for the Send Request dashboard's three multi-select dropdowns.
-- category values: 'CHA' | 'FREIGHT_FORWARDER' | 'CARRIER'
create table if not exists agents (
    id bigint primary key generated always as identity,
    agent_name text not null,
    location text,
    country text,
    email text not null,
    contact_person text,
    phone text,
    specialty text,
    modes text[],                 -- e.g. {sea_freight, air_freight, road}
    category text not null,      -- CHA | FREIGHT_FORWARDER | CARRIER
    created_at timestamptz default now(),
    unique (agent_name, email)
);

create index if not exists idx_agents_category on agents (category);

-- Manual sends capture container size separately from commodity
alter table rfq_jobs add column if not exists shipment_size text;
