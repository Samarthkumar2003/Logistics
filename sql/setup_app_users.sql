-- Operators who may call this API. Closes BUGS.md P1-1.
--
-- Deliberately NOT Supabase's built-in `auth.users`. This backend talks to
-- Supabase with the service role key as a plain database, never as an auth
-- provider: there is no Supabase client in the browser, no anon key on the
-- frontend, and no GoTrue session anywhere. Borrowing auth.users would mean the
-- API validating tokens minted by a service it does not otherwise use, for a
-- login flow it does not otherwise implement. This table is read by exactly one
-- module, backend/repositories/user_repo.py.
--
-- Run in the Supabase SQL editor. Additive and idempotent — safe to re-run.

CREATE TABLE IF NOT EXISTS app_users (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email         text NOT NULL,
    -- A bcrypt hash, always. 60 chars for the $2b$ format this app writes; the
    -- column is text rather than char(60) so raising BCRYPT_ROUNDS, or a future
    -- move to a longer format, is not a migration.
    password_hash text NOT NULL,
    full_name     text NOT NULL DEFAULT '',
    -- No behaviour is attached to this yet — every operator can do everything.
    -- It exists because it goes in the JWT, and adding a claim later invalidates
    -- every token already issued.
    role          text NOT NULL DEFAULT 'operator',
    -- The revocation switch. Flip to false and the next login fails; any token
    -- already issued keeps working until it expires, because there is no
    -- denylist. /auth/me re-reads this column, so a browser already open is
    -- bounced to /login on its next page load rather than at expiry.
    is_active     boolean NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_login_at timestamptz
);

-- Case-insensitive uniqueness, not `UNIQUE (email)`.
--
-- user_repo lowercases on the way in and on the way out, but a plain unique
-- constraint would still accept `Sam@Corp.com` alongside `sam@corp.com` if a row
-- were ever inserted by hand in the SQL editor — leaving two accounts for one
-- person, only one of which the login path can ever find. The index makes the
-- database enforce what the application assumes.
CREATE UNIQUE INDEX IF NOT EXISTS idx_app_users_email_lower
    ON app_users (lower(email));

-- Row-level security with no policies: the only grant left is the service role,
-- which bypasses RLS. Belt and braces — nothing hands out the anon key today —
-- but the cost of being wrong is the password_hash column, so it is on.
ALTER TABLE app_users ENABLE ROW LEVEL SECURITY;

-- Should a browser-side Supabase client ever be added, this is the policy to
-- write, and it must NOT include password_hash. Left commented rather than
-- omitted so the next person does not reach for `USING (true)`.
--
-- CREATE POLICY app_users_self_read ON app_users
--     FOR SELECT TO authenticated
--     USING (id = auth.uid());

COMMENT ON TABLE app_users IS
    'API operators. password_hash is bcrypt; written only by backend/scripts/create_user.py and POST /auth/change-password.';

-- ---------------------------------------------------------------------------
-- Seeding the first operator
-- ---------------------------------------------------------------------------
-- Do NOT write an INSERT here with a hash pasted in. bcrypt hashes belong to the
-- process that generated them, and a hash committed to this repo is a published
-- password. Use the CLI, which hashes with the same BCRYPT_ROUNDS the API
-- verifies with and never echoes the password:
--
--     python -m backend.scripts.create_user --email you@yourdomain.com
--
-- It prompts for the password twice, without echo, and inserts the row.
--
-- To confirm afterwards (never select password_hash into a shared console):
--
--     SELECT id, email, role, is_active, created_at FROM app_users;
--
-- To deactivate someone without deleting their history:
--
--     UPDATE app_users SET is_active = false WHERE lower(email) = 'them@yourdomain.com';
