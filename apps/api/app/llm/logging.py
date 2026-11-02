import hashlib

from app.llm.base import CompletionResult, LogContext
from app.models.message import Message


def _content_fingerprint(label: str, content: str) -> str:
    """Return useful audit metadata without retaining prompt/response PII."""

    digest = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
    return f"[{label} omitted; chars={len(content)}; sha256={digest}]"


async def log_completion(ctx: LogContext, result: CompletionResult, prompt_text: str) -> None:
    """Record a single LLM completion in the messages table.

    Two provenance rows are written, but raw prompts and responses are not copied
    into this general-purpose log. Product-owned outputs live on their domain
    records; retaining CV-bearing prompts here made candidate erasure impossible.
    Token counts remain attached to the assistant provenance row.
    """
    ctx.db.add(
        Message(
            organization_id=ctx.organization_id,
            session_id=ctx.session_id,
            role=ctx.request_role,
            content=_content_fingerprint("prompt", prompt_text),
            model_used=result.model_used,
            purpose=ctx.purpose,
        )
    )
    ctx.db.add(
        Message(
            organization_id=ctx.organization_id,
            session_id=ctx.session_id,
            role="assistant",
            content=_content_fingerprint("response", result.content),
            model_used=result.model_used,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            purpose=ctx.purpose,
        )
    )
    await ctx.db.flush()
