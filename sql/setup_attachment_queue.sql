-- Attachment background-download queue.
-- Ingest now inserts attachment rows as metadata only (processing_status='pending')
-- and a background worker downloads the bytes + uploads to storage afterwards, so
-- the ingest hot path is no longer blocked on Gmail/Storage I/O.
--
-- The worker needs the Gmail coordinates to fetch bytes later, which the original
-- schema did not store (it downloaded inline, immediately). Add them, plus an
-- attempt counter so a permanently-broken attachment stops retrying.
--
-- Safe to run more than once.

alter table attachments add column if not exists provider_msg_id text;
alter table attachments add column if not exists attachment_id   text;
alter table attachments add column if not exists attempts        integer default 0;

-- processing_status vocabulary is now: pending | stored | parsed | failed
-- (pending = enqueued, bytes not yet downloaded).
-- Partial index keeps the worker's "grab the next pending batch" query fast even
-- as the stored/parsed rows grow without bound.
create index if not exists idx_attachments_pending
  on attachments (created_at)
  where processing_status = 'pending';
