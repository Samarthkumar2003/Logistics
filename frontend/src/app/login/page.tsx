"use client";

import { useEffect, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';

import { hasValidSession, login } from '@/lib/api';

/** Where to land when there is no `?next=` to honour. */
const DEFAULT_LANDING = '/dashboard';

/**
 * Only same-origin, absolute-path redirects are followed.
 *
 * `?next=` comes from the URL bar, so it is attacker-controllable: without this
 * check, a link to /login?next=https://evil.example.com/login would show our real
 * login form and then hand the operator to a copy of it. Rejecting anything that
 * does not start with a single '/' rules out absolute URLs, and rejecting '//'
 * rules out protocol-relative ones, which browsers treat as absolute.
 *
 * It also rules out the `javascript:` scheme, which Next's own useRouter docs
 * single out: a `javascript:` href handed to router.replace executes in the page.
 */
function safeNextPath(raw: string | null): string {
  if (!raw) return DEFAULT_LANDING;
  let decoded: string;
  try {
    decoded = decodeURIComponent(raw);
  } catch {
    return DEFAULT_LANDING;
  }
  if (!decoded.startsWith('/') || decoded.startsWith('//')) return DEFAULT_LANDING;
  if (decoded.startsWith('/login')) return DEFAULT_LANDING;
  return decoded;
}

export default function LoginPage() {
  const router = useRouter();
  const emailRef = useRef<HTMLInputElement>(null);

  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [status, setStatus] = useState<'idle' | 'submitting' | 'error'>('idle');
  const [errorMsg, setErrorMsg] = useState('');

  useEffect(() => {
    // Already signed in and arrived here by typing the URL or hitting Back — send
    // them on rather than making them log in twice.
    if (hasValidSession()) {
      router.replace(safeNextPath(new URLSearchParams(window.location.search).get('next')));
      return;
    }
    emailRef.current?.focus();
  }, [router]);

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (status === 'submitting') return;

    setStatus('submitting');
    setErrorMsg('');

    try {
      await login(email, password);
      // `window.location.search` rather than `useSearchParams()`: that hook opts
      // the page into a Suspense boundary at build time, and this page has no
      // other reason to need one.
      const next = safeNextPath(new URLSearchParams(window.location.search).get('next'));
      // replace, not push: the login page must not be a Back destination from
      // inside the app.
      router.replace(next);
    } catch (err) {
      // Both together, per the project's error-state convention — a message with
      // status left on 'submitting' leaves the button spinning forever.
      setErrorMsg(err instanceof Error ? err.message : 'Sign in failed');
      setStatus('error');
      setPassword('');
    }
  }

  const busy = status === 'submitting';

  return (
    <main style={S.page}>
      <form onSubmit={handleSubmit} style={S.card} noValidate>
        <div style={S.brand}>Logistics Copilot</div>
        <h1 style={S.heading}>Sign in</h1>
        <p style={S.sub}>
          This desk sends real RFQs to real freight agents. Accounts are created by
          an administrator.
        </p>

        <label style={S.label} htmlFor="email">
          Email
        </label>
        <input
          id="email"
          ref={emailRef}
          type="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          // Turns on the browser/password-manager autofill for a login form. Without
          // it, managers frequently skip the fields entirely.
          autoComplete="username"
          required
          disabled={busy}
          style={S.input}
          placeholder="you@yourdomain.com"
        />

        <label style={S.label} htmlFor="password">
          Password
        </label>
        <input
          id="password"
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="current-password"
          required
          disabled={busy}
          style={S.input}
        />

        {errorMsg && (
          // role=alert so a screen reader announces the failure; without it the
          // message appears silently below a form the operator is still staring at.
          <div role="alert" style={S.error}>
            {errorMsg}
          </div>
        )}

        <button
          type="submit"
          disabled={busy || !email || !password}
          style={{
            ...S.button,
            opacity: busy || !email || !password ? 0.55 : 1,
            cursor: busy || !email || !password ? 'not-allowed' : 'pointer',
          }}
        >
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </main>
  );
}

/**
 * Inline style objects, per the project convention — no CSS files. Literal colours
 * rather than the dashboard's CSS variables: those are declared inside
 * dashboard.css and scoped to that subtree, so referencing them here would render
 * an unstyled form. Values copied from that palette so the two look like one app.
 */
const S: Record<string, React.CSSProperties> = {
  page: {
    minHeight: '100vh',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    background: '#0a0f1e',
    padding: 24,
    fontFamily: 'system-ui, -apple-system, "Segoe UI", sans-serif',
  },
  card: {
    width: '100%',
    maxWidth: 380,
    display: 'flex',
    flexDirection: 'column',
    background: '#111827',
    border: '1px solid rgba(255,255,255,0.08)',
    borderRadius: 12,
    padding: 28,
    boxShadow: '0 18px 40px rgba(0,0,0,0.35)',
  },
  brand: {
    fontSize: 11,
    letterSpacing: 1.4,
    textTransform: 'uppercase',
    color: '#3b82f6',
    fontWeight: 600,
    marginBottom: 14,
  },
  heading: {
    margin: '0 0 6px',
    fontSize: 21,
    fontWeight: 600,
    color: '#e2e8f0',
  },
  sub: {
    margin: '0 0 22px',
    fontSize: 12.5,
    lineHeight: 1.5,
    color: '#64748b',
  },
  label: {
    fontSize: 11.5,
    fontWeight: 600,
    letterSpacing: 0.3,
    textTransform: 'uppercase',
    color: '#94a3b8',
    marginBottom: 6,
  },
  input: {
    background: '#0d1424',
    border: '1px solid rgba(255,255,255,0.10)',
    borderRadius: 7,
    padding: '10px 12px',
    fontSize: 14,
    color: '#e2e8f0',
    marginBottom: 16,
    outline: 'none',
    fontFamily: 'inherit',
  },
  error: {
    background: 'rgba(239,68,68,0.10)',
    border: '1px solid rgba(239,68,68,0.30)',
    color: '#fca5a5',
    borderRadius: 7,
    padding: '9px 11px',
    fontSize: 12.5,
    lineHeight: 1.45,
    marginBottom: 16,
  },
  button: {
    background: '#3b82f6',
    color: '#fff',
    border: 'none',
    borderRadius: 7,
    padding: '11px 14px',
    fontSize: 14,
    fontWeight: 600,
    fontFamily: 'inherit',
  },
};
