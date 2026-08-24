interface SuggestionChipsProps {
  suggestions: string[];
  onPick: (text: string) => void;
}

export function SuggestionChips({ suggestions, onPick }: SuggestionChipsProps) {
  return (
    <div className="flex flex-wrap justify-center gap-3">
      {suggestions.map((text) => (
        <button
          key={text}
          onClick={() => onPick(text)}
          className="rounded-full border border-border bg-surface px-4 py-2 text-sm transition-colors hover:border-accent hover:text-accent"
        >
          {text}
        </button>
      ))}
    </div>
  );
}
