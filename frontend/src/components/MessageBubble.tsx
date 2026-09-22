import type { Message } from "../types";
import { MarkdownContent } from "./MarkdownContent";
import { StreamingIndicator } from "./StreamingIndicator";

interface MessageBubbleProps { message: Message; isStreaming?: boolean; }
function ShieldIcon({ className = "h-4 w-4" }: { className?: string }) { return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" className={className} aria-hidden><path d="M12 3.5 19 6v5.1c0 4.6-2.9 7.8-7 9.4-4.1-1.6-7-4.8-7-9.4V6l7-2.5Z" /><path d="m9.3 12 1.8 1.8 3.7-4" /></svg>; }
function UserIcon() { return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" className="h-3 w-3" aria-hidden><circle cx="12" cy="8" r="3.5" /><path d="M5.5 20c.8-3.5 3-5.2 6.5-5.2s5.7 1.7 6.5 5.2" /></svg>; }

export function MessageBubble({ message, isStreaming = false }: MessageBubbleProps) {
  const isUser = message.role === "user";
  if (isUser) return <div className="flex justify-end"><div className="max-w-[88%] sm:max-w-[75%]"><div className="mb-1 flex items-center justify-end gap-1.5 px-1 text-[8px] font-semibold uppercase tracking-[0.12em] text-[#8b9bad]"><span>You</span><UserIcon /></div><div className="rounded-[16px] rounded-br-md border border-[#d9e8fb] bg-[#eaf3ff] px-4 py-3 text-[13px] leading-5 text-[#29415d]"><MarkdownContent content={message.content} /></div></div></div>;
  return <article className="w-full"><div className="flex items-center gap-2"><div className="flex h-7 w-7 items-center justify-center rounded-[9px] bg-white text-[#1769c2] shadow-[0_2px_8px_rgba(37,99,180,0.08)]" aria-hidden><ShieldIcon className="h-4 w-4" /></div><div><p className="text-[10px] font-semibold text-[#294663]">Privy</p><p className="text-[8px] uppercase tracking-[0.12em] text-[#8b9bad]">Protected response</p></div></div>{isStreaming && !message.content ? <div className="mt-3 pl-9"><StreamingIndicator /></div> : <div className="mt-2 pl-0 text-[13px] leading-6 text-[#3f4f61] sm:pl-9"><MarkdownContent content={message.content} />{isStreaming && <span className="stream-cursor" aria-hidden />}</div>}{!isStreaming && message.masked_count > 0 && <div className="mt-2.5 pl-0 sm:pl-9"><span className="inline-flex items-center gap-1.5 rounded-full border border-[#cfe2f7] bg-[#f1f7fe] px-2.5 py-1 text-[10px] font-medium text-[#2f6fae]"><ShieldIcon className="h-3 w-3" />{message.masked_count.toLocaleString()} sensitive values protected</span></div>}</article>;
}
