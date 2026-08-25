let getAccessToken: (() => Promise<string>) | null = null;

export function setAccessTokenGetter(getter: (() => Promise<string>) | null) {
  getAccessToken = getter;
}

export async function authHeaders(): Promise<Record<string, string>> {
  if (!getAccessToken) return {};
  const token = await getAccessToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}
