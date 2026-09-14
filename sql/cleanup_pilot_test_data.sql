-- =============================================================================
-- Handover cleanup: drop the pilot's RFQ activity and dashboard history
-- Run in Supabase Dashboard -> SQL Editor. One transaction.
-- =============================================================================
--
-- Scope is deliberately narrow: `rfq_jobs` and `metrics_snapshots` only.
--
-- The mailbox is NOT touched. `emails` and `attachments` hold Dhaval's real
-- correspondence from 2026-03-26 onward, and the eight test replies we sent from
-- our own Gmail during the pilot are being kept too, at the owner's request, as
-- sample rate-card mail.
--
-- The 23 rows in `rfq_jobs` go because their state is fiction. Every one began as
-- a real customer enquiry, but EMAIL_REDIRECT pointed each outbound RFQ at a
-- personal Gmail, so no freight agent was ever contacted, and the quotes behind
-- `quotes_received` and `approved` are our own impersonated replies. Keeping them
-- would hand Bhatia a board claiming work that never happened. The customer
-- emails that triggered them stay in the mailbox and can be re-processed for real.
--
-- `metrics_snapshots` is an hourly counter series recorded entirely during the
-- pilot, so the dashboard's trend lines start from Bhatia's first real day.
--
-- Every delete is guarded by a row-count assertion. If this database is not in
-- the state it was audited in, the transaction aborts rather than guessing.

begin;

-- -----------------------------------------------------------------------------
-- 1. The pilot's RFQ jobs.
--
-- `quotations.rfq_reference` is a NOT NULL foreign key onto rfq_jobs.reference,
-- so it has to go first. It is empty today; the delete is here so this script
-- still works if a quote gets recorded before it is run.
-- -----------------------------------------------------------------------------
do $$
declare n int;
begin
  select count(*) into n from rfq_jobs;
  if n <> 23 then
    raise exception 'Expected 23 rfq_jobs, found %. Aborting - re-audit first.', n;
  end if;
end $$;

delete from quotations where rfq_reference in (select reference from rfq_jobs);
delete from rfq_jobs;

-- -----------------------------------------------------------------------------
-- 2. Unlink the emails that pointed at those jobs.
--
-- Required, not cosmetic. `emails.rfq_reference` is a plain text column — the
-- foreign key onto rfq_jobs was never enabled (see add_email_indexes.sql) — so
-- deleting the jobs above leaves these eight rows referencing a reference that
-- no longer resolves. The inbox and reply views join on it, so they would show a
-- reference that goes nowhere. Nulling makes them ordinary unlinked mail, which
-- is what they now are: they will appear in the unlinked rate-card queue.
-- -----------------------------------------------------------------------------
update emails set rfq_reference = null where rfq_reference is not null;

-- -----------------------------------------------------------------------------
-- 3. The pilot's dashboard history.
-- -----------------------------------------------------------------------------
delete from metrics_snapshots;

-- -----------------------------------------------------------------------------
-- 4. Prove nothing else moved before committing.
-- -----------------------------------------------------------------------------
do $$
declare e int; a int; g int; u int; j int; m int;
begin
  select count(*) into e from emails;
  select count(*) into a from attachments;
  select count(*) into g from agents;
  select count(*) into u from app_users;
  select count(*) into j from rfq_jobs;
  select count(*) into m from metrics_snapshots;
  raise notice 'emails: %, attachments: %, agents: %, users: %, rfq_jobs: %, snapshots: %',
               e, a, g, u, j, m;
  if e <> 31893  then raise exception 'emails changed to % (expected 31893). Rolling back.', e; end if;
  if a <> 249189 then raise exception 'attachments changed to % (expected 249189). Rolling back.', a; end if;
  if g <> 112    then raise exception 'agents changed to % (expected 112). Rolling back.', g; end if;
  if u <> 1      then raise exception 'app_users changed to % (expected 1). Rolling back.', u; end if;
  if j <> 0      then raise exception 'rfq_jobs still has % rows. Rolling back.', j; end if;
end $$;

commit;
