import type { ColumnInfo } from "../types";

interface MaskingColumnsPanelProps {
  filename: string;
  rowCount: number;
  columns: ColumnInfo[];
  selectedColumns: string[];
  useNer: boolean;
  isApplying: boolean;
  mode?: "new" | "edit";
  onChange: (columns: string[]) => void;
  onUseNerChange: (enabled: boolean) => void;
  onCancel: () => void;
  onApply: () => void;
}

const TYPE_LABELS: Record<string, string> = {
  PERSON: "Name", EMAIL: "Email", PHONE: "Phone", ADDRESS: "Address",
  ID: "ID number", DOB: "Date of birth", AMOUNT: "Amount", IP: "IP address",
};

function ShieldIcon() { return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" className="h-5 w-5" aria-hidden><path d="M12 3.5 19 6v5.1c0 4.6-2.9 7.8-7 9.4-4.1-1.6-7-4.8-7-9.4V6l7-2.5Z"/><path d="m9.3 12 1.8 1.8 3.7-4"/></svg>; }
function FileIcon() { return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" className="h-4 w-4" aria-hidden><path d="M6 4.5h8l4 4V20H6V4.5Z"/><path d="M14 4.5V9h4M9 13h6M9 16h4"/></svg>; }

export function MaskingColumnsPanel({ filename,rowCount,columns,selectedColumns,useNer,isApplying,mode="new",onChange,onUseNerChange,onCancel,onApply }: MaskingColumnsPanelProps) {
  const selected=new Set(selectedColumns);
  const detectedColumns=columns.filter(c=>c.type);
  const selectedFreeTextColumns=columns.filter(c=>!c.type && selected.has(c.name));
  const unmaskedDetectedCount=detectedColumns.filter(c=>!selected.has(c.name)).length;
  const canApply=selectedColumns.length>0 && !isApplying;
  const setAll=(enabled:boolean)=>onChange(enabled?columns.map(c=>c.name):[]);
  const setDetected=()=>onChange(detectedColumns.map(c=>c.name));
  const toggle=(name:string)=>{const next=new Set(selected); if(next.has(name)) next.delete(name); else next.add(name); onChange(Array.from(next));};

  return <div className="fixed inset-0 z-50 flex items-center justify-center bg-[#16365b]/20 p-4 backdrop-blur-sm">
    <div className="flex max-h-[92vh] w-full max-w-2xl flex-col overflow-hidden rounded-2xl border border-[#dce7f2] bg-white shadow-[0_24px_70px_rgba(31,64,96,0.18)]">
      <div className="border-b border-[#e5edf5] bg-[#fbfdff] px-6 py-5">
        <div className="flex items-start justify-between gap-5">
          <div className="flex min-w-0 items-start gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-[#eaf3ff] text-[#1769c2]"><ShieldIcon/></div>
            <div className="min-w-0"><p className="text-[11px] font-semibold uppercase tracking-[0.12em] text-[#7890a8]">{mode === "edit" ? "Edit protection" : "Protect your file"}</p><div className="mt-1 flex items-center gap-2 text-[15px] font-semibold text-[#193250]"><FileIcon/><span className="truncate" title={filename}>{filename}</span></div><p className="mt-1 text-[12px] text-[#8295aa]">{rowCount.toLocaleString()} rows · choose what Privy should protect</p></div>
          </div>
          <button type="button" onClick={onCancel} disabled={isApplying} aria-label="Close masking settings" className="privy-focus-ring flex h-9 w-9 shrink-0 items-center justify-center rounded-lg text-[22px] leading-none text-[#8295aa] hover:bg-[#eef4fb] hover:text-[#294663] disabled:opacity-30">×</button>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2 border-b border-[#edf2f7] px-6 py-3.5">
        <button type="button" onClick={setDetected} disabled={isApplying} className="privy-focus-ring rounded-lg border border-[#d5e3f1] bg-white px-3 py-2 text-[12px] font-medium text-[#476783] hover:bg-[#f4f8fd] disabled:opacity-40">Select detected PII ({detectedColumns.length})</button>
        <button type="button" onClick={()=>setAll(true)} disabled={isApplying} className="privy-focus-ring rounded-lg border border-[#d5e3f1] bg-white px-3 py-2 text-[12px] font-medium text-[#476783] hover:bg-[#f4f8fd] disabled:opacity-40">Select all</button>
        <button type="button" onClick={()=>setAll(false)} disabled={isApplying} className="privy-focus-ring rounded-lg border border-[#d5e3f1] bg-white px-3 py-2 text-[12px] font-medium text-[#476783] hover:bg-[#f4f8fd] disabled:opacity-40">Clear all</button>
      </div>

      <div className="flex-1 overflow-y-auto px-6 py-4">
        {unmaskedDetectedCount>0 && <div className="mb-4 rounded-xl border border-[#f2d59c] bg-[#fffaf0] px-4 py-3 text-[12px] leading-5 text-[#8a5a00]">{unmaskedDetectedCount} detected PII {unmaskedDetectedCount===1?"column is":"columns are"} not selected. Values in those columns will remain raw and may be sent to the AI.</div>}
        {selectedColumns.length===0 && <p className="mb-3 text-[12px] text-[#c2413b]">Select at least one column before attaching this file.</p>}
        {selectedFreeTextColumns.length>0 && <label className="mb-4 flex cursor-pointer items-start gap-3 rounded-xl border border-[#dce7f2] bg-[#f7faff] px-4 py-3.5 text-[12px] leading-5 text-[#526b84]"><input type="checkbox" checked={useNer} onChange={e=>onUseNerChange(e.target.checked)} disabled={isApplying} className="mt-1 h-4 w-4 accent-[#2563eb]"/><span><span className="block text-[13px] font-semibold text-[#294663]">Enhanced free-text PII scan (NER)</span>Scans selected non-structured columns for names and locations in prose. This adds processing time; structured pattern masking remains enabled.</span></label>}
        <div className="overflow-hidden rounded-xl border border-[#dce7f2]">
          {columns.map((column,index)=>{const isSelected=selected.has(column.name); const typeLabel=column.type?(TYPE_LABELS[column.type]??column.type):"Not detected"; return <label key={column.name} className={`flex cursor-pointer items-center gap-3 px-4 py-3.5 ${index>0?"border-t border-[#edf2f7]":""} ${isSelected?"bg-[#f5f9ff]":"bg-white"} hover:bg-[#f3f8fe]`}><input type="checkbox" checked={isSelected} onChange={()=>toggle(column.name)} disabled={isApplying} className="h-4 w-4 accent-[#2563eb]"/><span className="min-w-0 flex-1 truncate text-[13px] font-medium text-[#304965]" title={column.name}>{column.name}</span><span className={`shrink-0 rounded-full px-2.5 py-1 text-[11px] ${column.type?"bg-[#eaf3ff] font-semibold text-[#2c68a7]":"bg-[#f2f5f8] text-[#8295aa]"}`}>{typeLabel}</span></label>})}
        </div>
      </div>

      <div className="flex items-center justify-between border-t border-[#e5edf5] bg-[#fbfdff] px-6 py-4"><p className="text-[12px] text-[#8295aa]">{selectedColumns.length} of {columns.length} columns selected</p><div className="flex gap-2"><button type="button" onClick={onCancel} disabled={isApplying} className="privy-focus-ring rounded-lg border border-[#d5e3f1] bg-white px-4 py-2.5 text-[13px] font-medium text-[#58718c] hover:bg-[#f4f8fd] disabled:opacity-40">Cancel</button><button type="button" onClick={onApply} disabled={!canApply} className="privy-focus-ring rounded-lg bg-[#17385f] px-4 py-2.5 text-[13px] font-semibold text-white shadow-sm hover:bg-[#204a7a] disabled:cursor-not-allowed disabled:opacity-45">{isApplying?"Masking…":mode==="edit"?"Save changes":"Protect file"}</button></div></div>
    </div>
  </div>;
}
