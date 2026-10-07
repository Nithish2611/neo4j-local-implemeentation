"""Skip rules for the code graph: copies, vendor libraries, and the per-project ignore file."""

import pytest

from cognitive_graph.indexing_rules import is_indexable, load_ignore, matches_ignore


def ok(path, ignore=()):
    return is_indexable(tuple(path.split("/")), ignore=ignore)


@pytest.mark.parametrize("name", [
    "chatbot - Copy.js", "app copy.js", "app (1).js", "Copy of app.js", "app.orig.js", "app_old.js",
    "app-backup.js", "app.bak.js", "screen - copy (2).js",
])
def test_copies_and_backups_are_skipped(name):
    assert not ok(f"assets/js/{name}")


@pytest.mark.parametrize("name", [
    "toastr.js", "moment.min.js", "lodash.js", "select2.full.js", "angular-ui-router.js", "angular.js",
    "socket.io.js", "pdf.worker.js", "app.min.js", "underscore-min.js", "underscore.js",
])
def test_vendor_libraries_are_skipped(name):
    assert not ok(f"assets/js/{name}")


@pytest.mark.parametrize("path", [
    "assets/js/chatbot/chatbot.js", "assets/js/angularApp.js", "assets/js/momentum.js",
    "src/copyright.js", "src/copyService.js", "app/billing.py", "app/billing_old_name.py", "x/controller.php",
])
def test_your_own_code_is_kept(path):
    assert ok(path), path


def test_unsupported_and_build_paths_stay_skipped():
    assert not ok("README.md") and not ok("index.html") and not ok("node_modules/lib/x.js")
    assert not ok("dist/app.js") and not ok("assets/js/app.min.js")


def test_project_ignore_file_patterns(tmp_path):
    (tmp_path / ".cognitive-graph-ignore").write_text(
        "# generated and legacy code\n\nlegacy/\n*.test.js\nassets/js/old_*\nthirdparty/\n", encoding="utf-8")
    patterns = load_ignore(tmp_path)
    assert patterns == ("legacy/", "*.test.js", "assets/js/old_*", "thirdparty/")
    assert not ok("app/x.test.js", patterns) and ok("app/x.js", patterns)
    assert not ok("assets/js/old_menu.js", patterns) and ok("assets/js/menu.js", patterns)
    assert not ok("src/thirdparty/lib.js", patterns) and ok("src/first.js", patterns)
    assert not ok("legacy/a.py", patterns) and ok("app/a.py", patterns)


def test_missing_ignore_file_changes_nothing(tmp_path):
    assert load_ignore(tmp_path) == ()


def test_matching_rules_directly():
    assert matches_ignore("a/b/legacy/c.js", ("legacy/",))
    assert not matches_ignore("a/legacyish/c.js", ("legacy/",))
    assert matches_ignore("deep/x.bak.php", ("*.bak.php",))
    assert not matches_ignore("deep/x.php", ("*.bak.php",))


def test_full_ingestion_and_sync_agree_on_the_same_files(tmp_path):
    """Both code paths must select the same files, or the graph would disagree with itself."""
    from cognitive_graph.code_parser import CodeParser
    from cognitive_graph.ingestor import Ingestor

    files = {
        "keep.py": "def a():\n    pass\n",
        "assets/js/chatbot - Copy.js": "function c() {}\n",
        "assets/js/toastr.js": "function t() {}\n",
        "legacy/old.py": "def old():\n    pass\n",
        "scratch/tmp.py": "def tmp():\n    pass\n",
    }
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    (tmp_path / ".cognitive-graph-ignore").write_text("scratch/\n", encoding="utf-8")

    class Recorder:
        def __init__(self):
            self.replaced = []

        def replace_file(self, path, functions):
            self.replaced.append(path)

        def link_calls(self, edges):
            pass

    rec = Recorder()
    Ingestor(CodeParser(), rec).ingest_path(tmp_path, root=tmp_path)
    full = sorted(rec.replaced)

    from cognitive_graph.indexing_rules import SUPPORTED_SUFFIXES
    from cognitive_graph.graph_sync import source_files

    incremental = sorted(source_files(tmp_path, SUPPORTED_SUFFIXES)[0])
    assert full == incremental == ["keep.py"]
