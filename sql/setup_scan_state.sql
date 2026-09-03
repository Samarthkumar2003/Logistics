-- =============================================================================
-- Migration: move automation scan state from automation_state.json into the DB
-- Run in Supabase Dashboard -> SQL Editor.
-- =============================================================================

-- Which emails the pipeline has handled. The scan claims a row atomically
-- (UPDATE ... WHERE processed_at IS NULL), so duplicate RFQ sends become
-- impossible even with concurrent scans, restarts, or multiple workers.
alter table emails add column if not exists processed_at timestamptz;

-- Existing emails predate the pipeline — mark them processed so the first
-- scan doesn't fire RFQs for historical mail.
update emails set processed_at = now() where processed_at is null;

-- Fast lookup of the unprocessed backlog
create index if not exists idx_emails_unprocessed
    on emails (received_at) where processed_at is null;

-- Single-row config/status table (replaces the JSON file)
create table if not exists automation_state (
    id int primary key default 1 check (id = 1),
    enabled boolean not null default true,
    last_stats jsonb,
    updated_at timestamptz default now()
);
insert into automation_state (id, enabled) values (1, true)
    on conflict (id) do nothing;
