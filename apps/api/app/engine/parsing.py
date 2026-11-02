"""PDF text extraction + LLM-response JSON parsing + validation.

History:
- Originally pypdf (renamed PyPDF2 fork from v1)
- 2026-05-06: switched to pdfplumber as primary; pypdf kept as fallback.
  pypdf reads glyphs sequentially and inserts spaces by horizontal gap, which
  on Canva/Figma-exported PDFs (per-glyph positioning) breaks words mid-stream
  ("Г рафический", "Сту денческий"). pdfplumber clusters glyphs by coordinate
  proximity and produces clean Russian/Tajik text out of the box.
"""

import json
import re
from pathlib import Path
from typing import IO

import pdfplumber
import pypdf

CRITERIA = ["technical_skills", "quality_of_projects", "previous_experience", "impact"]
WEIGHTS = {
    "technical_skills": 0.4,
    "quality_of_projects": 0.3,
    "previous_experience": 0.2,
    "impact": 0.1,
}


def extract_text_from_pdf(path: str | Path) -> str:
    """Extract clean text from a CV PDF.

    Two-stage strategy:
      1. pdfplumber — handles Canva/Figma-exported PDFs where pypdf would
         otherwise emit "Г рафический дизайнер" instead of "Графический".
      2. pypdf fallback — used if pdfplumber returns empty (rare; encrypted
         or unusually structured PDFs) or throws.

    Both outputs go through `_fix_character_spaced_lines` as a defensive
    second pass. The post-processor only fires on lines that look heavily
    letter-spaced (>=50% single-character "words"), so feeding clean text
    through it is a no-op.
    """
    text_parts: list[str] = []
    try:
        with pdfplumber.open(path) as pdf:
            for page_num, page in enumerate(pdf.pages, start=1):
                try:
                    page_text = page.extract_text() or ""
                except Exception:
                    page_text = ""
                if page_text:
                    page_text = _fix_character_spaced_lines(page_text)
                    text_parts.append(
                        f"\n--- Страница {page_num} ---\n{page_text}"
                    )
    except Exception:
        text_parts = []  # fall through to pypdf

    out = "".join(text_parts).strip()
    if out:
        return out

    # Fallback: pypdf. Better-than-nothing on PDFs pdfplumber rejects.
    fallback_parts: list[str] = []
    with open(path, "rb") as f:
        reader = pypdf.PdfReader(f)
        for page_num, page in enumerate(reader.pages, start=1):
            try:
                page_text = page.extract_text() or ""
            except Exception:
                continue
            if page_text:
                page_text = _fix_character_spaced_lines(page_text)
                fallback_parts.append(
                    f"\n--- Страница {page_num} ---\n{page_text}"
                )
    return "".join(fallback_parts).strip()


def extract_text_from_docx(source: str | Path | IO[bytes]) -> str:
    """Extract text from a CV .docx, including table cells.

    Paragraph-only extraction (``p.text for p in doc.paragraphs``) misses any
    CV built on a table layout — a common template shape (two-column skills
    grid, a dates|role|description table) — and silently produces empty text
    instead of an error. Table cells are walked in document order and
    deduplicated against paragraph text: python-docx lists the same runs
    under both ``doc.paragraphs`` and ``doc.tables`` when a document mixes
    free paragraphs with tables, so appending both naively duplicates lines.

    ``source`` is a path (str/Path) or a binary file-like object — both are
    accepted directly by python-docx's ``Document()``, so this works both for
    an already-saved upload and for in-memory bytes (``io.BytesIO``).
    """
    from docx import Document  # python-docx

    doc = Document(str(source) if isinstance(source, Path) else source)

    parts: list[str] = []
    seen: set[str] = set()

    def _add(line: str) -> None:
        line = line.strip()
        if line and line not in seen:
            seen.add(line)
            parts.append(line)

    for p in doc.paragraphs:
        _add(p.text)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    _add(p.text)

    return "\n".join(parts)


def _fix_character_spaced_lines(text: str) -> str:
    """Repair `M a r k e t p l a c e` -> `Marketplace`.

    Canva/Figma-exported PDFs render each glyph at its own coordinate so
    pypdf's coordinate-based reader emits one space between every letter
    and two between words. Detected per-line: if more than half of the
    "words" on a line are single characters, we collapse single inner
    spaces while preserving double-spaces as word boundaries.

    Lines that are normal prose are left untouched.
    """
    out_lines: list[str] = []
    for line in text.split("\n"):
        out_lines.append(_fix_line(line))
    return "\n".join(out_lines)


def _fix_line(line: str) -> str:
    stripped = line.strip()
    if not stripped:
        return line
    words = stripped.split()
    if len(words) < 4:
        return line
    single = sum(1 for w in words if len(w) == 1)
    if single / len(words) < 0.5:
        return line
    # Word boundaries are double-spaces. Mark them, drop intra-word spaces,
    # restore the boundaries as single spaces. Preserve any leading whitespace
    # so indentation isn't destroyed.
    leading_ws = line[: len(line) - len(line.lstrip())]
    body = re.sub(r" {2,}", "\x00", stripped)
    body = body.replace(" ", "")
    body = body.replace("\x00", " ")
    return leading_ws + body


def extract_json_from_response(text: str) -> dict | None:
    """Robust JSON extraction with fallbacks (matches v1's behavior)."""
    text = text.strip()

    # Strip ```json ... ``` fences if present
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)

    # Whole-text JSON
    if text.startswith("{") and text.endswith("}"):
        try:
            scores = json.loads(text)
            if all(field in scores for field in CRITERIA):
                return scores
        except json.JSONDecodeError:
            pass

    # Slice between first { and last }
    start_idx, end_idx = text.find("{"), text.rfind("}") + 1
    if start_idx != -1 and end_idx > start_idx:
        try:
            scores = json.loads(text[start_idx:end_idx])
            if all(field in scores for field in CRITERIA):
                return scores
        except json.JSONDecodeError:
            pass

    return None


def validate_scores(scores: dict) -> dict:
    """Normalize to {criterion: int_0_to_100, criterion_comment: str} for each of CRITERIA.

    Accepts both v1 formats: a bare number or {"score": n, "comment": "..."}.
    """
    out: dict = {}
    for field in CRITERIA:
        if field in scores:
            value = scores[field]
            try:
                if isinstance(value, dict) and "score" in value:
                    score = int(float(value["score"]))
                    comment = (value.get("comment") or "").strip()
                else:
                    score = int(float(value))
                    comment = ""
                out[field] = max(0, min(100, score))
                out[f"{field}_comment"] = comment if comment else "No comment provided"
            except (ValueError, TypeError):
                out[field] = 0
                out[f"{field}_comment"] = "Error parsing score"
        else:
            out[field] = 0
            out[f"{field}_comment"] = "Field missing in AI response"
    return out


def calculate_overall_score(scores: dict) -> int:
    if not scores:
        return 0
    total = sum(float(scores.get(c, 0)) * w for c, w in WEIGHTS.items())
    return round(total)
