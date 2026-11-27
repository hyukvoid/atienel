import asyncio

from .embedding import vector_parameter
from .schemas import RunbookHit, RunbookRagInput, RunbookRagOutput

SEARCH = """
    WITH candidates AS MATERIALIZED (
        SELECT id AS chunk_id, source_kind, title, source_uri, source_revision, section,
               left(body, 1200) AS excerpt, embedding <=> %(vector)s::vector AS distance
        FROM runbook_chunks
        ORDER BY embedding <=> %(vector)s::vector LIMIT %(candidates)s
    )
    SELECT chunk_id, source_kind, title, source_uri, source_revision, section, excerpt,
           greatest(0.0, distance) AS cosine_distance
    FROM candidates WHERE distance <= 0.35
    ORDER BY distance, chunk_id LIMIT %(top_k)s
"""


async def search(request: RunbookRagInput, pool, embedder, inference_lock):
    async with inference_lock:
        vector = await asyncio.to_thread(embedder.vector, request.query, "query")
    async with pool.connection() as conn:
        cursor = await conn.execute(
            SEARCH,
            {
                "vector": vector_parameter(vector),
                "candidates": max(40, request.top_k * 4),
                "top_k": request.top_k,
            },
        )
        matches = [RunbookHit.model_validate(row) for row in await cursor.fetchall()]
    return RunbookRagOutput(status="ok" if matches else "no_matches", matches=matches)
