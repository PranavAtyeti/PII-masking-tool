import { Fragment, type ReactNode } from "react";

interface MarkdownContentProps {
  content: string;
}

function InlineMarkdown({ text }: { text: string }) {
  const tokens = text.split(/(\*\*[^*]+\*\*|__[^_]+__|`[^`]+`|\*[^*]+\*|_[^_]+_|\[[^\]]+\]\([^\)]+\))/g);

  return (
    <>
      {tokens.map((token, index) => {
        if (!token) return null;

        if (/^\*\*[^*]+\*\*$/.test(token) || /^__[^_]+__$/.test(token)) {
          return <strong key={index} className="font-semibold text-ink">{token.slice(2, -2)}</strong>;
        }

        if (/^`[^`]+`$/.test(token)) {
          return (
            <code key={index} className="rounded-md border border-border bg-bg px-1.5 py-0.5 font-mono text-[0.88em] text-ink">
              {token.slice(1, -1)}
            </code>
          );
        }

        if (/^\*[^*]+\*$/.test(token) || /^_[^_]+_$/.test(token)) {
          return <em key={index}>{token.slice(1, -1)}</em>;
        }

        const link = token.match(/^\[([^\]]+)\]\(([^\)]+)\)$/);
        if (link) {
          const [, label, href] = link;
          const safeHref = /^(https?:\/\/|mailto:)/i.test(href) ? href : null;

          if (safeHref) {
            return (
              <a
                key={index}
                href={safeHref}
                target="_blank"
                rel="noreferrer"
                className="text-accent underline decoration-accent/25 underline-offset-2 transition-colors hover:decoration-accent"
              >
                {label}
              </a>
            );
          }
        }

        return <Fragment key={index}>{token}</Fragment>;
      })}
    </>
  );
}

function splitTableRow(line: string): string[] {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
}

function isTableSeparator(line: string): boolean {
  const cells = splitTableRow(line);
  return cells.length > 0 && cells.every((cell) => /^:?-{3,}:?$/.test(cell));
}

function TableBlock({ lines }: { lines: string[] }) {
  const header = splitTableRow(lines[0]);
  const rows = lines.slice(2).map(splitTableRow);

  return (
    <div className="my-4 overflow-x-auto rounded-xl border border-border bg-surface shadow-sm">
      <table className="min-w-full border-collapse text-sm">
        <thead className="bg-bg/70">
          <tr>
            {header.map((cell, index) => (
              <th key={index} className="border-b border-border px-3.5 py-2.5 text-left text-xs font-semibold text-ink">
                <InlineMarkdown text={cell} />
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, rowIndex) => (
            <tr key={rowIndex} className="transition-colors hover:bg-bg/50">
              {header.map((_, cellIndex) => (
                <td key={cellIndex} className="border-b border-border/60 px-3.5 py-2.5 align-top text-ink/75 last:border-b-0">
                  <InlineMarkdown text={row[cellIndex] ?? ""} />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function renderBlocks(content: string): ReactNode[] {
  const lines = content.replace(/\r\n/g, "\n").split("\n");
  const blocks: ReactNode[] = [];
  let index = 0;
  let paragraph: string[] = [];

  const flushParagraph = () => {
    if (paragraph.length === 0) return;

    blocks.push(
      <p key={`p-${blocks.length}`} className="whitespace-pre-wrap leading-7 text-ink/85">
        {paragraph.map((line, i) => (
          <Fragment key={i}>
            {i > 0 && <br />}
            <InlineMarkdown text={line} />
          </Fragment>
        ))}
      </p>
    );

    paragraph = [];
  };

  while (index < lines.length) {
    const line = lines[index];

    if (line.trim().startsWith("```")) {
      flushParagraph();
      const language = line.trim().slice(3).trim();
      const codeLines: string[] = [];
      index += 1;

      while (index < lines.length && !lines[index].trim().startsWith("```")) {
        codeLines.push(lines[index]);
        index += 1;
      }

      if (index < lines.length) index += 1;

      blocks.push(
        <div key={`code-${blocks.length}`} className="my-4 overflow-hidden rounded-xl border border-slate-800/80 bg-slate-950 text-slate-100 shadow-sm">
          {language && (
            <div className="border-b border-white/10 px-3.5 py-2 font-mono text-[10px] uppercase tracking-wider text-white/45">
              {language}
            </div>
          )}
          <pre className="overflow-x-auto p-4 font-mono text-xs leading-6">
            <code>{codeLines.join("\n")}</code>
          </pre>
        </div>
      );
      continue;
    }

    const tableCandidate = line.includes("|") && index + 1 < lines.length && isTableSeparator(lines[index + 1]);

    if (tableCandidate) {
      flushParagraph();
      const tableLines = [line, lines[index + 1]];
      index += 2;

      while (index < lines.length && lines[index].includes("|")) {
        tableLines.push(lines[index]);
        index += 1;
      }

      blocks.push(<TableBlock key={`table-${blocks.length}`} lines={tableLines} />);
      continue;
    }

    const heading = line.match(/^(#{1,3})\s+(.+)$/);

    if (heading) {
      flushParagraph();
      const level = heading[1].length;
      const classes =
        level === 1
          ? "mt-6 mb-2 text-xl font-semibold tracking-tight"
          : level === 2
            ? "mt-5 mb-2 text-lg font-semibold tracking-tight"
            : "mt-4 mb-1.5 text-base font-semibold";

      blocks.push(
        <div key={`h-${blocks.length}`} className={classes}>
          <InlineMarkdown text={heading[2]} />
        </div>
      );

      index += 1;
      continue;
    }

    const bullet = line.match(/^\s*[-*]\s+(.+)$/);

    if (bullet) {
      flushParagraph();
      const items: string[] = [];

      while (index < lines.length) {
        const match = lines[index].match(/^\s*[-*]\s+(.+)$/);
        if (!match) break;
        items.push(match[1]);
        index += 1;
      }

      blocks.push(
        <ul key={`ul-${blocks.length}`} className="my-3 list-disc space-y-1.5 pl-5 leading-7 text-ink/85">
          {items.map((item, i) => (
            <li key={i}><InlineMarkdown text={item} /></li>
          ))}
        </ul>
      );
      continue;
    }

    const ordered = line.match(/^\s*\d+[.)]\s+(.+)$/);

    if (ordered) {
      flushParagraph();
      const items: string[] = [];

      while (index < lines.length) {
        const match = lines[index].match(/^\s*\d+[.)]\s+(.+)$/);
        if (!match) break;
        items.push(match[1]);
        index += 1;
      }

      blocks.push(
        <ol key={`ol-${blocks.length}`} className="my-3 list-decimal space-y-1.5 pl-5 leading-7 text-ink/85">
          {items.map((item, i) => (
            <li key={i}><InlineMarkdown text={item} /></li>
          ))}
        </ol>
      );
      continue;
    }

    if (!line.trim()) {
      flushParagraph();
      index += 1;
      continue;
    }

    paragraph.push(line);
    index += 1;
  }

  flushParagraph();
  return blocks;
}

export function MarkdownContent({ content }: MarkdownContentProps) {
  if (!content) return null;
  return <div className="markdown-content">{renderBlocks(content)}</div>;
}
