-- =============================================================================
-- Migration: sequential RFQ reference numbers
-- Run in Supabase Dashboard -> SQL Editor.
-- =============================================================================
--
-- RFQ references were `RFQ-YYYYMMDD-<8 random hex>`. They are now
-- `RFQ-NNNNNN`, a zero-padded number from the sequence below, so an operator
-- can read and say one out loud. The old form keeps resolving forever; see
-- backend/core/rfq_reference.py.
--
-- Why a sequence and not max(reference) + 1 in Python:
--
-- rfq_service drafts every agent's mail in a ThreadPoolExecutor, so a single
-- 7-agent send asks for 7 references from 7 threads at once, and the scheduler
-- can be sending at the same moment. A read-then-write counter hands the same
-- number to two callers, and because rfq_jobs.reference is unique the second
-- insert fails inside _reserve_jobs, aborting a whole batch the operator then
-- has to retry. nextval() is atomic and never returns a value twice, which is
-- the entire reason this lives in the database.
--
-- Gaps are expected and are not a bug. nextval() is deliberately not rolled
-- back, so a send that fails after allocating burns its number: 41, 42, 45.
-- Making the numbers gapless would mean locking a counter row, which serialises
-- every send in the system to buy nothing an operator can use.
--
-- Starting at 1000 keeps a new six-digit reference visually distinct from the
-- historical rows, and leaves RFQ-000000 free as the reserved placeholder the
-- draft preview shows for a sample that is sent to nobody.

create sequence if not exists rfq_reference_seq
  as bigint
  start with 1000
  increment by 1
  no maxvalue
  cache 1;

-- Returns an array rather than `setof bigint` so the client sees one predictable
-- JSON shape (a list of numbers) instead of a row set it has to unwrap.
--
-- Allocating the whole batch in one call, rather than one call per agent, means
-- a 7-agent send makes one round trip and cannot interleave with another send
-- halfway through its own block.
create or replace function next_rfq_references(n integer)
  returns bigint[]
  language sql
  volatile
as $$
  select array_agg(nextval('rfq_reference_seq'))
  from generate_series(1, greatest(n, 0));
$$;

-- PostgREST only exposes what the API roles may execute.
grant usage on sequence rfq_reference_seq to anon, authenticated, service_role;
grant execute on function next_rfq_references(integer) to anon, authenticated, service_role;

-- Sanity check. Expect {1000,1001,1002} on a fresh sequence, and note that
-- running this consumes those three numbers.
-- select next_rfq_references(3);
