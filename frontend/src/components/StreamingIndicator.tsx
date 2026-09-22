export function StreamingIndicator() {
  return (
    <div
      className="inline-flex items-center gap-2 rounded-lg border border-border bg-surface px-3 py-2 text-xs text-ink/50 shadow-sm"
      aria-label="Privy is thinking"
    >
      <span>Privy is thinking</span>
      <span className="flex items-center gap-1" aria-hidden>
        <span className="stream-dot" />
        <span className="stream-dot stream-dot-2" />
        <span className="stream-dot stream-dot-3" />
      </span>
    </div>
  );
}
