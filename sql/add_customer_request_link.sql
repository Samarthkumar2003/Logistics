-- Phase 1: link every RFQ job back to the customer request that spawned it.
--
-- Today rfq_jobs only copies the customer email's sender/subject/body and has a
-- per-agent `reference`. There is no id tying the several per-agent RFQs of one
-- customer request together, nor a link back to the inbox email. These columns
-- add that: customer_email_id is the customer email's provider_msg_id (the id
-- everything else in the app already keys on), and customer_thread_id is its
-- Gmail thread. Grouping rfq_jobs by customer_email_id = "all RFQs for this
-- request"; that is what the request page (Phase 3) is built on.
--
-- Run in the Supabase SQL editor. Additive and idempotent — safe to re-run.

ALTER TABLE rfq_jobs ADD COLUMN IF NOT EXISTS customer_email_id  text;
ALTER TABLE rfq_jobs ADD COLUMN IF NOT EXISTS customer_thread_id text;

-- The request page looks jobs up by customer_email_id, so index it.
CREATE INDEX IF NOT EXISTS idx_rfq_jobs_customer_email_id ON rfq_jobs (customer_email_id);
