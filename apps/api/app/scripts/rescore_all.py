"""Re-score every existing (candidate, position) pair under the current prompt.

Use after upgrading `app/engine/prompts.py` so that scores in the DB stay
consistent with the active rubric — otherwise old scored rows are mixed in
with new ones and the dashboard's ranking lies.

Idempotent: re-running just overwrites again. Sequential (one call at a
time) to keep OpenRouter spend predictable; flip `--concurrency N` to run
in parallel.

Usage:
    uv run python -m app.scripts.rescore_all
    uv run python -m app.scripts.rescore_all --concurrency 4
    uv run python -m app.scripts.rescore_all --org 00000000-0000-0000-0000-000000000001
"""

import argparse
import asyncio
import sys
from uuid import UUID

from sqlalchemy import select

from app.db import session_scope
from app.models.score import Score
from app.services.scoring import ScoringEngineError, score_candidate


async def main(org_id: UUID | None, concurrency: int) -> int:
    async with session_scope() as session:
        q = select(Score.candidate_id, Score.position_id, Score.organization_id)
        if org_id is not None:
            q = q.where(Score.organization_id == org_id)
        rows = (await session.execute(q)).all()

    if not rows:
        print("No scores to re-score.", flush=True)
        return 0

    print(f"Re-scoring {len(rows)} (candidate × position) pairs…", flush=True)

    sem = asyncio.Semaphore(concurrency)

    async def run(candidate_id: UUID, position_id: UUID, oid: UUID):
        async with sem:
            async with session_scope() as session:
                try:
                    res = await score_candidate(
                        candidate_id=candidate_id,
                        position_id=position_id,
                        db=session,
                        organization_id=oid,
                    )
                    return (candidate_id, position_id, res.overall_score, None)
                except ScoringEngineError as e:
                    return (candidate_id, position_id, None, str(e))
                except Exception as e:
                    return (candidate_id, position_id, None, f"{type(e).__name__}: {e}")

    results = await asyncio.gather(
        *[run(c, p, o) for c, p, o in rows]
    )

    succeeded = sum(1 for *_, err in results if err is None)
    failed = [(c, p, e) for c, p, _, e in results if e is not None]

    print(
        f"\nDone. Re-scored: {succeeded}, Failed: {len(failed)}",
        flush=True,
    )
    if failed:
        print("Failures:", flush=True)
        for c, p, e in failed:
            print(f"  candidate={c} position={p}: {e}", flush=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--org", type=str, default=None, help="Restrict to one organization_id (UUID)")
    ap.add_argument("--concurrency", type=int, default=2, help="Parallel calls (default 2)")
    args = ap.parse_args()
    org = UUID(args.org) if args.org else None
    sys.exit(asyncio.run(main(org_id=org, concurrency=args.concurrency)))
