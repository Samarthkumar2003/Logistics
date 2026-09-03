/**
 * api.ts
 * ------
 * The single place this app knows the API's address, and the single place it
 * attaches a bearer token.
 *
 * Before this, `const API_BASE = 'http://localhost:8001'` was copy-pasted into
 * five files. Any deployment needed all five changed, and the one that got missed
 * would keep working perfectly on the developer's machine.
 *
 * `apiFetch` returns a plain `Response`, deliberately: every existing call site
 * already reads `res.ok` / `res.status` / `await res.json()`, so adopting it is a
 * one-line change per call rather than a rewrite of the error handling in five
 * pages.
 */

/**
 * NEXT_PUBLIC_ prefix is required — this runs in the browser, and Next only
 * exposes prefixed variables there.
 *
 * IMPORTANT, and the thing that will bite on Railway: NEXT_PUBLIC_* values are
 * INLINED INTO THE BUNDLE AT BUILD TIME, not read at runtime. `next build` must
 * see this variable, so it has to be set as a build-time variable on the frontend
 * service. Setting it only at runtime leaves the literal fallback below compiled
 * into the JavaScript, and the deployed dashboard quietly calls localhost — which
 * fails on every machine except the one running the API locally.
 *
 * No trailing slash: every path below starts with one.
 */
export const API_BASE = (
  process.env.NEXT_PUBLIC_API_BASE || 'http://localhost:8001'
).replace(/\/$/, '');

/** localStorage keys. Namespaced so they cannot collide with `dashboard-theme`. */
const TOKEN_KEY = 'copilot-auth-token';
const EXPIRY_KEY = 'copilot-auth-expires-at';

export const LOGIN_PATH = '/login';

export interface AuthUser {
  id: string;
  email: string;
  full_name: string;
  role: string;
}

export interface LoginResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
  user: AuthUser;
}

/**
 * localStorage, not an httpOnly cookie.
 *
 * The honest trade-off: a cookie would be out of reach of injected JavaScript,
 * which localStorage is not. But the API is on a different origin from this app
 * (separate Railway services, separate domains), so a cookie would have to be
 * `SameSite=None; Secure` with credentialed CORS — which forbids the wildcard in
 * `allow_headers` the backend uses and adds a CSRF surface that a bearer token
 * does not have. Given an internal dashboard behind a login, the bearer token is
 * the smaller problem. Revisit if this is ever exposed to the public internet.
 *
 * Wrapped in try/catch because Safari's private mode throws on localStorage
 * access, and an exception here would blank the whole app rather than log anyone in.
 */
export function getToken(): string | null {
  try {
    return window.localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function storeSession(token: string, expiresInSeconds: number): void {
  try {
    window.localStorage.setItem(TOKEN_KEY, token);
    // Stored as an absolute local timestamp derived from the *relative* value the
    // API returns. The API deliberately sends seconds rather than an absolute
    // time: this browser's clock and the server's disagree by an unknown amount,
    // and comparing against the server's absolute expiry would log people out
    // early or late depending on that skew.
    window.localStorage.setItem(
      EXPIRY_KEY,
      String(Date.now() + expiresInSeconds * 1000),
    );
  } catch {
    // Storage unavailable. The token still works for this page's lifetime; the
    // next navigation lands on /login. Better than failing the login outright.
  }
}

export function clearSession(): void {
  try {
    window.localStorage.removeItem(TOKEN_KEY);
    window.localStorage.removeItem(EXPIRY_KEY);
  } catch {
    /* nothing to clear */
  }
}

/**
 * Is there a token that has not already expired by this browser's clock?
 *
 * A local check, so the common case — a session left open overnight — redirects
 * instantly instead of firing a request that is certain to 401. It is not a
 * security check: the signature is what the API trusts, and only the API can
 * verify it. A tampered expiry buys nothing but a 401 one moment later.
 */
export function hasValidSession(): boolean {
  const token = getToken();
  if (!token) return false;
  try {
    const expiresAt = Number(window.localStorage.getItem(EXPIRY_KEY) || 0);
    // 0 means an old session stored before expiry tracking existed. Treat as
    // valid and let the API decide, rather than logging the operator out on deploy.
    if (expiresAt && Date.now() >= expiresAt) return false;
  } catch {
    return true;
  }
  return true;
}

/** Send the operator to /login, preserving where they were trying to go. */
export function redirectToLogin(): void {
  if (typeof window === 'undefined') return;
  const { pathname, search } = window.location;
  if (pathname === LOGIN_PATH) return;
  const next = encodeURIComponent(pathname + search);
  // `location.replace`, not `router.push`: this is called from inside fetch error
  // paths that have no access to a Next router instance, and replace keeps the
  // expired page out of the back-button history.
  window.location.replace(`${LOGIN_PATH}?next=${next}`);
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

/**
 * `fetch` against the API with the bearer token attached.
 *
 * `path` is API-relative and must start with '/'.
 *
 * On 401 this clears the stored session and redirects to /login, then throws so
 * the caller's `await` never resolves into code that assumes it has data. The
 * throw matters: returning the 401 Response would let every call site's
 * `if (!res.ok)` branch paint its own error banner over a page that is about to
 * navigate away, so the operator sees "Failed to load jobs" instead of a login form.
 */
export async function apiFetch(
  path: string,
  init: RequestInit = {},
): Promise<Response> {
  const token = getToken();
  const headers = new Headers(init.headers);
  if (token) headers.set('Authorization', `Bearer ${token}`);
  // Only when there is a body. Setting it on a GET makes some proxies treat the
  // request as having an entity, and it is what turns a plain GET into a
  // CORS-preflighted one for no reason.
  if (init.body && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }

  const response = await fetch(`${API_BASE}${path}`, { ...init, headers });

  if (response.status === 401) {
    clearSession();
    redirectToLogin();
    throw new ApiError(401, 'Session expired — redirecting to sign in');
  }

  return response;
}

/** POST /auth/login. Stores the session on success. */
export async function login(
  email: string,
  password: string,
): Promise<LoginResponse> {
  // Plain fetch, not apiFetch: a 401 here is a wrong password, and apiFetch would
  // "helpfully" redirect to the login page the operator is already looking at,
  // wiping the form and the error message with it.
  const response = await fetch(`${API_BASE}/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  });

  if (!response.ok) {
    const detail = await response
      .json()
      .then((body) => body?.detail)
      .catch(() => null);
    throw new ApiError(
      response.status,
      detail || `Sign in failed (${response.status})`,
    );
  }

  const body: LoginResponse = await response.json();
  storeSession(body.access_token, body.expires_in);
  return body;
}

/** GET /auth/me. Confirms with the API that the stored token is still good. */
export async function fetchCurrentUser(): Promise<AuthUser> {
  const response = await apiFetch('/auth/me');
  if (!response.ok) {
    throw new ApiError(response.status, 'Could not load your account');
  }
  return response.json();
}

export function logout(): void {
  clearSession();
  redirectToLogin();
}
