let getAccessToken: (() => Promise<string>) | null = null;
let getGuestSession: (() => string | null) | null = null;

export function setAccessTokenGetter(
  getter: (() => Promise<string>) | null
) {
  getAccessToken = getter;
}

export function setGuestSessionGetter(
  getter: (() => string | null) | null
) {
  getGuestSession = getter;
}

export async function authHeaders(): Promise<Record<string, string>> {
  if (getAccessToken) {
    const token = await getAccessToken();

    if (token && typeof token === "string" && token.trim()) {
      return {
        Authorization: `Bearer ${token.trim()}`,
      };
    }
  }

  const guestSession = getGuestSession?.();

  if (guestSession && guestSession.trim()) {
    return {
      "X-Guest-Session": guestSession.trim(),
    };
  }

  return {};
}
