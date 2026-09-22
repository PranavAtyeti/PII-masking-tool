import { useEffect, useRef } from "react";
import type { ColumnInfo, Message, ModelOption } from "../types";
import { MessageBubble } from "./MessageBubble";
import { SuggestionChips } from "./SuggestionChips";
import { ChatInput, type ChatAttachment } from "./ChatInput";
import { MaskingColumnsPanel } from "./MaskingColumnsPanel";
import { ModelSelector } from "./ModelSelector";

interface ChatPaneProps {
  messages: Message[];
  suggestions: string[];
  attachments?: ChatAttachment[];
  isStreaming: boolean;
  isUploading: boolean;
  pendingFile?: File | null;
  pendingColumns?: ColumnInfo[];
  pendingRowCount?: number;
  selectedColumns?: string[];
  useNerForFile?: boolean;
  isEditingFile?: boolean;
  onSelectedColumnsChange?: (columns: string[]) => void;
  onUseNerForFileChange?: (enabled: boolean) => void;
  onCancelFile?: () => void;
  onApplyFile?: () => void;
  onEditFile?: (fileId: string) => void;
  onRemoveFile?: (fileId: string) => void;
  onStop?: () => void;
  onSend: (text: string) => void;
  onUploadFiles: (files: File[]) => void;
  pendingQueueCount?: number;
  models?: ModelOption[];
  selectedModelId?: string;
  onModelChange?: (modelId: string) => void;
  onNewChat?: () => void;
  onOpenSettings?: () => void;
}

