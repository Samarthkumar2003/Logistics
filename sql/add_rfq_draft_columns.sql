-- =============================================================================
-- Migration: keep the exact mail we drafted, so a stuck send can be retried
-- Run in Supabase Dashboard -> SQL Editor.
-- =============================================================================
--
-- The send is two-phase: rfq_service reserves a job row at status 'sending',
-- hands the mail to the provider, then advances the row to 'rfqs_sent' or
-- 'send_failed'. A crash between those two steps leaves the row at 'sending'
-- forever, and the drafted mail existed only in that dead process's memory.
--
-- The retry sweep (backend/services/retry_service.py) needs that text back. The
-- alternative was re-drafting from the shipment fields, which sends the vendor
-- different words than the RFQ we may or may not have already sent them — and
-- burns an LLM call on a path that exists only for crashes. Storing the draft
-- makes a retry byte-identical to the original, which is the only version that
-- is safe to send twice if our Sent-folder evidence is ever wrong.
--
-- Pre-send text, deliberately: EMAIL_REDIRECT (safe mode) rewrites the
-- recipient and prefixes the subject inside the sender, so storing the drafted
-- form means a retry re-applies whatever redirect is configured at retry time
-- rather than replaying a stale test address into production.

alter table rfq_jobs add column if not exists draft_subject text;
alter table rfq_jobs add column if not exists draft_body    text;

-- The address we actually addressed, not the agent name. `agents_contacted`
-- holds a name, and agent_repo.email_for_name deliberately refuses to resolve a
-- name shared by several offices (BUGS.md P2-1) — so a retry that started from
-- the name could not resend to precisely those agents, and guessing a branch
-- would mail the wrong office. Recording the resolved recipient removes the
-- question.
alter table rfq_jobs add column if not exists draft_to      text;

-- The retry sweep's only query: rows abandoned at 'sending'. Partial, because
-- in steady state this matches zero rows and the index should cost nothing.
create index if not exists idx_rfq_jobs_sending
    on rfq_jobs (created_at) where status = 'sending';

-- Existing rows predate the two-phase write and are all in a terminal-ish
-- state already, so they need no backfill: nothing is at 'sending', and the
-- sweep only ever reads columns for rows it finds there.
