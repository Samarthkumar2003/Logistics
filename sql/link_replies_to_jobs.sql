-- =============================================================================
-- Replace parsed quotations with linked reply emails.
-- Target: Supabase (PostgreSQL). Run in Dashboard -> SQL Editor.
-- =============================================================================
--
-- The system no longer extracts rates out of agent replies, and no longer
-- predicts or grades prices. An agent's reply is shown to the operator as the
-- email it actually is — body and attachments — and the operator reads it.
--
-- All that has to be stored is WHICH request a reply belongs to. That is one
-- column on `emails`, pointing at the RFQ reference the reply quoted back.

ALTER TABLE emails ADD COLUMN IF NOT EXISTS rfq_reference text;

-- The request page looks replies up by reference, so index it. Partial: the
-- overwhelming majority of emails are not RFQ replies.
CREATE INDEX IF NOT EXISTS idx_emails_rfq_reference
    ON emails (rfq_reference)
    WHERE rfq_reference IS NOT NULL;


-- ---------------------------------------------------------------------------
-- Retry support for the scan
-- ---------------------------------------------------------------------------
-- Previously the scan stamped processed_at BEFORE doing the work, so anything
-- that threw was claimed and never looked at again. The claim is now released
-- on failure, with an attempt counter so a poison message cannot loop forever.

ALTER TABLE emails ADD COLUMN IF NOT EXISTS processing_attempts integer NOT NULL DEFAULT 0;
ALTER TABLE emails ADD COLUMN IF NOT EXISTS processing_error text;

-- Rows the scan has given up on: unprocessed but out of attempts.
CREATE INDEX IF NOT EXISTS idx_emails_processing_failed
    ON emails (processing_attempts)
    WHERE processed_at IS NULL AND processing_attempts > 0;


-- ---------------------------------------------------------------------------
-- Discard previously parsed rate data
-- ---------------------------------------------------------------------------
-- The `quotations` TABLE is deliberately kept — only its rows go. Every value
-- in it came from an LLM reading an email, including transit times the schema
-- forced the model to invent when the email did not state one, and price
-- verdicts graded against a USD range regardless of the quote's actual
-- currency. None of it is trustworthy enough to keep showing.
--
-- Uncomment and run once you have taken any backup you want:

-- DELETE FROM quotations;

-- The parking queue only existed to hold parsed rates that could not be
-- attributed. With no parsing there is nothing to park — an unmatched reply is
-- simply an email with rfq_reference IS NULL, still visible in the inbox, so
-- the state is a query rather than a second copy of the email:
--
--   SELECT provider_msg_id, sender, subject FROM emails
--   WHERE classification = 'quotation_rate_card' AND rfq_reference IS NULL
--   ORDER BY received_at DESC;
--
-- Check it is empty before dropping — if the parking code ever ran here, those
-- rows hold parsed rates that exist nowhere else:
--
--   SELECT count(*) FROM quotations_unmatched;
--
-- Then uncomment:

-- DROP TABLE IF EXISTS quotations_unmatched;
