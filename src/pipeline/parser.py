"""
Tree-sitter AST parser.
Extracts functions, classes, methods, and imports from source files.
Returns language-agnostic ParsedFile / ParsedSymbol dataclasses.

Supported languages: Python, TypeScript/TSX, JavaScript, Java, Go, Rust.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)

# ── Extension → language name ─────────────────────────────────────────────────

EXTENSION_TO_LANGUAGE: dict[str, str] = {
    # Tree-sitter supported (AST parsing)
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    # Detected but no tree-sitter grammar — indexed via sliding-window chunker
    ".cpp": "cpp",
    ".c": "c",
    ".cs": "c_sharp",
    ".rb": "ruby",
    ".swift": "swift",
    ".kt": "kotlin",
    ".json": "json",
    ".md": "markdown",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".html": "html",
    ".css": "css",
    ".scss": "scss",
    ".sh": "shell",
    ".sql": "sql",
    ".xml": "xml",
    ".toml": "toml",
}


def detect_language(file_path: str) -> str | None:
    return EXTENSION_TO_LANGUAGE.get(Path(file_path).suffix.lower())


# ── Data structures ───────────────────────────────────────────────────────────


@dataclass
class ParsedSymbol:
    name: str
    qualified_name: str  # "ClassName.method_name" or just "function_name"
    kind: str  # "function" | "class" | "method"
    start_line: int  # 1-indexed
    end_line: int  # 1-indexed
    source: str  # raw source text of this symbol
    signature: str | None = None
    docstring: str | None = None
    parent_name: str | None = None  # set for methods
    children: list[ParsedSymbol] = field(default_factory=list)
    is_exported: bool = False


@dataclass
class ParsedFile:
    file_path: str
    language: str
    source: str
    imports: list[str] = field(default_factory=list)
    top_level_symbols: list[ParsedSymbol] = field(default_factory=list)

    @property
    def all_symbols(self) -> list[ParsedSymbol]:
        """Flatten all symbols including nested methods."""
        result: list[ParsedSymbol] = []
        for sym in self.top_level_symbols:
            result.append(sym)
            result.extend(sym.children)
        return result


# ── Tree-sitter language loader ───────────────────────────────────────────────


def _get_ts_language(language: str):
    """Lazy-load the Tree-sitter Language for a given language name."""
    from tree_sitter import Language

    if language == "python":
        import tree_sitter_python as ts_mod

        return Language(ts_mod.language())

    if language in ("typescript", "tsx"):
        import tree_sitter_typescript as ts_mod

        if language == "tsx":
            return Language(ts_mod.language_tsx())
        return Language(ts_mod.language_typescript())

    if language == "javascript":
        import tree_sitter_javascript as ts_mod

        return Language(ts_mod.language())

    if language == "java":
        import tree_sitter_java as ts_mod

        return Language(ts_mod.language())

    if language == "go":
        import tree_sitter_go as ts_mod

        return Language(ts_mod.language())

    if language == "rust":
        import tree_sitter_rust as ts_mod

        return Language(ts_mod.language())

    raise ValueError(f"Unsupported language: {language}")


def _make_parser(language: str):
    from tree_sitter import Parser

    ts_lang = _get_ts_language(language)
    return Parser(ts_lang)


# ── Shared helpers ────────────────────────────────────────────────────────────


def _node_text(node, source_bytes: bytes) -> str:
    return source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _node_lines(node) -> tuple[int, int]:
    """Return 1-indexed (start_line, end_line)."""
    return node.start_point[0] + 1, node.end_point[0] + 1


# ── Python parser ─────────────────────────────────────────────────────────────


def _py_extract_docstring(body_node, source_bytes: bytes) -> str | None:
    """First expression_statement > string in a block = docstring."""
    if body_node is None:
        return None
    for child in body_node.named_children:
        if child.type == "expression_statement":
            for expr in child.named_children:
                if expr.type == "string":
                    raw = _node_text(expr, source_bytes).strip()
                    # Strip triple or single quotes
                    for q in ('"""', "'''", '"', "'"):
                        if raw.startswith(q) and raw.endswith(q) and len(raw) > 2 * len(q):
                            return raw[len(q) : -len(q)].strip()
                    return raw
        break  # docstring must be first statement
    return None


