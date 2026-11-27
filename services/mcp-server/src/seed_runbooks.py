import hashlib
import json
import re

import psycopg

from .database import conninfo
from .embedding import RUNBOOK_DIR, Embedder, manifest, vector_parameter

UPSERT = """
    INSERT INTO runbook_chunks
        (source_kind, source_uri, source_revision, title, section, chunk_no, body, embedding)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::vector)
    ON CONFLICT (source_uri, source_revision, chunk_no) DO UPDATE SET
        source_kind = EXCLUDED.source_kind, title = EXCLUDED.title,
        section = EXCLUDED.section, body = EXCLUDED.body, embedding = EXCLUDED.embedding
"""


def chunks(markdown: str):
    for section in re.split(r"^## ", markdown, flags=re.MULTILINE)[1:]:
        title, _, body = section.partition("\n")
        if not title.strip():
            raise ValueError("Runbook section heading must contain text")
        for paragraph in body.strip().split("\n\n"):
            text = paragraph.strip()
            while text:
                cut = len(text) if len(text) <= 360 else text.rfind(" ", 180, 360)
                if cut < 0:
                    cut = 360
                yield title.strip(), text[:cut]
                text = text[cut:].strip()


def main():
    config = manifest()
    embedder = Embedder()
    model = config["model"]
    model_fingerprint = hashlib.sha256(json.dumps(model, sort_keys=True).encode()).hexdigest()
    rows = []
    for doc in config["documents"]:
        path = RUNBOOK_DIR / doc["file"]
        content = path.read_bytes()
        revision = f"{model_fingerprint}:{hashlib.sha256(content).hexdigest()}"
        sections = list(chunks(content.decode()))
        if not sections:
            raise ValueError(f"Document has no chunks: {path.name}")
        for number, (section, body) in enumerate(sections):
            text = f"{doc['title']} — {section}\n{body}"
            rows.append(
                (
                    doc["kind"],
                    f"repo:///data/runbooks/{path.name}",
                    revision,
                    doc["title"],
                    section,
                    number,
                    body,
                    vector_parameter(embedder.vector(text, "passage")),
                )
            )
    with psycopg.connect(conninfo()) as conn, conn.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(603021)")
        cursor.execute(
            "SELECT EXISTS (SELECT 1 FROM runbook_chunks WHERE source_revision NOT LIKE %s)",
            (model_fingerprint + ":%",),
        )
        if cursor.fetchone()[0]:
            raise RuntimeError("Runbook index contains a different model configuration")
        cursor.executemany(UPSERT, rows)
    print(f"Seeded {len(rows)} runbook chunks")


if __name__ == "__main__":
    main()
