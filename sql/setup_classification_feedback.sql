-- =============================================================================
-- Human label corrections.
-- Target: Supabase (PostgreSQL). Run in Dashboard -> SQL Editor.
-- =============================================================================
--
-- Split out of the old setup_classifier.sql, which also created the training
-- corpus and a KNN classifier RPC for the fine-tuned/SVM/MLP tiers. Those tiers
-- were removed — classification is a rules pass plus one LLM call — so only this
-- table survives.
--
-- This is a corrections LOG, not training data: the authoritative label an
-- operator picked lands in email_classifications (see setup_classification_cache
-- .sql), which is what the inbox reads. This table keeps the before/after pair so
-- you can measure classifier accuracy and mine disagreements for prompt work.

create table if not exists classification_feedback (
  id              bigint primary key generated always as identity,
  email_subject   text default '',
  email_body      text not null,
  email_sender    text default '',
  predicted_label text not null,
  corrected_label text not null,
  confidence      float default 0,
  created_at      timestamptz default now()
);

create index if not exists idx_feedback_created
  on classification_feedback (created_at desc);

-- Disagreements only — the rows worth reading.
create index if not exists idx_feedback_wrong
  on classification_feedback (corrected_label)
  where predicted_label is distinct from corrected_label;


-- ---------------------------------------------------------------------------
-- Cleanup for existing projects. NOT run automatically — these DROP live
-- objects. Apply by hand once you are satisfied nothing else reads them.
-- ---------------------------------------------------------------------------
--
-- The KNN tier's classifier function and its training corpus:
--   DROP FUNCTION IF EXISTS classify_email(vector, int);
--   DROP TABLE IF EXISTS email_training_data;
--
-- The column that tracked whether a correction had been folded into training.
-- Nothing writes it any more:
--   ALTER TABLE classification_feedback DROP COLUMN IF EXISTS added_to_training;