def _py_function_signature(node, source_bytes: bytes) -> str:
    name_node = node.child_by_field_name("name")
    params_node = node.child_by_field_name("parameters")
    return_node = node.child_by_field_name("return_type")

    name = _node_text(name_node, source_bytes) if name_node else "?"
    params = _node_text(params_node, source_bytes) if params_node else "()"
    sig = f"def {name}{params}"
    if return_node:
        sig += f" -> {_node_text(return_node, source_bytes)}"
    return sig


def _py_parse_function(node, source_bytes: bytes, parent_name: str | None = None) -> ParsedSymbol:
    name_node = node.child_by_field_name("name")
    name = _node_text(name_node, source_bytes) if name_node else "<anonymous>"
    qualified = f"{parent_name}.{name}" if parent_name else name
    kind = "method" if parent_name else "function"
    start, end = _node_lines(node)
    body = node.child_by_field_name("body")
    return ParsedSymbol(
        name=name,
        qualified_name=qualified,
        kind=kind,
        start_line=start,
        end_line=end,
        source=_node_text(node, source_bytes),
        signature=_py_function_signature(node, source_bytes),
        docstring=_py_extract_docstring(body, source_bytes),
        parent_name=parent_name,
        is_exported=not name.startswith("_"),
    )


def _py_parse_class(node, source_bytes: bytes) -> ParsedSymbol:
    name_node = node.child_by_field_name("name")
    name = _node_text(name_node, source_bytes) if name_node else "<anonymous>"
    start, end = _node_lines(node)
    body = node.child_by_field_name("body")
    docstring = _py_extract_docstring(body, source_bytes) if body else None

    # Extract class header line (for signature)
    first_line = _node_text(node, source_bytes).splitlines()[0].rstrip(":")
    signature = first_line

    sym = ParsedSymbol(
        name=name,
        qualified_name=name,
        kind="class",
        start_line=start,
        end_line=end,
        source=_node_text(node, source_bytes),
        signature=signature,
        docstring=docstring,
        is_exported=not name.startswith("_"),
    )

    # Extract methods from class body
    if body:
        for child in body.named_children:
            inner = child
            # Handle decorated methods
            if inner.type == "decorated_definition":
                inner = inner.child_by_field_name("definition") or inner
            if inner.type == "function_definition":
                method = _py_parse_function(inner, source_bytes, parent_name=name)
                sym.children.append(method)

    return sym


def _py_extract_imports(root, source_bytes: bytes) -> list[str]:
    imports = []
    for child in root.named_children:
        if child.type in ("import_statement", "import_from_statement"):
            imports.append(_node_text(child, source_bytes).strip())
    return imports


def _parse_python(source: str, file_path: str) -> ParsedFile:
    parser = _make_parser("python")
    source_bytes = source.encode("utf-8")
    tree = parser.parse(source_bytes)
    root = tree.root_node

    imports = _py_extract_imports(root, source_bytes)
    symbols: list[ParsedSymbol] = []

    for child in root.named_children:
        node = child

        # Unwrap decorated definitions
        if node.type == "decorated_definition":
            node = node.child_by_field_name("definition") or node

        if node.type == "function_definition":
            symbols.append(_py_parse_function(node, source_bytes))
        elif node.type == "class_definition":
            symbols.append(_py_parse_class(node, source_bytes))

    return ParsedFile(
        file_path=file_path,
        language="python",
        source=source,
        imports=imports,
        top_level_symbols=symbols,
    )


# ── TypeScript / JavaScript parser ────────────────────────────────────────────

_TS_FUNCTION_TYPES = {
    "function_declaration",
    "function",
    "arrow_function",
    "generator_function_declaration",
}

_TS_CLASS_TYPES = {
    "class_declaration",
    "class",
    "abstract_class_declaration",
}

_TS_METHOD_TYPES = {
    "method_definition",
    "public_field_definition",
}


def _ts_function_signature(node, source_bytes: bytes) -> str:
    name_node = node.child_by_field_name("name")
    params_node = node.child_by_field_name("parameters") or node.child_by_field_name(
        "formal_parameters"
    )
    name = _node_text(name_node, source_bytes) if name_node else "<anonymous>"
    params = _node_text(params_node, source_bytes) if params_node else "()"
    return f"function {name}{params}"


