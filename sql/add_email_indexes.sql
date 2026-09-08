-- ============================================================
-- Indexes the backend's hot queries already assume, and the uniqueness the
-- dedup logic already assumes. Additive and idempotent — safe to re-run.
--
-- Run this in the Supabase SQL Editor. Read the PREFLIGHT below FIRST: the
-- unique index will refuse to build if the table already contradicts it, which
-- is the point, but you want to know that before you run it rather than from an
-- error message.
--
-- Nothing here changes application behaviour. The unique index is the one
-- exception: after it exists, a second insert of the same Gmail id fails loudly
-- instead of silently creating a duplicate row.
-- ============================================================

-- ------------------------------------------------------------
-- PREFLIGHT — run these two SELECTs on their own first
-- ------------------------------------------------------------
--
-- 1. Duplicate provider_msg_id. Must return zero rows, or the unique index
--    below cannot be created. If it returns rows, the duplicates are real
--    ingestion bugs already in the data and need deciding on (keep newest?
--    merge rfq_reference?) before uniqueness can be enforced.
--
--      select provider_msg_id, count(*), array_agg(id) as row_ids
--      from emails
--      where provider_msg_id <> ''
--      group by provider_msg_id
--      having count(*) > 1;
--
-- 2. How much of the table is legacy rows with no provider_msg_id. These are
--    excluded from the index by the WHERE clause, so a large number here is
--    fine, it just means those rows keep the old lookup behaviour.
--
--      select count(*) filter (where provider_msg_id = '') as blank,
--             count(*) as total
--      from emails;


-- ------------------------------------------------------------
-- 1. provider_msg_id — the actual dedup key, previously unconstrained
-- ------------------------------------------------------------
-- `message_id` (the RFC-2822 header) carries the only UNIQUE on this table, but
-- email_repo comments that it is NOT the key anything looks up by: it can be
-- absent, while the provider's own id is always present. So every read path uses
-- provider_msg_id, and nothing stopped two rows sharing one.
--
-- That mattered beyond tidiness. Each lookup ends in .limit(1), so with a
-- duplicate present the row returned is whichever Postgres reached first. A
-- rate card could attach its rfq_reference to one copy and the inbox render the
-- other, and the reply would read as missing while sitting in the table.
--
-- Partial, excluding ''. The column defaults to empty rather than NULL, so
-- without the WHERE clause every legacy row would collide with every other one
-- and the index could never be built. Empty means "unknown", and unknowns do not
-- conflict with each other.
--
-- This also serves as the plain index for the six read paths in email_repo that
-- filter on this column, so no separate index is needed.
create unique index if not exists idx_emails_provider_msg_id
  on emails (provider_msg_id)
  where provider_msg_id <> '';


-- ------------------------------------------------------------
-- 2. classification_status — the pending-work predicate
-- ------------------------------------------------------------
-- setup_email_store.sql indexes `classification` (the label) but not
-- `classification_status` (whether a label exists yet). Those are different
-- columns and the second is the one the retry job and the metrics gauge scan:
-- list_inbox filters on it for the 'pending' tab, and count_pending_classifications
-- counts it on every /metrics call. Both were sequential scans over a table that
-- only grows.
create index if not exists idx_emails_classification_status
  on emails (classification_status);


-- ------------------------------------------------------------
-- 3. thread_id — reply context
-- ------------------------------------------------------------
-- list_thread_messages fetches a linked rate card's whole thread so the operator
-- reads the agent's latest word rather than their first. It is an IN over
-- thread_id with nothing to support it, selecting full bodies.
--
-- Partial for the same reason as above: thread_id defaults to '' and those rows
-- are not a thread.
create index if not exists idx_emails_thread_id
  on emails (thread_id)
  where thread_id <> '';


-- ------------------------------------------------------------
-- OPTIONAL — foreign key on emails.rfq_reference
-- ------------------------------------------------------------
-- Left commented, deliberately, because unlike everything above it can fail on
-- live data and its failure mode is worse than a refused index.
--
-- quotations.rfq_reference has this FK (setup_database_v2.sql) and
-- emails.rfq_reference does not, so a typo or a deleted job leaves an email
-- pointing at nothing. The reply then vanishes from the request page with no
-- error anywhere.
--
-- ON DELETE SET NULL rather than CASCADE: deleting a job must not delete the
-- vendor's email. It turns a dangling pointer into a visibly unlinked rate card,
-- which is a state the UI already has a tab for.
--
-- PREFLIGHT — must return zero rows before you uncomment:
--
--      select e.id, e.rfq_reference
--      from emails e
--      left join rfq_jobs j on j.reference = e.rfq_reference
--      where e.rfq_reference is not null and j.reference is null;
--
-- alter table emails
--   add constraint fk_emails_rfq_reference
--   foreign key (rfq_reference) references rfq_jobs (reference)
--   on delete set null;


-- ------------------------------------------------------------
-- Verify
-- ------------------------------------------------------------
--      select indexname from pg_indexes
--      where tablename = 'emails' order by indexname;
--
-- Expect at least: idx_emails_classification, idx_emails_classification_status,
-- idx_emails_provider_msg_id, idx_emails_received, idx_emails_rfq_reference,
-- idx_emails_thread_id, idx_emails_unprocessed.
