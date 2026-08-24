import { useEffect, useRef } from "react";
import type { ColumnInfo, Message } from "../types";
import { MessageBubble } from "./MessageBubble";
import { SuggestionChips } from "./SuggestionChips";
import { ChatInput } from "./ChatInput";
import { MaskingColumnsPanel } from "./MaskingColumnsPanel";

interface ChatAttachment {
  filename: string;
  keptPrivateCount: number;
  canEdit: boolean;
}

interface ChatPaneProps {
  messages: Message[];
  suggestions: string[];
  attachment?: ChatAttachment;
  isStreaming: boolean;
  isUploading: boolean;
  pendingFile?: File | null;
  pendingColumns?: ColumnInfo[];
  pendingRowCount?: number;
  selectedColumns?: string[];
  isEditingFile?: boolean;
  onSelectedColumnsChange?: (columns: string[]) => void;
  onCancelFile?: () => void;
  onApplyFile?: () => void;
  onEditFile?: () => void;
  onRemoveFile?: () => void;
  onStop?: () => void;
  onSend: (text: string) => void;
  onUploadFile: (file: File) => void;
}

export function ChatPane({
  messages,
  suggestions,
  attachment,
  isStreaming,
  isUploading,
  pendingFile,
  pendingColumns = [],
  pendingRowCount = 0,
  selectedColumns = [],
  isEditingFile = false,
  onSelectedColumnsChange,
  onCancelFile,
  onApplyFile,
  onEditFile,
  onRemoveFile,
  onStop,
  onSend,
  onUploadFile,
}: ChatPaneProps) {
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages]);

  const isEmpty = messages.length === 0;

  return (
    <main className="flex min-w-0 flex-1 flex-col bg-bg">
      <div className="flex h-14 items-center border-b border-border bg-surface/80 px-6 backdrop-blur">
        <div className="min-w-0">
          <h2 className="flex items-center gap-2 font-display text-sm font-semibold text-ink">
            <span aria-hidden>🔒</span>
            <span>Privy</span>
          </h2>
          <p className="ml-6 text-[11px] text-ink/40">Private AI workspace</p>
        </div>
      </div>

      <div ref={scrollRef} className="flex-1 overflow-y-auto px-6 py-7">
        {isEmpty ? (
          <div className="mx-auto flex h-full max-w-3xl flex-col items-center justify-center gap-7 text-center">
            <div>
              <div className="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-2xl border border-border bg-surface text-2xl shadow-sm">
                🔒
              </div>
              <h3 className="font-display text-xl font-semibold text-ink">Your private AI workspace</h3>
              <p className="mx-auto mt-2 max-w-md text-sm leading-6 text-ink/50">
                Ask questions about your data without exposing the original PII.
              </p>
            </div>
            <SuggestionChips suggestions={suggestions} onPick={onSend} />
          </div>
        ) : (
          <div className="mx-auto flex max-w-4xl flex-col gap-7">
            {messages.map((m, i) => (
              <MessageBubble
                key={i}
                message={m}
                isStreaming={isStreaming && i === messages.length - 1 && m.role === "assistant"}
              />
            ))}
          </div>
        )}
      </div>

      <div className="mx-auto w-full max-w-4xl px-6 pb-4 pt-2">
        <ChatInput
          onSend={onSend}
          onUploadFile={onUploadFile}
          onEditFile={onEditFile}
          onRemoveFile={onRemoveFile}
          onStop={onStop}
          attachment={attachment}
          disabled={isStreaming}
          isStreaming={isStreaming}
          isUploading={isUploading}
          hasPendingFile={Boolean(pendingFile)}
        />
        <p className="mt-2 text-center text-[11px] text-ink/35">
          Originals stay on this device. Only masked values are sent to the AI.
        </p>
      </div>

      {pendingFile && onSelectedColumnsChange && onCancelFile && onApplyFile && (
        <MaskingColumnsPanel
          filename={pendingFile.name}
          rowCount={pendingRowCount}
          columns={pendingColumns}
          selectedColumns={selectedColumns}
          isApplying={isUploading}
          mode={isEditingFile ? "edit" : "new"}
          onChange={onSelectedColumnsChange}
          onCancel={onCancelFile}
          onApply={onApplyFile}
        />
      )}
    </main>
  );
}