function ShieldIcon({ className = "h-4 w-4" }: { className?: string }) { return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" className={className} aria-hidden><path d="M12 3.5 19 6v5.1c0 4.6-2.9 7.8-7 9.4-4.1-1.6-7-4.8-7-9.4V6l7-2.5Z" /><path d="m9.3 12 1.8 1.8 3.7-4" /></svg>; }
function SlidersIcon() { return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" className="h-4 w-4" aria-hidden><path d="M4 7h16M4 17h16" /><circle cx="9" cy="7" r="2" fill="white" /><circle cx="15" cy="17" r="2" fill="white" /></svg>; }
function MenuIcon() { return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" className="h-4 w-4" aria-hidden><path d="M5 7h14M5 12h14M5 17h14" /></svg>; }
function SparkIcon() { return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" className="h-5 w-5" aria-hidden><path d="m12 3 1.5 5.5L19 10l-5.5 1.5L12 17l-1.5-5.5L5 10l5.5-1.5L12 3Z" /><path d="m19 16 .7 2.3L22 19l-2.3.7L19 22l-.7-2.3L16 19l2.3-.7L19 16Z" /></svg>; }
function FileIcon() { return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" className="h-4 w-4" aria-hidden><path d="M6 4.5h8l4 4V20H6V4.5Z" /><path d="M14 4.5V9h4M9 13h6M9 16h4" /></svg>; }

export function ChatPane({ messages, suggestions, attachments = [], isStreaming, isUploading, pendingFile, pendingColumns = [], pendingRowCount = 0, selectedColumns = [], useNerForFile = false, isEditingFile = false, onSelectedColumnsChange, onUseNerForFileChange, onCancelFile, onApplyFile, onEditFile, onRemoveFile, onStop, onSend, onUploadFiles, pendingQueueCount = 0, models = [], selectedModelId = "", onModelChange, onNewChat, onOpenSettings }: ChatPaneProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const isEmpty = messages.length === 0;
  useEffect(() => { scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" }); }, [messages]);

  return <main className="relative flex min-w-0 flex-1 flex-col overflow-hidden bg-white text-[#203653]">
    <header className="z-20 flex min-h-[56px] shrink-0 items-center justify-between border-b border-[#e3eaf3] bg-white px-4 sm:px-6">
      <button type="button" onClick={() => onNewChat?.()} className="group flex items-center gap-2 rounded-lg px-2 py-1.5 text-[13px] font-semibold text-[#36506e] hover:bg-[#f1f6fc]" aria-label="New chat">
        <MenuIcon /><span>New chat</span>
      </button>
      <div className="flex items-center gap-2">
        {models.length > 0 && onModelChange && <div className="rounded-full border border-[#dce5ef] bg-white shadow-sm"><ModelSelector models={models} selectedModelId={selectedModelId} onChange={onModelChange} disabled={isStreaming || isUploading || Boolean(pendingFile)} /></div>}
        <button type="button" onClick={() => onOpenSettings?.()} disabled={!onOpenSettings} aria-label="Workspace settings" title="Workspace settings" className="privy-focus-ring hidden h-9 w-9 items-center justify-center rounded-lg text-[#7890a8] hover:bg-[#f1f6fc] hover:text-[#294663] disabled:cursor-default disabled:opacity-40 sm:flex"><SlidersIcon /></button>
      </div>
    </header>

    <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto">
      {isEmpty ? <div className="mx-auto flex min-h-full w-full max-w-3xl flex-col items-center justify-center px-5 py-12">
        <div className="flex h-16 w-16 items-center justify-center rounded-full bg-[#eaf3ff] text-[#1769c2]"><ShieldIcon className="h-8 w-8" /></div>
        <h3 className="mt-5 text-center text-[28px] font-semibold tracking-[-0.035em] text-[#193250]">Welcome to Privy</h3>
        <p className="mt-2 max-w-md text-center text-[14px] leading-6 text-[#8295aa]">Ask questions about your files, analyze your data, and explore insights — while your sensitive values remain protected.</p>
        {suggestions.length > 0 && <div className="mt-7 w-full max-w-2xl"><SuggestionChips suggestions={suggestions} onPick={onSend} /></div>}
        <div className="mt-7 flex items-center gap-1.5 text-[10px] text-[#7890a8]"><ShieldIcon className="h-3 w-3 text-[#2f72bb]" /><span>Your data is protected. Sensitive values are masked before AI processing.</span></div>
      </div> : <div className="mx-auto w-full max-w-4xl px-5 pb-8 pt-6 sm:px-8">
        {attachments.length > 0 && <div className="mb-6 flex items-center gap-2.5 border-b border-[#eef2f7] pb-3"><FileIcon className="text-[#55708d]" /><div><p className="text-[11px] font-semibold text-[#294663]">{attachments[0]?.filename}{attachments.length > 1 ? ` + ${attachments.length - 1} more` : ""}</p><p className="text-[9px] text-[#8a9bad]">Protected file · {attachments.length} file{attachments.length === 1 ? "" : "s"}</p></div></div>}
        <div className="flex flex-col gap-9">{messages.map((message, index) => <MessageBubble key={index} message={message} isStreaming={isStreaming && index === messages.length - 1 && message.role === "assistant"} />)}</div>
      </div>}
    </div>

    <div className="z-20 shrink-0 bg-white px-4 pb-3 pt-2 sm:px-6 sm:pb-4"><div className="mx-auto w-full max-w-3xl"><ChatInput onSend={onSend} onUploadFiles={onUploadFiles} onEditFile={onEditFile} onRemoveFile={onRemoveFile} onStop={onStop} attachments={attachments} disabled={false} isStreaming={isStreaming} isUploading={isUploading} hasPendingFile={Boolean(pendingFile)} pendingQueueCount={pendingQueueCount} /><div className="mt-2 text-center text-[9px] text-[#8c9caf]">Protected workspace · sensitive values stay masked</div></div></div>

    {pendingFile && onSelectedColumnsChange && onUseNerForFileChange && onCancelFile && onApplyFile && <MaskingColumnsPanel filename={pendingFile.name} rowCount={pendingRowCount} columns={pendingColumns} selectedColumns={selectedColumns} useNer={useNerForFile} isApplying={isUploading} mode={isEditingFile ? "edit" : "new"} onChange={onSelectedColumnsChange} onUseNerChange={onUseNerForFileChange} onCancel={onCancelFile} onApply={onApplyFile} />}
  </main>;
}
