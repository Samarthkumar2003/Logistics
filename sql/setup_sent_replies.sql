-- =============================================================================
-- Replies the desk sends from the app.
-- Target: Supabase (PostgreSQL). Run in Dashboard -> SQL Editor.
-- =============================================================================
--
-- Why the app keeps its own copy: ingest reads INBOX only (gmail_connector
-- `labelIds: INBOX`), so a message this app sends never comes back into
-- `emails`. Without this table the dashboard could send a reply and then have
-- no record that it did.
--
-- One row per send attempt the operator made. The row is written with status
-- 'sending' BEFORE the provider is called and updated with the outcome after,
-- so a process that dies mid-send leaves a visible 'sending' row rather than
-- nothing at all.
--
-- `email_id` is emails.provider_msg_id, the key every other email lookup uses
-- (see email_repo.py). No foreign key: provider_msg_id is not declared unique on
-- `emails`, and a reply must stay on record even if its email is purged.
--
-- `idempotency_key` comes from the browser, one per composed reply. A double
-- click, or a retried request whose first attempt did land, finds the existing
-- row and sends nothing. With live email armed that is the difference between
-- one reply and two in a customer's inbox.
--
-- Attachment BYTES are not stored, only their names, which is why a failed
-- reply with attachments can be edited and re-sent but not retried as-is.

create table if not exists sent_replies (
  id               uuid primary key default gen_random_uuid(),
  email_id         text not null,
  thread_id        text not null default '',
  to_addr          text not null,
  cc               text[] not null default '{}',
  subject          text not null,
  body             text not null,
  attachment_names text[] not null default '{}',
  status           text not null default 'sending'
                   -- unknown: the provider errored in a way that does not prove the mail
                   -- did not go (timeout, 5xx). Never retried automatically.
                   check (status in ('sending', 'sent', 'failed', 'unknown')),
  error            text,
  provider_msg_id  text,
  sent_by          text not null default '',
  sent_by_name     text not null default '',
  idempotency_key  text not null unique,
  created_at       timestamptz not null default now(),
  sent_at          timestamptz
);

-- The card's history list and the inbox page's per-email summary both filter by
-- email_id and want the newest first.
create index if not exists idx_sent_replies_email
  on sent_replies (email_id, created_at desc);
