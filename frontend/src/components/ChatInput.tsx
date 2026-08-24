import { useRef, useState, type ChangeEvent, type KeyboardEvent } from "react";

interface ChatAttachment {
  filename: string;
  keptPrivateCount: number;
  canEdit: boolean;
}

interface ChatInputProps {
  onSend: (text: string) => void;
  onUploadFile: (file: File) => void;
  onEditFile?: () => void;
  onRemoveFile?: () => void;
  attachment?: ChatAttachment;
  disabled?: boolean;
  isUploading?: boolean;
  hasPendingFile?: boolean;
  placeholder?: string;
}

export function ChatInput({
  onSend,
  onUploadFile,
  onEditFile,
  onRemoveFile,
  attachment,
  disabled = false,
  isUploading = false,
  hasPendingFile = false,
  placeholder = "Message Privy",
}: ChatInputProps) {
  const [value, setValue] = useState("");
  const fileInputRef = useRef<HTMLInputElement>(null);

  const composerDisabled = disabled || isUploading || hasPendingFile;

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
    const file = e.target.files?.[0];
    if (file) onUploadFile(file);
    e.target.value = "";
  };

  const ext = attachment?.filename.split(".").pop()?.toLowerCase();
  const fileIcon = ext === "xlsx" || ext === "xls" ? "▦" : "≡";

  return (
    <div className="rounded-2xl border border-border bg-surface p-2 shadow-sm transition-shadow focus-within:shadow-md">
      {attachment && (
        <div className="mb-2 px-1 pt-1">
          <div className="flex items-center gap-3 rounded-xl border border-border bg-bg px-3 py-2.5">
            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-white text-lg text-ink/70 shadow-sm">
              {fileIcon}
            </div>
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-medium text-ink" title={attachment.filename}>
                {attachment.filename}
              </p>
              <p className="mt-0.5 text-xs text-ink/50">
                {attachment.keptPrivateCount.toLocaleString()} values masked
              </p>
            </div>
            <div className="flex shrink-0 items-center gap-1">
              {onEditFile && (
                <button
                  type="button"
                  onClick={() => {
                    if (attachment.canEdit) {
                      onEditFile();
                    } else {
                      fileInputRef.current?.click();
                    }
                  }}
                  disabled={composerDisabled}
                  className="rounded-lg px-2.5 py-1.5 text-xs font-medium text-ink/60 hover:bg-white hover:text-ink disabled:cursor-not-allowed disabled:opacity-30"
                >
                  {attachment.canEdit ? "Edit masking" : "Re-attach to edit"}
                </button>
              )}
              {onRemoveFile && (
                <button
                  type="button"
                  onClick={onRemoveFile}
                  disabled={composerDisabled}
                  aria-label="Remove attached file"
                  title="Remove file"
                  className="rounded-lg px-2 py-1.5 text-lg leading-none text-ink/35 hover:bg-white hover:text-ink disabled:cursor-not-allowed disabled:opacity-30"
                >
                  ×
                </button>
              )}
            </div>
          </div>
        </div>
      )}

      <div className="flex items-end gap-1">
        <input
          ref={fileInputRef}
          type="file"
          accept=".csv,.xlsx,.xls"
          className="hidden"
          onChange={handleFileChange}
        />
        <button
          type="button"
          onClick={() => fileInputRef.current?.click()}
          disabled={composerDisabled}
          aria-label="Attach a file"
          title="Attach a file"
          className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full text-xl text-ink/50 transition-colors hover:bg-bg hover:text-ink disabled:cursor-not-allowed disabled:opacity-30"
        >
          {isUploading ? "…" : "+"}
        </button>
        <textarea
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={isUploading ? "Masking your file…" : placeholder}
          rows={1}
          disabled={composerDisabled}
          className="max-h-40 flex-1 resize-none bg-transparent px-1 py-1.5 text-sm outline-none placeholder:text-ink/40"
        />
        <button
          type="button"
          onClick={submit}
          disabled={composerDisabled || !value.trim()}
          aria-label="Send message"
          className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-ink text-white transition-opacity disabled:cursor-not-allowed disabled:opacity-20"
        >
          ↑
        </button>
      </div>
    </div>
  );
}
