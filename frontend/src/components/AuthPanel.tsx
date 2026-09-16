import { FormEvent, useState } from "react";
import type { CurrentUser } from "../types";
import { api, ApiError } from "../api";
import { setAccessToken } from "../auth";

interface AuthPanelProps {
  onAuthenticated: (accessToken: string, user: CurrentUser) => void | Promise<void>;
  onGuest: () => void | Promise<void>;
  onClose?: () => void;
  error?: string | null;
  compact?: boolean;
}

export function AuthPanel({
  onAuthenticated,
  onGuest,
  onClose,
  error,
  compact = false,
}: AuthPanelProps) {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [busy, setBusy] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setLocalError(null);

    const normalizedEmail = email.trim().toLowerCase();
    if (!normalizedEmail) {
      setLocalError("Email is required.");
      return;
    }

    if (mode === "register" && password.length < 8) {
      setLocalError("Password must be at least 8 characters.");
      return;
    }

    setBusy(true);
    try {
      const result =
        mode === "login"
          ? await api.login(normalizedEmail, password)
          : await api.register(normalizedEmail, password, displayName.trim());

      setAccessToken(result.access_token);
      await onAuthenticated(result.access_token, result.user);
    } catch (e) {
      setLocalError(
        e instanceof ApiError
          ? e.message
          : e instanceof Error
            ? e.message
            : "Authentication failed."
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={compact ? "rounded-2xl border border-border bg-surface p-7 shadow-xl" : "flex h-screen w-screen items-center justify-center bg-bg px-6"}>
      <div className={compact ? "w-full" : "w-full max-w-md rounded-2xl border border-border bg-surface p-8 text-center shadow-sm"}>
        <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-2xl bg-bg text-2xl" aria-hidden>
          🔒
        </div>

        <div className="text-center">
          <h1 className="font-display text-2xl font-semibold">Welcome to Privy</h1>
          <p className="mt-2 text-sm leading-6 text-ink/60">
            Protect sensitive data before it reaches the AI.
          </p>
        </div>

        <div className="mt-6 flex rounded-xl border border-border bg-bg p-1">
          <button
            type="button"
            onClick={() => {
              setMode("login");
              setLocalError(null);
            }}
            className={`flex-1 rounded-lg px-3 py-2 text-sm font-medium ${mode === "login" ? "bg-surface text-ink shadow-sm" : "text-ink/50"}`}
          >
            Sign in
          </button>
          <button
            type="button"
            onClick={() => {
              setMode("register");
              setLocalError(null);
            }}
            className={`flex-1 rounded-lg px-3 py-2 text-sm font-medium ${mode === "register" ? "bg-surface text-ink shadow-sm" : "text-ink/50"}`}
          >
            Create account
          </button>
        </div>

        <form onSubmit={handleSubmit} className="mt-5 space-y-3 text-left">
          {mode === "register" && (
            <label className="block">
              <span className="mb-1.5 block text-xs font-medium text-ink/65">Display name</span>
              <input
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                autoComplete="name"
                className="w-full rounded-xl border border-border bg-bg px-3.5 py-3 text-sm outline-none focus:border-ink/30"
                placeholder="Your name"
              />
            </label>
          )}

          <label className="block">
            <span className="mb-1.5 block text-xs font-medium text-ink/65">Email</span>
            <input
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              autoComplete="email"
              className="w-full rounded-xl border border-border bg-bg px-3.5 py-3 text-sm outline-none focus:border-ink/30"
              placeholder="you@example.com"
            />
          </label>

          <label className="block">
            <span className="mb-1.5 block text-xs font-medium text-ink/65">Password</span>
            <input
              type="password"
              required
              minLength={mode === "register" ? 8 : 1}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete={mode === "register" ? "new-password" : "current-password"}
              className="w-full rounded-xl border border-border bg-bg px-3.5 py-3 text-sm outline-none focus:border-ink/30"
              placeholder={mode === "register" ? "At least 8 characters" : "Your password"}
            />
          </label>

          {(localError || error) && (
            <p className="text-sm text-red-600">{localError || error}</p>
          )}

          <button
            type="submit"
            disabled={busy}
            className="w-full rounded-xl bg-ink px-4 py-3 text-sm font-medium text-white hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {busy ? "Please wait…" : mode === "login" ? "Sign in" : "Create account"}
          </button>
        </form>

        <div className="my-4 flex items-center gap-3 text-xs text-ink/35">
          <span className="h-px flex-1 bg-border" />
          or
          <span className="h-px flex-1 bg-border" />
        </div>

        <button
          type="button"
          onClick={() => void onGuest()}
          disabled={busy}
          className="w-full rounded-xl border border-border px-4 py-3 text-sm font-medium text-ink hover:bg-bg disabled:opacity-50"
        >
          Try Privy as Guest
        </button>

        <p className="mt-3 text-center text-xs text-ink/40">
          Guest sessions are temporary. Sign in to keep your chats tied to an account.
        </p>

        {onClose && (
          <button
            type="button"
            onClick={onClose}
            className="mt-4 w-full rounded-xl px-4 py-2 text-sm text-ink/55 hover:bg-bg"
          >
            Cancel
          </button>
        )}
      </div>
    </div>
  );
}
