import type { ColumnInfo } from "../types";

interface MaskingColumnsPanelProps {
  filename: string;
  rowCount: number;
  columns: ColumnInfo[];
  selectedColumns: string[];
  isApplying: boolean;
  mode?: "new" | "edit";
  onChange: (columns: string[]) => void;
  onCancel: () => void;
  onApply: () => void;
}

const TYPE_LABELS: Record<string, string> = {
  PERSON: "Name",
  EMAIL: "Email",
  PHONE: "Phone",
  ADDRESS: "Address",
  ID: "ID number",
  DOB: "Date of birth",
  AMOUNT: "Amount",
  IP: "IP address",
};

export function MaskingColumnsPanel({
  filename,
  rowCount,
  columns,
  selectedColumns,
  isApplying,
  mode = "new",
  onChange,
  onCancel,
  onApply,
}: MaskingColumnsPanelProps) {
  const selected = new Set(selectedColumns);
  const detectedColumns = columns.filter((c) => c.type);
  const unmaskedDetectedCount = detectedColumns.filter((c) => !selected.has(c.name)).length;
  const canApply = selectedColumns.length > 0 && !isApplying;

  const setAll = (enabled: boolean) => onChange(enabled ? columns.map((c) => c.name) : []);
  const setDetected = () => onChange(detectedColumns.map((c) => c.name));

  const toggle = (name: string) => {
    const next = new Set(selected);
    if (next.has(name)) next.delete(name);
    else next.add(name);
    onChange(Array.from(next));
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4 backdrop-blur-[2px]">
      <div className="w-full max-w-xl overflow-hidden rounded-2xl border border-border bg-surface shadow-2xl">
        <div className="border-b border-border px-5 py-4">
          <div className="flex items-start justify-between gap-4">
            <div className="min-w-0">
              <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-ink/40">
                {mode === "edit" ? "Edit masking" : "Protect your file"}
              </p>
              <div className="flex items-center gap-2 text-sm font-semibold text-ink">
                <span aria-hidden>▦</span>
                <span className="truncate" title={filename}>{filename}</span>
              </div>
              <p className="mt-1 text-xs text-ink/50">
                {rowCount.toLocaleString()} rows · choose which columns Privy should mask
              </p>
            </div>
            <button
              type="button"
              onClick={onCancel}
              disabled={isApplying}
              className="rounded-lg px-2 py-1 text-xl leading-none text-ink/40 hover:bg-bg hover:text-ink disabled:opacity-30"
              aria-label="Close masking settings"
            >
              ×
            </button>
          </div>
        </div>

        <div className="space-y-3 px-5 pt-4">
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <button type="button" onClick={setDetected} disabled={isApplying} className="rounded-full border border-border px-3 py-1.5 text-ink/70 hover:bg-bg disabled:opacity-40">
              Select detected PII ({detectedColumns.length})
            </button>
            <button type="button" onClick={() => setAll(true)} disabled={isApplying} className="rounded-full border border-border px-3 py-1.5 text-ink/70 hover:bg-bg disabled:opacity-40">
              Select all
            </button>
            <button type="button" onClick={() => setAll(false)} disabled={isApplying} className="rounded-full border border-border px-3 py-1.5 text-ink/70 hover:bg-bg disabled:opacity-40">
              Clear all
            </button>
          </div>

          {unmaskedDetectedCount > 0 && (
            <div className="rounded-xl border border-amber-200 bg-amber-50 px-3 py-2.5 text-xs leading-5 text-amber-900">
              {unmaskedDetectedCount} detected PII {unmaskedDetectedCount === 1 ? "column is" : "columns are"} not selected. Values in those columns will remain raw and may be sent to the AI.
            </div>
          )}
          {selectedColumns.length === 0 && (
            <p className="text-xs text-red-600">Select at least one column before attaching this file.</p>
          )}
        </div>

        <div className="max-h-[52vh] overflow-y-auto px-5 py-4">
          <div className="overflow-hidden rounded-xl border border-border">
            {columns.map((column, index) => {
              const isSelected = selected.has(column.name);
              const typeLabel = column.type ? TYPE_LABELS[column.type] ?? column.type : "Not detected";
              return (
                <label key={column.name} className={`flex cursor-pointer items-center gap-3 px-4 py-3 ${index > 0 ? "border-t border-border" : ""} ${isSelected ? "bg-bg/60" : ""} hover:bg-bg/70`}>
                  <input
                    type="checkbox"
                    checked={isSelected}
                    onChange={() => toggle(column.name)}
                    disabled={isApplying}
                    className="h-4 w-4 accent-current"
                  />
                  <span className="min-w-0 flex-1 truncate text-sm text-ink" title={column.name}>{column.name}</span>
                  <span className={`shrink-0 rounded-full px-2 py-1 text-[11px] ${column.type ? "bg-ink/10 font-medium text-ink/70" : "bg-bg text-ink/40"}`}>
                    {typeLabel}
                  </span>
                </label>
              );
            })}
          </div>
        </div>

        <div className="flex items-center justify-between border-t border-border px-5 py-4">
          <p className="text-xs text-ink/50">{selectedColumns.length} of {columns.length} columns selected</p>
          <div className="flex gap-2">
            <button type="button" onClick={onCancel} disabled={isApplying} className="rounded-xl px-4 py-2 text-sm text-ink/60 hover:bg-bg disabled:opacity-40">Cancel</button>
            <button type="button" onClick={onApply} disabled={!canApply} className="rounded-xl bg-ink px-4 py-2 text-sm font-medium text-white hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50">
              {isApplying ? "Masking…" : mode === "edit" ? "Save changes" : "Apply & attach"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