def _ts_parse_function(
    node, source_bytes: bytes, parent_name: str | None = None, exported: bool = False
) -> ParsedSymbol:
    name_node = node.child_by_field_name("name")
    name = _node_text(name_node, source_bytes) if name_node else "<anonymous>"
    qualified = f"{parent_name}.{name}" if parent_name else name
    kind = "method" if parent_name else "function"
    start, end = _node_lines(node)
    return ParsedSymbol(
        name=name,
        qualified_name=qualified,
        kind=kind,
        start_line=start,
        end_line=end,
        source=_node_text(node, source_bytes),
        signature=_ts_function_signature(node, source_bytes),
        parent_name=parent_name,
        is_exported=exported,
    )


def _ts_parse_class(node, source_bytes: bytes, exported: bool = False) -> ParsedSymbol:
    name_node = node.child_by_field_name("name")
    name = _node_text(name_node, source_bytes) if name_node else "<anonymous>"
    start, end = _node_lines(node)

    first_line = _node_text(node, source_bytes).splitlines()[0].rstrip("{").strip()
    sym = ParsedSymbol(
        name=name,
        qualified_name=name,
        kind="class",
        start_line=start,
        end_line=end,
        source=_node_text(node, source_bytes),
        signature=first_line,
        is_exported=exported,
    )

    body = node.child_by_field_name("body")
    if body:
        for child in body.named_children:
            if child.type in _TS_METHOD_TYPES:
                method_name_node = child.child_by_field_name("name")
                method_name = (
                    _node_text(method_name_node, source_bytes) if method_name_node else "<anon>"
                )
                qualified = f"{name}.{method_name}"
                m_start, m_end = _node_lines(child)
                method = ParsedSymbol(
                    name=method_name,
                    qualified_name=qualified,
                    kind="method",
                    start_line=m_start,
                    end_line=m_end,
                    source=_node_text(child, source_bytes),
                    parent_name=name,
                )
                sym.children.append(method)

    return sym


def _ts_extract_imports(root, source_bytes: bytes) -> list[str]:
    imports = []
    for child in root.named_children:
        if child.type == "import_statement":
            imports.append(_node_text(child, source_bytes).strip())
    return imports


def _parse_typescript(source: str, file_path: str, language: str = "typescript") -> ParsedFile:
    parser = _make_parser(language)
    source_bytes = source.encode("utf-8")
    tree = parser.parse(source_bytes)
    root = tree.root_node

    imports = _ts_extract_imports(root, source_bytes)
    symbols: list[ParsedSymbol] = []

    for child in root.named_children:
        node = child
        exported = False

        # Unwrap export statements
        if node.type == "export_statement":
            exported = True
            node = node.child_by_field_name("declaration") or node

        if node.type in _TS_FUNCTION_TYPES:
            symbols.append(_ts_parse_function(node, source_bytes, exported=exported))
        elif node.type in _TS_CLASS_TYPES:
            symbols.append(_ts_parse_class(node, source_bytes, exported=exported))
        elif node.type in ("lexical_declaration", "variable_declaration"):
            # Handle: const foo = () => { ... }
            for decl in node.named_children:
                if decl.type == "variable_declarator":
                    val = decl.child_by_field_name("value")
                    if val and val.type in _TS_FUNCTION_TYPES:
                        name_node = decl.child_by_field_name("name")
                        name = _node_text(name_node, source_bytes) if name_node else "<anon>"
                        start, end = _node_lines(node)
                        symbols.append(
                            ParsedSymbol(
                                name=name,
                                qualified_name=name,
                                kind="function",
                                start_line=start,
                                end_line=end,
                                source=_node_text(node, source_bytes),
                                is_exported=exported,
                            )
                        )

    return ParsedFile(
        file_path=file_path,
        language=language,
        source=source,
        imports=imports,
        top_level_symbols=symbols,
    )


# ── Go parser ─────────────────────────────────────────────────────────────────


