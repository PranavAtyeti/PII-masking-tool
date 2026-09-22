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

      <aside className="relative flex h-full w-full max-w-lg flex-col border-l border-[#dce7f2] bg-white shadow-[0_18px_60px_rgba(31,64,96,0.16)]">
        <div className="flex items-center justify-between border-b border-[#e5edf5] px-7 py-5">
          <div>
            <p className="font-display text-[17px] font-semibold text-ink">Privy Settings</p>
            <p className="mt-0.5 text-[13px] text-[#8295aa]">Manage the AI configuration used by this workspace.</p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close settings"
            className="rounded-lg px-2.5 py-2 text-lg text-ink/45 hover:bg-[#f7faff] hover:text-ink"
          >
            ×
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-7 py-7">
          {loading ? (
            <div className="rounded-xl border border-[#e5edf5] bg-[#f7faff] px-4 py-3 text-sm text-ink/55">
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
                  <h3 className="text-[15px] font-semibold text-[#294663]">AI configuration</h3>
                  <p className="mt-1 text-[13px] leading-6 text-[#8295aa]">
                    These settings control the model Privy uses for chat responses.
                  </p>
                </div>

                <label className="mb-2 block text-[13px] font-medium text-[#526b84]">Model</label>
                <select
                  value={model}
                  onChange={(e) => setModel(e.target.value)}
                  className="w-full rounded-lg border border-[#dce7f2] bg-white px-3.5 py-3 text-[14px] text-[#294663] outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/10"
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
                    className="mt-2 w-full rounded-lg border border-[#dce7f2] bg-white px-3.5 py-3 text-[14px] text-[#294663] outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/10"
                  />
                )}
              </section>

              <section>
                <div className="mb-3">
                  <h3 className="text-[15px] font-semibold text-[#294663]">Groq API key</h3>
                  <p className="mt-1 text-[13px] leading-6 text-[#8295aa]">
                    The full key is never returned by the backend. Leave this blank to keep the current key.
                  </p>
                </div>

                <div className="mb-2 flex items-center justify-between rounded-xl border border-[#dce7f2] bg-[#f7faff] px-3.5 py-3">
                  <span className="text-[13px] text-[#8295aa]">Current status</span>
                  <span className="text-[13px] font-semibold text-[#476783]">
                    {config.api_key_set ? `Configured · ${config.api_key_preview}` : "Not configured"}
                  </span>
                </div>

                <input
                  type="password"
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  placeholder={config.api_key_set ? "Enter a new key to replace it" : "Paste your Groq API key"}
                  autoComplete="new-password"
                  className="w-full rounded-lg border border-[#dce7f2] bg-white px-3.5 py-3 text-[14px] text-[#294663] outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/10"
                />
              </section>

              <section className="rounded-xl border border-[#dce7f2] bg-[#f7faff] px-4 py-4">
                <div className="flex items-start gap-3">
                  <span className="mt-0.5" aria-hidden>🔒</span>
                  <div>
                    <p className="text-[13px] font-semibold text-[#294663]">Local configuration</p>
                    <p className="mt-1 text-[12px] leading-5 text-[#8295aa]">
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

        <div className="border-t border-[#e5edf5] px-7 py-5">
          <button
            type="button"
            onClick={handleSave}
            disabled={loading || saving || !config}
            className="w-full rounded-xl bg-[#17385f] px-4 py-3 text-[13px] font-medium text-white transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {saving ? "Saving…" : "Save changes"}
          </button>
        </div>
      </aside>
    </div>
  );
}
