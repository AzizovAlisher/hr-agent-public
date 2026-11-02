"""Regression tests for DOCX extraction and the scoring trust boundary.

Real incident: a candidate's `pdf_path` column held a `.docx` file (the column
name is a historical misnomer — see app/services/uploads.py), `raw_cv` was
empty because the old paragraph-only extractor missed table-based CV layouts,
and scoring once parsed that untrusted path in-process. Production re-scoring
now resolves the tenant object and uses the bounded parser subprocess; the
legacy synchronous helper may only return text already stored in the database.
"""

import io

from docx import Document

from app.engine.parsing import extract_text_from_docx
from app.models.candidate import Candidate
from app.services.scoring import _candidate_text


def _docx_with_table_only(text: str) -> io.BytesIO:
    """A .docx whose only content lives in a table cell — no free paragraphs.

    This is the shape that broke paragraph-only extraction: a CV built as a
    two-column skills grid or a dates|role|description table.
    """
    doc = Document()
    table = doc.add_table(rows=1, cols=1)
    table.rows[0].cells[0].text = text
    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf


def _docx_with_paragraph(text: str) -> io.BytesIO:
    doc = Document()
    doc.add_paragraph(text)
    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf


def test_extract_text_from_docx_reads_table_cells():
    src = _docx_with_table_only("Python, Django, PostgreSQL — 5 years")
    text = extract_text_from_docx(src)
    assert "Python, Django, PostgreSQL — 5 years" in text


def test_extract_text_from_docx_reads_paragraphs_too():
    src = _docx_with_paragraph("Senior Backend Engineer")
    text = extract_text_from_docx(src)
    assert "Senior Backend Engineer" in text


def test_extract_text_from_docx_deduplicates_paragraph_and_table_overlap():
    doc = Document()
    doc.add_paragraph("Иван Иванов")
    table = doc.add_table(rows=1, cols=1)
    table.rows[0].cells[0].text = "Python developer"
    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)

    text = extract_text_from_docx(buf)
    assert text.count("Иван Иванов") == 1
    assert "Python developer" in text


def test_extract_text_from_docx_accepts_a_saved_path(tmp_path):
    path = tmp_path / "cv.docx"
    doc = Document()
    doc.add_paragraph("Reads from a real file path too")
    doc.save(str(path))

    text = extract_text_from_docx(path)
    assert "Reads from a real file path too" in text


def test_candidate_text_does_not_parse_an_untrusted_local_path(tmp_path):

    path = tmp_path / "cand.docx"
    doc = Document()
    doc.add_paragraph("Опыт работы: 5 лет Python")
    doc.save(str(path))

    candidate = Candidate(
        source="manual",
        raw_cv=None,
        pdf_path=str(path),
    )

    assert _candidate_text(candidate) == ""


def test_candidate_text_prefers_stored_raw_cv_over_reextraction():
    candidate = Candidate(
        source="manual",
        raw_cv="already extracted text",
        pdf_path="storage/cvs/does-not-need-to-exist.docx",
    )
    assert _candidate_text(candidate) == "already extracted text"


def test_candidate_text_empty_raw_cv_never_bypasses_bounded_parser(tmp_path):

    path = tmp_path / "cand.docx"
    doc = Document()
    doc.add_paragraph("Recovered on re-score")
    doc.save(str(path))

    candidate = Candidate(source="manual", raw_cv="", pdf_path=str(path))
    assert _candidate_text(candidate) == ""
