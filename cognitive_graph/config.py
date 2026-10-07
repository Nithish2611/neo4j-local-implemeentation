"""Central configuration, read from environment / .env."""
import os
from dataclasses import dataclass

from dotenv import find_dotenv, load_dotenv


@dataclass(frozen=True)
class Settings:
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str
    claude_cli: str
    claude_model: str | None
    claude_timeout: float

    @classmethod
    def from_env(cls, env_file=None) -> "Settings":
        """`env_file` (e.g. <project>/.env) is loaded if it exists; otherwise the usual .env search."""
        if env_file is not None and os.path.isfile(env_file):
            load_dotenv(env_file)
        elif env_file is not None:
            load_dotenv(find_dotenv(usecwd=True))  # the working folder, never this package's own folder
        else:
            load_dotenv()
        return cls(
            neo4j_uri=os.getenv("NEO4J_URI", "bolt://127.0.0.1:7687"),
            neo4j_user=os.getenv("NEO4J_USER", "neo4j"),
            neo4j_password=os.getenv("NEO4J_PASSWORD", "password123"),
            claude_cli=os.getenv("CLAUDE_CLI", "claude"),
            claude_model=os.getenv("CLAUDE_MODEL") or None,
            claude_timeout=float(os.getenv("CLAUDE_TIMEOUT", "180")),
        )
