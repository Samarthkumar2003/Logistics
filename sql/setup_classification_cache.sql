-- ============================================================
-- Email classification cache
-- Stores the label for each email keyed by its stable email id
-- (Message-ID, or imap-<n> fallback), so the LLM is called once
-- per email instead of on every inbox refresh.
-- Run this in the Supabase SQL Editor.
-- ============================================================

create table if not exists email_classifications (
  email_id    text primary key,
  subject     text default '',
  sender      text default '',
  label       text not null,
  confidence  float default 0,
  method      text default '',
  created_at  timestamptz default now(),
  updated_at  timestamptz default now()
);

create index if not exists idx_email_classifications_label
  on email_classifications (label);
