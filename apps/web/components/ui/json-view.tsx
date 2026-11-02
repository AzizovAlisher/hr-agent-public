import { Fragment } from "react";

/**
 * Lightweight JSON viewer with key/string/number coloring. Renders JSON.stringify
 * output character-by-character into spans — safe (no innerHTML) and stable.
 */
export function JsonView({ value }: { value: unknown }) {
  const text = JSON.stringify(value, null, 2);
  if (typeof text !== "string") return null;
  return (
    <pre className="overflow-x-auto py-3 font-mono text-[11.5px] leading-[1.6]">
      <code>{tokenize(text)}</code>
    </pre>
  );
}

function tokenize(text: string): React.ReactNode[] {
  const out: React.ReactNode[] = [];
  let i = 0;
  let key = 0;

  while (i < text.length) {
    const ch = text[i];

    // String literal
    if (ch === '"') {
      const start = i;
      i += 1;
      while (i < text.length) {
        if (text[i] === "\\") {
          i += 2;
          continue;
        }
        if (text[i] === '"') break;
        i += 1;
      }
      i += 1; // closing "
      const literal = text.slice(start, i);
      // Look ahead for `:` to decide key vs value-string
      let j = i;
      while (j < text.length && /\s/.test(text[j])) j += 1;
      const isKey = text[j] === ":";
      out.push(
        <span
          key={key++}
          className={isKey ? "text-text" : ""}
          style={!isKey ? { color: "oklch(0.78 0.14 75)" } : undefined}
        >
          {literal}
        </span>,
      );
      continue;
    }

    // Number
    if (/[-0-9]/.test(ch)) {
      const start = i;
      while (i < text.length && /[-0-9.eE+]/.test(text[i])) i += 1;
      out.push(
        <span key={key++} style={{ color: "var(--accent)" }}>
          {text.slice(start, i)}
        </span>,
      );
      continue;
    }

    // Keyword (true/false/null)
    if (/[a-z]/.test(ch)) {
      const start = i;
      while (i < text.length && /[a-z]/.test(text[i])) i += 1;
      out.push(
        <span key={key++} style={{ color: "var(--accent)" }}>
          {text.slice(start, i)}
        </span>,
      );
      continue;
    }

    // Punctuation / whitespace — pass through
    out.push(<Fragment key={key++}>{ch}</Fragment>);
    i += 1;
  }

  return out;
}
