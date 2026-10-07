"""Which files enter the code graph. Dependency-free on purpose: the hooks import this on every
prompt, so it must not pull in neo4j or tree-sitter (see tests: SUPPORTED_SUFFIXES must equal
CodeParser.supported_extensions).

Three layers, all applied by `is_indexable` for both full ingestion and incremental sync:
  1. built-in rules: unsupported types, minified files, vendor/build folders, well-known
     third-party libraries, and backup/duplicate filenames ("app - Copy.js", "app (1).js", ...);
  2. an optional per-project ignore file, `<project root>/.cognitive-graph-ignore`;
  3. nothing else: your own code is never skipped by guesswork.
A file that becomes excluded is removed from the graph by the next sync.
"""
import fnmatch
import re
from pathlib import Path

SUPPORTED_SUFFIXES = frozenset({".py", ".js", ".jsx", ".mjs", ".cjs", ".php"})
IGNORE_FILE = ".cognitive-graph-ignore"
SKIP_DIRS = {".git", ".venv", "venv", "env", "__pycache__", "node_modules", "legacy", "build", "dist", "vendor",
             "vendor_php", ".cognitive-graph", "bower_components", "jspm_packages"}
# Third-party libraries that are commonly checked in next to first-party code. Matched as substrings of the
# lower-cased file name, so entries are specific (a dot or suffix) to avoid catching your own files such as
# "angularApp.js" or "momentum.js".
SKIP_FILE_HINTS = (
    "jquery", "bootstrap", "modernizr", "popper", "slick", "chart.js", "chart.min", "chart.bundle",
    "toastr", "lodash", "underscore.", "underscore-min", "vendor.min", "moment.", "select2", "datatables", "sweetalert", "owl.carousel",
    "highcharts", "socket.io", "crypto-js", "jszip", "html2canvas", "pdf.worker", "fullcalendar", "tinymce",
    "ckeditor", "polyfill",
    "angular.", "angular-aria", "angular-sanitize", "angular-route", "angular-animate", "angular-ui-router",
    "angular-resource", "angular-cookies", "angular-touch", "angular-messages",
)
# Editor/OS leftovers and copies: "chatbot - Copy.js", "app copy.js", "app (1).js", "Copy of app.js",
# "app.orig.js", "app_old.js", "app-backup.js".
_BACKUP_NAME = re.compile(
    r"(?:\s-\s*copy(?:\s*\(\d+\))?|\scopy(?:\s*\(\d+\))?|\s\(\d+\)|[._-](?:orig|bak|backup|old))\.[a-z0-9]+$|^copy of\s",
    re.IGNORECASE)


def load_ignore(root) -> tuple[str, ...]:
    """Patterns from `<root>/.cognitive-graph-ignore` (one per line, `#` comments, blank lines ignored)."""
    try:
        text = (Path(root) / IGNORE_FILE).read_text(encoding="utf-8")
    except OSError:
        return ()
    return tuple(line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#"))


def matches_ignore(rel: str, patterns) -> bool:
    """gitignore-like, intentionally small. `dir/` ignores that folder (at the root or nested);
    a pattern without `/` matches a file name anywhere (`*.test.js`, `legacy_*.php`); a pattern with
    `/` is matched against the whole relative path (`assets/js/old_*`), where `*` also crosses folders."""
    rel = rel.replace("\\", "/")
    name = rel.rsplit("/", 1)[-1]
    for pat in patterns:
        pat = pat.replace("\\", "/")
        if pat.endswith("/"):
            d = pat.rstrip("/").lstrip("/")
            if rel.startswith(d + "/") or f"/{d}/" in f"/{rel}":
                return True
        elif "/" not in pat:
            if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(rel, pat):
                return True
        elif fnmatch.fnmatch(rel, pat.lstrip("/")) or fnmatch.fnmatch(rel, pat.lstrip("/") + "/*"):
            return True
    return False


def is_indexable(rel_parts: tuple[str, ...], suffixes=SUPPORTED_SUFFIXES, ignore=()) -> bool:
    """The single rule shared by full ingestion and incremental sync. `ignore` is `load_ignore(root)`."""
    name = rel_parts[-1].lower()
    if Path(name).suffix not in suffixes or name.endswith(".min.js"):
        return False
    if any(hint in name for hint in SKIP_FILE_HINTS) or _BACKUP_NAME.search(name) or SKIP_DIRS.intersection(rel_parts[:-1]):
        return False
    return not (ignore and matches_ignore("/".join(rel_parts), ignore))
