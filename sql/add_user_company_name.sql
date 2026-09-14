-- =============================================================================
-- Migration: the company that signs vendor mail lives on the operator's row
-- Run in Supabase Dashboard -> SQL Editor.
-- =============================================================================
--
-- This was COMPANY_NAME in the environment, and the acceptance email did not even
-- use that — it closed with a hardcoded "Logistics Copilot", our internal product
-- name, sent over a customer's business to freight agents who have never heard of
-- us. Both are now gone; app/sender.py reads this column at send time.
--
-- Why the row and not the environment: changing the environment is a redeploy,
-- and a redeploy to correct a company name that is currently wrong in front of
-- vendors is the wrong shape of fix. This is one UPDATE that takes effect on the
-- next email.
--
-- Why the row and not the JWT: a token is minted at login, so a company changed
-- today would keep sending the old name until every operator's session expired.
-- Reading the row costs one primary-key lookup on a path that already calls an
-- LLM, and it is the pattern this codebase already uses for `is_active` — see
-- AppUser in domain/models.py.
--
-- A blank value is deliberately not an error. SenderIdentity renders a name-only
-- signature, so an operator whose company has not been set yet still signs as a
-- real person rather than being blocked from drafting.

alter table app_users add column if not exists company_name text;

-- The default is per-deployment, which is correct under the silo model in
-- Documentation/07-multi-tenancy-plan.md: one Supabase project per client, so
-- every operator in this project belongs to the same company. A new operator
-- inherits it rather than silently sending vendor mail with no company line.
alter table app_users alter column company_name set default 'Bhatia Shipping';

update app_users
   set company_name = 'Bhatia Shipping'
 where company_name is null or btrim(company_name) = '';

select email, full_name, role, company_name from app_users;
