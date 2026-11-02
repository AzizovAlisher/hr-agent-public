import { Fragment, type ReactNode } from "react";

/**
 * Tiny Markdown-ish renderer for chat assistant content.
 * Supports: paragraphs, bullet lists (- / *), numbered lists (1. ), inline `code`,
 * **bold**, *italic*, fenced ```code blocks```, and linebreaks within paragraphs.
 *
 * Trusted-input only (assistant text). Does not handle HTML, tables, or links.
 */
export function RichText({ text }: { text: string }) {
  const blocks = parseBlocks(text);
  return (
    <>
      {blocks.map((b, i) => (
        <Block key={i} block={b} />
      ))}
    </>
  );
}

/** @ui-only Parsed rich-text render block. */
type Block =
  | { kind: "para"; lines: string[] }
  | { kind: "ul"; items: string[] }
  | { kind: "ol"; items: string[] }
  | { kind: "code"; lang: string; text: string };

function parseBlocks(input: string): Block[] {
  const lines = input.replace(/\r\n/g, "\n").split("\n");
  const blocks: Block[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    // Fenced code
    if (line.startsWith("```")) {
      const lang = line.slice(3).trim();
      const buf: string[] = [];
      i += 1;
      while (i < lines.length && !lines[i].startsWith("```")) {
        buf.push(lines[i]);
        i += 1;
      }
      i += 1; // skip closing fence
      blocks.push({ kind: "code", lang, text: buf.join("\n") });
      continue;
    }

    // Blank line → paragraph break
    if (line.trim() === "") {
      i += 1;
      continue;
    }

    // Bullet list
    if (/^\s*[-*]\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*[-*]\s+/, ""));
        i += 1;
      }
      blocks.push({ kind: "ul", items });
      continue;
    }

    // Numbered list
    if (/^\s*\d+\.\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*\d+\.\s+/, ""));
        i += 1;
      }
      blocks.push({ kind: "ol", items });
      continue;
    }

    // Paragraph (collect contiguous non-empty, non-list lines)
    const paraLines: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() !== "" &&
      !/^\s*[-*]\s+/.test(lines[i]) &&
      !/^\s*\d+\.\s+/.test(lines[i]) &&
      !lines[i].startsWith("```")
    ) {
      paraLines.push(lines[i]);
      i += 1;
    }
    if (paraLines.length) blocks.push({ kind: "para", lines: paraLines });
  }

  return blocks;
}

function Block({ block }: { block: Block }) {
  if (block.kind === "code") {
    return (
      <pre className="my-2 overflow-x-auto rounded-[6px] border border-border bg-bg px-3 py-2.5 font-mono text-[12px] leading-[1.5] text-text-muted">
        <code>{block.text}</code>
      </pre>
    );
  }

  if (block.kind === "ul") {
    return (
      <ul className="my-1.5 ml-4 flex list-disc flex-col gap-1 marker:text-text-faint">
        {block.items.map((it, i) => (
          <li key={i} className="text-[13px] leading-[1.6] text-text">
            {renderInline(it)}
          </li>
        ))}
      </ul>
    );
  }

  if (block.kind === "ol") {
    return (
      <ol className="my-1.5 ml-5 flex list-decimal flex-col gap-1 marker:font-mono marker:text-text-faint">
        {block.items.map((it, i) => (
          <li key={i} className="text-[13px] leading-[1.6] text-text">
            {renderInline(it)}
          </li>
        ))}
      </ol>
    );
  }

  // Paragraph
  return (
    <p className="my-1.5 text-[13px] leading-[1.6] text-text first:mt-0 last:mb-0">
      {block.lines.map((line, i) => (
        <Fragment key={i}>
          {i > 0 ? <br /> : null}
          {renderInline(line)}
        </Fragment>
      ))}
    </p>
  );
}

/**
 * Inline transformations: `code`, **bold**, *italic*. Greedy, single-pass tokenizer
 * to avoid the regex-split traps when patterns nest.
 */
function renderInline(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  let buf = "";
  let i = 0;
  const flush = () => {
    if (buf) {
      out.push(buf);
      buf = "";
    }
  };

  while (i < text.length) {
    const ch = text[i];

    if (ch === "`") {
      const end = text.indexOf("`", i + 1);
      if (end !== -1) {
        flush();
        out.push(
          <code
            key={out.length}
            className="rounded-[4px] bg-bg-active px-1 py-px font-mono text-[12px]"
          >
            {text.slice(i + 1, end)}
          </code>,
        );
        i = end + 1;
        continue;
      }
    }

    if (ch === "*" && text[i + 1] === "*") {
      const end = text.indexOf("**", i + 2);
      if (end !== -1) {
        flush();
        out.push(
          <strong key={out.length} className="font-semibold text-text">
            {renderInline(text.slice(i + 2, end))}
          </strong>,
        );
        i = end + 2;
        continue;
      }
    }

    if (ch === "*") {
      const end = text.indexOf("*", i + 1);
      if (end !== -1 && end !== i + 1) {
        flush();
        out.push(
          <em key={out.length} className="italic">
            {text.slice(i + 1, end)}
          </em>,
        );
        i = end + 1;
        continue;
      }
    }

    buf += ch;
    i += 1;
  }
  flush();
  return out;
}
