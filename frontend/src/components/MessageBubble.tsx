import type { Message } from "../types";

interface MessageBubbleProps {
  message: Message;
  isStreaming?: boolean;
}

export function MessageBubble({ message, isStreaming }: MessageBubbleProps) {
  const isUser = message.role === "user";

  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"}`}>
      <div
        className={`max-w-[75%] rounded-lg border px-4 py-3 text-sm leading-relaxed ${
          isUser
            ? "border-accent-2/20 bg-accent-2/5"
            : "border-border bg-surface"
        }`}
      >
        <p className="whitespace-pre-wrap">
          {message.content}
          {isStreaming && <span className="stream-cursor" aria-hidden />}
        </p>
        {!isUser && message.masked_count > 0 && (
          <span className="mt-2 inline-flex items-center gap-1 rounded-full bg-accent/10 px-2.5 py-1 text-xs font-medium text-accent">
            🛡️ {message.masked_count} kept private
          </span>
        )}
      </div>
    </div>
  );
}
