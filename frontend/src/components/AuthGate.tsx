"use client";

import { useEffect, useSyncExternalStore } from 'react';
import { usePathname } from 'next/navigation';

import {
  LOGIN_PATH,
  fetchCurrentUser,
  hasValidSession,
  redirectToLogin,
} from '@/lib/api';

/**
 * Nothing to subscribe to.
 *
 * `useSyncExternalStore` is used here purely for its server/client snapshot
 * split, not to observe a changing store: the token only changes on login or
 * logout, and both navigate the page. A no-op subscribe with a no-op unsubscribe
 * is the documented way to say that.
 */
const NO_SUBSCRIBE = () => () => {};

/** Client snapshot. A primitive, so React's Object.is check is stable. */
const readSession = () => hasValidSession();

/**
 * Server snapshot: assume not signed in.
 *
 * localStorage does not exist during SSR. Assuming *signed out* is the safe
 * direction — the worst case is one blank frame before hydration corrects it,
 * whereas assuming signed in would render the dashboard to someone who is not.
 */
const readServerSession = () => false;

/**
 * Wraps the whole app and keeps unauthenticated people out of it.
 *
 * A client component in the root layout, NOT a Next `proxy.ts` (the file formerly
 * called `middleware.ts` — renamed in 16). The reason is where the token lives: a
 * proxy runs on the server and can only see cookies and headers, and this app
 * holds its bearer token in localStorage, which no server-side hook can read. A
 * proxy would let every request through and gate nothing.
 *
 * That is not a security hole, because it is not the security boundary. The API
 * rejects every request without a valid signature — see backend/app/auth.py. This
 * component exists so an expired session shows a login form instead of a
 * dashboard full of failed requests.
 */
export default function AuthGate({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const isLoginPage = pathname === LOGIN_PATH;

  /**
   * Read during render rather than in an effect.
   *
   * The obvious version of this component holds a status in `useState` and moves
   * it in a `useEffect`, because localStorage cannot be read during SSR. That
   * works but it is a setState inside an effect — a second render pass on every
   * page load, and an error under `react-hooks/set-state-in-effect`.
   * `useSyncExternalStore` exists for exactly this shape: the server renders
   * `readServerSession`, the client switches to `readSession` after hydration,
   * and React reconciles the two without a mismatch warning.
   */
  const signedIn = useSyncExternalStore(
    NO_SUBSCRIBE,
    readSession,
    readServerSession,
  );

  useEffect(() => {
    // The login page is never gated — gating it would redirect it to itself.
    if (isLoginPage) return;

    if (!signedIn) {
      // Local expiry check has already failed, so skip the round trip that is
      // certain to 401 and send them straight to the form.
      redirectToLogin();
      return;
    }

    // Optimistic: the token looks valid, so the app is already rendering below
    // while this confirms in the background. Blocking on /auth/me would put a
    // spinner in front of every page load for a check that almost always passes.
    //
    // Swallowed on purpose, and this is the whole error policy for the check:
    // apiFetch has already cleared the session and started the redirect if the
    // response was a 401. Anything else — API down, network dropped — must NOT
    // log the operator out, or they land on a login page that equally cannot
    // reach the API and have no way back in.
    fetchCurrentUser().catch(() => {});
  }, [isLoginPage, signedIn, pathname]);

  if (isLoginPage) return <>{children}</>;

  if (!signedIn) {
    // Deliberately near-blank rather than a spinner. This is on screen for one
    // frame when a token is present, and a flash of "Loading…" followed
    // immediately by a login form reads as a failure. Inline styles per the
    // project convention.
    return (
      <div
        style={{
          minHeight: '100vh',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          color: 'var(--text-muted, #888)',
          fontFamily: 'system-ui, -apple-system, sans-serif',
          fontSize: 13,
        }}
        aria-busy="true"
      >
        Redirecting to sign in…
      </div>
    );
  }

  return <>{children}</>;
}
