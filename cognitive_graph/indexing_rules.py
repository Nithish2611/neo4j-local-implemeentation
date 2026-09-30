"""Which files enter the code graph. Dependency-free on purpose: the hooks import this on every
prompt, so it must not pull in neo4j or tree-sitter (see tests: SUPPORTED_SUFFIXES must equal
CodeParser.supported_extensions)."""
from pathlib import Path

SUPPORTED_SUFFIXES = frozenset({".py", ".js", ".jsx", ".mjs", ".cjs", ".php"})
SKIP_DIRS = {".git", ".venv", "venv", "env", "__pycache__", "node_modules", "legacy", "build", "dist", "vendor",
             "vendor_php", ".cognitive-graph"}
# Third-party bundles that are commonly checked in next to first-party code.
SKIP_FILE_HINTS = ("jquery", "bootstrap", "modernizr", "popper", "slick", "chart.js", "chart.min", "chart.bundle")


def is_indexable(rel_parts: tuple[str, ...], suffixes=SUPPORTED_SUFFIXES) -> bool:
    """The single rule shared by full ingestion and incremental sync."""
    name = rel_parts[-1].lower()
    if Path(name).suffix not in suffixes or name.endswith(".min.js"):
        return False
    return not any(hint in name for hint in SKIP_FILE_HINTS) and not SKIP_DIRS.intersection(rel_parts[:-1])
