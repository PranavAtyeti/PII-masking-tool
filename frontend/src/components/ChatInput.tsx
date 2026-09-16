import {
  useLayoutEffect,
  useRef,
  useState,
  type ChangeEvent,
  type KeyboardEvent,
} from "react";
import { MAX_FILES_PER_SELECTION } from "../uploadLimits";

export interface ChatAttachment {
  fileId: string;
  filename: string;
  maskedCount: number;
  canEdit: boolean;
}

interface ChatInputProps {
  onSend: (text: string) => void;
  onUploadFiles: (files: File[]) => void;
  onEditFile?: (fileId: string) => void;
  onRemoveFile?: (fileId: string) => void;
  attachments?: ChatAttachment[];
  disabled?: boolean;
  isStreaming?: boolean;
  isUploading?: boolean;
  hasPendingFile?: boolean;
  pendingQueueCount?: number;
  placeholder?: string;
  onStop?: () => void;
}

export function ChatInput({
  onSend,
  onUploadFiles,
  onEditFile,
  onRemoveFile,
  attachments = [],
  disabled = false,
  isStreaming = false,
  isUploading = false,
  hasPendingFile = false,
  pendingQueueCount = 0,
  placeholder = "Message Privy",
  onStop,
}: ChatInputProps) {
  const [value, setValue] = useState("");
  const [attachmentsOpen, setAttachmentsOpen] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const composerDisabled = disabled || isUploading || hasPendingFile;
  const totalMasked = attachments.reduce((sum, item) => sum + item.maskedCount, 0);

  useLayoutEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;

    textarea.style.height = "auto";

    const maxHeight = Math.max(180, Math.floor(window.innerHeight * 0.4));
    const nextHeight = Math.min(textarea.scrollHeight, maxHeight);

    textarea.style.height = `${Math.max(44, nextHeight)}px`;
    textarea.style.overflowY = textarea.scrollHeight > maxHeight ? "auto" : "hidden";
  }, [value]);

  const submit = () => {
    const trimmed = value.trim();
    if (!trimmed || composerDisabled) return;
    onSend(trimmed);
    setValue("");
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  const handleFileChange = (e: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files ?? []);
    if (files.length > 0) onUploadFiles(files);
    e.target.value = "";
  };

  return (
    <div className="rounded-2xl border border-border bg-surface p-2 shadow-sm transition-shadow focus-within:shadow-md">
      {hasPendingFile && pendingQueueCount > 0 && (
        <div className="mb-2 px-2 text-[11px] text-ink/45">
          {pendingQueueCount} more file{pendingQueueCount === 1 ? "" : "s"} waiting to be prepared
        </div>
      )}

      {attachmentsOpen && attachments.length > 0 && (
        <div
          id="privy-attachment-details"
          className="mb-2 space-y-2 rounded-xl border border-border bg-bg p-2"
        >
          {attachments.map((attachment) => {
            const ext = attachment.filename.split(".").pop()?.toLowerCase();
            const fileIcon = ext === "xlsx" || ext === "xls" ? "▦" : "≡";

            return (
              <div
                key={attachment.fileId}
                className="flex items-center gap-3 rounded-lg border border-border bg-surface px-3 py-2.5"
              >
                <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-bg text-base text-ink/70">
                  {fileIcon}
                </div>

                <div className="min-w-0 flex-1">
                  <p
                    className="truncate text-sm font-medium text-ink"
                    title={attachment.filename}
                  >
                    {attachment.filename}
                  </p>
                  <p className="mt-0.5 text-xs text-ink/50">
                    {attachment.maskedCount.toLocaleString()} values masked
                  </p>
                </div>

                <div className="flex shrink-0 items-center gap-1">
                  {onEditFile && (
                    <button
                      type="button"
                      onClick={() => onEditFile(attachment.fileId)}
                      disabled={composerDisabled}
                      className="rounded-lg px-2.5 py-1.5 text-xs font-medium text-ink/60 hover:bg-bg hover:text-ink disabled:cursor-not-allowed disabled:opacity-30"
                    >
                      {attachment.canEdit ? "Edit masking" : "Re-attach to edit"}
                    </button>
                  )}

                  {onRemoveFile && (
                    <button
                      type="button"
                      onClick={() => onRemoveFile(attachment.fileId)}
                      disabled={composerDisabled}
                      aria-label={`Remove ${attachment.filename}`}
                      title="Remove file"
                      className="rounded-lg px-2 py-1.5 text-lg leading-none text-ink/35 hover:bg-bg hover:text-ink disabled:cursor-not-allowed disabled:opacity-30"
                    >
                      ×
                    </button>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}

      <div className="flex items-end gap-1">
        <input
          ref={fileInputRef}
          type="file"
          multiple
          accept=".csv,.xlsx,.xls"
          className="hidden"
          onChange={handleFileChange}
        />

        <button
          type="button"
          onClick={() => fileInputRef.current?.click()}
          disabled={composerDisabled}
          aria-label="Attach files"
          title={`Attach up to ${MAX_FILES_PER_SELECTION} files at a time`}
          className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full text-xl text-ink/50 transition-colors hover:bg-bg hover:text-ink disabled:cursor-not-allowed disabled:opacity-30"
        >
          {isUploading ? "…" : "+"}
        </button>

        {attachments.length > 0 && (
          <button
            type="button"
            onClick={() => setAttachmentsOpen((open) => !open)}
            disabled={composerDisabled}
            aria-expanded={attachmentsOpen}
            aria-controls="privy-attachment-details"
            aria-label={attachmentsOpen ? "Collapse attachments" : "Show attachments"}
            title={`${attachments.length} file${attachments.length === 1 ? "" : "s"} attached · ${totalMasked.toLocaleString()} values masked`}
            className="flex h-9 shrink-0 items-center gap-1 rounded-full border border-border bg-bg px-2.5 text-xs font-medium text-ink/60 transition-colors hover:bg-white hover:text-ink disabled:cursor-not-allowed disabled:opacity-30"
          >
            <span aria-hidden>📎</span>
            <span>{attachments.length}</span>
            <span className="text-[10px] text-ink/40" aria-hidden>
              {attachmentsOpen ? "⌃" : "⌄"}
            </span>
          </button>
        )}

        <textarea
          ref={textareaRef}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={isUploading ? "Preparing your file…" : placeholder}
          rows={1}
          disabled={composerDisabled}
          className="min-h-[44px] max-h-[40vh] flex-1 resize-none overflow-y-hidden bg-transparent px-1 py-1.5 text-sm leading-6 outline-none placeholder:text-ink/40"
        />

        {isStreaming ? (
          <button
            type="button"
            onClick={onStop}
            aria-label="Stop generating"
            title="Stop generating"
            className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-ink text-xs text-white transition-opacity hover:opacity-90"
          >
            ■
          </button>
        ) : (
          <button
            type="button"
            onClick={submit}
            disabled={composerDisabled || !value.trim()}
            aria-label="Send message"
            className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-ink text-white transition-opacity disabled:cursor-not-allowed disabled:opacity-20"
          >
            ↑
          </button>
        )}
      </div>
    </div>
  );
}
