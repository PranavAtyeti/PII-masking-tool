import { useMemo, useState } from "react";
import type { ModelOption } from "../types";

interface ModelSelectorProps {
  models: ModelOption[];
  selectedModelId: string;
  onChange: (modelId: string) => void;
  disabled?: boolean;
}

export function ModelSelector({
  models,
  selectedModelId,
  onChange,
  disabled = false,
}: ModelSelectorProps) {
  const [open, setOpen] = useState(false);

  const selected = useMemo(
    () => models.find((model) => model.id === selectedModelId) ?? models[0],
    [models, selectedModelId]
  );

  const grouped = useMemo(() => {
    const groups = new Map<string, ModelOption[]>();
    for (const model of models) {
      const current = groups.get(model.provider) ?? [];
      current.push(model);
      groups.set(model.provider, current);
    }
    return [...groups.entries()];
  }, [models]);

  if (!selected) return null;

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        disabled={disabled}
        className="flex items-center gap-2 rounded-lg px-3 py-2 text-[13px] text-[#294663]/65 transition-colors hover:bg-[#f1f6fc] hover:text-[#294663] disabled:cursor-not-allowed disabled:opacity-40"
        aria-haspopup="menu"
        aria-expanded={open}
      >
        <span className="max-w-44 truncate font-medium text-[#294663]/80">
          {selected.label}
        </span>
        <span className="text-[11px] text-[#294663]/40">▾</span>
      </button>

      {open && (
        <>
          <button
            type="button"
            aria-label="Close model selector"
            onClick={() => setOpen(false)}
            className="fixed inset-0 z-40 cursor-default"
          />

          <div
            role="menu"
            className="absolute right-0 top-full z-50 mt-1 w-80 overflow-hidden rounded-xl border border-[#dce7f2] bg-white p-1.5 shadow-xl"
          >
            <div className="px-2.5 py-2">
              <p className="text-[13px] font-semibold text-[#294663]">Choose a model</p>
              <p className="mt-0.5 text-[12px] text-[#8295aa]">
                Your selection applies to this browser.
              </p>
            </div>

            {grouped.map(([provider, providerModels]) => (
              <div key={provider} className="mt-1">
                <div className="px-2.5 pb-1 pt-2 text-[11px] font-semibold uppercase tracking-wider text-[#294663]/35">
                  {provider}
                </div>

                {providerModels.map((model) => {
                  const active = model.id === selected.id;
                  return (
                    <button
                      key={model.id}
                      type="button"
                      role="menuitemradio"
                      aria-checked={active}
                      onClick={() => {
                        onChange(model.id);
                        setOpen(false);
                      }}
                      className={`flex w-full items-center gap-3 rounded-lg px-2.5 py-2 text-left transition-colors hover:bg-[#f1f6fc] ${
                        active ? "bg-[#eef5fd]" : ""
                      }`}
                    >
                      <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-surface text-xs ring-1 ring-inset ring-[#dce7f2]">
                        {model.provider === "gemini" ? "✦" : "◉"}
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-[13px] font-semibold text-[#294663]">
                          {model.label}
                        </span>
                        <span className="mt-0.5 block truncate text-[11px] text-[#294663]/40">
                          {model.description}
                        </span>
                      </span>
                      {active && <span className="text-xs text-[#294663]/70">✓</span>}
                    </button>
                  );
                })}
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
