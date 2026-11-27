import hmac
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Principal:
    id: str
    permissions: frozenset[str]


def load_tokens() -> list[tuple[bytes, Principal]]:
    tokens = [
        (os.environ[key].encode(), Principal(name, frozenset(permissions)))
        for key, name, permissions in [
            ("INGEST_TOKEN", "event-watcher", ["incidents:ingest"]),
            ("READER_TOKEN", "local-reader", ["incidents:read"]),
            ("APPROVER_TOKEN", "local-approver", ["incidents:read", "incidents:approve"]),
        ]
    ]
    if any(not token for token, _ in tokens) or len({token for token, _ in tokens}) != len(tokens):
        raise ValueError("API tokens must be nonempty and distinct")
    return tokens


def authenticate(header: str | None, tokens: list[tuple[bytes, Principal]]) -> Principal | None:
    if header:
        scheme, _, token = header.partition(" ")
        if scheme.lower() == "bearer":
            for expected, principal in tokens:
                if hmac.compare_digest(token.encode(), expected):
                    return principal
    return None
