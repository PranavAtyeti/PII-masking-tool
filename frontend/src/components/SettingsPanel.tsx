import { useEffect, useState } from "react";
import type { AdminConfig } from "../types";
import { ApiError, api } from "../api";

interface SettingsPanelProps {
  open: boolean;
  onClose: () => void;
}

export function SettingsPanel({ open, onClose }: SettingsPanelProps) {
  const [config, setConfig] = useState<AdminConfig | null>(null);
  const [model, setModel] = useState("");
  const [customModel, setCustomModel] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const isCustomModel = Boolean(config && model === "__custom__");
  const effectiveModel = isCustomModel ? customModel.trim() : model.trim();

  useEffect(() => {
    if (!open) return;

    setLoading(true);
    setError(null);
    setSaved(false);
    setApiKey("");

    api.getAdminConfig()
      .then((next) => {
        setConfig(next);
        if (next.common_models.includes(next.model)) {
          setModel(next.model);
          setCustomModel("");
        } else {
          setModel("__custom__");
          setCustomModel(next.model);
        }
      })
      .catch((e) => {
        setError(e instanceof Error ? e.message : "Couldn't load settings.");
      })
      .finally(() => setLoading(false));
  }, [open]);

  useEffect(() => {
    if (!open) return;

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, onClose]);

  async function handleSave() {
    if (!effectiveModel) {
      setError("Choose a model before saving.");
      return;
    }

    setSaving(true);
    setError(null);
    setSaved(false);

    try {
      const payload: { api_key?: string; model?: string } = { model: effectiveModel };
      if (apiKey.trim()) payload.api_key = apiKey.trim();

      const next = await api.updateAdminConfig(payload);
      setConfig(next);
      setApiKey("");
      setSaved(true);

      if (next.common_models.includes(next.model)) {
        setModel(next.model);
        setCustomModel("");
      } else {
        setModel("__custom__");
        setCustomModel(next.model);
      }
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Couldn't save settings.");
    } finally {
      setSaving(false);
    }
  }

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      <button
        type="button"
        aria-label="Close settings"
        onClick={onClose}
        className="absolute inset-0 bg-black/20 backdrop-blur-[1px]"
      />

      <aside className="relative flex h-full w-full max-w-md flex-col border-l border-border bg-surface shadow-2xl">
        <div className="flex items-center justify-between border-b border-border px-6 py-4">
          <div>
            <p className="font-display text-base font-semibold text-ink">Privy Settings</p>
            <p className="mt-0.5 text-xs text-ink/45">Manage the AI configuration used by this workspace.</p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close settings"
            className="rounded-lg px-2.5 py-2 text-lg text-ink/45 hover:bg-bg hover:text-ink"
          >
            ×
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-6 py-6">
          {loading ? (
            <div className="rounded-xl border border-border bg-bg px-4 py-3 text-sm text-ink/55">
              Loading settings…
            </div>
          ) : error && !config ? (
            <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
              {error}
            </div>
          ) : config ? (
            <div className="space-y-7">
              <section>
                <div className="mb-3">
                  <h3 className="text-sm font-semibold text-ink">AI configuration</h3>
                  <p className="mt-1 text-xs leading-5 text-ink/45">
                    These settings control the model Privy uses for chat responses.
                  </p>
                </div>

                <label className="mb-2 block text-xs font-medium text-ink/70">Model</label>
                <select
                  value={model}
                  onChange={(e) => setModel(e.target.value)}
                  className="w-full rounded-xl border border-border bg-surface px-3 py-2.5 text-sm text-ink outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/10"
                >
                  {config.common_models.map((name) => (
                    <option key={name} value={name}>
                      {name}
                    </option>
                  ))}
                  <option value="__custom__">Custom model…</option>
                </select>

                {isCustomModel && (
                  <input
                    value={customModel}
                    onChange={(e) => setCustomModel(e.target.value)}
                    placeholder="provider/model-name"
                    className="mt-2 w-full rounded-xl border border-border bg-surface px-3 py-2.5 text-sm text-ink outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/10"
                  />
                )}
              </section>

              <section>
                <div className="mb-3">
                  <h3 className="text-sm font-semibold text-ink">Groq API key</h3>
                  <p className="mt-1 text-xs leading-5 text-ink/45">
                    The full key is never returned by the backend. Leave this blank to keep the current key.
                  </p>
                </div>

                <div className="mb-2 flex items-center justify-between rounded-xl border border-border bg-bg px-3 py-2.5">
                  <span className="text-xs text-ink/60">Current status</span>
                  <span className="text-xs font-medium text-ink/75">
                    {config.api_key_set ? `Configured · ${config.api_key_preview}` : "Not configured"}
                  </span>
                </div>

                <input
                  type="password"
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  placeholder={config.api_key_set ? "Enter a new key to replace it" : "Paste your Groq API key"}
                  autoComplete="new-password"
                  className="w-full rounded-xl border border-border bg-surface px-3 py-2.5 text-sm text-ink outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/10"
                />
              </section>

              <section className="rounded-xl border border-border bg-bg px-4 py-3.5">
                <div className="flex items-start gap-3">
                  <span className="mt-0.5" aria-hidden>🔒</span>
                  <div>
                    <p className="text-xs font-semibold text-ink">Local configuration</p>
                    <p className="mt-1 text-xs leading-5 text-ink/50">
                      Settings are stored by the Privy backend for this local workspace. The API key is only sent when you explicitly replace it.
                    </p>
                  </div>
                </div>
              </section>
            </div>
          ) : null}

          {error && config && (
            <div className="mt-5 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
              {error}
            </div>
          )}

          {saved && (
            <div className="mt-5 rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-700">
              ✓ Settings saved.
            </div>
          )}
        </div>

        <div className="border-t border-border px-6 py-4">
          <button
            type="button"
            onClick={handleSave}
            disabled={loading || saving || !config}
            className="w-full rounded-xl bg-ink px-4 py-2.5 text-sm font-medium text-white transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {saving ? "Saving…" : "Save changes"}
          </button>
        </div>
      </aside>
    </div>
  );
}
