from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers

from app.routers.candidates import MAX_CANDIDATE_CV_BYTES, upload_file

pytestmark = pytest.mark.asyncio


async def test_candidate_upload_rejects_file_over_15_mb_before_parsing():
    oversized = UploadFile(
        file=BytesIO(b"x" * (MAX_CANDIDATE_CV_BYTES + 1)),
        filename="oversized.pdf",
        headers=Headers({"content-type": "application/pdf"}),
    )

    with pytest.raises(HTTPException) as exc:
        await upload_file(
            file=oversized,
            source_ref=None,
            db=None,  # type: ignore[arg-type] -- rejected before database access
            user=SimpleNamespace(organization_id=uuid4()),  # type: ignore[arg-type]
        )

    assert exc.value.status_code == 413
    assert exc.value.detail == "CV file exceeds the 15 MiB upload limit."
