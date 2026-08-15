-- setup_metrics_snapshots.sql
-- Trend history for GET /metrics, written hourly by the scheduler
-- (backend/app/lifespan.py -> _metrics_snapshot_job).
--
-- /metrics on its own is present-tense: it answers "is anything stuck now?"
-- but not "has it been getting worse for a week?". These rows are that second
-- question. One row an hour is ~8,800 a year — small enough to keep forever and
-- to eyeball without a charting tool.
--
-- Flat integer columns rather than jsonb: every query against this table is an
-- aggregate over one column, and `avg(scan_backlog)` should not need a cast.

create table if not exists metrics_snapshots (
    id                      bigserial primary key,
    captured_at             timestamptz not null default now(),

    -- emails
    scan_backlog            integer not null,
    processing_failed       integer not null,
    pending_classification  integer not null,
    rate_cards_unlinked     integer not null,

    -- rfq_jobs by status
    jobs_rfqs_sent          integer not null,
    jobs_quotes_received    integer not null,
    jobs_approved           integer not null,

    -- context for reading the row back: counts during a redirected-mail period
    -- are not comparable with live ones
    safe_mode               boolean not null default false
);

create index if not exists metrics_snapshots_captured_at_idx
    on metrics_snapshots (captured_at desc);

-- A snapshot is only written when every count succeeded; a failed query is
-- never stored as a number. So a gap in captured_at means "we could not
-- measure", and is meant to be visible.

-- Useful reads --------------------------------------------------------------

-- Last 48 hours, newest first
--   select captured_at, scan_backlog, rate_cards_unlinked, processing_failed
--   from metrics_snapshots
--   where captured_at > now() - interval '48 hours'
--   order by captured_at desc;

-- Daily movement of the unlinked backlog — the number that exposed the stale
-- process still writing to quotations
--   select date_trunc('day', captured_at) as day,
--          min(rate_cards_unlinked) as low,
--          max(rate_cards_unlinked) as high,
--          max(rate_cards_unlinked) - min(rate_cards_unlinked) as growth
--   from metrics_snapshots
--   group by 1 order by 1 desc limit 14;

-- Is the scan keeping up? A backlog that never returns to a low number means
-- arrivals are outpacing the 5-minute scan.
--   select date_trunc('hour', captured_at) as hour, max(scan_backlog)
--   from metrics_snapshots
--   where captured_at > now() - interval '7 days'
--   group by 1 order by 1 desc;
