-- ============================================================
-- Email classification cache
-- Stores the label for each email keyed by its stable Gmail message ID.
-- `email_id` is UNIQUE (one label per email) but not the PK —
-- the PK is an auto-generated UUID so the row is independently addressable.
-- Run this in the Supabase SQL Editor.
-- ============================================================

create extension if not exists "pgcrypto";

create table if not exists email_classifications (
  id          uuid primary key default gen_random_uuid(),
  email_id    text not null unique,   -- Gmail message ID (e.g. 19f12e12f8b9ed0f)
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
