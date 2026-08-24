import type { Message } from "../types";
import { MarkdownContent } from "./MarkdownContent";
import { StreamingIndicator } from "./StreamingIndicator";

interface MessageBubbleProps {
  message: Message;
  isStreaming?: boolean;
}

export function MessageBubble({ message, isStreaming = false }: MessageBubbleProps) {
  const isUser = message.role === "user";

  if (isUser) {
    return (
      <div className="flex justify-end">
        <div className="max-w-[78%] rounded-2xl rounded-br-md border border-accent-2/20 bg-accent-2/5 px-4 py-3 text-sm leading-6 text-ink">
          <MarkdownContent content={message.content} />
        </div>
      </div>
    );
  }

  return (
    <div className="flex justify-start">
      <div className="w-full max-w-[86%]">
        <div className="mb-2 flex items-center gap-2 text-xs font-medium text-ink/55">
          <span className="flex h-6 w-6 items-center justify-center rounded-full border border-border bg-surface text-[12px] shadow-sm" aria-hidden>
            🔒
          </span>
          <span>Privy</span>
        </div>

        {isStreaming && !message.content ? (
          <StreamingIndicator />
        ) : (
          <div className="text-sm leading-7 text-ink">
            <MarkdownContent content={message.content} />
            {isStreaming && <span className="stream-cursor" aria-hidden />}
          </div>
        )}

        {!isStreaming && message.masked_count > 0 && (
          <span className="mt-3 inline-flex items-center gap-1.5 rounded-full border border-accent/10 bg-accent/10 px-2.5 py-1 text-xs font-medium text-accent">
            🛡️ {message.masked_count.toLocaleString()} values kept private
          </span>
        )}
      </div>
    </div>
  );
}
