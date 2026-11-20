import type { ColumnInfo, ColumnPrivacyChoice, WorksheetInfo } from "../types";

interface MaskingColumnsPanelProps {
  filename: string;
  rowCount: number;
  columns: ColumnInfo[];
  worksheets?: WorksheetInfo[];
  privacyPolicy: Record<string, ColumnPrivacyChoice>;
  useNer: boolean;
  isApplying: boolean;
  mode?: "new" | "edit";
  onPrivacyPolicyChange: (policy: Record<string, ColumnPrivacyChoice>) => void;
  onUseNerChange: (enabled: boolean) => void;
  onCancel: () => void;
  onApply: () => void;
}

const TYPE_LABELS: Record<string, string> = {
  PERSON: "Personal name", EMAIL: "Email", PHONE: "Phone", ADDRESS: "Address",
  ID: "Identifier", DOB: "Birth date", AMOUNT: "Business measure", IP: "IP address",
};

function ShieldIcon() { return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" className="h-5 w-5" aria-hidden><path d="M12 3.5 19 6v5.1c0 4.6-2.9 7.8-7 9.4-4.1-1.6-7-4.8-7-9.4V6l7-2.5Z"/><path d="m9.3 12 1.8 1.8 3.7-4"/></svg>; }

export function MaskingColumnsPanel({ filename, rowCount, columns, worksheets = [], privacyPolicy, useNer, isApplying, mode = "new", onPrivacyPolicyChange, onUseNerChange, onCancel, onApply }: MaskingColumnsPanelProps) {
  const update = (name: string, action: ColumnPrivacyChoice["action"]) => {
    onPrivacyPolicyChange({ ...privacyPolicy, [name]: { action, share_confirmed: false } });
  };
  const confirmShare = (name: string, checked: boolean) => {
    onPrivacyPolicyChange({ ...privacyPolicy, [name]: { ...privacyPolicy[name], share_confirmed: checked } });
  };
  const pendingSensitiveShares = columns.filter((column) => column.sensitive && privacyPolicy[column.name]?.action === "share" && !privacyPolicy[column.name]?.share_confirmed);
  const canApply = !isApplying && pendingSensitiveShares.length === 0;
  const hasMaskColumns = columns.some((column) => privacyPolicy[column.name]?.action === "mask");

  return <div className="fixed inset-0 z-50 flex items-center justify-center bg-[#16365b]/20 p-4 backdrop-blur-sm">
    <div className="flex max-h-[92vh] w-full max-w-2xl flex-col overflow-hidden rounded-2xl border border-[#dce7f2] bg-white shadow-[0_24px_70px_rgba(31,64,96,0.18)]">
      <div className="border-b border-[#e5edf5] bg-[#fbfdff] px-6 py-5">
        <div className="flex items-start justify-between gap-5">
          <div className="flex min-w-0 items-start gap-3"><div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-[#eaf3ff] text-[#1769c2]"><ShieldIcon/></div><div className="min-w-0"><p className="text-[11px] font-semibold uppercase tracking-[0.12em] text-[#7890a8]">{mode === "edit" ? "Review file privacy" : "Choose file privacy"}</p><div className="mt-1 truncate text-[15px] font-semibold text-[#193250]" title={filename}>{filename}</div><p className="mt-1 text-[12px] text-[#8295aa]">{rowCount.toLocaleString()} rows · {worksheets.length || 1} worksheet{worksheets.length === 1 ? "" : "s"} · choose what the AI provider can receive</p></div></div>
          <button type="button" onClick={onCancel} disabled={isApplying} aria-label="Close privacy settings" className="privy-focus-ring flex h-9 w-9 shrink-0 items-center justify-center rounded-lg text-[22px] leading-none text-[#8295aa] hover:bg-[#eef4fb] disabled:opacity-30">×</button>
        </div>
      </div>
      <div className="flex-1 overflow-y-auto px-6 py-4">
        <div className="mb-4 rounded-xl border border-[#dce7f2] bg-[#f7faff] px-4 py-3 text-[12px] leading-5 text-[#526b84]">Share sends original values. Mask replaces detected values with local tokens; this may make numeric calculations unavailable. Exclude removes the column values from provider prompts and files. Automatic detection can miss sensitive data.</div>
        {worksheets.length > 0 && <div className="mb-4 flex flex-wrap gap-1.5" aria-label="Worksheets in uploaded workbook">{worksheets.map((sheet) => <span key={sheet.worksheet_id} className="rounded-full border border-[#dce7f2] bg-white px-2.5 py-1 text-[10px] text-[#617990]">{sheet.name}{sheet.visibility !== "visible" ? ` · ${sheet.visibility}` : ""}{sheet.is_empty ? " · empty" : ""}{sheet.is_irregular ? " · uneven rows" : ""}</span>)}</div>}
        {pendingSensitiveShares.length > 0 && <div className="mb-4 rounded-xl border border-[#f2d59c] bg-[#fffaf0] px-4 py-3 text-[12px] leading-5 text-[#8a5a00]">Confirm each sensitive Share choice below. Original values will be sent to the selected external AI provider without masking. Proceed only if you are authorized to share them.</div>}
        {columns.length === 0 && <p className="text-sm text-[#c2413b]">No columns were detected in this file.</p>}
        <div className="overflow-hidden rounded-xl border border-[#dce7f2]">
          {columns.map((column, index) => {
            const choice = privacyPolicy[column.name] ?? { action: column.recommended_action, share_confirmed: false };
            const isSensitiveShare = column.sensitive && choice.action === "share";
            return <div key={column.name} className={`${index ? "border-t border-[#edf2f7]" : ""} px-4 py-3.5 ${isSensitiveShare ? "bg-[#fffdfa]" : "bg-white"}`}>
              <div className="flex flex-wrap items-center gap-3">
                <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-[#304965]" title={column.display_name ?? column.name}>
                  {column.worksheet_name && <span className="mr-2 text-[10px] font-semibold uppercase tracking-wide text-[#8295aa]">{column.worksheet_name}{column.worksheet_visibility !== "visible" ? ` (${column.worksheet_visibility})` : ""} ·</span>}
                  {column.display_name ?? column.name}
                </span>
                <span className={`shrink-0 rounded-full px-2.5 py-1 text-[11px] ${column.sensitive ? "bg-[#fff0ed] font-semibold text-[#a64337]" : "bg-[#f2f5f8] text-[#718399]"}`}>{column.type ? (TYPE_LABELS[column.type] ?? column.type) : "No PII pattern detected"}</span>
                <select aria-label={`Privacy action for ${column.name}`} value={choice.action} disabled={isApplying} onChange={(event) => update(column.name, event.target.value as ColumnPrivacyChoice["action"])} className="privy-focus-ring rounded-lg border border-[#d5e3f1] bg-white px-2.5 py-2 text-[12px] font-medium text-[#304965]">
                  <option value="share">Share</option><option value="mask">Mask</option><option value="exclude">Exclude</option>
                </select>
              </div>
              <p className="mt-1 text-[11px] text-[#8295aa]">Recommended: {column.recommended_action} · {column.reason}</p>
              {isSensitiveShare && <label className="mt-3 flex cursor-pointer items-start gap-2.5 rounded-lg border border-[#f0d39a] bg-[#fff8e8] p-3 text-[11px] leading-5 text-[#805400]"><input type="checkbox" checked={choice.share_confirmed} onChange={(event) => confirmShare(column.name, event.target.checked)} disabled={isApplying} className="mt-1 h-4 w-4 accent-[#b7791f]"/><span>I understand original values in <strong>{column.name}</strong> will be sent to the selected external AI provider without masking, and I am authorized to share them.</span></label>}
            </div>;
          })}
        </div>
        {hasMaskColumns && <label className="mt-4 flex cursor-pointer items-start gap-3 rounded-xl border border-[#dce7f2] bg-[#f7faff] px-4 py-3.5 text-[12px] leading-5 text-[#526b84]"><input type="checkbox" checked={useNer} onChange={(event) => onUseNerChange(event.target.checked)} disabled={isApplying} className="mt-1 h-4 w-4 accent-[#2563eb]"/><span><strong className="block text-[13px] text-[#294663]">Enhanced free-text PII scan (NER)</strong>Scans masked prose columns for names and locations. This adds processing time; structured pattern masking remains active.</span></label>}
      </div>
      <div className="flex items-center justify-between border-t border-[#e5edf5] bg-[#fbfdff] px-6 py-4"><p className="text-[12px] text-[#8295aa]">{columns.length} columns · {pendingSensitiveShares.length ? "Sensitive Share confirmation required" : "Privacy choices ready"}</p><div className="flex gap-2"><button type="button" onClick={onCancel} disabled={isApplying} className="privy-focus-ring rounded-lg border border-[#d5e3f1] bg-white px-4 py-2.5 text-[13px] font-medium text-[#58718c]">Cancel</button><button type="button" onClick={onApply} disabled={!canApply} className="privy-focus-ring rounded-lg bg-[#17385f] px-4 py-2.5 text-[13px] font-semibold text-white disabled:cursor-not-allowed disabled:opacity-45">{isApplying ? "Applying…" : mode === "edit" ? "Save privacy choices" : "Attach file"}</button></div></div>
    </div>
  </div>;
}
