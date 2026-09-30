"""Central configuration, read from environment / .env."""
import os
from dataclasses import dataclass

from dotenv import find_dotenv, load_dotenv


@dataclass(frozen=True)
class Settings:
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str
    llm_provider: str
    ollama_base_url: str
    ollama_model: str
    gemini_api_key: str | None
    gemini_model: str

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
            llm_provider=os.getenv("LLM_PROVIDER", "ollama").lower(),
            ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
            ollama_model=os.getenv("OLLAMA_MODEL", "qwen2.5-coder:7b"),
            gemini_api_key=os.getenv("MY_API_KEY"),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.8-flash"),
        )
