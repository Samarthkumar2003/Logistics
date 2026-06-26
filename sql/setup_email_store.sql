-- ============================================================
-- Email store — persist raw emails + attachments + sync watermark
-- Phase 1 of the shipment-history plan. Makes ingestion idempotent
-- (UNIQUE message_id) so each email is classified/extracted once ever,
-- and gives re-extraction a local source instead of re-crawling Gmail.
-- Run this in the Supabase SQL Editor.
--
-- Also create a Storage bucket named  rate-card-attachments
-- (Supabase Dashboard → Storage → New bucket). Attachment BYTES live
-- there at key <attachment uuid>.<ext>; the DB row holds metadata only.
-- ============================================================

create extension if not exists "pgcrypto";  -- gen_random_uuid()

create table if not exists emails (
  id                    uuid primary key default gen_random_uuid(),
  message_id            text unique,            -- RFC822 Message-ID; dedup key
  provider              text default '',         -- gmail | outlook | imap
  provider_msg_id       text default '',         -- gmail id / imap uid (for body/attachment fetch)
  thread_id             text default '',
  sender                text default '',
  subject               text default '',
  body                  text default '',         -- extracted plain text
  has_attachments       boolean default false,
  received_at           timestamptz,
  classification        text default '',          -- customer_requirement | quotation_rate_card | general
  classification_status text default 'pending',   -- pending | classified
  created_at            timestamptz default now()
);
create index if not exists idx_emails_classification on emails (classification);
create index if not exists idx_emails_received on emails (received_at desc);

create table if not exists attachments (
  id                uuid primary key default gen_random_uuid(),  -- the UUID identifier
  email_id          uuid references emails(id) on delete cascade,
  file_name         text default '',
  mime_type         text default '',
  storage_path      text default '',         -- <uuid>.<ext> in the rate-card-attachments bucket
  size_bytes        integer,
  processing_status text default 'stored',   -- stored | parsed | failed
  created_at        timestamptz default now()
);
create index if not exists idx_attachments_email on attachments (email_id);

create table if not exists sync_state (
  provider          text primary key,        -- gmail | outlook | imap
  last_received_at  timestamptz,             -- high-water mark for incremental fetch
  updated_at        timestamptz default now()
);
