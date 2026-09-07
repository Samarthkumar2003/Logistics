-- Attachment dedup + auditable furniture decisions.
--
-- Two additive columns, both filled going forward by
-- backend/connectors/email_store.py:
--
--   content_id    the MIME Content-ID header — the value is_body_furniture
--                 actually decides on. It was read off the in-memory part and
--                 discarded, so no stored row could be re-judged on the filter's
--                 own criterion afterwards; a retrospective prune had to infer
--                 intent from file names.
--   content_hash  sha256 of the bytes. `storage_path` is now derived from it
--                 (<aa>/<sha256>.<ext>), so identical files share one object.
--                 Previously every row minted a fresh UUID, and one 426,103-byte
--                 animated signature banner occupied 5,958 separate objects —
--                 2.5 GB of a single file.
--
-- RUN THIS BEFORE DEPLOYING the code that writes them. PostgREST rejects an
-- insert or update naming a column that does not exist, which would fail every
-- attachment enqueue and every download completion — the queue would stall with
-- rows stuck `pending` and retrying to MAX_ATTACHMENT_ATTEMPTS.
--
-- Safe to run more than once. Nothing is backfilled and nothing is deleted:
-- existing rows keep their UUID-keyed storage_path, which still resolves.

alter table attachments add column if not exists content_id   text default '';
alter table attachments add column if not exists content_hash text default '';

-- Makes "every row sharing one object" answerable. PostgREST cannot aggregate,
-- so the duplicate census below has to be run from the SQL editor.
create index if not exists idx_attachments_content_hash
  on attachments (content_hash)
  where content_hash <> '';


-- ---------------------------------------------------------------------------
-- Duplicate census. Read-only — run after the code has been live long enough to
-- have hashed a representative sample.
-- ---------------------------------------------------------------------------
--
-- select content_hash,
--        count(*)                as copies,
--        max(size_bytes)         as bytes_each,
--        (count(*) - 1) * max(size_bytes) as wasted_bytes,
--        max(file_name)          as example
--   from attachments
--  where content_hash <> ''
--  group by content_hash
-- having count(*) > 1
--  order by wasted_bytes desc
--  limit 50;
--
-- The historical (pre-content-addressing) equivalent, which cannot use
-- content_hash and so approximates identity by exact byte size:
--
-- select mime_type, size_bytes,
--        count(*) as copies,
--        (count(*) - 1) * size_bytes as wasted_bytes
--   from attachments
--  where processing_status = 'stored' and size_bytes is not null
--  group by mime_type, size_bytes
-- having count(*) > 1
--  order by wasted_bytes desc
--  limit 50;
