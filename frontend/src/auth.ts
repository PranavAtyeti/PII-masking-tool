let getAccessToken: (() => Promise<string>) | null = null;

export function setAccessTokenGetter(
  getter: (() => Promise<string>) | null
) {
  getAccessToken = getter;
}

export async function authHeaders(): Promise<Record<string, string>> {
  if (!getAccessToken) {
    throw new Error("Auth0 token getter is not ready.");
  }

  const token = await getAccessToken();

  if (!token || typeof token !== "string" || !token.trim()) {
    throw new Error("Auth0 did not return an access token.");
  }

  return {
    Authorization: `Bearer ${token.trim()}`,
  };
}