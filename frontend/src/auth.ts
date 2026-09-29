export interface AuthUser {
  sub: string;
  email: string | null;
  display_name: string | null;
  role: "admin" | "user" | "guest";
  created_at: number;
  last_login_at: number;
  email_verified?: boolean;
}

export interface AuthResponse {
  access_token: string;
  token_type: "bearer" | string;
  user: AuthUser;
}

type AccessTokenGetter = (() => Promise<string>) | null;

type GuestSessionGetter = (() => string | null) | null;

let accessToken: string | null = null;
let accessTokenGetter: AccessTokenGetter = null;
let guestSessionGetter: GuestSessionGetter = null;
let refreshPromise: Promise<AuthResponse | null> | null = null;

export function setAccessToken(token: string | null) {
  accessToken = token;
}

export function clearAccessToken() {
  accessToken = null;
}

export function getAccessToken(): string | null {
  return accessToken;
}

// Kept as a compatibility hook for older callers during the Auth0 removal.
export function setAccessTokenGetter(getter: AccessTokenGetter) {
  accessTokenGetter = getter;
}

export function setGuestSessionGetter(getter: GuestSessionGetter) {
  guestSessionGetter = getter;
}

export async function authHeaders(): Promise<Record<string, string>> {
  if (accessToken) {
    return { Authorization: `Bearer ${accessToken}` };
  }

  if (accessTokenGetter) {
    const token = await accessTokenGetter();
    if (token) {
      accessToken = token;
      return { Authorization: `Bearer ${token}` };
    }
  }

  const guestSession = guestSessionGetter?.();
  return guestSession ? { "X-Guest-Session": guestSession } : {};
}

const API_ORIGIN = (
  (import.meta as ImportMeta & {
    env?: {
      VITE_API_BASE_URL?: string;
    };
  }).env?.VITE_API_BASE_URL ?? ""
).replace(/\/$/, "");
const API_BASE = `${API_ORIGIN}/api`;

export async function refreshAccessToken(): Promise<AuthResponse | null> {
  if (refreshPromise) return refreshPromise;

  refreshPromise = (async () => {
    const response = await fetch(`${API_BASE}/auth/refresh`, {
      method: "POST",
      credentials: "include",
    });

    if (!response.ok) {
      clearAccessToken();
      return null;
    }

    const data = (await response.json()) as AuthResponse;
    accessToken = data.access_token;
    return data;
  })();

  try {
    return await refreshPromise;
  } finally {
    refreshPromise = null;
  }
}

export async function restoreSession(): Promise<AuthResponse | null> {
  return refreshAccessToken();
}