def _parse_go(source: str, file_path: str) -> ParsedFile:
    parser = _make_parser("go")
    source_bytes = source.encode("utf-8")
    tree = parser.parse(source_bytes)
    root = tree.root_node

    imports: list[str] = []
    symbols: list[ParsedSymbol] = []

    for child in root.named_children:
        if child.type in ("import_declaration", "import_spec"):
            imports.append(_node_text(child, source_bytes).strip())

        elif child.type == "function_declaration":
            name_node = child.child_by_field_name("name")
            name = _node_text(name_node, source_bytes) if name_node else "<anon>"
            start, end = _node_lines(child)
            symbols.append(
                ParsedSymbol(
                    name=name,
                    qualified_name=name,
                    kind="function",
                    start_line=start,
                    end_line=end,
                    source=_node_text(child, source_bytes),
                )
            )

        elif child.type == "method_declaration":
            name_node = child.child_by_field_name("name")
            receiver_node = child.child_by_field_name("receiver")
            name = _node_text(name_node, source_bytes) if name_node else "<anon>"
            receiver = ""
            if receiver_node:
                for rc in receiver_node.named_children:
                    type_node = rc.child_by_field_name("type")
                    if type_node:
                        receiver = _node_text(type_node, source_bytes).lstrip("*")
                        break
            qualified = f"{receiver}.{name}" if receiver else name
            start, end = _node_lines(child)
            symbols.append(
                ParsedSymbol(
                    name=name,
                    qualified_name=qualified,
                    kind="method",
                    start_line=start,
                    end_line=end,
                    source=_node_text(child, source_bytes),
                    parent_name=receiver or None,
                )
            )

        elif child.type == "type_declaration":
            for spec in child.named_children:
                if spec.type == "type_spec":
                    name_node = spec.child_by_field_name("name")
                    name = _node_text(name_node, source_bytes) if name_node else "<anon>"
                    start, end = _node_lines(child)
                    symbols.append(
                        ParsedSymbol(
                            name=name,
                            qualified_name=name,
                            kind="class",
                            start_line=start,
                            end_line=end,
                            source=_node_text(child, source_bytes),
                        )
                    )

    return ParsedFile(
        file_path=file_path,
        language="go",
        source=source,
        imports=imports,
        top_level_symbols=symbols,
    )


# ── Java parser ───────────────────────────────────────────────────────────────


def _parse_java(source: str, file_path: str) -> ParsedFile:
    parser = _make_parser("java")
    source_bytes = source.encode("utf-8")
    tree = parser.parse(source_bytes)
    root = tree.root_node

    imports: list[str] = []
    symbols: list[ParsedSymbol] = []

    def _walk_for_classes(node):
        for child in node.named_children:
            if child.type == "import_declaration":
                imports.append(_node_text(child, source_bytes).strip())
            elif child.type in ("class_declaration", "interface_declaration", "enum_declaration"):
                name_node = child.child_by_field_name("name")
                name = _node_text(name_node, source_bytes) if name_node else "<anon>"
                start, end = _node_lines(child)
                sym = ParsedSymbol(
                    name=name,
                    qualified_name=name,
                    kind="class",
                    start_line=start,
                    end_line=end,
                    source=_node_text(child, source_bytes),
                )
                body = child.child_by_field_name("body")
                if body:
                    for member in body.named_children:
                        if member.type in ("method_declaration", "constructor_declaration"):
                            m_name_node = member.child_by_field_name("name")
                            m_name = (
                                _node_text(m_name_node, source_bytes) if m_name_node else "<anon>"
                            )
                            m_start, m_end = _node_lines(member)
                            sym.children.append(
                                ParsedSymbol(
                                    name=m_name,
                                    qualified_name=f"{name}.{m_name}",
                                    kind="method",
                                    start_line=m_start,
                                    end_line=m_end,
                                    source=_node_text(member, source_bytes),
                                    parent_name=name,
                                )
                            )
                symbols.append(sym)
            else:
                _walk_for_classes(child)

    _walk_for_classes(root)
    return ParsedFile(
        file_path=file_path,
        language="java",
        source=source,
        imports=imports,
        top_level_symbols=symbols,
    )


# ── Public entry point ────────────────────────────────────────────────────────


def parse_file(file_path: str, source: str) -> ParsedFile | None:
    """
    Parse a source file and return a ParsedFile with all symbols extracted.
    Returns None if the language is unsupported.
    """
    language = detect_language(file_path)
    if not language:
        logger.debug("parse_file: unsupported extension for %s", file_path)
        return None

    try:
        if language == "python":
            return _parse_python(source, file_path)
        if language in ("typescript", "tsx"):
            return _parse_typescript(source, file_path, language)
        if language == "javascript":
            return _parse_typescript(source, file_path, "javascript")
        if language == "go":
            return _parse_go(source, file_path)
        if language == "java":
            return _parse_java(source, file_path)
        # Fallback: return a ParsedFile with no symbols (still gets chunked as plain text)
        return ParsedFile(file_path=file_path, language=language, source=source)

    except Exception as exc:
        logger.warning("parse_file: error parsing %s: %s", file_path, exc, exc_info=exc)
        # Return a minimal ParsedFile so the file still gets chunked by content
        return ParsedFile(file_path=file_path, language=language or "unknown", source=source)
