-- Persisted history of the daily sync-gap audit.
-- One row per (provider, calendar day): Gmail's INBOX count vs what we stored,
-- refreshed every audit run (upsert on the primary key). Lets the dashboard show
-- a per-day sync-health strip and lets us reason about drift over time instead of
-- only seeing it flash past in the logs.
--
-- Safe to run more than once.

create table if not exists sync_gap_audits (
    provider     text not null default 'gmail',
    day          date not null,
    gmail_count  integer not null,       -- Gmail INBOX messages that day
    db_count     integer not null,       -- rows stored with received_at that day
    missing      integer not null,       -- max(0, gmail - db); > 0 means a gap
    in_sync      boolean not null,       -- missing == 0
    audited_at   timestamptz not null default now(),
    primary key (provider, day)
);

-- Fast "show me the days that are behind" query.
create index if not exists idx_sync_gap_audits_missing
    on sync_gap_audits (day) where missing > 0;
