"""AST extraction with tree-sitter (0.23+ API) for Python, JavaScript and PHP.

Each language is described by a small LanguageSpec: which nodes are function
entities (and what they are called) and how to read the callee name off a call
node. Matching on exact node types, rather than substrings like "function", keeps
call expressions and function bodies from being mistaken for definitions.
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import tree_sitter_javascript
import tree_sitter_php
import tree_sitter_python
from tree_sitter import Language, Node, Parser


@dataclass(frozen=True)
class FunctionEntity:
    name: str
    raw_code: str
    start_line: int  # 1-based, inclusive
    end_line: int  # 1-based, inclusive
    calls: tuple[str, ...] = field(default_factory=tuple)  # names called in the body

    @property
    def lines(self) -> list[int]:
        return [self.start_line, self.end_line]


def _text(node: Node | None) -> str | None:
    return node.text.decode("utf-8") if node is not None else None


# --- per-language rules --------------------------------------------------------
# entity(node)    -> (name, node whose text/lines are stored) if `node` is a function
#                    we want as a graph node, else None
# call_name(node) -> callee name if `node` is a call, else None

def _python_entity(node: Node):
    if node.type == "function_definition":
        return _text(node.child_by_field_name("name")), node
    return None


def _python_call(node: Node):
    if node.type != "call":
        return None
    target = node.child_by_field_name("function")
    if target is not None and target.type == "attribute":
        target = target.child_by_field_name("attribute")
    return _text(target) if target is not None and target.type == "identifier" else None


_JS_NAMED = {"function_declaration", "generator_function_declaration", "method_definition"}
_JS_ANON = {"arrow_function", "function_expression", "function", "generator_function"}


def _js_entity(node: Node):
    if node.type in _JS_NAMED:
        return _text(node.child_by_field_name("name")), node
    if node.type in _JS_ANON:
        # `const f = () => ...` / `const f = function () {...}` are named by the
        # variable. Other anonymous functions (callbacks) are not entities; their
        # calls count towards the enclosing function.
        decl = node.parent
        if decl is not None and decl.type == "variable_declarator" and decl.child_by_field_name("value") == node:
            name = decl.child_by_field_name("name")
            if name is not None and name.type == "identifier":
                statement = decl.parent
                if statement is not None and statement.type in {"lexical_declaration", "variable_declaration"} \
                        and statement.named_child_count == 1:
                    return _text(name), statement
                return _text(name), decl
    return None


def _js_call(node: Node):
    if node.type != "call_expression":
        return None
    target = node.child_by_field_name("function")
    if target is not None and target.type == "member_expression":
        target = target.child_by_field_name("property")
    return _text(target) if target is not None and target.type in {"identifier", "property_identifier"} else None


def _php_entity(node: Node):
    if node.type in {"function_definition", "method_declaration"}:
        return _text(node.child_by_field_name("name")), node
    return None


def _php_call(node: Node):
    if node.type == "function_call_expression":
        target = node.child_by_field_name("function")
        if target is not None and target.type == "qualified_name":  # Ns\foo()
            names = [c for c in target.named_children if c.type == "name"]
            target = names[-1] if names else None
        return _text(target) if target is not None and target.type == "name" else None
    if node.type in {"member_call_expression", "scoped_call_expression", "nullsafe_member_call_expression"}:
        return _text(node.child_by_field_name("name"))
    return None


def _php_language() -> Language:
    # Older tree-sitter-php releases expose language(); newer ones language_php().
    factory = getattr(tree_sitter_php, "language_php", None) or tree_sitter_php.language
    return Language(factory())


@dataclass(frozen=True)
class LanguageSpec:
    id: str
    language: Language
    entity: Callable[[Node], tuple[str | None, Node] | None]
    call_name: Callable[[Node], str | None]


_PYTHON = LanguageSpec("python", Language(tree_sitter_python.language()), _python_entity, _python_call)
_JAVASCRIPT = LanguageSpec("javascript", Language(tree_sitter_javascript.language()), _js_entity, _js_call)
_PHP = LanguageSpec("php", _php_language(), _php_entity, _php_call)

SPECS: dict[str, LanguageSpec] = {
    ".py": _PYTHON,
    ".js": _JAVASCRIPT,
    ".jsx": _JAVASCRIPT,
    ".mjs": _JAVASCRIPT,
    ".cjs": _JAVASCRIPT,
    ".php": _PHP,
}


class CodeParser:
    supported_extensions = frozenset(SPECS)

    def __init__(self) -> None:
        self._parsers = {spec.id: Parser(spec.language) for spec in set(SPECS.values())}

    @staticmethod
    def language_id(path: str | Path) -> str | None:
        spec = SPECS.get(Path(path).suffix.lower())
        return spec.id if spec else None

    def parse_file(self, path: str | Path) -> list[FunctionEntity]:
        path = Path(path)
        return self.parse_source(path.read_bytes(), path.suffix)

    def parse_source(self, source: bytes | str, extension: str = ".py") -> list[FunctionEntity]:
        spec = SPECS.get(extension.lower())
        if spec is None:
            raise ValueError(f"Unsupported file type {extension!r} (supported: {sorted(SPECS)})")
        if isinstance(source, str):
            source = source.encode("utf-8")
        tree = self._parsers[spec.id].parse(source)
        text = source.decode("utf-8", errors="replace")
        lines = text.replace("\r\n", "\n").split("\n")
        return list(self._walk(tree.root_node, spec, lines))

    def has_errors(self, source: bytes | str, extension: str = ".py") -> bool:
        """True if tree-sitter found syntax errors (used to vet AI-written edits)."""
        spec = SPECS[extension.lower()]
        if isinstance(source, str):
            source = source.encode("utf-8")
        return self._parsers[spec.id].parse(source).root_node.has_error

    def _walk(self, root: Node, spec: LanguageSpec, lines: list[str]):
        """Iterative DFS; yields every function entity in source order.

        raw_code is whole source lines, so it keeps the entity's leading
        indentation (a method's first line is indented like the rest) and equals
        the file's text for [start_line, end_line] exactly."""
        stack = [root]
        while stack:
            node = stack.pop()
            entity = spec.entity(node)
            if entity is not None and entity[0]:
                name, span = entity
                first_row = span.start_point[0]
                last_row = span.end_point[0]
                if span.end_point[1] == 0 and last_row > first_row:
                    last_row -= 1  # node ended right after a newline
                yield FunctionEntity(
                    name=name,
                    raw_code="\n".join(lines[first_row:last_row + 1]),
                    start_line=first_row + 1,
                    end_line=last_row + 1,
                    calls=tuple(sorted(self._collect_calls(node, spec))),
                )
            stack.extend(reversed(node.children))

    @staticmethod
    def _collect_calls(func: Node, spec: LanguageSpec) -> set[str]:
        """Names called inside `func`. Nested functions that are entities of their
        own are skipped (they get their own CALLS edges); anonymous callbacks are
        not entities, so their calls belong to `func`. Attribute/method calls are
        matched by method name only (best effort, no type resolution)."""
        calls: set[str] = set()
        stack = list(func.children)
        while stack:
            node = stack.pop()
            entity = spec.entity(node)
            if entity is not None and entity[0]:
                continue
            name = spec.call_name(node)
            if name:
                calls.add(name)
            stack.extend(node.children)
        return calls
