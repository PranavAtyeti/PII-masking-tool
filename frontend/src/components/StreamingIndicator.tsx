export function StreamingIndicator() {
  return (
    <span className="inline-flex items-center gap-1 rounded-full bg-bg px-2.5 py-1 text-[11px] text-ink/50">
      <span>Privy is thinking</span>
      <span className="stream-dot stream-dot-1" aria-hidden />
      <span className="stream-dot stream-dot-2" aria-hidden />
      <span className="stream-dot stream-dot-3" aria-hidden />
    </span>
  );
}
