from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse


ROOT = Path(__file__).resolve().parents[1]
I_EXE = ROOT / "build" / ("I.exe" if sys.platform == "win32" else "I")
PROJECT_ENTRY_CACHE: dict[Path, tuple[tuple[tuple[str, int], ...], dict[Path, Path]]] = {}
PROJECT_ENTRY_EXCLUDED_DIRS = {".git", ".cache", "build", "extern"}


def trace(message: str) -> None:
    target = os.environ.get("I_LSP_TRACE") or os.environ.get("I_LSP_LOG")
    if not target:
        return
    path = ROOT / "build" / "i_lsp.log" if target.lower() in ("1", "true", "yes") else Path(target)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(f"{time.time():.3f} {message}\n")
    except OSError:
        pass


DECL_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*(?:<[^>\n]+>)?(?:[A-Za-z_][A-Za-z0-9_]*)?)\s*:\s*"
    r"(struct|union|enum|alias|proc)\b(?:\s*<[^>\n]+>)?"
)
IMPORT_RE = re.compile(r'^\s*import\s+"([^"]+)"')
CINCLUDE_RE = re.compile(r'^\s*cinclude\s+"([^"]+)"')
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:<[^>\n]+>)?(?:[A-Za-z_][A-Za-z0-9_]*)?")
FIELD_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*([^;=@{]+(?:<[^;\n=@{]+>)?)"
    r"(?:\s*@\s*\"((?:\\.|[^\"])*)\")?\s*;"
)
VAR_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*([^=;{\n]+(?:<[^=\n;{]+>)?)")
PARAM_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*:\s*([^,\)\n]+)")
ENUM_ITEM_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\b")
CONTROL_KEYWORD_RE = re.compile(r"\b(for|while|do|switch)\b")
GENERIC_IDENT_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)<([^>\n]+)>([A-Za-z_][A-Za-z0-9_]*)?")
SIMPLE_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"(?:0[xX][0-9A-Fa-f]+|0[bB][01]+|(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?[fFuUlL]*)"
    r"(?![A-Za-z_])"
)
STRING_LITERAL_RE = re.compile(r'"(?:\\.|[^"\\])*"')
ATTRIBUTE_OPERATOR_RE = re.compile(r"@\s*\"")


SYMBOL_KIND = {
    "enum": 10,
    "enumMember": 22,
    "proc": 12,
    "alias": 13,
    "struct": 23,
    "union": 23,
}

COMPLETION_KIND = {
    "enum": 7,
    "enumMember": 20,
    "proc": 3,
    "alias": 7,
    "struct": 7,
    "union": 7,
}

SEMANTIC_TOKEN_TYPES = [
    "type",
    "function",
    "variable",
    "keyword",
    "parameter",
    "property",
    "enumMember",
    "number",
    "string",
    "operator",
]
SEMANTIC_TOKEN_MODIFIERS = [
    "declaration",
    "definition",
    "readonly",
    "defaultLibrary",
    "local",
    "global",
    "member",
    "generic",
]
SEMANTIC_TOKEN_INDEX = {name: index for index, name in enumerate(SEMANTIC_TOKEN_TYPES)}
SEMANTIC_MODIFIER_INDEX = {name: index for index, name in enumerate(SEMANTIC_TOKEN_MODIFIERS)}

KEYWORDS = {
    "alias",
    "and",
    "break",
    "case",
    "cinclude",
    "const",
    "continue",
    "default",
    "do",
    "else",
    "enum",
    "for",
    "if",
    "import",
    "or",
    "proc",
    "return",
    "shl",
    "shr",
    "struct",
    "switch",
    "union",
    "while",
}

BUILTIN_TYPES = {
    "bool",
    "char",
    "f32",
    "f64",
    "i8",
    "i16",
    "i32",
    "i64",
    "u8",
    "u16",
    "u32",
    "u64",
    "void",
}


@dataclass(frozen=True)
class Diagnostic:
    line: int
    col: int
    message: str
    severity: int = 1
    end_line: int | None = None
    end_col: int | None = None
    source: str = "i-lsp"
    uri: str = ""


class DiagnosticList(list):
    def __init__(self, enabled: bool = True) -> None:
        super().__init__()
        self.enabled = enabled

    def append(self, item) -> None:
        if self.enabled:
            super().append(item)


@dataclass(frozen=True)
class Symbol:
    name: str
    kind: str
    uri: str
    line: int
    col: int
    detail: str
    source_len: int = 0
    params: tuple[str, ...] = ()
    return_type: str = ""
    variadic: bool = False
    target_type: str = ""
    enum_owner: str = ""
    enum_item: str = ""
    type_param: str = ""
    generic_pattern: str = ""


@dataclass(frozen=True)
class FieldSymbol:
    owner: str
    name: str
    type_name: str
    attrs: str
    uri: str
    line: int
    col: int
    detail: str
    type_param: str = ""


@dataclass(frozen=True)
class VariableSymbol:
    name: str
    type_name: str
    uri: str
    line: int
    col: int
    detail: str
    kind: str = "variable"
    scope: str = ""


@dataclass(frozen=True)
class ImportSymbol:
    path: str
    uri: str
    line: int
    col: int
    end_col: int
    target_uri: str
    target_path: str


@dataclass(frozen=True)
class CIncludeSymbol:
    path: str
    uri: str
    line: int
    col: int
    end_col: int
    target_uri: str
    target_path: str


@dataclass
class Document:
    uri: str
    path: Path | None
    text: str
    symbols: list[Symbol]
    fields: dict[str, list[FieldSymbol]]
    variables: dict[str, VariableSymbol]
    variables_all: list[VariableSymbol]
    imports: list[ImportSymbol]
    cincludes: list[CIncludeSymbol]
    diagnostics: list[Diagnostic]


def path_to_uri(path: Path) -> str:
    return path.resolve().as_uri()


def uri_to_path(uri: str) -> Path | None:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    raw = unquote(parsed.path)
    if re.match(r"^/[A-Za-z]:/", raw):
        raw = raw[1:]
    return Path(raw)


def resolved_path(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


def project_root_for_path(path: Path) -> Path:
    start = resolved_path(path).parent
    for candidate in (start, *start.parents):
        if (candidate / "bunyan.py").exists() or (candidate / "CMakeLists.txt").exists() or (candidate / ".git").exists():
            return candidate
    return start


def project_i_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*.i"):
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if any(part in PROJECT_ENTRY_EXCLUDED_DIRS for part in relative.parts):
            continue
        files.append(resolved_path(path))
    return sorted(set(files), key=lambda item: str(item).lower())


def project_entry_snapshot(files: list[Path]) -> tuple[tuple[str, int], ...]:
    snapshot: list[tuple[str, int]] = []
    for path in files:
        try:
            snapshot.append((str(path), path.stat().st_mtime_ns))
        except OSError:
            snapshot.append((str(path), 0))
    return tuple(snapshot)


def import_targets_for_file(path: Path, known_files: set[Path]) -> list[Path]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    targets: list[Path] = []
    for line in text.splitlines():
        match = IMPORT_RE.match(line)
        if not match:
            continue
        raw = match.group(1)
        target = Path(raw)
        if not target.is_absolute():
            target = path.parent / target
        target = resolved_path(target)
        if target in known_files:
            targets.append(target)
    return targets


def candidate_entry_score(root: Path, path: Path) -> tuple[int, int, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    try:
        relative = path.relative_to(root)
        depth = len(relative.parts)
    except ValueError:
        depth = 99
    if path == resolved_path(root / "src" / "main.i"):
        priority = 0
    elif re.search(r"(?m)^\s*main\s*:\s*proc\b", text):
        priority = 1
    elif path.name.lower() in ("main.i", "win32.i") or path.name.lower().endswith("_win32.i"):
        priority = 2
    else:
        priority = 3
    return priority, depth, str(path).lower()


def reachable_i_files(start: Path, adjacency: dict[Path, list[Path]]) -> set[Path]:
    reached: set[Path] = set()
    stack = [start]
    while stack:
        path = stack.pop()
        if path in reached:
            continue
        reached.add(path)
        stack.extend(adjacency.get(path, []))
    return reached


def project_entry_map(root: Path) -> dict[Path, Path]:
    root = resolved_path(root)
    files = project_i_files(root)
    snapshot = project_entry_snapshot(files)
    cached = PROJECT_ENTRY_CACHE.get(root)
    if cached and cached[0] == snapshot:
        return cached[1]

    known_files = set(files)
    adjacency: dict[Path, list[Path]] = {}
    imported: set[Path] = set()
    for path in files:
        targets = import_targets_for_file(path, known_files)
        adjacency[path] = targets
        imported.update(targets)

    candidates = [path for path in files if path not in imported]
    if not candidates:
        candidates = list(files)
    candidates.sort(key=lambda path: candidate_entry_score(root, path))

    entry_by_file: dict[Path, Path] = {}
    for candidate in candidates:
        for reached in reachable_i_files(candidate, adjacency):
            entry_by_file.setdefault(reached, candidate)
    for path in files:
        entry_by_file.setdefault(path, path)

    PROJECT_ENTRY_CACHE[root] = (snapshot, entry_by_file)
    return entry_by_file


def project_entry_for_doc(doc: Document) -> Path | None:
    if not doc.path:
        return None
    explicit = os.environ.get("I_LSP_ENTRY") or os.environ.get("I_ENTRY")
    if explicit:
        return resolved_path(Path(explicit))
    path = resolved_path(doc.path)
    root = project_root_for_path(path)
    return project_entry_map(root).get(path, path)


def ident_start(ch: str) -> bool:
    return ch == "_" or ch.isalpha()


def ident_continue(ch: str) -> bool:
    return ch == "_" or ch.isalnum()


def parse_balanced_angle(text: str, open_pos: int) -> int:
    if open_pos >= len(text) or text[open_pos] != "<":
        return -1
    depth = 0
    for pos in range(open_pos, len(text)):
        ch = text[pos]
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
            if depth == 0:
                return pos + 1
    return -1


def generic_ident_parts(ident: str) -> tuple[str, str, str] | None:
    if not ident:
        return None
    match = re.match(r"[A-Za-z_][A-Za-z0-9_]*", ident)
    if not match:
        return None
    base = match.group(0)
    base_end = match.end()
    if base_end >= len(ident) or ident[base_end] != "<":
        return None
    angle_end = parse_balanced_angle(ident, base_end)
    if angle_end < 0:
        return None
    tail = ident[angle_end:]
    if tail and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", tail):
        return None
    return base, ident[base_end + 1 : angle_end - 1].strip(), tail


def iter_identifier_spans(line: str):
    pos = 0
    while pos < len(line):
        ch = line[pos]
        if not ident_start(ch):
            pos += 1
            continue
        if pos > 0 and ident_continue(line[pos - 1]):
            pos += 1
            continue

        start = pos
        pos += 1
        while pos < len(line) and ident_continue(line[pos]):
            pos += 1

        if pos < len(line) and line[pos] == "<":
            angle_end = parse_balanced_angle(line, pos)
            if angle_end > 0:
                pos = angle_end
                if pos < len(line) and ident_start(line[pos]):
                    pos += 1
                    while pos < len(line) and ident_continue(line[pos]):
                        pos += 1

        yield start, pos, line[start:pos]


def split_top_level_type_args(args: str) -> list[str]:
    out: list[str] = []
    start = 0
    depth = 0
    for i, ch in enumerate(args):
        if ch == "<":
            depth += 1
        elif ch == ">" and depth > 0:
            depth -= 1
        elif ch == "," and depth == 0:
            out.append(args[start:i].strip())
            start = i + 1
    final = args[start:].strip()
    if final:
        out.append(final)
    return out


def normalize_symbol_name(name: str) -> str:
    parts = generic_ident_parts(name.strip())
    if parts:
        base, _args, tail = parts
        return f"{base}<T>{tail}"
    return re.sub(r"<[^>]*>", "<T>", name)


def base_symbol_name(name: str) -> str:
    match = re.match(r"[A-Za-z_][A-Za-z0-9_]*", name)
    return match.group(0) if match else name


def normalize_type_name(type_name: str) -> str:
    out = type_name.strip()
    out = re.sub(r"\b(const|let)\b", "", out).strip()
    while out.startswith("*"):
        out = out[1:].strip()
    while True:
        next_out = re.sub(r"^\[[^\]]*\]\s*", "", out).strip()
        if next_out == out:
            break
        out = next_out
    return base_symbol_name(out)


def generic_type_arg(type_name: str) -> str:
    out = type_name.strip()
    out = re.sub(r"\b(const|let)\b", "", out).strip()
    while out.startswith("*"):
        out = out[1:].strip()
    parts = generic_ident_parts(re.sub(r"\s+", "", out))
    return parts[1] if parts else ""


def substitute_simple_generic_type(type_name: str, owner_type: str, type_param: str = "T") -> str:
    arg = generic_type_arg(owner_type)
    if not arg:
        return type_name
    param = type_param.strip() or "T"
    return re.sub(rf"\b{re.escape(param)}\b", arg, type_name)


def skip_ws(text: str, pos: int) -> int:
    while pos < len(text) and text[pos].isspace():
        pos += 1
    return pos


def skip_optional_index(text: str, pos: int) -> int:
    pos = skip_ws(text, pos)
    if pos >= len(text) or text[pos] != "[":
        return pos
    depth = 0
    while pos < len(text):
        if text[pos] == "[":
            depth += 1
        elif text[pos] == "]":
            depth -= 1
            if depth == 0:
                return pos + 1
        pos += 1
    return pos


def parse_ident(text: str, pos: int) -> tuple[str, int, int] | None:
    pos = skip_ws(text, pos)
    match = re.match(r"[A-Za-z_][A-Za-z0-9_]*", text[pos:])
    if not match:
        return None
    return match.group(0), pos, pos + len(match.group(0))


def control_context_for_open_brace(line: str, brace_col: int) -> str:
    prefix = line[:brace_col]
    delimiter = max(prefix.rfind("{"), prefix.rfind("}"), prefix.rfind(";"))
    local_prefix = prefix[delimiter + 1 :]
    matches = list(CONTROL_KEYWORD_RE.finditer(local_prefix))
    if not matches:
        return "other"
    keyword = matches[-1].group(1)
    if keyword == "switch":
        return "switch"
    return "loop"


def sanitize_code_line(line: str) -> str:
    if line.lstrip().startswith("#"):
        return " " * len(line)
    if line.find('"') < 0 and line.find("'") < 0 and line.find("/") < 0:
        return line

    chars = list(line)
    in_string = False
    in_char = False
    escaped = False
    i = 0

    while i < len(chars):
        ch = chars[i]
        next_ch = chars[i + 1] if i + 1 < len(chars) else ""

        if in_string:
            chars[i] = " "
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue

        if in_char:
            chars[i] = " "
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == "'":
                in_char = False
            i += 1
            continue

        if ch == "/" and next_ch == "/":
            for j in range(i, len(chars)):
                chars[j] = " "
            break

        if ch == '"':
            chars[i] = " "
            in_string = True
            i += 1
            continue

        if ch == "'":
            chars[i] = " "
            in_char = True
            i += 1
            continue

        i += 1

    return "".join(chars)


def line_col_to_offset(text: str, line: int, col: int) -> int:
    offset = 0
    lines = text.splitlines(keepends=True)
    for i in range(min(line, len(lines))):
        offset += len(lines[i])
    if line < len(lines):
        offset += min(col, len(lines[line]))
    return min(offset, len(text))


def token_range_at(text: str, line: int, col: int) -> tuple[str, int, int]:
    lines = text.splitlines()
    if line >= len(lines):
        return "", 0, 0
    clean_line = sanitize_code_line(lines[line])
    for start, end, ident in iter_identifier_spans(clean_line):
        if start <= col <= end:
            return generic_token_range_at(ident, start, col)
    return "", 0, 0


def generic_token_range_at(ident: str, start: int, col: int) -> tuple[str, int, int]:
    parts = generic_ident_parts(ident)
    if not parts:
        return ident, start, start + len(ident)

    base, args, tail = parts
    base_start = start
    base_end = base_start + len(base)
    if tail and base_start <= col <= base_end:
        return base, base_start, base_end

    args_start = base_end + 1
    args_end = args_start + len(args)
    if args_start <= col <= args_end:
        for arg_start, arg_end, arg_ident in iter_identifier_spans(args):
            absolute_start = args_start + arg_start
            absolute_end = args_start + arg_end
            if absolute_start <= col <= absolute_end:
                return generic_token_range_at(arg_ident, absolute_start, col)

    if tail:
        tail_start = start + len(base) + len(args) + 2
        tail_end = tail_start + len(tail)
        if tail_start <= col <= tail_end:
            return ident, start, start + len(ident)

    return ident, start, start + len(ident)


def token_at(text: str, line: int, col: int) -> str:
    ident, _start, _end = token_range_at(text, line, col)
    return ident


def position_to_lsp(line: int, start: int, end: int) -> dict:
    return {
        "start": {"line": line, "character": start},
        "end": {"line": line, "character": end},
    }


def iter_identifiers(text: str):
    for line_no, raw_line in enumerate(text.splitlines()):
        line = sanitize_code_line(raw_line)
        for start, end, ident in iter_identifier_spans(line):
            yield line_no, start, end, ident, raw_line


def document_lines(doc: Document) -> list[str]:
    cached_text = getattr(doc, "_lines_cache_text", None)
    cached_lines = getattr(doc, "_lines_cache", None)
    if cached_text == doc.text and isinstance(cached_lines, list):
        return cached_lines
    lines = doc.text.splitlines()
    setattr(doc, "_lines_cache_text", doc.text)
    setattr(doc, "_lines_cache", lines)
    return lines


def document_clean_lines(doc: Document) -> list[str]:
    cached_text = getattr(doc, "_clean_lines_cache_text", None)
    cached_lines = getattr(doc, "_clean_lines_cache", None)
    if cached_text == doc.text and isinstance(cached_lines, list):
        return cached_lines
    lines = [sanitize_code_line(line) for line in document_lines(doc)]
    setattr(doc, "_clean_lines_cache_text", doc.text)
    setattr(doc, "_clean_lines_cache", lines)
    return lines


def document_identifiers(doc: Document) -> list[tuple[int, int, int, str, str]]:
    cached_text = getattr(doc, "_identifier_cache_text", None)
    cached_items = getattr(doc, "_identifier_cache", None)
    if cached_text == doc.text and isinstance(cached_items, list):
        return cached_items
    items = list(iter_identifiers(doc.text))
    setattr(doc, "_identifier_cache_text", doc.text)
    setattr(doc, "_identifier_cache", items)
    return items


def proc_range_for_line(doc: Document, target_line: int) -> tuple[int, int] | None:
    cached_text = getattr(doc, "_proc_range_cache_text", None)
    cached_ranges = getattr(doc, "_proc_range_cache", None)
    if cached_text == doc.text and isinstance(cached_ranges, list):
        if 0 <= target_line < len(cached_ranges):
            return cached_ranges[target_line]
        return None

    lines = document_lines(doc)
    ranges: list[tuple[int, int] | None] = [None] * len(lines)
    start_line = -1
    depth = 0
    for line_no, line in enumerate(document_clean_lines(doc)):
        decl_match = DECL_RE.match(line)
        if decl_match and decl_match.group(2) == "proc" and "{" in line:
            start_line = line_no
            depth = line.count("{") - line.count("}")
            if depth <= 0:
                ranges[line_no] = (line_no, line_no)
                start_line = -1
                depth = 0
                continue
        elif depth > 0:
            depth += line.count("{") - line.count("}")
            if depth <= 0:
                for proc_line in range(max(start_line, 0), line_no + 1):
                    ranges[proc_line] = (start_line, line_no)
                start_line = -1
                depth = 0
    setattr(doc, "_proc_range_cache_text", doc.text)
    setattr(doc, "_proc_range_cache", ranges)
    if 0 <= target_line < len(ranges):
        return ranges[target_line]
    return None


def variable_in_same_proc(doc: Document, variable: VariableSymbol, line: int) -> bool:
    if variable.scope:
        return variable.scope == proc_scope_for_line(doc, line)
    target_range = proc_range_for_line(doc, line)
    variable_range = proc_range_for_line(doc, variable.line)
    return bool(target_range and variable_range and target_range == variable_range)


def proc_scope_for_line(doc: Document, target_line: int) -> str:
    cached_text = getattr(doc, "_proc_scope_cache_text", None)
    cached_scopes = getattr(doc, "_proc_scope_cache", None)
    if cached_text == doc.text and isinstance(cached_scopes, dict) and target_line in cached_scopes:
        return cached_scopes[target_line]

    proc_symbols = getattr(doc, "_proc_symbol_cache", None)
    if getattr(doc, "_proc_symbol_cache_text", None) != doc.text or not isinstance(proc_symbols, list):
        proc_symbols = sorted(
            (symbol for symbol in doc.symbols if symbol.kind == "proc"),
            key=lambda symbol: (symbol.line, symbol.col),
        )
        setattr(doc, "_proc_symbol_cache_text", doc.text)
        setattr(doc, "_proc_symbol_cache", proc_symbols)
    current = ""
    for index, symbol in enumerate(proc_symbols):
        next_line = proc_symbols[index + 1].line if index + 1 < len(proc_symbols) else None
        if symbol.line <= target_line and (next_line is None or target_line < next_line):
            current = symbol.name
    if current:
        if cached_text != doc.text or not isinstance(cached_scopes, dict):
            cached_scopes = {}
            setattr(doc, "_proc_scope_cache_text", doc.text)
            setattr(doc, "_proc_scope_cache", cached_scopes)
        cached_scopes[target_line] = current
        return current
    proc_range = proc_range_for_line(doc, target_line)
    if not proc_range:
        if cached_text != doc.text or not isinstance(cached_scopes, dict):
            cached_scopes = {}
            setattr(doc, "_proc_scope_cache_text", doc.text)
            setattr(doc, "_proc_scope_cache", cached_scopes)
        cached_scopes[target_line] = ""
        return ""
    for symbol in proc_symbols:
        if symbol.line == proc_range[0]:
            if cached_text != doc.text or not isinstance(cached_scopes, dict):
                cached_scopes = {}
                setattr(doc, "_proc_scope_cache_text", doc.text)
                setattr(doc, "_proc_scope_cache", cached_scopes)
            cached_scopes[target_line] = symbol.name
            return symbol.name
    if cached_text != doc.text or not isinstance(cached_scopes, dict):
        cached_scopes = {}
        setattr(doc, "_proc_scope_cache_text", doc.text)
        setattr(doc, "_proc_scope_cache", cached_scopes)
    cached_scopes[target_line] = ""
    return ""


def iter_numbers(text: str):
    for line_no, raw_line in enumerate(text.splitlines()):
        line = sanitize_code_line(raw_line)
        for match in NUMBER_RE.finditer(line):
            yield line_no, match.start(), match.end()


def iter_string_literals(text: str):
    for line_no, raw_line in enumerate(text.splitlines()):
        if raw_line.lstrip().startswith("#"):
            continue
        for match in STRING_LITERAL_RE.finditer(raw_line):
            yield line_no, match.start(), match.end()


def iter_attribute_operators(text: str):
    for line_no, raw_line in enumerate(text.splitlines()):
        if raw_line.lstrip().startswith("#"):
            continue
        for match in ATTRIBUTE_OPERATOR_RE.finditer(raw_line):
            yield line_no, match.start(), match.start() + 1


def iter_field_accesses(text: str):
    for line_no, raw_line in enumerate(text.splitlines()):
        line = sanitize_code_line(raw_line)
        for match in re.finditer(r"([A-Za-z_][A-Za-z0-9_]*)(?:\s*\[[^\]]+\])?\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)", line):
            yield line_no, match.start(1), match.end(1), match.group(1), match.start(2), match.end(2), match.group(2)


def find_matching_paren(line: str, open_index: int) -> int:
    depth = 0
    for i in range(open_index, len(line)):
        ch = line[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def iter_calls(text: str):
    ident = r"[A-Za-z_][A-Za-z0-9_]*(?:<[^>\n]+>)?(?:[A-Za-z_][A-Za-z0-9_]*)?"
    call_re = re.compile(rf"({ident}(?:(?:\s*\[[^\]]+\])?\s*\.\s*[A-Za-z_][A-Za-z0-9_]*)*)\s*\(")
    for line_no, raw_line in enumerate(text.splitlines()):
        line = sanitize_code_line(raw_line)
        for match in call_re.finditer(line):
            name = match.group(1)
            if name in KEYWORDS or name in {"sizeof", "alignof", "cast"}:
                continue
            open_index = line.find("(", match.start(1))
            close_index = find_matching_paren(line, open_index)
            if close_index < 0:
                continue
            yield line_no, match.start(1), name, line[open_index + 1 : close_index]


def iter_assignments(text: str):
    assign_re = re.compile(r"(?<![=!<>+\-*/%&|^])=(?![=>])")
    for line_no, raw_line in enumerate(text.splitlines()):
        line = sanitize_code_line(raw_line)
        for match in assign_re.finditer(line):
            lhs = line[: match.start()].strip()
            rhs = line[match.end() :].strip().rstrip(";")
            if not lhs or not rhs:
                continue
            decl = VAR_RE.match(line)
            if decl and not line[: decl.start(1)].strip() and match.start() >= decl.end(2):
                yield line_no, decl.start(1), decl.group(2).strip(), rhs, True
                break
            yield line_no, len(line[: match.start()]) - len(line[: match.start()].lstrip()), lhs, rhs, False
            break


def symbol_names_match(candidate: str, target: str) -> bool:
    return (
        candidate == target
        or normalize_symbol_name(candidate) == normalize_symbol_name(target)
        or base_symbol_name(candidate) == base_symbol_name(target)
    )


def identifier_reference_ranges(ident: str, start: int, target: str) -> list[tuple[int, int]]:
    parts = generic_ident_parts(ident)
    if not parts:
        if symbol_names_match(ident, target):
            return [(start, start + len(ident))]
        return []

    if symbol_names_match(ident, target):
        return [(start, start + len(ident))]

    base, args, _tail = parts
    ranges: list[tuple[int, int]] = []
    if symbol_names_match(base, target):
        ranges.append((start, start + len(base)))

    args_start = start + len(base) + 1
    for arg_start, arg_end, arg in iter_identifier_spans(args):
        if symbol_names_match(arg, target):
            ranges.append((args_start + arg_start, args_start + arg_end))
    return ranges


def resolve_import(path: Path | None, import_path: str) -> Path | None:
    raw = Path(import_path)
    if raw.is_absolute():
        return raw
    if path:
        return (path.parent / raw).resolve()
    return raw.resolve()


def extract_path_symbols(
    uri: str,
    text: str,
    path: Path | None,
    collect_diagnostics: bool = True,
) -> tuple[list[ImportSymbol], list[CIncludeSymbol], list[Diagnostic]]:
    imports: list[ImportSymbol] = []
    cincludes: list[CIncludeSymbol] = []
    diagnostics: list[Diagnostic] = DiagnosticList(collect_diagnostics)
    for line_no, raw_line in enumerate(text.splitlines()):
        import_match = IMPORT_RE.match(raw_line)
        if import_match:
            import_path = import_match.group(1)
            if not import_path.endswith(".i"):
                diagnostics.append(
                    Diagnostic(
                        line_no,
                        import_match.start(1),
                        "parse error: import expects an .i module; use cinclude for C headers",
                    )
                )
            else:
                imported = resolve_import(path, import_path)
                if imported and not imported.exists():
                    diagnostics.append(Diagnostic(line_no, import_match.start(1), f"missing import: {import_path}"))
                elif imported:
                    imports.append(
                        ImportSymbol(
                            import_path,
                            uri,
                            line_no,
                            import_match.start(1),
                            import_match.end(1),
                            path_to_uri(imported),
                            str(imported),
                        )
                    )

        cinclude_match = CINCLUDE_RE.match(raw_line)
        if cinclude_match:
            include_path = cinclude_match.group(1)
            included = resolve_import(path, include_path)
            if included and included.exists():
                cincludes.append(
                    CIncludeSymbol(
                        include_path,
                        uri,
                        line_no,
                        cinclude_match.start(1),
                        cinclude_match.end(1),
                        path_to_uri(included),
                        str(included),
                    )
                )
    return imports, cincludes, diagnostics


def analyze_paths_only(uri: str, text: str, path: Path | None, collect_diagnostics: bool = True) -> Document:
    imports, cincludes, diagnostics = extract_path_symbols(uri, text, path, collect_diagnostics)
    return Document(uri, path, text, [], {}, {}, [], imports, cincludes, diagnostics)


def analyze(uri: str, text: str, path: Path | None, collect_diagnostics: bool = True) -> Document:
    symbols: list[Symbol] = []
    fields: dict[str, list[FieldSymbol]] = {}
    variables: dict[str, VariableSymbol] = {}
    variables_all: list[VariableSymbol] = []
    imports, cincludes, diagnostics = extract_path_symbols(uri, text, path, collect_diagnostics)
    brace_stack: list[tuple[str, int, int]] = []
    control_stack: list[tuple[str, int, int]] = []
    seen: dict[str, Symbol] = {}
    aggregate_owner = ""
    aggregate_depth = 0
    aggregate_fields_seen: dict[str, tuple[int, int]] = {}
    enum_owner = ""
    enum_depth = 0
    enum_items_seen: dict[str, tuple[int, int]] = {}
    proc_depth = 0
    proc_locals_seen: dict[str, tuple[int, int]] = {}

    for line_no, raw_line in enumerate(text.splitlines()):
        line = sanitize_code_line(raw_line)
        decl_match = DECL_RE.match(line)
        if decl_match:
            name = decl_match.group(1)
            kind = decl_match.group(2)
            detail = raw_line.strip()
            symbol = Symbol(name, kind, uri, line_no, decl_match.start(1), detail)
            symbols.append(symbol)
            key = normalize_symbol_name(name)
            if key in seen:
                prev = seen[key]
                diagnostics.append(
                    Diagnostic(
                        line_no,
                        decl_match.start(1),
                        f"duplicate declaration '{name}', previous at {prev.line + 1}:{prev.col + 1}",
                    )
                )
            else:
                seen[key] = symbol

            if kind == "proc":
                params_start = line.find("(")
                params_end = line.find(")", params_start + 1)
                seen_params: dict[str, tuple[int, int]] = {}
                if params_start >= 0 and params_end > params_start:
                    params_text = line[params_start + 1 : params_end]
                    for param_match in PARAM_RE.finditer(params_text):
                        param_name = param_match.group(1)
                        type_name = param_match.group(2).strip()
                        col = params_start + 1 + param_match.start(1)
                        previous = seen_params.get(param_name)
                        if previous:
                            diagnostics.append(
                                Diagnostic(
                                    line_no,
                                    col,
                                    f"semantic error: duplicate proc parameter '{param_name}' (previous at {previous[0] + 1}:{previous[1] + 1})",
                                )
                            )
                        else:
                            seen_params[param_name] = (line_no, col)
                        variable = VariableSymbol(
                            param_name,
                            type_name,
                            uri,
                            line_no,
                            col,
                            f"{param_name}: {type_name}",
                            "parameter",
                        )
                        variables[param_name] = variable
                        variables_all.append(variable)
                if "{" in line:
                    proc_depth = line.count("{") - line.count("}")
                    proc_locals_seen = dict(seen_params)
                    if proc_depth <= 0:
                        proc_depth = 0
                        proc_locals_seen = {}

            if kind in ("struct", "union") and "{" in line:
                aggregate_owner = name
                aggregate_depth = line.count("{") - line.count("}")
                aggregate_fields_seen = {}
                fields.setdefault(aggregate_owner, [])
                if aggregate_depth <= 0:
                    aggregate_owner = ""
                    aggregate_depth = 0
                    aggregate_fields_seen = {}

            if kind == "enum" and "{" in line:
                enum_owner = name
                enum_depth = line.count("{") - line.count("}")
                enum_items_seen = {}
                if enum_depth <= 0:
                    enum_owner = ""
                    enum_depth = 0
                    enum_items_seen = {}

        elif aggregate_owner:
            field_match = FIELD_RE.match(raw_line)
            if field_match:
                field_name = field_match.group(1)
                type_name = field_match.group(2).strip()
                attrs = field_match.group(3) or ""
                field_col = field_match.start(1)
                previous = aggregate_fields_seen.get(field_name)
                if previous:
                    diagnostics.append(
                        Diagnostic(
                            line_no,
                            field_col,
                            f"semantic error: duplicate field '{field_name}' (previous at {previous[0] + 1}:{previous[1] + 1})",
                        )
                    )
                else:
                    aggregate_fields_seen[field_name] = (line_no, field_col)
                fields.setdefault(aggregate_owner, []).append(
                    FieldSymbol(
                        aggregate_owner,
                        field_name,
                        type_name,
                        attrs,
                        uri,
                        line_no,
                        field_col,
                        f"{aggregate_owner}.{field_name}: {type_name}",
                    )
                )

        elif enum_owner:
            item_match = ENUM_ITEM_RE.match(line)
            if item_match and item_match.group(1) not in {"external"}:
                item_name = item_match.group(1)
                item_col = item_match.start(1)
                previous = enum_items_seen.get(item_name)
                if previous:
                    diagnostics.append(
                        Diagnostic(
                            line_no,
                            item_col,
                            f"semantic error: duplicate enum item '{item_name}' (previous at {previous[0] + 1}:{previous[1] + 1})",
                        )
                    )
                else:
                    enum_items_seen[item_name] = (line_no, item_col)
                generated_name = f"{enum_owner}_{item_name}"
                symbols.append(
                    Symbol(
                        generated_name,
                        "enumMember",
                        uri,
                        line_no,
                        item_col,
                        f"{enum_owner}.{item_name}: enum member",
                        len(item_name),
                    )
                )

        if not aggregate_owner and not enum_owner:
            var_match = VAR_RE.match(line)
            if var_match and not DECL_RE.match(line):
                var_name = var_match.group(1)
                type_name = var_match.group(2).strip()
                var_kind = "global" if not any(open_ch == "{" for open_ch, _line, _col in brace_stack) else "variable"
                var_col = var_match.start(1)
                if var_kind == "variable" and proc_depth > 0:
                    previous = proc_locals_seen.get(var_name)
                    if previous:
                        diagnostics.append(
                            Diagnostic(
                                line_no,
                                var_col,
                                f"semantic error: duplicate local declaration '{var_name}' (previous at {previous[0] + 1}:{previous[1] + 1})",
                            )
                        )
                    else:
                        proc_locals_seen[var_name] = (line_no, var_col)
                variable = VariableSymbol(
                    var_name,
                    type_name,
                    uri,
                    line_no,
                    var_col,
                    f"{var_name}: {type_name}",
                    var_kind,
                )
                variables[var_name] = variable
                variables_all.append(variable)

        control_keywords = {match.start(): match.group(1) for match in re.finditer(r"\b(break|continue)\b", line)}
        for col, ch in enumerate(line):
            control_keyword = control_keywords.get(col)
            if control_keyword == "break" and not any(ctx in {"loop", "switch"} for ctx, _line, _col in control_stack):
                diagnostics.append(Diagnostic(line_no, col, "semantic error: break outside loop or switch"))
            elif control_keyword == "continue" and not any(ctx == "loop" for ctx, _line, _col in control_stack):
                diagnostics.append(Diagnostic(line_no, col, "semantic error: continue outside loop"))

            if ch in "({[":
                brace_stack.append((ch, line_no, col))
                if ch == "{":
                    control_stack.append((control_context_for_open_brace(line, col), line_no, col))
            elif ch in ")}]":
                if not brace_stack:
                    diagnostics.append(Diagnostic(line_no, col, f"unmatched '{ch}'"))
                    continue
                open_ch, open_line, open_col = brace_stack.pop()
                if open_ch == "{" and control_stack:
                    control_stack.pop()
                pairs = {"(": ")", "{": "}", "[": "]"}
                if pairs[open_ch] != ch:
                    diagnostics.append(
                        Diagnostic(
                            line_no,
                            col,
                            f"mismatched '{open_ch}' from {open_line + 1}:{open_col + 1} closed by '{ch}'",
                        )
                    )

        if aggregate_owner:
            aggregate_depth += line.count("{") - line.count("}") if not decl_match else 0
            if aggregate_depth <= 0:
                aggregate_owner = ""
                aggregate_depth = 0
                aggregate_fields_seen = {}
        if enum_owner:
            enum_depth += line.count("{") - line.count("}") if not decl_match else 0
            if enum_depth <= 0:
                enum_owner = ""
                enum_depth = 0
                enum_items_seen = {}
        if proc_depth > 0:
            proc_depth += line.count("{") - line.count("}") if not decl_match else 0
            if proc_depth <= 0:
                proc_depth = 0
                proc_locals_seen = {}

    for open_ch, line_no, col in brace_stack:
        diagnostics.append(Diagnostic(line_no, col, f"unclosed '{open_ch}'"))

    doc = Document(uri, path, text, symbols, fields, variables, variables_all, imports, cincludes, diagnostics)
    setattr(doc, "_symbol_refresh_fingerprint", symbol_refresh_fingerprint(doc))
    return doc


def symbol_refresh_fingerprint(doc: Document) -> tuple:
    fields: list[tuple] = []
    for owner, owner_fields in sorted(doc.fields.items()):
        for field in owner_fields:
            fields.append((owner, field.name, field.type_name, field.attrs, field.line, field.col))
    return (
        tuple((item.path, item.line, item.col, item.target_uri) for item in doc.imports),
        tuple((item.path, item.line, item.col, item.target_uri) for item in doc.cincludes),
        tuple((symbol.kind, symbol.name, symbol.line, symbol.col, symbol.detail, symbol.source_len) for symbol in doc.symbols),
        tuple(fields),
        tuple((variable.kind, variable.name, variable.type_name, variable.line, variable.col, variable.detail) for variable in doc.variables_all),
    )


class Workspace:
    def __init__(self, collect_python_diagnostics: bool = True) -> None:
        self.collect_python_diagnostics = collect_python_diagnostics
        self.documents: dict[str, Document] = {}
        self.symbols: dict[str, Symbol] = {}
        self.fields: dict[str, list[FieldSymbol]] = {}
        self.global_variables: dict[str, VariableSymbol] = {}
        self.import_parents: dict[str, ImportSymbol] = {}
        self.duplicate_symbols: list[tuple[Symbol, Symbol]] = []
        self.duplicate_globals: list[tuple[VariableSymbol, VariableSymbol]] = []
        self.duplicate_values: list[tuple[Symbol | VariableSymbol, Symbol | VariableSymbol]] = []
        self.compiler_symbol_cache: dict[
            str,
            tuple[
                str,
                float | None,
                float | None,
                bool,
                list[Symbol],
                list[VariableSymbol],
                list[FieldSymbol],
            ],
        ] = {}
        self.compiler_workspace_symbol_cache: dict[
            str,
            tuple[
                str,
                float | None,
                float | None,
                bool,
                list[Symbol],
                list[VariableSymbol],
                list[FieldSymbol],
            ],
        ] = {}
        self.identifier_reference_index_snapshot: tuple[tuple[str, str], ...] | None = None
        self.identifier_reference_index_cache: dict[str, list[tuple[str, int, int, int]]] = {}
        self.applied_compiler_workspace_symbol_key: tuple[str, str, float | None, float | None] | None = None
        self.applied_compiler_workspace_symbol_uris: set[str] = set()
        self.index_revision = 0

    def invalidate_identifier_reference_index(self) -> None:
        self.identifier_reference_index_snapshot = None
        self.identifier_reference_index_cache = {}

    def compiler_workspace_symbol_key(self, doc: Document) -> tuple[str, str, float | None, float | None]:
        mtime: float | None = None
        if doc.path:
            try:
                mtime = doc.path.stat().st_mtime
            except OSError:
                mtime = None
        compiler_mtime: float | None = None
        try:
            compiler_mtime = I_EXE.stat().st_mtime
        except OSError:
            compiler_mtime = None
        return (doc.uri, doc.text, mtime, compiler_mtime)

    def workspace_identifier_snapshot(self) -> tuple[tuple[str, str], ...]:
        return tuple((uri, doc.text) for uri, doc in sorted(self.documents.items()))

    def identifier_reference_index(self) -> dict[str, list[tuple[str, int, int, int]]]:
        snapshot = self.workspace_identifier_snapshot()
        if self.identifier_reference_index_snapshot == snapshot:
            return self.identifier_reference_index_cache

        index: dict[str, list[tuple[str, int, int, int]]] = {}

        def add(key: str, uri: str, line: int, start: int, end: int) -> None:
            if not key:
                return
            index.setdefault(key, []).append((uri, line, start, end))

        def add_keys(keys: list[str], uri: str, line: int, start: int, end: int) -> None:
            seen: set[str] = set()
            for key in keys:
                if key in seen:
                    continue
                seen.add(key)
                add(key, uri, line, start, end)

        for doc in self.documents.values():
            for line, start, end, ident, _raw in document_identifiers(doc):
                if "<" not in ident:
                    add(ident, doc.uri, line, start, end)
                    continue
                parts = generic_ident_parts(ident)
                if parts:
                    base, args, _tail = parts
                    add_keys([ident, normalize_symbol_name(ident)], doc.uri, line, start, end)
                    add(base, doc.uri, line, start, start + len(base))
                    args_start = start + len(base) + 1
                    for arg_start, arg_end, arg in iter_identifier_spans(args):
                        add_keys(
                            [arg, normalize_symbol_name(arg), base_symbol_name(arg)],
                            doc.uri,
                            line,
                            args_start + arg_start,
                            args_start + arg_end,
                        )
                    continue
                add_keys([ident, normalize_symbol_name(ident), base_symbol_name(ident)], doc.uri, line, start, end)

        self.identifier_reference_index_snapshot = snapshot
        self.identifier_reference_index_cache = index
        return index

    def identifier_reference_locations(self, name: str) -> list[tuple[str, int, int, int]]:
        index = self.identifier_reference_index()
        keys = [name, normalize_symbol_name(name), base_symbol_name(name)]
        locations: list[tuple[str, int, int, int]] = []
        seen: set[tuple[str, int, int, int]] = set()
        for key in keys:
            for location in index.get(key, []):
                if location in seen:
                    continue
                seen.add(location)
                locations.append(location)
        return locations

    def upsert(
        self,
        uri: str,
        text: str,
        load_imports: bool = True,
        apply_compiler_symbols: bool = True,
        reindex_workspace: bool = True,
        refresh_semantic_diagnostics: bool = True,
    ) -> Document:
        trace(
            "workspace upsert "
            f"uri={uri} load_imports={load_imports} "
            f"compiler_symbols={apply_compiler_symbols} reindex={reindex_workspace} "
            f"semantic_diagnostics={refresh_semantic_diagnostics}"
        )
        self.applied_compiler_workspace_symbol_key = None
        self.applied_compiler_workspace_symbol_uris = set()
        path = uri_to_path(uri)
        doc = analyze(uri, text, path, self.collect_python_diagnostics)
        if apply_compiler_symbols:
            self.apply_compiler_symbols(doc)
        self.documents[uri] = doc
        if load_imports:
            self.load_imports(doc, set(), apply_compiler_symbols)
        if reindex_workspace:
            self.reindex(refresh_semantic_diagnostics)
        return doc

    def upsert_paths_only(self, uri: str, text: str) -> Document:
        trace(f"workspace upsert paths-only uri={uri}")
        self.applied_compiler_workspace_symbol_key = None
        self.applied_compiler_workspace_symbol_uris = set()
        path = uri_to_path(uri)
        doc = analyze_paths_only(uri, text, path, self.collect_python_diagnostics)
        self.documents[uri] = doc
        self.invalidate_identifier_reference_index()
        return doc

    def update_dirty_text(self, uri: str, text: str) -> Document:
        path = uri_to_path(uri)
        previous = self.documents.get(uri)
        if not previous:
            return self.upsert_paths_only(uri, text)
        imports, cincludes, diagnostics = extract_path_symbols(uri, text, path, self.collect_python_diagnostics)
        doc = Document(
            uri,
            path,
            text,
            list(previous.symbols),
            {owner: list(fields) for owner, fields in previous.fields.items()},
            dict(previous.variables),
            list(previous.variables_all),
            imports,
            cincludes,
            diagnostics,
        )
        self.documents[uri] = doc
        self.invalidate_identifier_reference_index()
        return doc

    def open_path(self, path: Path) -> Document:
        uri = path_to_uri(path)
        return self.upsert(uri, path.read_text(encoding="utf-8"))

    def load_imports(self, doc: Document, seen: set[str], apply_compiler_symbols: bool = True) -> None:
        if doc.uri in seen:
            return
        seen.add(doc.uri)
        for import_symbol in doc.imports:
            imported = uri_to_path(import_symbol.target_uri)
            if not imported or not imported.exists():
                continue
            uri = import_symbol.target_uri
            if uri in self.documents:
                self.load_imports(self.documents[uri], seen, apply_compiler_symbols)
                continue
            try:
                imported_doc = analyze(uri, imported.read_text(encoding="utf-8"), imported, self.collect_python_diagnostics)
            except OSError:
                continue
            if apply_compiler_symbols:
                self.apply_compiler_symbols(imported_doc)
            self.documents[uri] = imported_doc
            self.load_imports(imported_doc, seen, apply_compiler_symbols)

    def compiler_symbols(self, doc: Document) -> tuple[bool, list[Symbol], list[VariableSymbol], list[FieldSymbol]]:
        mtime: float | None = None
        if doc.path:
            try:
                mtime = doc.path.stat().st_mtime
            except OSError:
                mtime = None
        compiler_mtime: float | None = None
        try:
            compiler_mtime = I_EXE.stat().st_mtime
        except OSError:
            compiler_mtime = None
        cached = self.compiler_symbol_cache.get(doc.uri)
        if cached and cached[0] == doc.text and cached[1] == mtime and cached[2] == compiler_mtime:
            return cached[3], cached[4], cached[5], cached[6]
        available, symbols, variables, fields = run_compiler_symbols(doc)
        self.compiler_symbol_cache[doc.uri] = (doc.text, mtime, compiler_mtime, available, symbols, variables, fields)
        return available, symbols, variables, fields

    def apply_compiler_symbols(self, doc: Document) -> None:
        available, symbols, variables, fields = self.compiler_symbols(doc)
        if not available:
            return
        doc.symbols = symbols
        compiler_fields: dict[str, list[FieldSymbol]] = {}
        for field in fields:
            compiler_fields.setdefault(field.owner, []).append(field)
        doc.fields = compiler_fields
        doc.variables_all = variables
        variables: dict[str, VariableSymbol] = {}
        for variable in doc.variables_all:
            variables[variable.name] = variable
        doc.variables = variables

    def compiler_workspace_symbols(self, doc: Document) -> tuple[bool, list[Symbol], list[VariableSymbol], list[FieldSymbol]]:
        mtime: float | None = None
        if doc.path:
            try:
                mtime = doc.path.stat().st_mtime
            except OSError:
                mtime = None
        compiler_mtime: float | None = None
        try:
            compiler_mtime = I_EXE.stat().st_mtime
        except OSError:
            compiler_mtime = None
        cached = self.compiler_workspace_symbol_cache.get(doc.uri)
        if cached and cached[0] == doc.text and cached[1] == mtime and cached[2] == compiler_mtime:
            return cached[3], cached[4], cached[5], cached[6]
        available, symbols, variables, fields = run_compiler_symbols(doc, include_imports=True)
        self.cache_compiler_workspace_symbols(doc, available, symbols, variables, fields)
        return available, symbols, variables, fields

    def cache_compiler_workspace_symbols(
        self,
        doc: Document,
        available: bool,
        symbols: list[Symbol],
        variables: list[VariableSymbol],
        fields: list[FieldSymbol],
    ) -> None:
        mtime: float | None = None
        if doc.path:
            try:
                mtime = doc.path.stat().st_mtime
            except OSError:
                mtime = None
        compiler_mtime: float | None = None
        try:
            compiler_mtime = I_EXE.stat().st_mtime
        except OSError:
            compiler_mtime = None
        self.compiler_workspace_symbol_cache[doc.uri] = (doc.text, mtime, compiler_mtime, available, symbols, variables, fields)

    def apply_compiler_workspace_symbols(self, doc: Document) -> bool:
        key = self.compiler_workspace_symbol_key(doc)
        if (
            self.applied_compiler_workspace_symbol_key
            and self.applied_compiler_workspace_symbol_key[3] == key[3]
            and doc.uri in self.applied_compiler_workspace_symbol_uris
        ):
            return True
        available, symbols, variables, fields = self.compiler_workspace_symbols(doc)
        if not available:
            self.applied_compiler_workspace_symbol_key = None
            self.applied_compiler_workspace_symbol_uris = set()
            return False

        uris = {symbol.uri for symbol in symbols}
        uris.update(variable.uri for variable in variables)
        uris.update(field.uri for field in fields)
        uris.add(doc.uri)
        for uri in sorted(uris):
            if uri in self.documents:
                continue
            path = uri_to_path(uri)
            if not path or not path.exists():
                continue
            try:
                self.documents[uri] = analyze_paths_only(
                    uri,
                    path.read_text(encoding="utf-8"),
                    path,
                    self.collect_python_diagnostics,
                )
            except OSError:
                continue

        symbols_by_uri: dict[str, list[Symbol]] = {}
        variables_by_uri: dict[str, list[VariableSymbol]] = {}
        fields_by_uri: dict[str, list[FieldSymbol]] = {}
        for symbol in symbols:
            symbols_by_uri.setdefault(symbol.uri, []).append(symbol)
        for variable in variables:
            variables_by_uri.setdefault(variable.uri, []).append(variable)
        for field in fields:
            fields_by_uri.setdefault(field.uri, []).append(field)

        for uri in uris:
            target = self.documents.get(uri)
            if not target:
                continue
            target.symbols = symbols_by_uri.get(uri, [])
            target.variables_all = variables_by_uri.get(uri, [])
            target.variables = {variable.name: variable for variable in target.variables_all}
            compiler_fields: dict[str, list[FieldSymbol]] = {}
            for field in fields_by_uri.get(uri, []):
                compiler_fields.setdefault(field.owner, []).append(field)
            target.fields = compiler_fields

        self.reindex(refresh_semantic_diagnostics=False)
        self.applied_compiler_workspace_symbol_key = key
        self.applied_compiler_workspace_symbol_uris = uris
        return True

    def preserve_compiler_workspace_symbols_for_stable_edit(
        self,
        doc: Document,
        previous: Document | None,
        previous_applied_key: tuple[str, str, float | None, float | None] | None,
        previous_applied_uris: set[str],
    ) -> bool:
        if previous_applied_key is None or not previous or previous.uri != doc.uri or previous.uri not in previous_applied_uris:
            return False
        if getattr(previous, "_symbol_refresh_fingerprint", None) != getattr(doc, "_symbol_refresh_fingerprint", None):
            return False
        doc.symbols = previous.symbols
        doc.fields = previous.fields
        doc.variables_all = previous.variables_all
        doc.variables = previous.variables
        self.applied_compiler_workspace_symbol_key = self.compiler_workspace_symbol_key(doc)
        self.applied_compiler_workspace_symbol_uris = set(previous_applied_uris)
        return True

    def reindex(self, refresh_semantic_diagnostics: bool = True) -> None:
        self.index_revision += 1
        self.invalidate_identifier_reference_index()
        symbols: dict[str, Symbol] = {}
        fields: dict[str, list[FieldSymbol]] = {}
        global_variables: dict[str, VariableSymbol] = {}
        import_parents: dict[str, ImportSymbol] = {}
        duplicates: list[tuple[Symbol, Symbol]] = []
        duplicate_globals: list[tuple[VariableSymbol, VariableSymbol]] = []
        duplicate_values: list[tuple[Symbol | VariableSymbol, Symbol | VariableSymbol]] = []
        primary_symbols: dict[str, Symbol] = {}
        value_sites: dict[str, Symbol | VariableSymbol] = {}
        for doc in self.documents.values():
            for import_symbol in doc.imports:
                if import_symbol.target_uri in self.documents:
                    import_parents.setdefault(import_symbol.target_uri, import_symbol)
        for doc in self.documents.values():
            for variable in doc.variables_all:
                if variable.kind == "global":
                    key = normalize_symbol_name(variable.name)
                    previous = global_variables.get(key)
                    if previous and previous.uri != variable.uri:
                        duplicate_globals.append((variable, previous))
                    else:
                        global_variables.setdefault(key, variable)
                    previous_value = value_sites.get(key)
                    if previous_value and previous_value.uri != variable.uri:
                        duplicate_values.append((variable, previous_value))
                    else:
                        value_sites.setdefault(key, variable)
            for symbol in doc.symbols:
                key = normalize_symbol_name(symbol.name)
                previous = primary_symbols.get(key)
                if previous and previous.uri != symbol.uri:
                    duplicates.append((symbol, previous))
                else:
                    primary_symbols.setdefault(key, symbol)
                if symbol.kind == "proc":
                    previous_value = value_sites.get(key)
                    if previous_value and previous_value.uri != symbol.uri:
                        duplicate_values.append((symbol, previous_value))
                    else:
                        value_sites.setdefault(key, symbol)
                symbols.setdefault(normalize_symbol_name(symbol.name), symbol)
                base = re.match(r"[A-Za-z_][A-Za-z0-9_]*", symbol.name)
                if base:
                    symbols.setdefault(base.group(0), symbol)
            for owner, owner_fields in doc.fields.items():
                fields.setdefault(owner, []).extend(owner_fields)
                normalized_owner = normalize_symbol_name(owner)
                if normalized_owner != owner:
                    fields.setdefault(normalized_owner, []).extend(owner_fields)
        self.symbols = symbols
        self.fields = fields
        self.global_variables = global_variables
        self.import_parents = import_parents
        self.duplicate_symbols = duplicates
        self.duplicate_globals = duplicate_globals
        self.duplicate_values = duplicate_values
        if refresh_semantic_diagnostics:
            self.refresh_semantic_diagnostics()

    def import_chain_for_uri(self, uri: str) -> str:
        parts: list[str] = []
        seen: set[str] = set()
        current = uri
        while current not in seen:
            seen.add(current)
            edge = self.import_parents.get(current)
            if not edge:
                break
            parent_path = uri_to_path(edge.uri)
            parent_label = str(parent_path) if parent_path else edge.uri
            parts.append(f"{parent_label}:{edge.line + 1}:{edge.col + 1} -> {edge.path}")
            current = edge.uri
        parts.reverse()
        return " -> ".join(parts)

    def import_context_note_for_uri(self, uri: str, label: str = "imported through") -> str:
        chain = self.import_chain_for_uri(uri)
        return f"; {label}: {chain}" if chain else ""

    def refresh_semantic_diagnostics(self) -> None:
        for doc in self.documents.values():
            doc.diagnostics = [
                diag
                for diag in doc.diagnostics
                if not diag.message.startswith("type error:") and not diag.message.startswith("module error:")
            ]
        for symbol, previous in self.duplicate_symbols:
            doc = self.documents.get(symbol.uri)
            if not doc:
                continue
            previous_path = uri_to_path(previous.uri)
            previous_label = str(previous_path) if previous_path else previous.uri
            import_note = self.import_context_note_for_uri(symbol.uri)
            previous_import_note = self.import_context_note_for_uri(previous.uri, "previous imported through")
            doc.diagnostics.append(
                Diagnostic(
                    symbol.line,
                    symbol.col,
                    f"module error: duplicate declaration '{symbol.name}', previous at {previous_label}:{previous.line + 1}:{previous.col + 1}{import_note}{previous_import_note}",
                )
            )
        for variable, previous in self.duplicate_globals:
            doc = self.documents.get(variable.uri)
            if not doc:
                continue
            previous_path = uri_to_path(previous.uri)
            previous_label = str(previous_path) if previous_path else previous.uri
            import_note = self.import_context_note_for_uri(variable.uri)
            previous_import_note = self.import_context_note_for_uri(previous.uri, "previous imported through")
            doc.diagnostics.append(
                Diagnostic(
                    variable.line,
                    variable.col,
                    f"module error: duplicate global declaration '{variable.name}', previous at {previous_label}:{previous.line + 1}:{previous.col + 1}{import_note}{previous_import_note}",
                )
            )
        for value, previous in self.duplicate_values:
            doc = self.documents.get(value.uri)
            if not doc:
                continue
            previous_path = uri_to_path(previous.uri)
            previous_label = str(previous_path) if previous_path else previous.uri
            import_note = self.import_context_note_for_uri(value.uri)
            previous_import_note = self.import_context_note_for_uri(previous.uri, "previous imported through")
            doc.diagnostics.append(
                Diagnostic(
                    value.line,
                    value.col,
                    f"module error: duplicate value declaration '{value.name}', previous at {previous_label}:{previous.line + 1}:{previous.col + 1}{import_note}{previous_import_note}",
                )
            )
        for doc in self.documents.values():
            for line, field_start, owner, field_name in iter_missing_field_accesses(self, doc):
                doc.diagnostics.append(
                    Diagnostic(
                        line,
                        field_start,
                        f"type error: type '{owner}' has no field '{field_name}'",
                    )
                )
            for line, call_start, name, arg_text in iter_calls(doc.text):
                params = callable_parameter_labels_for_name(self, doc, name, line)
                if not params:
                    continue
                variadic = bool(params and params[-1] == "...")
                required = len(params) - 1 if variadic else len(params)
                args = split_top_level_comma_spans(arg_text) if arg_text.strip() else []
                actual = len(args)
                if actual < required or (not variadic and actual != required):
                    expectation = f"at least {required}" if variadic else str(required)
                    doc.diagnostics.append(
                        Diagnostic(
                            line,
                            call_start,
                            f"type error: call '{name}' expects {expectation} args, got {actual}",
                        )
                    )
                    continue
                for index, (arg, arg_offset) in enumerate(args[: len(params)]):
                    expected = proc_parameter_type(params[index])
                    actual_type = infer_simple_expr_type(self, doc, arg, line)
                    if not expected or not actual_type:
                        continue
                    if type_assignment_compatible(expected, actual_type):
                        continue
                    doc.diagnostics.append(
                        Diagnostic(
                            line,
                            call_start + len(name) + 1 + arg_offset,
                            f"type error: call '{name}' arg {index + 1} expects '{canonical_type(expected)}', got '{canonical_type(actual_type)}'",
                        )
                    )
            for line, assign_start, lhs, rhs, is_decl in iter_assignments(doc.text):
                expected = lhs if is_decl else infer_simple_expr_type(self, doc, lhs, line)
                actual = infer_simple_expr_type(self, doc, rhs, line)
                if not expected or not actual:
                    continue
                if type_assignment_compatible(expected, actual):
                    continue
                target = "declaration" if is_decl else lhs.strip()
                doc.diagnostics.append(
                    Diagnostic(
                        line,
                        assign_start,
                        f"type error: cannot assign '{canonical_type(actual)}' to '{canonical_type(expected)}' in {target}",
                    )
                )

    def find_symbol(self, name: str) -> Symbol | None:
        return self.symbols.get(normalize_symbol_name(name)) or self.symbols.get(name)

    def find_enum_member_usage(self, name: str) -> Symbol | None:
        for symbol in self.all_symbols():
            if symbol.kind == "enumMember" and enum_member_usage_name(symbol) == name:
                return symbol
        return None

    def fields_for_owner(self, owner: str) -> list[FieldSymbol]:
        return self.fields.get(normalize_type_name(owner), []) or self.fields.get(owner, [])

    def find_field(self, owner: str, name: str) -> FieldSymbol | None:
        for field in self.fields_for_owner(owner):
            if field.name == name:
                return field
        return None

    def find_variable(self, doc: Document, name: str) -> VariableSymbol | None:
        local = doc.variables.get(name)
        if local and local.kind == "global":
            return local
        global_var = self.global_variables.get(normalize_symbol_name(name))
        return global_var or local

    def find_variable_at(self, doc: Document, name: str, line: int) -> VariableSymbol | None:
        for variable in reversed(doc.variables_all):
            if variable.name == name and variable.kind in ("variable", "parameter") and variable_in_same_proc(doc, variable, line):
                return variable
        return self.find_variable(doc, name)

    def all_symbols(self) -> list[Symbol]:
        out: list[Symbol] = []
        for doc in self.documents.values():
            out.extend(doc.symbols)
        return out

    def workspace_symbols(self, query: str) -> list[Symbol | VariableSymbol]:
        query = query.strip().lower()
        out: list[Symbol | VariableSymbol] = []
        seen: set[tuple[str, str, int, int, str]] = set()
        for symbol in self.all_symbols():
            if query and query not in symbol.name.lower() and query not in symbol.detail.lower():
                continue
            key = (symbol.uri, symbol.name, symbol.line, symbol.col, symbol.kind)
            if key in seen:
                continue
            seen.add(key)
            out.append(symbol)
        for variable in self.global_variables.values():
            if query and query not in variable.name.lower() and query not in variable.detail.lower():
                continue
            key = (variable.uri, variable.name, variable.line, variable.col, variable.kind)
            if key in seen:
                continue
            seen.add(key)
            out.append(variable)
        out.sort(key=lambda symbol: (symbol.name.lower(), symbol.uri, symbol.line, symbol.col))
        return out

    def completion_symbols_for_doc(self, doc: Document | None) -> list[VariableSymbol | Symbol]:
        out: list[VariableSymbol | Symbol] = []
        seen: set[str] = set()
        if doc:
            for variable in doc.variables.values():
                key = f"var:{variable.name}"
                if key not in seen:
                    seen.add(key)
                    out.append(variable)
        for variable in self.global_variables.values():
            key = f"var:{variable.name}"
            if key in seen:
                continue
            seen.add(key)
            out.append(variable)
        for symbol in self.all_symbols():
            key = f"sym:{normalize_symbol_name(symbol.name)}"
            if key in seen:
                continue
            seen.add(key)
            out.append(symbol)
        return out

    def completion_symbols_at(self, doc: Document | None, line: int) -> list[VariableSymbol | Symbol]:
        out: list[VariableSymbol | Symbol] = []
        seen: set[str] = set()
        if doc:
            current_scope = proc_scope_for_line(doc, line)
            current_range = proc_range_for_line(doc, line)
            for variable in doc.variables_all:
                if variable.kind not in ("variable", "parameter"):
                    continue
                if variable.scope:
                    if variable.scope != current_scope:
                        continue
                else:
                    variable_range = proc_range_for_line(doc, variable.line)
                    if not current_range or not variable_range or current_range != variable_range:
                        continue
                key = f"var:{variable.name}"
                if key in seen:
                    continue
                seen.add(key)
                out.append(variable)
        for variable in self.global_variables.values():
            key = f"var:{variable.name}"
            if key in seen:
                continue
            seen.add(key)
            out.append(variable)
        for symbol in self.all_symbols():
            key = f"sym:{normalize_symbol_name(symbol.name)}"
            if key in seen:
                continue
            seen.add(key)
            out.append(symbol)
        return out

    def references(self, name: str) -> list[dict]:
        return [
            {"uri": uri, "range": position_to_lsp(line, start, end)}
            for uri, line, start, end in self.identifier_reference_locations(name)
        ]

    def rename_edits(self, name: str, new_name: str) -> dict:
        changes: dict[str, list[dict]] = {}
        for uri, line, start, end in self.identifier_reference_locations(name):
            changes.setdefault(uri, []).append({"range": position_to_lsp(line, start, end), "newText": new_name})
        return {"changes": changes}

    def variable_references(self, target: VariableSymbol) -> list[dict]:
        if target.kind == "global":
            locations: list[dict] = []
            for uri, line, start, end in self.identifier_reference_locations(target.name):
                doc = self.documents.get(uri)
                if not doc:
                    continue
                resolved = self.find_variable_at(doc, target.name, line)
                if resolved and resolved.kind == "global" and resolved.name == target.name:
                    locations.append({"uri": uri, "range": position_to_lsp(line, start, end)})
            return locations
        doc = self.documents.get(target.uri)
        proc_range = proc_range_for_line(doc, target.line) if doc else None
        if not doc or not proc_range:
            return [{"uri": target.uri, "range": position_to_lsp(target.line, target.col, target.col + len(target.name))}]
        locations: list[dict] = []
        for line, start, end, ident, _raw in document_identifiers(doc):
            if proc_range[0] <= line <= proc_range[1] and ident == target.name:
                locations.append({"uri": doc.uri, "range": position_to_lsp(line, start, end)})
        return locations

    def variable_rename_edits(self, target: VariableSymbol, new_name: str) -> dict:
        if target.kind == "global":
            changes: dict[str, list[dict]] = {}
            for uri, line, start, end in self.identifier_reference_locations(target.name):
                doc = self.documents.get(uri)
                if not doc:
                    continue
                resolved = self.find_variable_at(doc, target.name, line)
                if resolved and resolved.kind == "global" and resolved.name == target.name:
                    changes.setdefault(uri, []).append({"range": position_to_lsp(line, start, end), "newText": new_name})
            return {"changes": changes}
        changes: dict[str, list[dict]] = {}
        doc = self.documents.get(target.uri)
        proc_range = proc_range_for_line(doc, target.line) if doc else None
        if not doc or not proc_range:
            return {"changes": changes}
        edits: list[dict] = []
        for line, start, end, ident, _raw in document_identifiers(doc):
            if proc_range[0] <= line <= proc_range[1] and ident == target.name:
                edits.append({"range": position_to_lsp(line, start, end), "newText": new_name})
        if edits:
            changes[doc.uri] = edits
        return {"changes": changes}

    def field_references(self, target: FieldSymbol) -> list[dict]:
        locations: list[dict] = []
        for doc in self.documents.values():
            for owner_fields in doc.fields.values():
                for field in owner_fields:
                    if field.owner == target.owner and field.name == target.name:
                        locations.append(
                            {
                                "uri": field.uri,
                                "range": position_to_lsp(field.line, field.col, field.col + len(field.name)),
                            }
                        )
        for uri, line, start, end in self.identifier_reference_locations(target.name):
            doc = self.documents.get(uri)
            if not doc:
                continue
            field = field_access_at(self, doc, line, start)
            if field and field.owner == target.owner and field.name == target.name:
                locations.append({"uri": uri, "range": position_to_lsp(line, start, end)})
        return locations

    def field_rename_edits(self, target: FieldSymbol, new_name: str) -> dict:
        changes: dict[str, list[dict]] = {}
        for doc in self.documents.values():
            for owner_fields in doc.fields.values():
                for field in owner_fields:
                    if field.owner == target.owner and field.name == target.name:
                        changes.setdefault(field.uri, []).append(
                            {
                                "range": position_to_lsp(field.line, field.col, field.col + len(field.name)),
                                "newText": new_name,
                            }
                        )
        for uri, line, start, end in self.identifier_reference_locations(target.name):
            doc = self.documents.get(uri)
            if not doc:
                continue
            field = field_access_at(self, doc, line, start)
            if field and field.owner == target.owner and field.name == target.name:
                changes.setdefault(uri, []).append({"range": position_to_lsp(line, start, end), "newText": new_name})
        return {"changes": changes}

    def enum_member_references(self, target: Symbol) -> list[dict]:
        owner, item = enum_member_parts(target)
        if not owner or not item:
            return self.references(target.name)
        usage_name = enum_member_usage_name(target)
        locations: list[dict] = [
            {
                "uri": target.uri,
                "range": position_to_lsp(target.line, target.col, target.col + len(item)),
            }
        ]
        for uri, line, start, end in self.identifier_reference_locations(usage_name):
            locations.append({"uri": uri, "range": position_to_lsp(line, start, end)})
        return locations

    def enum_member_rename_edits(self, target: Symbol, new_name: str) -> dict:
        owner, item = enum_member_parts(target)
        if not owner or not item:
            return self.rename_edits(target.name, new_name)
        new_item = new_name
        prefix = f"{owner}_"
        if new_item.startswith(prefix):
            new_item = new_item[len(prefix) :]
        old_usage_name = enum_member_usage_name(target)
        new_usage_name = f"{owner}_{new_item}"
        changes: dict[str, list[dict]] = {
            target.uri: [
                {
                    "range": position_to_lsp(target.line, target.col, target.col + len(item)),
                    "newText": new_item,
                }
            ]
        }
        for uri, line, start, end in self.identifier_reference_locations(old_usage_name):
            changes.setdefault(uri, []).append({"range": position_to_lsp(line, start, end), "newText": new_usage_name})
        return {"changes": changes}


def diagnostic_to_lsp(diag: Diagnostic) -> dict:
    end_line = diag.end_line if diag.end_line is not None else diag.line
    end_col = diag.end_col if diag.end_col is not None else diag.col + 1
    if end_line < diag.line or (end_line == diag.line and end_col <= diag.col):
        end_line = diag.line
        end_col = diag.col + 1
    return {
        "range": {
            "start": {"line": diag.line, "character": diag.col},
            "end": {"line": end_line, "character": end_col},
        },
        "severity": diag.severity,
        "source": diag.source,
        "message": diag.message,
    }


def compiler_diag_path(value: object) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return Path(value).resolve()
    except OSError:
        return None


def compiler_diag_to_lsp_diag(doc: Document, item: object) -> Diagnostic | None:
    if not isinstance(item, dict):
        return None
    item_uri = ""
    if doc.path:
        item_path = compiler_diag_path(item.get("file"))
        if item_path:
            item_uri = path_to_uri(item_path)
    try:
        line = max(int(item.get("line", 1)) - 1, 0)
        col = max(int(item.get("column", 1)) - 1, 0)
    except (TypeError, ValueError):
        line = 0
        col = 0
    end_line = line
    end_col = col + 1
    try:
        if "end_line" in item:
            end_line = max(int(item.get("end_line", line + 1)) - 1, 0)
        if "end_column" in item:
            end_col = max(int(item.get("end_column", col + 2)) - 1, 0)
    except (TypeError, ValueError):
        end_line = line
        end_col = col + 1
    if end_line < line or (end_line == line and end_col <= col):
        end_line = line
        end_col = col + 1
    message = item.get("message", "compiler diagnostic")
    if not isinstance(message, str):
        message = str(message)
    notes = item.get("notes", [])
    if isinstance(notes, list):
        note_messages = [
            note.get("message", "")
            for note in notes
            if isinstance(note, dict) and isinstance(note.get("message"), str) and note.get("message")
        ]
        if note_messages:
            message = message + "\n" + "\n".join(f"note: {note}" for note in note_messages)
    severity = 2 if item.get("severity") == "warning" else 1
    return Diagnostic(line, col, message, severity, end_line, end_col, "I", item_uri)


def compiler_diag_items_to_lsp_diags(doc: Document, items: object) -> list[Diagnostic]:
    if not isinstance(items, list):
        return []
    diagnostics: list[Diagnostic] = []
    for item in items:
        diag = compiler_diag_to_lsp_diag(doc, item)
        if diag:
            diagnostics.append(diag)
    return diagnostics


def run_compiler_diagnostics(doc: Document) -> tuple[bool, list[Diagnostic]]:
    if not doc.path or doc.path.suffix != ".i" or not I_EXE.exists():
        return False, []
    entry_path = project_entry_for_doc(doc) or doc.path
    doc_path = resolved_path(doc.path)
    try:
        result = subprocess.run(
            [str(I_EXE), "check", str(entry_path), "--diagnostics=json", "--stdin-path", str(doc_path)],
            cwd=ROOT,
            input=doc.text,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False, []
    output = result.stdout.strip()
    if not output:
        return True, []
    try:
        items = json.loads(output)
    except json.JSONDecodeError:
        return False, []
    if not isinstance(items, list):
        return False, []
    trace(f"compiler diagnostics entry={entry_path} dirty={doc_path}")
    return True, compiler_diag_items_to_lsp_diags(doc, items)


def compiler_symbol_to_lsp_symbol(
    doc: Document,
    item: object,
    include_imports: bool = False,
    file_uri_cache: dict[str, tuple[Path, str]] | None = None,
    doc_resolved: Path | None = None,
) -> Symbol | VariableSymbol | FieldSymbol | None:
    if not isinstance(item, dict):
        return None
    symbol_uri = doc.uri
    if doc.path:
        item_path: Path | None = None
        item_uri = ""
        raw_file = item.get("file")
        if isinstance(raw_file, str) and raw_file:
            if file_uri_cache is not None and raw_file in file_uri_cache:
                item_path, item_uri = file_uri_cache[raw_file]
            else:
                item_path = compiler_diag_path(raw_file)
                item_uri = item_path.as_uri() if item_path else ""
                if file_uri_cache is not None and item_path:
                    file_uri_cache[raw_file] = (item_path, item_uri)
        target_path = doc_resolved if doc_resolved is not None else doc.path.resolve()
        if item_path and item_path != target_path and not include_imports:
            return None
        if item_path and include_imports:
            symbol_uri = item_uri
    name = item.get("name", "")
    kind = item.get("kind", "")
    detail = item.get("detail", "")
    if not isinstance(name, str) or not name or not isinstance(kind, str) or not isinstance(detail, str):
        return None
    try:
        line = max(int(item.get("line", 1)) - 1, 0)
        col = max(int(item.get("column", 1)) - 1, 0)
        source_len = max(int(item.get("source_len", len(name))), 0)
    except (TypeError, ValueError):
        line = 0
        col = 0
        source_len = len(name)
    if kind == "field":
        owner = item.get("owner", "")
        attrs = item.get("attrs", "")
        if not isinstance(owner, str) or not owner:
            return None
        if not isinstance(attrs, str):
            attrs = ""
        raw_type = item.get("type", "")
        type_name = raw_type if isinstance(raw_type, str) and raw_type else detail.partition(":")[2].strip() if ":" in detail else ""
        raw_type_param = item.get("type_param", "")
        type_param = raw_type_param if isinstance(raw_type_param, str) else ""
        return FieldSymbol(owner, name, type_name, attrs, symbol_uri, line, col, detail, type_param)
    if kind in ("global", "parameter", "variable"):
        raw_type = item.get("type", "")
        type_name = raw_type if isinstance(raw_type, str) and raw_type else detail.partition(":")[2].strip() if ":" in detail else ""
        raw_scope = item.get("scope", "")
        scope = raw_scope if isinstance(raw_scope, str) else ""
        return VariableSymbol(name, type_name, symbol_uri, line, col, detail, kind, scope)
    params: list[str] = []
    raw_params = item.get("params")
    if isinstance(raw_params, list):
        for raw_param in raw_params:
            if not isinstance(raw_param, dict):
                continue
            param_name = raw_param.get("name", "")
            param_type = raw_param.get("type", "")
            if isinstance(param_name, str) and param_name and isinstance(param_type, str) and param_type:
                params.append(f"{param_name}:{param_type}")
    variadic = bool(item.get("variadic")) if kind == "proc" else False
    if variadic:
        params.append("...")
    return_type = item.get("return_type", "")
    if not isinstance(return_type, str):
        return_type = ""
    target_type = item.get("target_type", "")
    if not isinstance(target_type, str):
        target_type = ""
    enum_owner = ""
    enum_item = ""
    if kind == "enumMember":
        raw_owner = item.get("owner", "")
        raw_item = item.get("item", "")
        if isinstance(raw_owner, str):
            enum_owner = raw_owner
        if isinstance(raw_item, str):
            enum_item = raw_item
    raw_type_param = item.get("type_param", "")
    type_param = raw_type_param if isinstance(raw_type_param, str) else ""
    raw_generic_pattern = item.get("generic_pattern", "")
    generic_pattern = raw_generic_pattern if isinstance(raw_generic_pattern, str) else ""
    return Symbol(
        name,
        kind,
        symbol_uri,
        line,
        col,
        detail,
        source_len,
        tuple(params),
        return_type,
        variadic,
        target_type,
        enum_owner,
        enum_item,
        type_param,
        generic_pattern,
    )


def compiler_symbol_items_to_lsp_symbols(
    doc: Document,
    items: object,
    include_imports: bool = False,
) -> tuple[list[Symbol], list[VariableSymbol], list[FieldSymbol]]:
    if not isinstance(items, list):
        return [], [], []
    symbols: list[Symbol] = []
    globals_: list[VariableSymbol] = []
    fields: list[FieldSymbol] = []
    file_uri_cache: dict[str, tuple[Path, str]] = {}
    doc_resolved: Path | None = None
    if doc.path:
        try:
            doc_resolved = doc.path.resolve()
        except OSError:
            doc_resolved = None
    for item in items:
        symbol = compiler_symbol_to_lsp_symbol(doc, item, include_imports, file_uri_cache, doc_resolved)
        if isinstance(symbol, VariableSymbol):
            globals_.append(symbol)
        elif isinstance(symbol, FieldSymbol):
            fields.append(symbol)
        elif isinstance(symbol, Symbol):
            symbols.append(symbol)
    return symbols, globals_, fields


def run_compiler_symbols(doc: Document, include_imports: bool = False) -> tuple[bool, list[Symbol], list[VariableSymbol], list[FieldSymbol]]:
    if not doc.path or doc.path.suffix != ".i" or not I_EXE.exists():
        return False, [], [], []
    entry_path = project_entry_for_doc(doc) or doc.path
    doc_path = resolved_path(doc.path)
    try:
        result = subprocess.run(
            [str(I_EXE), "symbols", str(entry_path), "--stdin-path", str(doc_path)],
            cwd=ROOT,
            input=doc.text,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False, [], [], []
    if result.returncode != 0:
        return False, [], [], []
    output = result.stdout.strip()
    if not output:
        return True, [], [], []
    try:
        items = json.loads(output)
    except json.JSONDecodeError:
        return False, [], [], []
    if not isinstance(items, list):
        return False, [], [], []
    trace(f"compiler symbols entry={entry_path} dirty={doc_path} include_imports={include_imports}")
    symbols, globals_, fields = compiler_symbol_items_to_lsp_symbols(doc, items, include_imports)
    return True, symbols, globals_, fields


def run_compiler_lsp(
    doc: Document,
) -> tuple[bool, list[Diagnostic], bool, list[Symbol], list[VariableSymbol], list[FieldSymbol]]:
    if not doc.path or doc.path.suffix != ".i" or not I_EXE.exists():
        return False, [], False, [], [], []
    entry_path = project_entry_for_doc(doc) or doc.path
    doc_path = resolved_path(doc.path)
    try:
        result = subprocess.run(
            [str(I_EXE), "lsp", str(entry_path), "--stdin-path", str(doc_path)],
            cwd=ROOT,
            input=doc.text,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False, [], False, [], [], []
    output = result.stdout.strip()
    if not output:
        return result.returncode == 0, [], False, [], [], []
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return False, [], False, [], [], []
    if isinstance(payload, list):
        return True, compiler_diag_items_to_lsp_diags(doc, payload), False, [], [], []
    if not isinstance(payload, dict):
        return False, [], False, [], [], []
    diagnostics = compiler_diag_items_to_lsp_diags(doc, payload.get("diagnostics", []))
    symbols, globals_, fields = compiler_symbol_items_to_lsp_symbols(doc, payload.get("symbols", []), True)
    return True, diagnostics, True, symbols, globals_, fields


def symbol_range_for_children(symbol: Symbol, children: list[dict]) -> dict:
    source_len = symbol.source_len if symbol.source_len > 0 else len(symbol.name)
    end_line = symbol.line
    end_char = symbol.col + max(1, source_len)
    if children:
        last = children[-1]["range"]["end"]
        end_line = max(end_line, int(last["line"]))
        if int(last["line"]) >= symbol.line:
            end_char = int(last["character"])
    return {
        "start": {"line": symbol.line, "character": symbol.col},
        "end": {"line": end_line, "character": end_char},
    }


def field_symbol_to_lsp(field: FieldSymbol) -> dict:
    return {
        "name": field.name,
        "kind": 8,
        "detail": field.type_name,
        "range": {
            "start": {"line": field.line, "character": field.col},
            "end": {"line": field.line, "character": field.col + len(field.name)},
        },
        "selectionRange": {
            "start": {"line": field.line, "character": field.col},
            "end": {"line": field.line, "character": field.col + len(field.name)},
        },
    }


def enum_member_symbol_to_lsp(symbol: Symbol) -> dict:
    owner, item = enum_member_parts(symbol)
    display = item or symbol.name
    source_len = symbol.source_len if symbol.source_len > 0 else len(display)
    return {
        "name": display,
        "kind": SYMBOL_KIND.get(symbol.kind, 22),
        "detail": symbol.name,
        "range": {
            "start": {"line": symbol.line, "character": symbol.col},
            "end": {"line": symbol.line, "character": symbol.col + max(1, source_len)},
        },
        "selectionRange": {
            "start": {"line": symbol.line, "character": symbol.col},
            "end": {"line": symbol.line, "character": symbol.col + max(1, source_len)},
        },
    }


def symbol_to_lsp(symbol: Symbol, doc: Document | None = None) -> dict:
    source_len = symbol.source_len if symbol.source_len > 0 else len(symbol.name)
    children: list[dict] = []
    if doc and symbol.kind in ("struct", "union"):
        children = [field_symbol_to_lsp(field) for field in doc.fields.get(symbol.name, [])]
    elif doc and symbol.kind == "enum":
        children = [
            enum_member_symbol_to_lsp(child)
            for child in doc.symbols
            if child.kind == "enumMember" and enum_member_parts(child)[0] == symbol.name
        ]
    symbol_range = symbol_range_for_children(symbol, children)
    out = {
        "name": symbol.name,
        "kind": SYMBOL_KIND.get(symbol.kind, 13),
        "detail": symbol.kind,
        "range": symbol_range,
        "selectionRange": {
            "start": {"line": symbol.line, "character": symbol.col},
            "end": {"line": symbol.line, "character": symbol.col + max(1, source_len)},
        },
    }
    if children:
        out["children"] = children
    return out

def location_to_lsp(symbol: Symbol | FieldSymbol | VariableSymbol) -> dict:
    source_len = symbol.source_len if isinstance(symbol, Symbol) and symbol.source_len > 0 else len(symbol.name)
    return {
        "uri": symbol.uri,
        "range": {
            "start": {"line": symbol.line, "character": symbol.col},
            "end": {"line": symbol.line, "character": symbol.col + max(1, source_len)},
        },
    }


def workspace_symbol_to_lsp(symbol: Symbol | VariableSymbol) -> dict:
    return {
        "name": symbol.name,
        "kind": SYMBOL_KIND.get(symbol.kind, 13) if isinstance(symbol, Symbol) else 13,
        "location": location_to_lsp(symbol),
        "containerName": symbol.kind if isinstance(symbol, Symbol) else "global",
    }


def import_location_to_lsp(symbol: ImportSymbol) -> dict:
    return {
        "uri": symbol.target_uri,
        "range": {
            "start": {"line": 0, "character": 0},
            "end": {"line": 0, "character": 0},
        },
    }


def cinclude_location_to_lsp(symbol: CIncludeSymbol) -> dict:
    return {
        "uri": symbol.target_uri,
        "range": {
            "start": {"line": 0, "character": 0},
            "end": {"line": 0, "character": 0},
        },
    }


def import_hover_markdown(symbol: ImportSymbol) -> str:
    return f"`import \"{symbol.path}\"`\n\nresolves to `{symbol.target_path}`"


def cinclude_hover_markdown(symbol: CIncludeSymbol) -> str:
    return f"`cinclude \"{symbol.path}\"`\n\nresolves to `{symbol.target_path}`"


def completion_filter_text(name: str) -> str:
    return re.sub(r"<[^>\n]*>", "", name)


def completion_data(kind: str, name: str, uri: str, line: int, col: int) -> dict:
    return {
        "kind": kind,
        "name": name,
        "uri": uri,
        "line": line,
        "character": col,
    }


def symbol_completion_detail(symbol: Symbol) -> str:
    if symbol.kind == "proc" and proc_symbol_has_signature_metadata(symbol):
        return proc_signature_label_for_symbol(symbol, symbol.name)
    return symbol.detail


def snippet_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("$", "\\$").replace("}", "\\}")


def proc_placeholder_label(param_label: str) -> str:
    param_label = param_label.strip()
    if not param_label:
        return "arg"
    if param_label == "...":
        return "..."
    name, sep, type_name = param_label.partition(":")
    if sep and name.strip():
        return name.strip()
    return type_name.strip() if sep else param_label


def proc_snippet_text(symbol: Symbol, call_name: str, display_name: str | None = None) -> str:
    name = display_name if display_name is not None else call_name
    params = proc_parameter_labels_for_symbol(symbol, call_name)
    if not params:
        return f"{snippet_escape(name)}()$0"
    parts: list[str] = []
    for index, param in enumerate(params, start=1):
        parts.append(f"${{{index}:{snippet_escape(proc_placeholder_label(param))}}}")
    return f"{snippet_escape(name)}({', '.join(parts)})$0"


def completion_to_lsp(workspace: Workspace, symbol: Symbol, include_documentation: bool = True) -> dict:
    item = {
        "label": symbol.name,
        "kind": COMPLETION_KIND.get(symbol.kind, 6),
        "detail": symbol_completion_detail(symbol),
        "sortText": f"{symbol.kind}:{symbol.name}",
        "data": completion_data(symbol.kind, symbol.name, symbol.uri, symbol.line, symbol.col),
    }
    if include_documentation:
        item["documentation"] = {"kind": "markdown", "value": hover_markdown_for_symbol(workspace, symbol)}
    filter_text = completion_filter_text(symbol.name)
    if filter_text != symbol.name:
        item["filterText"] = filter_text
        item["insertText"] = symbol.name
    if symbol.kind == "proc":
        item["insertTextFormat"] = 2
        item["insertText"] = proc_snippet_text(symbol, symbol.name)
    return item


def variable_completion_to_lsp(workspace: Workspace, symbol: VariableSymbol, include_documentation: bool = True) -> dict:
    item = {
        "label": symbol.name,
        "kind": 6,
        "detail": symbol.detail,
        "sortText": f"{symbol.kind}:{symbol.name}",
        "data": completion_data(symbol.kind, symbol.name, symbol.uri, symbol.line, symbol.col),
    }
    if include_documentation:
        item["documentation"] = {"kind": "markdown", "value": hover_markdown_for_symbol(workspace, symbol)}
    return item


def field_completion_to_lsp(workspace: Workspace, field: FieldSymbol, include_documentation: bool = True) -> dict:
    item = {
        "label": field.name,
        "kind": 5,
        "detail": field.detail,
        "sortText": f"field:{field.name}",
        "data": completion_data("field", f"{field.owner}.{field.name}", field.uri, field.line, field.col),
    }
    if include_documentation:
        item["documentation"] = {"kind": "markdown", "value": hover_markdown_for_symbol(workspace, field)}
    return item


def reflect_field_completion_to_lsp(workspace: Workspace, field: FieldSymbol, include_documentation: bool = True) -> dict:
    item = {
        "label": field.name,
        "kind": 5,
        "detail": field.detail,
        "sortText": f"reflect:{field.name}",
        "data": completion_data("reflectField", f"{field.owner}.{field.name}", field.uri, field.line, field.col),
    }
    if include_documentation:
        item["documentation"] = {"kind": "markdown", "value": hover_markdown_for_symbol(workspace, field)}
    return item


def argument_completion_item(variable: VariableSymbol, expected_type: str, insert_text: str, detail: str, rank: int) -> dict:
    return {
        "label": insert_text,
        "kind": 6,
        "detail": detail,
        "documentation": {
            "kind": "markdown",
            "value": f"Value for expected type `{expected_type}` from `{variable.detail}`.",
        },
        "insertText": insert_text,
        "sortText": f"arg:{rank:02d}:{variable.name}",
        "data": completion_data(variable.kind, variable.name, variable.uri, variable.line, variable.col),
    }


def proc_argument_completions_at(workspace: Workspace, doc: Document | None, line: int, col: int) -> list[dict]:
    if not doc:
        return []
    signature = signature_help_at(workspace, doc, line, col)
    if not signature:
        return []
    signatures = signature.get("signatures", [])
    if not signatures:
        return []
    parameters = signatures[0].get("parameters", [])
    if not parameters:
        return []
    active = int(signature.get("activeParameter", 0))
    if active < 0 or active >= len(parameters):
        return []
    label = parameters[active].get("label", "")
    if not isinstance(label, str) or ":" not in label:
        return []
    expected_type = label.split(":", 1)[1].strip()
    expected_canon = canonical_type(expected_type)
    if not expected_canon:
        return []
    items: list[dict] = []
    seen: set[str] = set()
    for symbol in workspace.completion_symbols_at(doc, line):
        if not isinstance(symbol, VariableSymbol):
            continue
        actual_type = canonical_type(symbol.type_name)
        if type_assignment_compatible(expected_type, symbol.type_name):
            insert_text = symbol.name
            if insert_text in seen:
                continue
            seen.add(insert_text)
            items.append(argument_completion_item(symbol, expected_type, insert_text, symbol.detail, 0))
            continue
        if expected_canon.startswith("*") and actual_type == expected_canon[1:]:
            insert_text = f"{symbol.name}.&"
            if insert_text in seen:
                continue
            seen.add(insert_text)
            detail = f"{symbol.name}: {symbol.type_name} -> {expected_type}"
            items.append(argument_completion_item(symbol, expected_type, insert_text, detail, 1))
    return items


def value_completions_for_expected_type(
    workspace: Workspace,
    doc: Document,
    line: int,
    expected_type: str,
) -> list[dict]:
    expected_canon = canonical_type(expected_type)
    if not expected_canon:
        return []
    items: list[dict] = []
    seen: set[str] = set()
    for symbol in workspace.completion_symbols_at(doc, line):
        if not isinstance(symbol, VariableSymbol):
            continue
        actual_type = canonical_type(symbol.type_name)
        if type_assignment_compatible(expected_type, symbol.type_name):
            insert_text = symbol.name
            if insert_text in seen:
                continue
            seen.add(insert_text)
            items.append(argument_completion_item(symbol, expected_type, insert_text, symbol.detail, 0))
            continue
        if expected_canon.startswith("*") and actual_type == expected_canon[1:]:
            insert_text = f"{symbol.name}.&"
            if insert_text in seen:
                continue
            seen.add(insert_text)
            detail = f"{symbol.name}: {symbol.type_name} -> {expected_type}"
            items.append(argument_completion_item(symbol, expected_type, insert_text, detail, 1))
    return items


def enum_members_for_owner(workspace: Workspace, owner: str) -> list[Symbol]:
    out: list[Symbol] = []
    for symbol in workspace.all_symbols():
        if symbol.kind != "enumMember":
            continue
        member_owner, _member_name = enum_member_parts(symbol)
        if member_owner == owner:
            out.append(symbol)
    return out


def enum_completion_to_lsp(symbol: Symbol, expected_type: str) -> dict:
    return {
        "label": symbol.name,
        "kind": COMPLETION_KIND["enumMember"],
        "detail": symbol.detail,
        "documentation": {"kind": "markdown", "value": f"Enum value for expected type `{expected_type}`."},
        "sortText": f"enum:{symbol.name}",
        "data": completion_data(symbol.kind, symbol.name, symbol.uri, symbol.line, symbol.col),
    }


def enum_dot_completion_to_lsp(symbol: Symbol, owner: str, line: int, replace_start: int, col: int) -> dict:
    member_owner, member_name = enum_member_parts(symbol)
    label = member_name if member_owner == owner and member_name else symbol.name
    return {
        "label": label,
        "kind": COMPLETION_KIND["enumMember"],
        "detail": symbol.detail,
        "documentation": {"kind": "markdown", "value": f"Enum value for `{owner}`."},
        "sortText": f"enum:{label}",
        "filterText": label,
        "insertText": label,
        "textEdit": {
            "range": position_to_lsp(line, replace_start, col),
            "newText": label,
        },
        "data": completion_data(symbol.kind, symbol.name, symbol.uri, symbol.line, symbol.col),
    }


def expected_assignment_type_at(workspace: Workspace, doc: Document, line: int, col: int) -> str:
    lines = doc.text.splitlines()
    if line >= len(lines):
        return ""
    prefix = sanitize_code_line(lines[line][:col])
    assign_re = re.compile(r"(?<![=!<>+\-*/%&|^])=(?![=>])")
    matches = list(assign_re.finditer(prefix))
    if not matches:
        return ""
    assign = matches[-1]
    lhs = prefix[: assign.start()].strip()
    if not lhs:
        return ""
    decl = VAR_RE.match(prefix)
    if decl and assign.start() >= decl.end(2):
        return decl.group(2).strip()
    return infer_simple_expr_type(workspace, doc, lhs, line)


def enum_completions_at(workspace: Workspace, doc: Document | None, line: int, col: int) -> list[dict]:
    if not doc:
        return []
    expected_type = expected_assignment_type_at(workspace, doc, line, col)
    if not expected_type:
        return []
    symbol = workspace.find_symbol(normalize_type_name(expected_type))
    if not symbol or symbol.kind != "enum":
        return []
    return [enum_completion_to_lsp(member, symbol.name) for member in enum_members_for_owner(workspace, symbol.name)]


def enum_dot_completions_at(workspace: Workspace, doc: Document | None, line: int, col: int) -> list[dict]:
    if not doc:
        return []
    lines = document_clean_lines(doc)
    if line >= len(lines):
        return []
    prefix = lines[line][:col]
    match = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)?$", prefix)
    if not match:
        return []
    owner = match.group(1)
    typed_member = match.group(2) or ""
    symbol = workspace.find_symbol(owner)
    if not symbol or symbol.kind != "enum":
        return []
    replace_start = match.start(2) if match.start(2) >= 0 else col
    out: list[dict] = []
    for member in enum_members_for_owner(workspace, owner):
        _member_owner, member_name = enum_member_parts(member)
        label = member_name or member.name
        if typed_member and not label.startswith(typed_member):
            continue
        out.append(enum_dot_completion_to_lsp(member, owner, line, replace_start, col))
    out.sort(key=lambda item: str(item.get("label", "")).lower())
    return out


def expected_type_completions_at(workspace: Workspace, doc: Document | None, line: int, col: int) -> list[dict]:
    if not doc:
        return []
    expected_type = expected_assignment_type_at(workspace, doc, line, col)
    if not expected_type:
        return []
    if workspace.find_symbol(normalize_type_name(expected_type)):
        symbol = workspace.find_symbol(normalize_type_name(expected_type))
        if symbol and symbol.kind == "enum":
            return []
    return value_completions_for_expected_type(workspace, doc, line, expected_type)


def struct_initializer_owner_at(workspace: Workspace, doc: Document, line: int, col: int) -> tuple[str, str]:
    lines = doc.text.splitlines()
    if line >= len(lines):
        return "", ""
    prefix = sanitize_code_line(lines[line][:col])
    dot = prefix.rfind(".")
    brace = prefix.rfind("{")
    if dot < 0 or brace < 0 or dot < brace:
        return "", ""
    before_brace = prefix[:brace]
    decl = VAR_RE.match(before_brace)
    assign_re = re.compile(r"(?<![=!<>+\-*/%&|^])=(?![=>])")
    matches = list(assign_re.finditer(before_brace))
    if matches:
        after_assign = before_brace[matches[-1].end() :].strip()
        if after_assign:
            matches = []
    if matches:
        if decl and matches[-1].start() >= decl.end(2):
            return decl.group(2).strip(), prefix[brace + 1 :]
        lhs = before_brace[: matches[-1].start()].strip()
        return infer_simple_expr_type(workspace, doc, lhs, line), prefix[brace + 1 :]

    signature = signature_help_at(workspace, doc, line, col)
    if not signature:
        return "", ""
    signatures = signature.get("signatures", [])
    if not signatures:
        return "", ""
    parameters = signatures[0].get("parameters", [])
    if not parameters:
        return "", ""
    active = int(signature.get("activeParameter", 0))
    if active < 0 or active >= len(parameters):
        return "", ""
    label = parameters[active].get("label", "")
    if not isinstance(label, str) or ":" not in label:
        return "", ""
    return label.split(":", 1)[1].strip(), prefix[brace + 1 :]


def struct_literal_field_completions_at(workspace: Workspace, doc: Document | None, line: int, col: int) -> list[dict]:
    if not doc:
        return []
    owner, init_prefix = struct_initializer_owner_at(workspace, doc, line, col)
    if not owner:
        return []
    fields = workspace.fields_for_owner(owner)
    if not fields:
        return []
    used = set(re.findall(r"\.\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", init_prefix))
    items: list[dict] = []
    for field in fields:
        if field.name in used:
            continue
        concrete = field_with_owner_type(field, owner)
        items.append(
            {
                "label": concrete.name,
                "kind": 5,
                "detail": concrete.detail,
                "documentation": {"kind": "markdown", "value": hover_markdown_for_symbol(workspace, concrete)},
                "insertText": f"{concrete.name} = ",
                "sortText": f"field:{concrete.name}",
                "data": completion_data("field", f"{concrete.owner}.{concrete.name}", concrete.uri, concrete.line, concrete.col),
            }
        )
    return items


def path_completion_context(doc: Document, line: int, col: int, keyword: str) -> tuple[int, str] | None:
    lines = doc.text.splitlines()
    if line >= len(lines):
        return None
    raw_line = lines[line]
    match = re.match(rf'^\s*{re.escape(keyword)}\s+"([^"]*)', raw_line)
    if not match:
        return None
    path_start = match.start(1)
    if col < path_start:
        return None
    close_quote = raw_line.find('"', path_start)
    if close_quote >= 0 and col > close_quote:
        return None
    typed = raw_line[path_start : min(col, len(raw_line))]
    return path_start, typed


def path_completions_at(
    workspace: Workspace,
    doc: Document | None,
    line: int,
    col: int,
    keyword: str,
    suffixes: tuple[str, ...],
    file_detail: str,
) -> list[dict]:
    _ = workspace
    if not doc or not doc.path:
        return []
    context = path_completion_context(doc, line, col, keyword)
    if not context:
        return []
    path_start, typed = context
    if typed.endswith("/") or typed.endswith("\\"):
        typed_dir_text = typed.replace("\\", "/").rstrip("/")
        typed_name = ""
    else:
        typed_norm = typed.replace("\\", "/")
        slash = typed_norm.rfind("/")
        typed_dir_text = typed_norm[:slash] if slash >= 0 else ""
        typed_name = typed_norm[slash + 1 :] if slash >= 0 else typed_norm
    typed_dir = Path(typed_dir_text) if typed_dir_text else Path("")
    base_dir = (doc.path.parent / typed_dir).resolve()
    if not base_dir.exists() or not base_dir.is_dir():
        return []

    items: list[dict] = []
    try:
        entries = sorted(base_dir.iterdir(), key=lambda path: (not path.is_dir(), path.name.lower()))
    except OSError:
        return []
    for entry in entries:
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            suffix = f"{entry.name}/"
            kind = 19
        elif entry.suffix.lower() in suffixes:
            suffix = entry.name
            kind = 17
        else:
            continue
        if typed_name and not suffix.lower().startswith(typed_name.lower()):
            continue
        completion_path = f"{typed_dir_text}/{suffix}" if typed_dir_text else suffix
        items.append(
            {
                "label": completion_path,
                "kind": kind,
                "detail": file_detail if kind == 17 else "directory",
                "sortText": f"{keyword}:{0 if kind == 19 else 1}:{completion_path}",
                "textEdit": {
                    "range": position_to_lsp(line, path_start, col),
                    "newText": completion_path,
                },
            }
        )
    return items


def import_path_completions_at(workspace: Workspace, doc: Document | None, line: int, col: int) -> list[dict]:
    return path_completions_at(workspace, doc, line, col, "import", (".i",), "I module")


def cinclude_path_completions_at(workspace: Workspace, doc: Document | None, line: int, col: int) -> list[dict]:
    return path_completions_at(
        workspace,
        doc,
        line,
        col,
        "cinclude",
        (".h", ".hh", ".hpp", ".c", ".cc", ".cpp", ".inl"),
        "C/C++ file",
    )


def field_with_owner_type(field: FieldSymbol, owner_type: str) -> FieldSymbol:
    concrete_type = substitute_simple_generic_type(field.type_name, owner_type, field.type_param)
    owner_label = owner_type.strip() or field.owner
    return FieldSymbol(
        owner=field.owner,
        name=field.name,
        type_name=concrete_type,
        attrs=field.attrs,
        uri=field.uri,
        line=field.line,
        col=field.col,
        detail=f"{owner_label}.{field.name}: {concrete_type}",
        type_param=field.type_param,
    )


REFLECT_TYPE_FIELDS: tuple[tuple[str, str], ...] = (
    ("name", "*const char"),
    ("size", "u64"),
    ("align", "u64"),
    ("field_count", "u64"),
    ("fields", "*const i_reflect_field"),
)

REFLECT_ENUM_FIELDS: tuple[tuple[str, str], ...] = (
    ("name", "*const char"),
    ("size", "u64"),
    ("align", "u64"),
    ("value_count", "u64"),
    ("values", "*const i_reflect_enum_value"),
)


def reflect_metadata_fields_for_symbol(symbol: Symbol, owner_type: str) -> list[FieldSymbol]:
    if symbol.kind == "enum":
        field_types = REFLECT_ENUM_FIELDS
    elif symbol.kind in {"struct", "union"}:
        field_types = REFLECT_TYPE_FIELDS
    else:
        return []

    owner_label = f"{owner_type}<>"
    return [
        FieldSymbol(
            owner=owner_label,
            name=name,
            type_name=type_name,
            attrs="",
            uri=symbol.uri,
            line=symbol.line,
            col=symbol.col,
            detail=f"{owner_label}.{name}: {type_name}",
        )
        for name, type_name in field_types
    ]


def completion_symbol_from_data(
    workspace: Workspace, data: dict
) -> Symbol | FieldSymbol | VariableSymbol | None:
    kind = data.get("kind", "")
    name = data.get("name", "")
    uri = data.get("uri", "")
    line = int(data.get("line", -1))
    col = int(data.get("character", -1))
    doc = workspace.documents.get(uri)

    if kind == "field":
        owner, sep, field_name = name.partition(".")
        if sep:
            for field in workspace.fields_for_owner(owner):
                if field.name == field_name and field.uri == uri and field.line == line and field.col == col:
                    return field
            return workspace.find_field(owner, field_name)
        return None

    if kind in {"variable", "parameter", "global"}:
        if doc:
            for variable in doc.variables_all:
                if variable.name == name and variable.line == line and variable.col == col:
                    return variable
        if kind == "global":
            variable = workspace.global_variables.get(normalize_symbol_name(name))
            if variable:
                return variable
        if doc:
            return workspace.find_variable(doc, name)
        return None

    if doc:
        for symbol in doc.symbols:
            if symbol.name == name and symbol.kind == kind and symbol.line == line and symbol.col == col:
                return symbol
    symbol = workspace.find_symbol(name)
    if symbol and (not kind or symbol.kind == kind):
        return symbol
    return None


def resolve_completion_item(workspace: Workspace, item: dict) -> dict:
    data = item.get("data")
    if not isinstance(data, dict):
        return item
    symbol = completion_symbol_from_data(workspace, data)
    if symbol is None:
        return item
    if isinstance(symbol, FieldSymbol):
        resolved = field_completion_to_lsp(workspace, symbol)
    elif isinstance(symbol, VariableSymbol):
        resolved = variable_completion_to_lsp(workspace, symbol)
    else:
        resolved = completion_to_lsp(workspace, symbol)
    out = dict(item)
    out.update(resolved)
    return out


def hover_markdown_for_symbol(workspace: Workspace, symbol: Symbol | FieldSymbol | VariableSymbol) -> str:
    if isinstance(symbol, VariableSymbol):
        proc_detail = proc_signature_detail_for_type(workspace, symbol.type_name)
        if proc_detail:
            return f"`{symbol.detail}`\n\nresolves to `{proc_detail}`"
        return f"`{symbol.detail}`"
    if isinstance(symbol, FieldSymbol):
        proc_detail = proc_signature_detail_for_type(workspace, symbol.type_name)
        attr_detail = f"\n\nattrs: `{symbol.attrs}`" if symbol.attrs else ""
        if proc_detail:
            return f"`{symbol.detail}`\n\nresolves to `{proc_detail}`{attr_detail}"
        return f"`{symbol.detail}`{attr_detail}"
    if isinstance(symbol, Symbol) and symbol.kind == "alias":
        display_rhs = alias_rhs(symbol.detail)
        rhs = symbol.target_type or display_rhs
        proc_detail = proc_signature_detail_for_alias_symbol(symbol)
        if not proc_detail:
            proc_detail = proc_signature_detail_for_type(workspace, rhs)
        if proc_detail and proc_detail != display_rhs:
            return f"`{symbol.detail}`\n\nresolves to `{proc_detail}`"
        return f"`{symbol.detail}`"
    if isinstance(symbol, Symbol) and symbol.kind == "proc" and proc_symbol_has_signature_metadata(symbol):
        return f"`{proc_signature_label_for_symbol(symbol, symbol.name)}`"
    if isinstance(symbol, Symbol) and symbol.kind in ("struct", "union"):
        fields = workspace.fields_for_owner(symbol.name)
        if fields:
            field_lines = []
            for field in fields:
                attr_detail = f" attrs: `{field.attrs}`" if field.attrs else ""
                field_lines.append(f"- `{field.name}: {field.type_name}`{attr_detail}")
            return f"`{symbol.detail}`\n\nfields:\n" + "\n".join(field_lines)
        return f"`{symbol.detail}`"
    if isinstance(symbol, Symbol) and symbol.kind == "enum":
        members = [
            candidate
            for doc in workspace.documents.values()
            for candidate in doc.symbols
            if candidate.kind == "enumMember" and enum_member_parts(candidate)[0] == symbol.name
        ]
        if members:
            member_lines = []
            for member in members:
                owner, item = enum_member_parts(member)
                display = item if owner == symbol.name and item else member.name
                member_lines.append(f"- `{display}` = `{member.name}`")
            return f"`{symbol.detail}`\n\nvalues:\n" + "\n".join(member_lines)
        return f"`{symbol.detail}`"
    if isinstance(symbol, Symbol) and symbol.kind == "enumMember":
        owner, _item = enum_member_parts(symbol)
        if owner:
            return f"`{symbol.detail}`\n\ntype `{owner}`"
    return f"`{symbol.detail}`"


def field_declaration_at(doc: Document, line: int, start: int, ident: str) -> FieldSymbol | None:
    for owner_fields in doc.fields.values():
        for field in owner_fields:
            if field.line == line and field.col == start and field.name == ident:
                return field
    return None


def enum_member_declaration_at(doc: Document, line: int, start: int, ident: str) -> Symbol | None:
    for symbol in doc.symbols:
        if symbol.kind != "enumMember":
            continue
        source_len = symbol.source_len if symbol.source_len > 0 else len(symbol.name)
        if symbol.line == line and symbol.col == start and len(ident) == source_len:
            return symbol
    return None


def import_at(doc: Document, line: int, col: int) -> ImportSymbol | None:
    for symbol in doc.imports:
        if symbol.line == line and symbol.col <= col <= symbol.end_col:
            return symbol
    return None


def cinclude_at(doc: Document, line: int, col: int) -> CIncludeSymbol | None:
    for symbol in doc.cincludes:
        if symbol.line == line and symbol.col <= col <= symbol.end_col:
            return symbol
    return None


def enum_member_parts(symbol: Symbol) -> tuple[str, str]:
    if symbol.kind != "enumMember":
        return "", ""
    if symbol.enum_owner and symbol.enum_item:
        return symbol.enum_owner, symbol.enum_item
    detail_match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*): enum member", symbol.detail)
    if detail_match:
        return detail_match.group(1), detail_match.group(2)
    owner, sep, item = symbol.name.partition("_")
    return (owner, item) if sep else ("", "")


def enum_member_usage_name(symbol: Symbol) -> str:
    owner, item = enum_member_parts(symbol)
    return f"{owner}_{item}" if owner and item else symbol.name


def enum_member_dot_access_at(workspace: Workspace, raw_line: str, start: int, ident: str) -> Symbol | None:
    dot = start - 1
    if dot < 0 or raw_line[dot] != ".":
        return None
    left = sanitize_code_line(raw_line[:dot]).rstrip()
    match = re.search(r"([A-Za-z_][A-Za-z0-9_]*)$", left)
    if not match:
        return None
    owner = match.group(1)
    owner_symbol = workspace.find_symbol(owner)
    if not owner_symbol or owner_symbol.kind != "enum":
        return None
    for member in enum_members_for_owner(workspace, owner):
        member_owner, member_name = enum_member_parts(member)
        if member_owner == owner and member_name == ident:
            return member
    return None


def identifier_is_call(raw_line: str, start: int, ident: str) -> bool:
    pos = start + len(ident)
    while pos < len(raw_line) and raw_line[pos].isspace():
        pos += 1
    return pos < len(raw_line) and raw_line[pos] == "("


def semantic_token_kind(workspace: Workspace, doc: Document, line: int, ident: str, raw_line: str, start: int) -> str | None:
    if ident in KEYWORDS:
        return "keyword"
    if ident in BUILTIN_TYPES:
        return "type"
    if start > 0 and raw_line[start - 1] == ".":
        enum_member = enum_member_dot_access_at(workspace, raw_line, start, ident)
        if enum_member:
            return "enumMember"
        field = field_access_at(workspace, doc, line, start)
        if field and identifier_is_call(raw_line, start, ident) and proc_signature_detail_for_type(workspace, field.type_name):
            return "function"
        return "property" if field else None
    if field_declaration_at(doc, line, start, ident):
        return "property"
    if enum_member_declaration_at(doc, line, start, ident):
        return "enumMember"
    variable = workspace.find_variable_at(doc, ident, line)
    if variable:
        if identifier_is_call(raw_line, start, ident) and proc_signature_detail_for_type(workspace, variable.type_name):
            return "function"
        return "parameter" if variable.kind == "parameter" else "variable"
    symbol = workspace.find_symbol(ident)
    if not symbol:
        symbol = workspace.find_enum_member_usage(ident)
    if not symbol:
        return None
    if symbol.kind in ("struct", "union", "alias", "enum"):
        return "type"
    if symbol.kind == "enumMember":
        return "enumMember"
    if symbol.kind == "proc":
        return "function"
    return "variable"


def semantic_modifier_mask(*names: str) -> int:
    mask = 0
    for name in names:
        index = SEMANTIC_MODIFIER_INDEX.get(name)
        if index is not None:
            mask |= 1 << index
    return mask


def semantic_token_modifier_mask(workspace: Workspace, doc: Document, line: int, ident: str, raw_line: str, start: int, kind: str) -> int:
    modifiers: list[str] = []
    if ident in BUILTIN_TYPES:
        modifiers.append("defaultLibrary")

    if start > 0 and raw_line[start - 1] == ".":
        enum_member = enum_member_dot_access_at(workspace, raw_line, start, ident)
        if enum_member:
            modifiers.append("member")
            return semantic_modifier_mask(*modifiers)
        field = field_access_at(workspace, doc, line, start)
        if field:
            modifiers.append("member")
        return semantic_modifier_mask(*modifiers)

    if field_declaration_at(doc, line, start, ident):
        modifiers.extend(["declaration", "member"])
        return semantic_modifier_mask(*modifiers)

    if enum_member_declaration_at(doc, line, start, ident):
        modifiers.append("declaration")
        return semantic_modifier_mask(*modifiers)

    variable = workspace.find_variable_at(doc, ident, line)
    if variable:
        if variable.line == line and variable.col == start:
            modifiers.append("declaration")
        if variable.kind == "global":
            modifiers.append("global")
        elif variable.kind == "parameter":
            modifiers.append("definition" if variable.line == line and variable.col == start else "local")
        else:
            modifiers.append("local")
        return semantic_modifier_mask(*modifiers)

    symbol = workspace.find_symbol(ident)
    if not symbol:
        symbol = workspace.find_enum_member_usage(ident)
    if symbol:
        if symbol.line == line and symbol.col == start:
            modifiers.append("definition" if symbol.kind == "proc" else "declaration")
        if symbol.kind == "proc" and "<" in symbol.name:
            modifiers.append("generic")
        return semantic_modifier_mask(*modifiers)

    return semantic_modifier_mask(*modifiers)


def cached_proc_signature_detail(workspace: Workspace, type_name: str, cache: dict[str, str]) -> str:
    if not type_name:
        return ""
    cached = cache.get(type_name)
    if cached is not None:
        return cached
    detail = proc_signature_detail_for_type(workspace, type_name)
    cache[type_name] = detail
    return detail


def semantic_token_info(
    workspace: Workspace,
    doc: Document,
    line: int,
    ident: str,
    raw_line: str,
    start: int,
    field_decl_cache: dict[tuple[int, int, str], FieldSymbol],
    enum_decl_cache: dict[tuple[int, int, str], Symbol],
    field_access_cache: dict[tuple[int, int], FieldSymbol | bool],
    variable_cache: dict[tuple[str, int], VariableSymbol | bool],
    symbol_cache: dict[str, Symbol | bool],
    proc_detail_cache: dict[str, str],
) -> tuple[str, int] | None:
    modifiers: list[str] = []
    if ident in KEYWORDS:
        return "keyword", 0
    if ident in BUILTIN_TYPES:
        return "type", semantic_modifier_mask("defaultLibrary")

    if start > 0 and raw_line[start - 1] == ".":
        enum_member = enum_member_dot_access_at(workspace, raw_line, start, ident)
        if enum_member:
            return "enumMember", semantic_modifier_mask("member")
        key = (line, start)
        cached_field = field_access_cache.get(key)
        if cached_field is None:
            field = field_access_at(workspace, doc, line, start)
            field_access_cache[key] = field if field else False
        else:
            field = cached_field if isinstance(cached_field, FieldSymbol) else None
        if not field:
            return None
        modifiers.append("member")
        kind = "function" if identifier_is_call(raw_line, start, ident) and cached_proc_signature_detail(workspace, field.type_name, proc_detail_cache) else "property"
        return kind, semantic_modifier_mask(*modifiers)

    field = field_decl_cache.get((line, start, ident))
    if field:
        return "property", semantic_modifier_mask("declaration", "member")

    enum_decl = enum_decl_cache.get((line, start, ident))
    if enum_decl:
        return "enumMember", semantic_modifier_mask("declaration")

    variable_key = (ident, line)
    cached_variable = variable_cache.get(variable_key)
    if cached_variable is None:
        variable = workspace.find_variable_at(doc, ident, line)
        variable_cache[variable_key] = variable if variable else False
    else:
        variable = cached_variable if isinstance(cached_variable, VariableSymbol) else None
    if variable:
        if variable.line == line and variable.col == start:
            modifiers.append("declaration")
        if variable.kind == "global":
            modifiers.append("global")
        elif variable.kind == "parameter":
            modifiers.append("definition" if variable.line == line and variable.col == start else "local")
        else:
            modifiers.append("local")
        kind = "function" if identifier_is_call(raw_line, start, ident) and cached_proc_signature_detail(workspace, variable.type_name, proc_detail_cache) else "parameter" if variable.kind == "parameter" else "variable"
        return kind, semantic_modifier_mask(*modifiers)

    cached_symbol = symbol_cache.get(ident)
    if cached_symbol is None:
        symbol = workspace.find_symbol(ident) or workspace.find_enum_member_usage(ident)
        symbol_cache[ident] = symbol if symbol else False
    else:
        symbol = cached_symbol if isinstance(cached_symbol, Symbol) else None
    if not symbol:
        return None

    if symbol.line == line and symbol.col == start:
        modifiers.append("definition" if symbol.kind == "proc" else "declaration")
    if symbol.kind == "proc" and "<" in symbol.name:
        modifiers.append("generic")

    if symbol.kind in ("struct", "union", "alias", "enum"):
        return "type", semantic_modifier_mask(*modifiers)
    if symbol.kind == "enumMember":
        return "enumMember", semantic_modifier_mask(*modifiers)
    if symbol.kind == "proc":
        return "function", semantic_modifier_mask(*modifiers)
    return "variable", semantic_modifier_mask(*modifiers)


def generic_identifier_semantic_spans(workspace: Workspace, doc: Document, line: int, ident: str, start: int) -> list[tuple[int, int, str, int]]:
    parts = generic_ident_parts(ident)
    if not parts:
        return []

    symbol = workspace.find_symbol(ident)
    generic_call_symbol = generic_proc_symbol_for_call(workspace, ident)
    base, args, tail = parts
    if tail and (not symbol or symbol.kind != "proc"):
        return []
    if not tail and symbol and symbol.kind not in ("struct", "union", "alias", "enum") and not generic_call_symbol:
        return []

    base_modifiers: list[str] = ["generic"]
    base_symbol = workspace.find_symbol(base)
    if base_symbol and base_symbol.line == line and base_symbol.col == start:
        base_modifiers.append("declaration")
    base_kind = "function" if generic_call_symbol or (base_symbol and base_symbol.kind == "proc" and not tail) else "type"
    spans: list[tuple[int, int, str, int]] = [(start, len(base), base_kind, semantic_modifier_mask(*base_modifiers))]
    args_start = start + len(base) + 1
    for arg_start, arg_end, arg in iter_identifier_spans(args):
        nested = generic_identifier_semantic_spans(workspace, doc, line, arg, args_start + arg_start)
        if nested:
            spans.extend(nested)
            continue
        arg_modifiers = ["defaultLibrary"] if arg in BUILTIN_TYPES else []
        spans.append((args_start + arg_start, arg_end - arg_start, "type", semantic_modifier_mask(*arg_modifiers)))
    if tail:
        tail_modifiers = ["generic"]
        if symbol and symbol.line == line and symbol.col == start:
            tail_modifiers.append("definition")
        spans.append((start + len(base) + len(args) + 2, len(tail), "function", semantic_modifier_mask(*tail_modifiers)))
    return spans


def encode_semantic_tokens(tokens: list[tuple[int, int, int, int, int]]) -> list[int]:
    data: list[int] = []
    prev_line = 0
    prev_start = 0
    for line, start, length, kind, modifiers in sorted(tokens, key=lambda token: (token[0], token[1])):
        delta_line = line - prev_line
        delta_start = start if delta_line else start - prev_start
        data.extend([delta_line, delta_start, length, kind, modifiers])
        prev_line = line
        prev_start = start
    return data


def lexical_semantic_tokens_for_doc(doc: Document) -> list[int]:
    cached_text = getattr(doc, "_lexical_semantic_token_cache_text", None)
    cached_data = getattr(doc, "_lexical_semantic_token_cache", None)
    if cached_text == doc.text and isinstance(cached_data, list):
        return cached_data
    tokens: list[tuple[int, int, int, int, int]] = []
    builtin_mask = semantic_modifier_mask("defaultLibrary")
    for line_no, raw_line in enumerate(doc.text.splitlines()):
        if raw_line.lstrip().startswith("#"):
            continue
        clean_line = sanitize_code_line(raw_line)
        for match in SIMPLE_IDENT_RE.finditer(clean_line):
            ident = match.group(0)
            if ident in KEYWORDS:
                tokens.append((line_no, match.start(), len(ident), SEMANTIC_TOKEN_INDEX["keyword"], 0))
            elif ident in BUILTIN_TYPES:
                tokens.append((line_no, match.start(), len(ident), SEMANTIC_TOKEN_INDEX["type"], builtin_mask))
        for match in NUMBER_RE.finditer(clean_line):
            tokens.append((line_no, match.start(), match.end() - match.start(), SEMANTIC_TOKEN_INDEX["number"], 0))
        for match in STRING_LITERAL_RE.finditer(raw_line):
            tokens.append((line_no, match.start(), match.end() - match.start(), SEMANTIC_TOKEN_INDEX["string"], 0))
        for match in ATTRIBUTE_OPERATOR_RE.finditer(raw_line):
            tokens.append((line_no, match.start(), 1, SEMANTIC_TOKEN_INDEX["operator"], 0))
    data = encode_semantic_tokens(tokens)
    setattr(doc, "_lexical_semantic_token_cache_text", doc.text)
    setattr(doc, "_lexical_semantic_token_cache", data)
    return data


def semantic_tokens_for_doc(workspace: Workspace, doc: Document) -> list[int]:
    cache_key = (doc.text, workspace.index_revision)
    cached_key = getattr(doc, "_semantic_token_cache_key", None)
    cached_data = getattr(doc, "_semantic_token_cache", None)
    if cached_key == cache_key and isinstance(cached_data, list):
        return cached_data
    tokens: list[tuple[int, int, int, int, int]] = []
    field_decl_cache: dict[tuple[int, int, str], FieldSymbol] = {
        (field.line, field.col, field.name): field
        for owner_fields in doc.fields.values()
        for field in owner_fields
    }
    enum_decl_cache: dict[tuple[int, int, str], Symbol] = {
        (symbol.line, symbol.col, enum_member_parts(symbol)[1] or symbol.name): symbol
        for symbol in doc.symbols
        if symbol.kind == "enumMember"
    }
    field_access_cache: dict[tuple[int, int], FieldSymbol | bool] = {}
    variable_cache: dict[tuple[str, int], VariableSymbol | bool] = {}
    symbol_cache: dict[str, Symbol | bool] = {}
    proc_detail_cache: dict[str, str] = {}
    for line, start, end, ident, raw_line in document_identifiers(doc):
        generic_spans = generic_identifier_semantic_spans(workspace, doc, line, ident, start)
        if generic_spans:
            for span_start, length, kind, modifiers in generic_spans:
                tokens.append((line, span_start, length, SEMANTIC_TOKEN_INDEX[kind], modifiers))
            continue
        info = semantic_token_info(
            workspace,
            doc,
            line,
            ident,
            raw_line,
            start,
            field_decl_cache,
            enum_decl_cache,
            field_access_cache,
            variable_cache,
            symbol_cache,
            proc_detail_cache,
        )
        if info is None:
            continue
        kind, modifiers = info
        tokens.append((line, start, end - start, SEMANTIC_TOKEN_INDEX[kind], modifiers))
    for line, start, end in iter_numbers(doc.text):
        tokens.append((line, start, end - start, SEMANTIC_TOKEN_INDEX["number"], 0))
    for line, start, end in iter_string_literals(doc.text):
        tokens.append((line, start, end - start, SEMANTIC_TOKEN_INDEX["string"], 0))
    for line, start, end in iter_attribute_operators(doc.text):
        tokens.append((line, start, end - start, SEMANTIC_TOKEN_INDEX["operator"], 0))

    data = encode_semantic_tokens(tokens)
    setattr(doc, "_semantic_token_cache_key", cache_key)
    setattr(doc, "_semantic_token_cache", data)
    return data


def split_top_level_commas(text: str) -> list[str]:
    parts: list[str] = []
    start = 0
    paren_depth = 0
    bracket_depth = 0
    brace_depth = 0
    angle_depth = 0
    for i, ch in enumerate(text):
        if ch == "(":
            paren_depth += 1
        elif ch == ")" and paren_depth > 0:
            paren_depth -= 1
        elif ch == "[":
            bracket_depth += 1
        elif ch == "]" and bracket_depth > 0:
            bracket_depth -= 1
        elif ch == "{":
            brace_depth += 1
        elif ch == "}" and brace_depth > 0:
            brace_depth -= 1
        elif ch == "<":
            angle_depth += 1
        elif ch == ">" and angle_depth > 0:
            angle_depth -= 1
        elif ch == "," and paren_depth == 0 and bracket_depth == 0 and brace_depth == 0 and angle_depth == 0:
            parts.append(text[start:i].strip())
            start = i + 1
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def split_top_level_comma_spans(text: str) -> list[tuple[str, int]]:
    parts: list[tuple[str, int]] = []
    start = 0
    paren_depth = 0
    bracket_depth = 0
    brace_depth = 0
    angle_depth = 0
    for i, ch in enumerate(text):
        if ch == "(":
            paren_depth += 1
        elif ch == ")" and paren_depth > 0:
            paren_depth -= 1
        elif ch == "[":
            bracket_depth += 1
        elif ch == "]" and bracket_depth > 0:
            bracket_depth -= 1
        elif ch == "{":
            brace_depth += 1
        elif ch == "}" and brace_depth > 0:
            brace_depth -= 1
        elif ch == "<":
            angle_depth += 1
        elif ch == ">" and angle_depth > 0:
            angle_depth -= 1
        elif ch == "," and paren_depth == 0 and bracket_depth == 0 and brace_depth == 0 and angle_depth == 0:
            raw = text[start:i]
            stripped = raw.strip()
            if stripped:
                parts.append((stripped, start + len(raw) - len(raw.lstrip())))
            start = i + 1
    raw_tail = text[start:]
    tail = raw_tail.strip()
    if tail:
        parts.append((tail, start + len(raw_tail) - len(raw_tail.lstrip())))
    return parts


def proc_parameter_type(label: str) -> str:
    label = label.strip()
    if label == "...":
        return ""
    _name, sep, type_name = label.partition(":")
    return type_name.strip() if sep else label


def proc_parameter_labels(detail: str) -> list[str]:
    proc_pos = detail.find("proc")
    if proc_pos < 0:
        return []
    params_start = detail.find("(", proc_pos)
    if params_start < 0:
        return []
    depth = 0
    params_end = -1
    for i in range(params_start, len(detail)):
        ch = detail[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                params_end = i
                break
    if params_end < 0:
        return []
    params_text = detail[params_start + 1 : params_end].strip()
    if not params_text:
        return []
    return split_top_level_commas(params_text)


def proc_return_type(detail: str) -> str:
    proc_pos = detail.find("proc")
    if proc_pos < 0:
        return ""
    params_start = detail.find("(", proc_pos)
    if params_start < 0:
        return ""
    depth = 0
    params_end = -1
    for i in range(params_start, len(detail)):
        ch = detail[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                params_end = i
                break
    if params_end < 0:
        return ""
    tail = detail[params_end + 1 :]
    match = re.search(r"->\s*([^=\{;]+)", tail)
    return match.group(1).strip() if match else "void"


def canonical_type(type_name: str) -> str:
    out = re.sub(r"\b(const|let)\b", "", type_name.strip())
    out = re.sub(r"\s+", "", out)
    return out.rstrip(";")


def pointer_pointee_type(type_name: str) -> str:
    out = canonical_type(type_name)
    if out.startswith("*"):
        return out[1:]
    return ""


def indexed_element_type(type_name: str, workspace: Workspace | None = None) -> str:
    out = canonical_type(type_name)
    if workspace is not None:
        seen: set[str] = set()
        while re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", out):
            if out in seen:
                break
            seen.add(out)
            symbol = workspace.find_symbol(out)
            if not symbol or symbol.kind != "alias":
                break
            rhs = canonical_type(symbol.target_type or alias_rhs(symbol.detail))
            if not rhs:
                break
            out = rhs
    if out.startswith("*"):
        return out[1:]
    match = re.match(r"^\[[^\]]+\](.+)$", out)
    return match.group(1) if match else ""


def simple_literal_type(expr: str) -> str:
    expr = expr.strip()
    if expr in {"true", "false"}:
        return "bool"
    if re.fullmatch(r'"(?:\\.|[^"\\])*"', expr):
        return "*char"
    if re.fullmatch(r"'(?:\\.|[^'\\])'", expr):
        return "char"
    if re.fullmatch(r"0[xX][0-9A-Fa-f]+|0[bB][01]+|[0-9]+", expr):
        return "i32"
    if re.fullmatch(r"(?:[0-9]+\.[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?f?", expr):
        return "f32"
    return ""


def type_assignment_compatible(dst: str, src: str) -> bool:
    dst = canonical_type(dst)
    src = canonical_type(src)
    if not dst or not src:
        return True
    if dst == src:
        return True
    integer_types = {"char", "i8", "i16", "i32", "i64", "u8", "u16", "u32", "u64"}
    float_types = {"f32", "f64"}
    if dst in integer_types and src in integer_types:
        return True
    if dst in float_types and src in integer_types | float_types:
        return True
    if dst.startswith("*") and src == "0":
        return True
    return False


def alias_rhs(detail: str) -> str:
    _left, sep, right = detail.partition("=")
    if not sep:
        return detail.strip().rstrip(";")
    return right.strip().rstrip(";")


def generic_proc_call_parts(name: str) -> tuple[str, str, str] | None:
    return generic_ident_parts(re.sub(r"\s+", "", name.strip()))


def generic_proc_param_name(symbol: Symbol) -> str:
    if symbol.type_param:
        return symbol.type_param
    parts = generic_ident_parts(symbol.name)
    return parts[1].strip() if parts else ""


def canonical_type_text(type_name: str) -> str:
    return re.sub(r"\s+", "", type_name.strip())


def lsp_type_mangle(type_name: str) -> str:
    text = canonical_type_text(type_name)
    while text.startswith("*"):
        text = "ptr_" + text[1:]
    parts = generic_ident_parts(text)
    if parts:
        base, args, tail = parts
        pieces = [base]
        for arg in split_top_level_type_args(args):
            pieces.append(lsp_type_mangle(arg))
        if tail:
            pieces.append(tail)
        return "_".join(piece for piece in pieces if piece)
    if text.startswith("["):
        close = text.find("]")
        if close >= 0:
            count = text[1:close]
            elem = text[close + 1 :]
            return f"array_{count}_{lsp_type_mangle(elem)}"
    return re.sub(r"[^A-Za-z0-9_]", "_", text)


def generic_pattern_bind(pattern: str, arg: str, param: str) -> str:
    pattern = canonical_type_text(pattern)
    arg = canonical_type_text(arg)
    param = param.strip()
    if not pattern or not arg or not param:
        return ""
    if pattern == param:
        return arg

    pattern_parts = generic_ident_parts(pattern)
    arg_parts = generic_ident_parts(arg)
    if pattern_parts and arg_parts and pattern_parts[0] == arg_parts[0] and pattern_parts[2] == arg_parts[2]:
        pattern_args = split_top_level_type_args(pattern_parts[1])
        arg_args = split_top_level_type_args(arg_parts[1])
        if len(pattern_args) != len(arg_args):
            return ""
        bound = ""
        for pattern_arg, arg_arg in zip(pattern_args, arg_args):
            candidate = generic_pattern_bind(pattern_arg, arg_arg, param)
            if not candidate:
                if canonical_type_text(pattern_arg) != canonical_type_text(arg_arg):
                    return ""
                continue
            if bound and canonical_type_text(bound) != canonical_type_text(candidate):
                return ""
            bound = candidate
        return bound

    if pattern.startswith("*") and arg.startswith("*"):
        return generic_pattern_bind(pattern[1:], arg[1:], param)

    return ""


def generic_proc_bound_arg(symbol: Symbol, call_arg: str) -> str:
    param = generic_proc_param_name(symbol)
    if not param:
        return ""
    if symbol.generic_pattern:
        return generic_pattern_bind(symbol.generic_pattern, call_arg, param)
    return call_arg


def generic_proc_detail_for_call(symbol: Symbol, call_name: str) -> str:
    parts = generic_proc_call_parts(call_name)
    param = generic_proc_param_name(symbol)
    if not parts or not param:
        return symbol.detail
    _base, arg, _tail = parts
    bound_arg = generic_proc_bound_arg(symbol, arg) or arg
    detail = symbol.detail
    detail = detail.replace(symbol.name, call_name, 1)
    detail = re.sub(rf"\bproc\s*<\s*{re.escape(param)}\s*>", "proc", detail, count=1)
    detail = re.sub(rf"\b{re.escape(param)}\b", bound_arg, detail)
    return detail


def symbol_with_generic_call_detail(symbol: Symbol, call_name: str) -> Symbol:
    if symbol.kind != "proc" or not generic_proc_call_parts(call_name):
        return symbol
    detail = generic_proc_detail_for_call(symbol, call_name)
    return Symbol(
        call_name,
        symbol.kind,
        symbol.uri,
        symbol.line,
        symbol.col,
        detail,
        symbol.source_len,
        symbol.params,
        symbol.return_type,
        symbol.variadic,
        symbol.target_type,
        symbol.enum_owner,
        symbol.enum_item,
        symbol.type_param,
        symbol.generic_pattern,
    )


def symbol_with_call_name(symbol: Symbol, call_name: str) -> Symbol:
    if symbol.kind != "proc":
        return symbol
    detail = symbol.detail.replace(symbol.name, call_name, 1)
    return Symbol(
        call_name,
        symbol.kind,
        symbol.uri,
        symbol.line,
        symbol.col,
        detail,
        symbol.source_len,
        symbol.params,
        symbol.return_type,
        symbol.variadic,
        symbol.target_type,
        symbol.enum_owner,
        symbol.enum_item,
        symbol.type_param,
        symbol.generic_pattern,
    )


def generic_proc_symbol_for_call(workspace: Workspace, call_name: str) -> Symbol | None:
    parts = generic_proc_call_parts(call_name)
    if not parts or parts[2]:
        return None
    base, arg, _tail = parts

    concrete_name = f"{base}_{lsp_type_mangle(arg)}"
    concrete = workspace.find_symbol(concrete_name)
    if concrete and concrete.kind == "proc":
        return symbol_with_call_name(concrete, call_name)

    for symbol in workspace.all_symbols():
        if symbol.kind != "proc" or symbol.name != base or not symbol.type_param:
            continue
        if symbol.generic_pattern and not generic_pattern_bind(symbol.generic_pattern, arg, symbol.type_param):
            continue
        return symbol_with_generic_call_detail(symbol, call_name)
    return None


def proc_parameter_labels_for_symbol(symbol: Symbol, call_name: str) -> list[str]:
    if not symbol.params:
        return proc_parameter_labels(generic_proc_detail_for_call(symbol, call_name))
    labels = list(symbol.params)
    parts = generic_proc_call_parts(call_name)
    param = generic_proc_param_name(symbol)
    if parts and param:
        _base, arg, _tail = parts
        bound_arg = generic_proc_bound_arg(symbol, arg) or arg
        labels = [re.sub(rf"\b{re.escape(param)}\b", bound_arg, label) for label in labels]
    return labels


def proc_return_type_for_symbol(symbol: Symbol, call_name: str) -> str:
    ret = symbol.return_type or proc_return_type(generic_proc_detail_for_call(symbol, call_name))
    parts = generic_proc_call_parts(call_name)
    param = generic_proc_param_name(symbol)
    if ret and parts and param:
        _base, arg, _tail = parts
        bound_arg = generic_proc_bound_arg(symbol, arg) or arg
        ret = re.sub(rf"\b{re.escape(param)}\b", bound_arg, ret)
    return ret or "void"


def proc_symbol_has_signature_metadata(symbol: Symbol) -> bool:
    return bool(symbol.params or symbol.return_type or symbol.variadic)


def proc_signature_label_for_symbol(symbol: Symbol, call_name: str) -> str:
    if not proc_symbol_has_signature_metadata(symbol):
        return generic_proc_detail_for_call(symbol, call_name)
    params = ", ".join(proc_parameter_labels_for_symbol(symbol, call_name))
    ret = proc_return_type_for_symbol(symbol, call_name)
    return f"{call_name}:proc({params})->{ret}"


def proc_signature_detail_for_alias_symbol(symbol: Symbol) -> str:
    if not symbol.params:
        return ""
    prefix = "*proc" if canonical_type(symbol.target_type).startswith("*proc") else "proc"
    params = ", ".join(symbol.params)
    ret = symbol.return_type or proc_return_type(symbol.target_type) or "void"
    return f"{prefix}({params})->{ret}"


def proc_signature_symbol_for_type(workspace: Workspace, type_name: str, seen: set[str] | None = None) -> Symbol | None:
    type_name = type_name.strip()
    if seen is None:
        seen = set()
    base = normalize_type_name(type_name)
    if not base or base in seen:
        return None
    seen.add(base)
    symbol = workspace.find_symbol(base)
    if not symbol or symbol.kind != "alias":
        return None
    if symbol.params:
        return symbol
    rhs = symbol.target_type or alias_rhs(symbol.detail)
    if "proc" in rhs:
        return None
    return proc_signature_symbol_for_type(workspace, rhs, seen)


def proc_parameter_labels_for_type(workspace: Workspace, type_name: str) -> list[str]:
    symbol = proc_signature_symbol_for_type(workspace, type_name)
    if symbol and symbol.params:
        return list(symbol.params)
    return proc_parameter_labels(proc_signature_detail_for_type(workspace, type_name))


def proc_signature_detail_for_type(workspace: Workspace, type_name: str, seen: set[str] | None = None) -> str:
    type_name = type_name.strip()
    if "proc" in type_name:
        return type_name
    if seen is None:
        seen = set()
    base = normalize_type_name(type_name)
    if not base or base in seen:
        return ""
    seen.add(base)
    symbol = workspace.find_symbol(base)
    if not symbol or symbol.kind != "alias":
        return ""
    alias_proc_detail = proc_signature_detail_for_alias_symbol(symbol)
    if alias_proc_detail:
        return alias_proc_detail
    rhs = symbol.target_type or alias_rhs(symbol.detail)
    if "proc" in rhs:
        return rhs
    return proc_signature_detail_for_type(workspace, rhs, seen)


def proc_return_type_for_type(workspace: Workspace, type_name: str) -> str:
    type_name = type_name.strip()
    if "proc" in type_name:
        return proc_return_type(type_name)
    symbol = proc_signature_symbol_for_type(workspace, type_name)
    if symbol:
        if symbol.return_type:
            return symbol.return_type
        alias_proc_detail = proc_signature_detail_for_alias_symbol(symbol)
        if alias_proc_detail:
            return proc_return_type(alias_proc_detail)
        rhs = symbol.target_type or alias_rhs(symbol.detail)
        return proc_return_type(rhs)
    detail = proc_signature_detail_for_type(workspace, type_name)
    return proc_return_type(detail) if detail else ""


def callable_parameter_labels_for_name(workspace: Workspace, doc: Document, name: str, line: int | None = None) -> list[str]:
    name = re.sub(r"\s+", "", name)
    symbol = workspace.find_symbol(name) or generic_proc_symbol_for_call(workspace, name)
    if symbol and symbol.kind == "proc":
        return proc_parameter_labels_for_symbol(symbol, name)
    variable = workspace.find_variable_at(doc, name, line) if line is not None else workspace.find_variable(doc, name)
    if variable:
        return proc_parameter_labels_for_type(workspace, variable.type_name)
    chain_type = expr_chain_type(workspace, doc, name, line)
    if chain_type:
        return proc_parameter_labels_for_type(workspace, chain_type)
    return []


def callable_return_type_for_name(workspace: Workspace, doc: Document, name: str, line: int | None = None) -> str:
    name = re.sub(r"\s+", "", name)
    symbol = workspace.find_symbol(name)
    if symbol and symbol.kind == "proc":
        return proc_return_type_for_symbol(symbol, name)
    variable = workspace.find_variable_at(doc, name, line) if line is not None else workspace.find_variable(doc, name)
    if variable:
        return proc_return_type_for_type(workspace, variable.type_name)
    chain_type = expr_chain_type(workspace, doc, name, line)
    if chain_type:
        return proc_return_type_for_type(workspace, chain_type)
    return ""


def infer_simple_expr_type(workspace: Workspace, doc: Document, expr: str, line: int | None = None) -> str:
    expr = sanitize_code_line(expr).strip().rstrip(";")
    if not expr:
        return ""
    if expr.startswith("cast("):
        comma = expr.find(",")
        if comma > len("cast("):
            return expr[len("cast(") : comma].strip()
    if expr.endswith(".&"):
        inner = infer_simple_expr_type(workspace, doc, expr[:-2], line)
        return f"*{canonical_type(inner)}" if inner else ""
    literal = simple_literal_type(expr)
    if literal:
        return literal
    call_match = re.fullmatch(
        r"([A-Za-z_][A-Za-z0-9_]*(?:<[^()\n]+>)?(?:[A-Za-z_][A-Za-z0-9_]*)?(?:(?:\s*\[[^\]]+\])?\s*\.\s*[A-Za-z_][A-Za-z0-9_]*)*)\s*\(.*\)",
        expr,
    )
    if call_match:
        return callable_return_type_for_name(workspace, doc, call_match.group(1), line)
    indexed_match = re.fullmatch(r"(.+)\[[^\]]+\]", expr)
    if indexed_match:
        base_type = infer_simple_expr_type(workspace, doc, indexed_match.group(1), line)
        return indexed_element_type(base_type, workspace)
    chain_type = expr_chain_type(workspace, doc, expr, line)
    if chain_type:
        return chain_type
    ident = re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", expr)
    if ident:
        variable = workspace.find_variable_at(doc, expr, line) if line is not None else workspace.find_variable(doc, expr)
        return variable.type_name if variable else ""
    return ""


def call_context_before_cursor(doc: Document, line: int, col: int) -> tuple[str, int]:
    lines = doc.text.splitlines()
    if line >= len(lines):
        return "", 0
    prefix = sanitize_code_line(lines[line][:col])
    frames: list[dict] = []
    in_string = False
    in_char = False
    escaped = False
    for i, ch in enumerate(prefix):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if in_char:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == "'":
                in_char = False
            continue
        if ch == '"':
            in_string = True
            escaped = False
            continue
        if ch == "'":
            in_char = True
            escaped = False
            continue
        if ch == "(":
            name = callable_name_before_paren(prefix[:i])
            if name in KEYWORDS:
                name = ""
            frames.append({"name": name, "active": 0, "bracket": 0, "brace": 0})
            continue
        if not frames:
            continue
        top = frames[-1]
        if ch == "[":
            top["bracket"] += 1
        elif ch == "]" and top["bracket"] > 0:
            top["bracket"] -= 1
        elif ch == "{":
            top["brace"] += 1
        elif ch == "}" and top["brace"] > 0:
            top["brace"] -= 1
        elif ch == "," and top["bracket"] == 0 and top["brace"] == 0 and top["name"]:
            top["active"] += 1
        elif ch == ")":
            frames.pop()
    for frame in reversed(frames):
        if frame["name"]:
            return str(frame["name"]), int(frame["active"])
    return "", 0


def call_name_before_cursor(doc: Document, line: int, col: int) -> str:
    name, _active_parameter = call_context_before_cursor(doc, line, col)
    return name


def identifier_span_ending_at(text: str, end: int) -> tuple[int, int, str] | None:
    for start, span_end, ident in iter_identifier_spans(text):
        if span_end == end:
            return start, span_end, ident
    return None


def matching_open_bracket_before(text: str, close_pos: int) -> int:
    depth = 0
    for pos in range(close_pos, -1, -1):
        ch = text[pos]
        if ch == "]":
            depth += 1
        elif ch == "[":
            depth -= 1
            if depth == 0:
                return pos
    return -1


def callable_name_before_paren(prefix: str) -> str:
    clean = prefix.rstrip()
    if not clean:
        return ""
    span = identifier_span_ending_at(clean, len(clean))
    if not span:
        return ""
    start, _end, _ident = span

    while True:
        left = clean[:start].rstrip()
        while left.endswith("]"):
            open_pos = matching_open_bracket_before(left, len(left) - 1)
            if open_pos < 0:
                break
            left = left[:open_pos].rstrip()
        if not left.endswith("."):
            break
        before_dot = left[:-1].rstrip()
        prev = identifier_span_ending_at(before_dot, len(before_dot))
        if not prev:
            break
        start = prev[0]

    return re.sub(r"\s+", "", clean[start:])


def signature_help_at(workspace: Workspace, doc: Document, line: int, col: int) -> dict | None:
    name, active_parameter = call_context_before_cursor(doc, line, col)
    symbol = (workspace.find_symbol(name) or generic_proc_symbol_for_call(workspace, name)) if name else None
    label = ""
    parameters: list[dict] = []
    if symbol and symbol.kind == "proc":
        label = proc_signature_label_for_symbol(symbol, name)
        parameters = [{"label": param_label} for param_label in proc_parameter_labels_for_symbol(symbol, name)]
    elif name:
        variable = workspace.find_variable_at(doc, name, line)
        callable_type = variable.type_name if variable else expr_chain_type(workspace, doc, name, line)
        proc_detail = proc_signature_detail_for_type(workspace, callable_type) if callable_type else ""
        if not proc_detail:
            return None
        label = f"{name}: {callable_type} = {proc_detail}"
        parameters = [{"label": param_label} for param_label in proc_parameter_labels_for_type(workspace, callable_type)]
    else:
        return None
    if parameters:
        active_parameter = min(active_parameter, len(parameters) - 1)
    else:
        active_parameter = 0
    return {
        "signatures": [{"label": label, "parameters": parameters}],
        "activeSignature": 0,
        "activeParameter": active_parameter,
    }


def expr_chain_type(workspace: Workspace, doc: Document, expr: str, line: int | None = None) -> str:
    clean = sanitize_code_line(expr)
    base_ident = parse_ident(clean, 0)
    if not base_ident:
        return ""
    base_name, _base_start, base_end = base_ident
    base = workspace.find_variable_at(doc, base_name, line) if line is not None else workspace.find_variable(doc, base_name)
    if not base:
        return ""
    current_type = base.type_name
    pos = skip_optional_index(clean, base_end)
    while True:
        pos = skip_ws(clean, pos)
        if pos >= len(clean):
            return current_type
        if clean[pos] != ".":
            return ""
        field_ident = parse_ident(clean, pos + 1)
        if not field_ident:
            return ""
        field_name, _field_start, field_end = field_ident
        field = workspace.find_field(current_type, field_name)
        if not field:
            return ""
        current_type = substitute_simple_generic_type(field.type_name, current_type, field.type_param)
        pos = skip_optional_index(clean, field_end)


def field_access_at(workspace: Workspace, doc: Document, line: int, col: int) -> FieldSymbol | None:
    lines = document_clean_lines(doc)
    if line >= len(lines):
        return None
    clean_line = lines[line]
    for base_match in re.finditer(r"\b[A-Za-z_][A-Za-z0-9_]*\b", clean_line):
        base = workspace.find_variable_at(doc, base_match.group(0), line)
        if not base:
            continue
        current_type = base.type_name
        pos = skip_optional_index(clean_line, base_match.end())
        while True:
            pos = skip_ws(clean_line, pos)
            if pos >= len(clean_line) or clean_line[pos] != ".":
                break
            field_ident = parse_ident(clean_line, pos + 1)
            if not field_ident:
                break
            field_name, field_start, field_end = field_ident
            field = workspace.find_field(current_type, field_name)
            if field_start <= col <= field_end:
                return field_with_owner_type(field, current_type) if field else None
            if not field:
                break
            current_type = substitute_simple_generic_type(field.type_name, current_type, field.type_param)
            pos = skip_optional_index(clean_line, field_end)
    return None


def iter_missing_field_accesses(workspace: Workspace, doc: Document):
    for line_no, clean_line in enumerate(document_clean_lines(doc)):
        for base_match in re.finditer(r"\b[A-Za-z_][A-Za-z0-9_]*\b", clean_line):
            base = workspace.find_variable_at(doc, base_match.group(0), line_no)
            if not base:
                continue
            current_type = base.type_name
            pos = skip_optional_index(clean_line, base_match.end())
            while True:
                pos = skip_ws(clean_line, pos)
                if pos >= len(clean_line) or clean_line[pos] != ".":
                    break
                field_ident = parse_ident(clean_line, pos + 1)
                if not field_ident:
                    break
                field_name, field_start, field_end = field_ident
                owner_fields = workspace.fields_for_owner(current_type)
                if not owner_fields:
                    owner = normalize_type_name(current_type)
                    if owner in BUILTIN_TYPES:
                        yield line_no, field_start, owner, field_name
                    break
                field = next((candidate for candidate in owner_fields if candidate.name == field_name), None)
                if not field:
                    yield line_no, field_start, normalize_type_name(current_type), field_name
                    break
                current_type = substitute_simple_generic_type(field.type_name, current_type, field.type_param)
                pos = skip_optional_index(clean_line, field_end)


def field_completions_at(workspace: Workspace, doc: Document, line: int, col: int) -> list[FieldSymbol]:
    lines = document_clean_lines(doc)
    if line >= len(lines):
        return []
    prefix = lines[line][:col]
    for base_match in re.finditer(r"\b[A-Za-z_][A-Za-z0-9_]*\b", prefix):
        base = workspace.find_variable_at(doc, base_match.group(0), line)
        if not base:
            continue
        current_type = base.type_name
        pos = skip_optional_index(prefix, base_match.end())
        while True:
            pos = skip_ws(prefix, pos)
            if pos >= len(prefix):
                break
            if prefix[pos] != ".":
                break
            field_ident = parse_ident(prefix, pos + 1)
            if not field_ident:
                if skip_ws(prefix, pos + 1) == len(prefix):
                    return [field_with_owner_type(field, current_type) for field in workspace.fields_for_owner(current_type)]
                break
            field_name, field_start, field_end = field_ident
            if field_start <= len(prefix) <= field_end:
                return [field_with_owner_type(field, current_type) for field in workspace.fields_for_owner(current_type)]
            field = workspace.find_field(current_type, field_name)
            if not field:
                break
            current_type = substitute_simple_generic_type(field.type_name, current_type, field.type_param)
            pos = skip_optional_index(prefix, field_end)
            if pos == len(prefix):
                break
    return []


def reflect_field_completions_at(workspace: Workspace, doc: Document | None, line: int, col: int) -> list[FieldSymbol]:
    if not doc:
        return []
    lines = document_clean_lines(doc)
    if line >= len(lines):
        return []
    prefix = lines[line][:col]
    match = re.search(r"([A-Za-z_][A-Za-z0-9_]*(?:<[^>\n]+>)?)<>\s*\.\s*[A-Za-z_][A-Za-z0-9_]*$", prefix)
    if not match:
        match = re.search(r"([A-Za-z_][A-Za-z0-9_]*(?:<[^>\n]+>)?)<>\s*\.\s*$", prefix)
    if not match:
        return []
    owner_type = match.group(1).strip()
    symbol = workspace.find_symbol(owner_type) or workspace.find_symbol(normalize_type_name(owner_type))
    if not symbol:
        return []
    return reflect_metadata_fields_for_symbol(symbol, owner_type)


def type_field_completions_at(workspace: Workspace, doc: Document | None, line: int, col: int) -> list[FieldSymbol]:
    if not doc:
        return []
    lines = document_clean_lines(doc)
    if line >= len(lines):
        return []
    prefix = lines[line][:col]
    match = re.search(r"([A-Za-z_][A-Za-z0-9_]*(?:<[^>\n]*>)?)\s*\.\s*[A-Za-z_][A-Za-z0-9_]*$", prefix)
    if not match:
        match = re.search(r"([A-Za-z_][A-Za-z0-9_]*(?:<[^>\n]*>)?)\s*\.\s*$", prefix)
    if not match:
        return []
    owner_type = match.group(1).strip()
    if owner_type.endswith("<>"):
        return []
    owner = normalize_type_name(owner_type)
    if not owner or not workspace.find_symbol(owner):
        return []
    return [field_with_owner_type(field, owner_type) for field in workspace.fields_for_owner(owner_type)]


def generic_owner_proc_completions_at(workspace: Workspace, doc: Document | None, line: int, col: int) -> list[Symbol]:
    if not doc:
        return []
    lines = document_clean_lines(doc)
    if line >= len(lines):
        return []
    prefix = lines[line][:col]
    match = re.search(r"([A-Za-z_][A-Za-z0-9_]*)<([^>\n]*)>([A-Za-z_][A-Za-z0-9_]*)?$", prefix)
    if not match:
        return []
    owner = match.group(1)
    arg = match.group(2).strip()
    typed_member = match.group(3) or ""
    if not arg:
        return []
    out: list[Symbol] = []
    seen: set[str] = set()
    for symbol in workspace.all_symbols():
        if symbol.kind != "proc":
            continue
        parts = generic_proc_call_parts(symbol.name)
        if not parts:
            continue
        proc_owner, _param, member = parts
        if proc_owner != owner or not member.startswith(typed_member):
            continue
        call_name = f"{owner}<{arg}>{member}"
        if call_name in seen:
            continue
        seen.add(call_name)
        out.append(symbol_with_generic_call_detail(symbol, call_name))
    out.sort(key=lambda symbol: symbol.name.lower())
    return out


def generic_owner_proc_completion_items_at(
    workspace: Workspace,
    doc: Document | None,
    line: int,
    col: int,
    include_documentation: bool = False,
) -> list[dict]:
    if not doc:
        return []
    lines = document_clean_lines(doc)
    if line >= len(lines):
        return []
    prefix = lines[line][:col]
    match = re.search(r"([A-Za-z_][A-Za-z0-9_]*)<([^>\n]*)>([A-Za-z_][A-Za-z0-9_]*)?$", prefix)
    if not match:
        return []
    owner = match.group(1)
    arg = match.group(2).strip()
    typed_member = match.group(3) or ""
    if not arg:
        return []
    replace_start = match.start(3) if match.start(3) >= 0 else col

    items: list[dict] = []
    seen: set[str] = set()
    for symbol in workspace.all_symbols():
        if symbol.kind != "proc":
            continue
        parts = generic_proc_call_parts(symbol.name)
        if not parts:
            continue
        proc_owner, _param, member = parts
        if proc_owner != owner or not member.startswith(typed_member):
            continue
        call_name = f"{owner}<{arg}>{member}"
        if call_name in seen:
            continue
        seen.add(call_name)
        concrete = symbol_with_generic_call_detail(symbol, call_name)
        snippet = proc_snippet_text(concrete, call_name, member)
        item = {
            "label": member,
            "kind": COMPLETION_KIND.get(concrete.kind, 3),
            "detail": symbol_completion_detail(concrete),
            "filterText": member,
            "insertText": snippet,
            "insertTextFormat": 2,
            "sortText": f"proc:{member}",
            "textEdit": {
                "range": position_to_lsp(line, replace_start, col),
                "newText": snippet,
            },
            "data": completion_data("genericOwnerProc", call_name, concrete.uri, concrete.line, concrete.col),
        }
        if include_documentation:
            item["documentation"] = {"kind": "markdown", "value": hover_markdown_for_symbol(workspace, concrete)}
        items.append(item)

    items.sort(key=lambda item: str(item.get("label", "")).lower())
    return items


class LspServer:
    def __init__(self) -> None:
        self.workspace = Workspace(collect_python_diagnostics=not I_EXE.exists())
        self.compiler_diag_cache: dict[str, tuple[str, float | None, float | None, bool, list[Diagnostic]]] = {}
        self.send_lock = threading.Lock()
        self.compiler_diag_lock = threading.Lock()
        self.diagnostic_lock = threading.Lock()
        self.workspace_symbol_state_lock = threading.Lock()
        self.workspace_symbol_run_lock = threading.Lock()
        self.diagnostic_generation: dict[str, int] = {}
        self.diagnostic_timers: dict[str, threading.Timer] = {}
        self.pending_diagnostics: dict[str, tuple[Document, int]] = {}
        self.workspace_symbol_generation: dict[str, int] = {}
        self.workspace_symbol_timers: dict[str, threading.Timer] = {}
        self.pending_workspace_symbols: dict[str, tuple[Document, int]] = {}
        self.compiler_diag_published_uris: dict[str, set[str]] = {}
        raw_debounce_ms = os.environ.get("I_LSP_DIAGNOSTIC_DEBOUNCE_MS", "150")
        try:
            self.diagnostic_debounce_seconds = max(float(raw_debounce_ms), 0.0) / 1000.0
        except ValueError:
            self.diagnostic_debounce_seconds = 0.15
        raw_symbol_debounce_ms = os.environ.get("I_LSP_SYMBOL_DEBOUNCE_MS", "750")
        try:
            self.workspace_symbol_debounce_seconds = max(float(raw_symbol_debounce_ms), 0.0) / 1000.0
        except ValueError:
            self.workspace_symbol_debounce_seconds = 0.75
        raw_rich_semantic_tokens = os.environ.get("I_LSP_RICH_SEMANTIC_TOKENS", "")
        self.rich_semantic_tokens = raw_rich_semantic_tokens.lower() in ("1", "true", "yes", "on")

    def send(self, payload: dict) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        with self.send_lock:
            sys.stdout.buffer.write(header)
            sys.stdout.buffer.write(body)
            sys.stdout.buffer.flush()

    def diagnostics_payload_for_doc(self, doc: Document) -> tuple[dict, str, int]:
        compiler_available, compiler_diagnostics = self.compiler_diagnostics(doc)
        diagnostics = (
            [diag for diag in compiler_diagnostics if not diag.uri or diag.uri == doc.uri]
            if compiler_available
            else doc.diagnostics
        )
        source = "compiler" if compiler_available else "python"
        return (
            {
                "jsonrpc": "2.0",
                "method": "textDocument/publishDiagnostics",
                "params": {
                    "uri": doc.uri,
                    "diagnostics": [diagnostic_to_lsp(d) for d in diagnostics],
                },
            },
            source,
            len(diagnostics),
        )

    def compiler_diagnostics_payloads_for_doc(
        self,
        doc: Document,
        diagnostics: list[Diagnostic],
        publish_uris: set[str] | None = None,
    ) -> list[dict]:
        diagnostics_by_uri: dict[str, list[Diagnostic]] = {doc.uri: []}
        for uri in publish_uris or ():
            diagnostics_by_uri.setdefault(uri, [])
        for diag in diagnostics:
            diagnostics_by_uri.setdefault(diag.uri or doc.uri, []).append(diag)
        ordered_uris = [doc.uri]
        ordered_uris.extend(sorted(uri for uri in diagnostics_by_uri.keys() if uri != doc.uri))
        payloads = []
        for uri in ordered_uris:
            uri_diagnostics = diagnostics_by_uri.get(uri, [])
            payloads.append({
                "jsonrpc": "2.0",
                "method": "textDocument/publishDiagnostics",
                "params": {
                    "uri": uri,
                    "diagnostics": [diagnostic_to_lsp(diag) for diag in uri_diagnostics],
                },
            })
        return payloads

    def notify_diagnostics(self, doc: Document) -> None:
        payload, source, count = self.diagnostics_payload_for_doc(doc)
        trace(f"publish diagnostics uri={doc.uri} source={source} count={count}")
        self.send(payload)

    def schedule_diagnostics(self, doc: Document) -> None:
        uri = doc.uri
        with self.diagnostic_lock:
            generation = self.diagnostic_generation.get(uri, 0) + 1
            self.diagnostic_generation[uri] = generation
            old_timer = self.diagnostic_timers.pop(uri, None)
            self.pending_diagnostics[uri] = (doc, generation)
        if old_timer:
            old_timer.cancel()
        trace(f"schedule diagnostics uri={uri} generation={generation} debounce_ms={self.diagnostic_debounce_seconds * 1000.0:.0f}")
        if self.diagnostic_debounce_seconds <= 0.0:
            self.run_scheduled_diagnostics(doc, generation)
            return
        timer = threading.Timer(self.diagnostic_debounce_seconds, self.run_scheduled_diagnostics, args=(doc, generation))
        timer.daemon = True
        with self.diagnostic_lock:
            self.diagnostic_timers[uri] = timer
        timer.start()

    def run_scheduled_diagnostics(self, doc: Document, generation: int) -> None:
        uri = doc.uri
        with self.diagnostic_lock:
            current = self.workspace.documents.get(uri)
            if self.diagnostic_generation.get(uri) != generation or current is None or current.text != doc.text:
                trace(f"skip stale diagnostics uri={uri} generation={generation}")
                return
        started = time.perf_counter()
        available, diagnostics = run_compiler_diagnostics(doc)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if available:
            source = "compiler"
            count = len(diagnostics)
            current_publish_uris = {doc.uri}
            current_publish_uris.update(diag.uri or doc.uri for diag in diagnostics)
        else:
            payload, source, count = self.diagnostics_payload_for_doc(doc)
            payloads = [payload]
            current_publish_uris = set()
        with self.diagnostic_lock:
            current = self.workspace.documents.get(uri)
            if self.diagnostic_generation.get(uri) != generation or current is None or current.text != doc.text:
                trace(f"drop stale diagnostics uri={uri} generation={generation}")
                return
            self.diagnostic_timers.pop(uri, None)
            self.pending_diagnostics.pop(uri, None)
            if available:
                previous_publish_uris = self.compiler_diag_published_uris.get(uri, set())
                publish_uris = previous_publish_uris | current_publish_uris
                self.compiler_diag_published_uris[uri] = set(current_publish_uris)
            else:
                self.compiler_diag_published_uris.pop(uri, None)
                publish_uris = set()
        if available:
            self.cache_compiler_diagnostics(doc, True, diagnostics)
            payloads = self.compiler_diagnostics_payloads_for_doc(doc, diagnostics, publish_uris)
        trace(
            f"publish scheduled diagnostics uri={uri} source={source} count={count} "
            f"generation={generation} elapsed_ms={elapsed_ms:.1f}"
        )
        for payload in payloads:
            self.send(payload)

    def flush_pending_diagnostics(self, uri: str | None = None) -> None:
        with self.diagnostic_lock:
            items = [
                (pending_uri, doc, generation)
                for pending_uri, (doc, generation) in self.pending_diagnostics.items()
                if uri is None or pending_uri == uri
            ]
            for pending_uri, _doc, _generation in items:
                timer = self.diagnostic_timers.pop(pending_uri, None)
                if timer:
                    timer.cancel()
        for _pending_uri, doc, generation in items:
            self.run_scheduled_diagnostics(doc, generation)

    def cancel_pending_diagnostics(self) -> None:
        with self.diagnostic_lock:
            timers = list(self.diagnostic_timers.values())
            self.diagnostic_timers.clear()
            self.pending_diagnostics.clear()
        for timer in timers:
            timer.cancel()

    def schedule_workspace_symbols(self, doc: Document) -> None:
        uri = doc.uri
        with self.workspace_symbol_state_lock:
            generation = self.workspace_symbol_generation.get(uri, 0) + 1
            self.workspace_symbol_generation[uri] = generation
            old_timer = self.workspace_symbol_timers.pop(uri, None)
            self.pending_workspace_symbols[uri] = (doc, generation)
        if old_timer:
            old_timer.cancel()
        trace(
            f"schedule workspace symbols uri={uri} generation={generation} "
            f"debounce_ms={self.workspace_symbol_debounce_seconds * 1000.0:.0f}"
        )
        if self.workspace_symbol_debounce_seconds <= 0.0:
            self.run_scheduled_workspace_symbols(doc, generation)
            return
        timer = threading.Timer(self.workspace_symbol_debounce_seconds, self.run_scheduled_workspace_symbols, args=(doc, generation))
        timer.daemon = True
        with self.workspace_symbol_state_lock:
            self.workspace_symbol_timers[uri] = timer
        timer.start()

    def run_scheduled_workspace_symbols(self, doc: Document, generation: int) -> None:
        uri = doc.uri
        with self.workspace_symbol_state_lock:
            current = self.workspace.documents.get(uri)
            if self.workspace_symbol_generation.get(uri) != generation or current is None or current.text != doc.text:
                trace(f"skip stale workspace symbols uri={uri} generation={generation}")
                return
        started = time.perf_counter()
        reference_locations = 0
        with self.workspace_symbol_run_lock:
            available, symbols, variables, fields = self.workspace.compiler_workspace_symbols(doc)
            if available:
                self.workspace.apply_compiler_workspace_symbols(doc)
                reference_locations = sum(len(locations) for locations in self.workspace.identifier_reference_index().values())
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        with self.workspace_symbol_state_lock:
            current = self.workspace.documents.get(uri)
            if self.workspace_symbol_generation.get(uri) != generation or current is None or current.text != doc.text:
                trace(f"drop stale workspace symbols uri={uri} generation={generation}")
                return
            self.workspace_symbol_timers.pop(uri, None)
            self.pending_workspace_symbols.pop(uri, None)
        trace(
            f"prefetched workspace symbols uri={uri} available={available} "
            f"symbols={len(symbols)} variables={len(variables)} fields={len(fields)} "
            f"reference_locations={reference_locations} generation={generation} elapsed_ms={elapsed_ms:.1f}"
        )

    def flush_pending_workspace_symbols(self, uri: str | None = None) -> None:
        with self.workspace_symbol_state_lock:
            items = [
                (pending_uri, doc, generation)
                for pending_uri, (doc, generation) in self.pending_workspace_symbols.items()
                if uri is None or pending_uri == uri
            ]
            for pending_uri, _doc, _generation in items:
                timer = self.workspace_symbol_timers.pop(pending_uri, None)
                if timer:
                    timer.cancel()
        for _pending_uri, doc, generation in items:
            self.run_scheduled_workspace_symbols(doc, generation)

    def cancel_pending_workspace_symbols(self, uri: str | None = None) -> None:
        with self.workspace_symbol_state_lock:
            if uri is None:
                timers = list(self.workspace_symbol_timers.values())
                self.workspace_symbol_timers.clear()
                self.pending_workspace_symbols.clear()
            else:
                timer = self.workspace_symbol_timers.pop(uri, None)
                timers = [timer] if timer else []
                self.pending_workspace_symbols.pop(uri, None)
        for timer in timers:
            timer.cancel()

    def notify_workspace_diagnostics(self) -> None:
        for doc in self.workspace.documents.values():
            self.notify_diagnostics(doc)

    def compiler_diagnostics(self, doc: Document) -> tuple[bool, list[Diagnostic]]:
        mtime: float | None = None
        if doc.path:
            try:
                mtime = doc.path.stat().st_mtime
            except OSError:
                mtime = None
        compiler_mtime: float | None = None
        try:
            compiler_mtime = I_EXE.stat().st_mtime
        except OSError:
            compiler_mtime = None
        with self.compiler_diag_lock:
            cached = self.compiler_diag_cache.get(doc.uri)
            if cached and cached[0] == doc.text and cached[1] == mtime and cached[2] == compiler_mtime:
                trace(f"compiler diagnostics cache uri={doc.uri} available={cached[3]} count={len(cached[4])}")
                return cached[3], cached[4]
        started = time.perf_counter()
        available, diagnostics = run_compiler_diagnostics(doc)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        trace(
            f"compiler diagnostics run uri={doc.uri} "
            f"available={available} count={len(diagnostics)} elapsed_ms={elapsed_ms:.1f}"
        )
        self.cache_compiler_diagnostics(doc, available, diagnostics)
        return available, diagnostics

    def cache_compiler_diagnostics(self, doc: Document, available: bool, diagnostics: list[Diagnostic]) -> None:
        mtime: float | None = None
        if doc.path:
            try:
                mtime = doc.path.stat().st_mtime
            except OSError:
                mtime = None
        compiler_mtime: float | None = None
        try:
            compiler_mtime = I_EXE.stat().st_mtime
        except OSError:
            compiler_mtime = None
        with self.compiler_diag_lock:
            self.compiler_diag_cache[doc.uri] = (doc.text, mtime, compiler_mtime, available, diagnostics)

    def ensure_compiler_workspace_symbols(self, uri: str) -> bool:
        doc = self.workspace.documents.get(uri)
        if not doc:
            return False
        started = time.perf_counter()
        with self.workspace_symbol_run_lock:
            available = self.workspace.apply_compiler_workspace_symbols(doc)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        trace(f"workspace symbols ensure uri={uri} available={available} elapsed_ms={elapsed_ms:.1f}")
        return available

    def ensure_any_compiler_workspace_symbols(self) -> bool:
        for uri in list(self.workspace.documents.keys()):
            if self.ensure_compiler_workspace_symbols(uri):
                return True
        return False

    def workspace_symbols_pending(self, uri: str) -> bool:
        with self.workspace_symbol_state_lock:
            return uri in self.pending_workspace_symbols

    def handle(self, msg: dict) -> None:
        method = msg.get("method")
        msg_id = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            self.send(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "capabilities": {
                            "textDocumentSync": 1,
                            "documentSymbolProvider": True,
                            "definitionProvider": True,
                            "hoverProvider": True,
                            "documentHighlightProvider": True,
                            "completionProvider": {"triggerCharacters": [".", "<", "\"", "/"], "resolveProvider": True},
                            "referencesProvider": True,
                            "renameProvider": {"prepareProvider": True},
                            "workspaceSymbolProvider": True,
                            "signatureHelpProvider": {"triggerCharacters": ["(", ","]},
                            "semanticTokensProvider": {
                                "legend": {
                                    "tokenTypes": SEMANTIC_TOKEN_TYPES,
                                    "tokenModifiers": SEMANTIC_TOKEN_MODIFIERS,
                                },
                                "full": True,
                            },
                        }
                    },
                }
            )
        elif method == "shutdown":
            self.cancel_pending_diagnostics()
            self.cancel_pending_workspace_symbols()
            self.send({"jsonrpc": "2.0", "id": msg_id, "result": None})
        elif method == "textDocument/didOpen":
            text_doc = params.get("textDocument") or {}
            trace(f"didOpen uri={text_doc.get('uri', '')}")
            doc = self.workspace.upsert_paths_only(text_doc.get("uri", ""), text_doc.get("text", ""))
            self.schedule_diagnostics(doc)
            self.schedule_workspace_symbols(doc)
        elif method == "textDocument/didChange":
            text_doc = params.get("textDocument") or {}
            changes = params.get("contentChanges") or []
            if changes:
                uri = text_doc.get("uri", "")
                trace(f"didChange uri={uri}")
                previous_doc = self.workspace.documents.get(uri)
                new_text = changes[-1].get("text", "")
                if previous_doc and previous_doc.text == new_text:
                    trace(f"didChange uri={uri} skipped=no-op")
                    return
                previous_applied_key = self.workspace.applied_compiler_workspace_symbol_key
                previous_applied_uris = set(self.workspace.applied_compiler_workspace_symbol_uris)
                doc = self.workspace.update_dirty_text(uri, new_text)
                if previous_applied_key and previous_doc and previous_doc.uri in previous_applied_uris:
                    self.workspace.applied_compiler_workspace_symbol_key = self.workspace.compiler_workspace_symbol_key(doc)
                    self.workspace.applied_compiler_workspace_symbol_uris = set(previous_applied_uris)
                    trace(f"reuse workspace symbols uri={uri} reason=dirty-fast-path")
                self.schedule_diagnostics(doc)
                self.schedule_workspace_symbols(doc)
        elif method == "textDocument/documentSymbol":
            uri = (params.get("textDocument") or {}).get("uri", "")
            doc = self.workspace.documents.get(uri)
            result = []
            if doc:
                if self.workspace_symbols_pending(uri):
                    trace(f"document symbols uri={uri} source=current-buffer reason=symbol-prefetch-pending")
                else:
                    self.ensure_compiler_workspace_symbols(uri)
                doc = self.workspace.documents.get(uri) or doc
                result = [symbol_to_lsp(s, doc) for s in doc.symbols if s.kind != "enumMember"]
            self.send({"jsonrpc": "2.0", "id": msg_id, "result": result})
        elif method == "workspace/symbol":
            self.ensure_any_compiler_workspace_symbols()
            query = str(params.get("query", ""))
            result = [workspace_symbol_to_lsp(symbol) for symbol in self.workspace.workspace_symbols(query)]
            self.send({"jsonrpc": "2.0", "id": msg_id, "result": result})
        elif method == "textDocument/definition":
            result = None
            import_symbol = self.import_at_request(params)
            if import_symbol:
                result = import_location_to_lsp(import_symbol)
            else:
                cinclude_symbol = self.cinclude_at_request(params)
                if cinclude_symbol:
                    result = cinclude_location_to_lsp(cinclude_symbol)
                else:
                    uri = (params.get("textDocument") or {}).get("uri", "")
                    self.ensure_compiler_workspace_symbols(uri)
                    symbol = self.symbol_at_request(params)
                    if symbol:
                        result = location_to_lsp(symbol)
            self.send({"jsonrpc": "2.0", "id": msg_id, "result": result})
        elif method == "textDocument/hover":
            result = None
            import_symbol = self.import_at_request(params)
            if import_symbol:
                result = {"contents": {"kind": "markdown", "value": import_hover_markdown(import_symbol)}}
            else:
                cinclude_symbol = self.cinclude_at_request(params)
                if cinclude_symbol:
                    result = {"contents": {"kind": "markdown", "value": cinclude_hover_markdown(cinclude_symbol)}}
                else:
                    uri = (params.get("textDocument") or {}).get("uri", "")
                    self.ensure_compiler_workspace_symbols(uri)
                    symbol = self.symbol_at_request(params)
                    if symbol:
                        result = {"contents": {"kind": "markdown", "value": hover_markdown_for_symbol(self.workspace, symbol)}}
            self.send({"jsonrpc": "2.0", "id": msg_id, "result": result})
        elif method == "textDocument/completion":
            uri = (params.get("textDocument") or {}).get("uri", "")
            pos = params.get("position") or {}
            doc = self.workspace.documents.get(uri)
            import_items = import_path_completions_at(
                self.workspace,
                doc,
                int(pos.get("line", 0)),
                int(pos.get("character", 0)),
            )
            if import_items:
                self.send({"jsonrpc": "2.0", "id": msg_id, "result": {"isIncomplete": False, "items": import_items}})
                return
            cinclude_items = cinclude_path_completions_at(
                self.workspace,
                doc,
                int(pos.get("line", 0)),
                int(pos.get("character", 0)),
            )
            if cinclude_items:
                self.send({"jsonrpc": "2.0", "id": msg_id, "result": {"isIncomplete": False, "items": cinclude_items}})
                return
            if self.workspace_symbols_pending(uri):
                trace(f"completion uri={uri} source=current-buffer reason=symbol-prefetch-pending")
            else:
                self.ensure_compiler_workspace_symbols(uri)
            doc = self.workspace.documents.get(uri)
            request_line = int(pos.get("line", 0))
            request_col = int(pos.get("character", 0))
            reflect_fields = reflect_field_completions_at(
                self.workspace,
                doc,
                request_line,
                request_col,
            )
            if reflect_fields:
                items = [
                    reflect_field_completion_to_lsp(self.workspace, field, include_documentation=False)
                    for field in reflect_fields
                ]
            else:
                fields = (
                    field_completions_at(self.workspace, doc, request_line, request_col)
                    if doc
                    else []
                )
            if not reflect_fields and fields:
                items = [field_completion_to_lsp(self.workspace, field, include_documentation=False) for field in fields]
            elif not reflect_fields:
                type_fields = type_field_completions_at(
                    self.workspace,
                    doc,
                    request_line,
                    request_col,
                )
                struct_field_items = struct_literal_field_completions_at(
                    self.workspace,
                    doc,
                    request_line,
                    request_col,
                )
                argument_items = proc_argument_completions_at(
                    self.workspace,
                    doc,
                    request_line,
                    request_col,
                )
                enum_items = enum_completions_at(
                    self.workspace,
                    doc,
                    request_line,
                    request_col,
                )
                enum_dot_items = enum_dot_completions_at(
                    self.workspace,
                    doc,
                    request_line,
                    request_col,
                )
                expected_items = expected_type_completions_at(
                    self.workspace,
                    doc,
                    request_line,
                    request_col,
                )
                type_field_items = [
                    field_completion_to_lsp(self.workspace, field, include_documentation=False) for field in type_fields
                ]
                generic_owner_proc_items = generic_owner_proc_completion_items_at(
                    self.workspace,
                    doc,
                    request_line,
                    request_col,
                )
                context_items = (
                    type_field_items
                    or enum_dot_items
                    or generic_owner_proc_items
                    or struct_field_items
                    or argument_items
                    or enum_items
                    or expected_items
                )
                items = context_items if context_items else [
                    variable_completion_to_lsp(self.workspace, symbol, include_documentation=False)
                    if isinstance(symbol, VariableSymbol)
                    else completion_to_lsp(self.workspace, symbol, include_documentation=False)
                    for symbol in self.workspace.completion_symbols_at(doc, request_line)
                ]
            self.send({"jsonrpc": "2.0", "id": msg_id, "result": {"isIncomplete": False, "items": items}})
        elif method == "completionItem/resolve":
            self.send({"jsonrpc": "2.0", "id": msg_id, "result": resolve_completion_item(self.workspace, params)})
        elif method == "textDocument/references":
            uri = (params.get("textDocument") or {}).get("uri", "")
            self.ensure_compiler_workspace_symbols(uri)
            result = self.references_at_request(params)
            self.send({"jsonrpc": "2.0", "id": msg_id, "result": result})
        elif method == "textDocument/documentHighlight":
            uri = (params.get("textDocument") or {}).get("uri", "")
            if self.workspace_symbols_pending(uri):
                trace(f"document highlight uri={uri} source=current-buffer reason=symbol-prefetch-pending")
                result = []
            else:
                self.ensure_compiler_workspace_symbols(uri)
                result = self.document_highlights_at_request(params)
            self.send({"jsonrpc": "2.0", "id": msg_id, "result": result})
        elif method == "textDocument/rename":
            uri = (params.get("textDocument") or {}).get("uri", "")
            self.ensure_compiler_workspace_symbols(uri)
            new_name = params.get("newName", "")
            result = self.rename_at_request(params, new_name)
            self.send({"jsonrpc": "2.0", "id": msg_id, "result": result})
        elif method == "textDocument/prepareRename":
            uri = (params.get("textDocument") or {}).get("uri", "")
            self.ensure_compiler_workspace_symbols(uri)
            result = self.prepare_rename_at_request(params)
            self.send({"jsonrpc": "2.0", "id": msg_id, "result": result})
        elif method == "textDocument/semanticTokens/full":
            uri = (params.get("textDocument") or {}).get("uri", "")
            doc = self.workspace.documents.get(uri)
            if not self.rich_semantic_tokens:
                trace(f"semantic tokens uri={uri} source=lexical reason=rich-disabled")
                result = {"data": lexical_semantic_tokens_for_doc(doc)} if doc else {"data": []}
            elif self.workspace_symbols_pending(uri):
                trace(f"semantic tokens uri={uri} source=lexical reason=symbol-prefetch-pending")
                result = {"data": lexical_semantic_tokens_for_doc(doc)} if doc else {"data": []}
            else:
                self.ensure_compiler_workspace_symbols(uri)
                doc = self.workspace.documents.get(uri)
                result = {"data": semantic_tokens_for_doc(self.workspace, doc)} if doc else {"data": []}
            self.send({"jsonrpc": "2.0", "id": msg_id, "result": result})
        elif method == "textDocument/signatureHelp":
            result = None
            uri = (params.get("textDocument") or {}).get("uri", "")
            pos = params.get("position") or {}
            self.ensure_compiler_workspace_symbols(uri)
            doc = self.workspace.documents.get(uri)
            if doc:
                result = signature_help_at(self.workspace, doc, int(pos.get("line", 0)), int(pos.get("character", 0)))
            self.send({"jsonrpc": "2.0", "id": msg_id, "result": result})

    def import_at_request(self, params: dict) -> ImportSymbol | None:
        uri = (params.get("textDocument") or {}).get("uri", "")
        pos = params.get("position") or {}
        doc = self.workspace.documents.get(uri)
        if not doc:
            return None
        return import_at(doc, int(pos.get("line", 0)), int(pos.get("character", 0)))

    def cinclude_at_request(self, params: dict) -> CIncludeSymbol | None:
        uri = (params.get("textDocument") or {}).get("uri", "")
        pos = params.get("position") or {}
        doc = self.workspace.documents.get(uri)
        if not doc:
            return None
        return cinclude_at(doc, int(pos.get("line", 0)), int(pos.get("character", 0)))

    def symbol_at_request(self, params: dict) -> Symbol | FieldSymbol | VariableSymbol | None:
        uri = (params.get("textDocument") or {}).get("uri", "")
        pos = params.get("position") or {}
        doc = self.workspace.documents.get(uri)
        line = int(pos.get("line", 0))
        col = int(pos.get("character", 0))
        if doc:
            field = field_access_at(self.workspace, doc, line, col)
            if field:
                return field
            ident, start, _end = token_range_at(doc.text, line, col)
            if ident:
                field = field_declaration_at(doc, line, start, ident)
                if field:
                    return field
                enum_member = enum_member_declaration_at(doc, line, start, ident)
                if enum_member:
                    return enum_member
        name = self.name_at_request(params)
        if not name:
            return None
        if doc:
            variable = self.workspace.find_variable_at(doc, name, line)
            if variable:
                return variable
        symbol = self.workspace.find_symbol(name)
        if symbol:
            return symbol_with_generic_call_detail(symbol, name)
        generic_symbol = generic_proc_symbol_for_call(self.workspace, name)
        if generic_symbol:
            return generic_symbol
        enum_usage = self.workspace.find_enum_member_usage(name)
        if enum_usage:
            return enum_usage
        return None

    def name_at_request(self, params: dict) -> str:
        uri = (params.get("textDocument") or {}).get("uri", "")
        pos = params.get("position") or {}
        doc = self.workspace.documents.get(uri)
        if not doc:
            return ""
        return token_at(doc.text, int(pos.get("line", 0)), int(pos.get("character", 0)))

    def references_at_request(self, params: dict) -> list[dict]:
        symbol = self.symbol_at_request(params)
        if isinstance(symbol, FieldSymbol):
            return self.workspace.field_references(symbol)
        if isinstance(symbol, Symbol) and symbol.kind == "enumMember":
            return self.workspace.enum_member_references(symbol)
        if isinstance(symbol, VariableSymbol):
            return self.workspace.variable_references(symbol)
        name = self.name_at_request(params)
        return self.workspace.references(name) if name else []

    def document_highlights_at_request(self, params: dict) -> list[dict]:
        uri = (params.get("textDocument") or {}).get("uri", "")
        highlights: list[dict] = []
        for location in self.references_at_request(params):
            if location.get("uri") != uri:
                continue
            highlights.append({"range": location.get("range"), "kind": 1})
        return highlights

    def rename_at_request(self, params: dict, new_name: str) -> dict:
        if not new_name:
            return {"changes": {}}
        symbol = self.symbol_at_request(params)
        if isinstance(symbol, FieldSymbol):
            return self.workspace.field_rename_edits(symbol, new_name)
        if isinstance(symbol, Symbol) and symbol.kind == "enumMember":
            return self.workspace.enum_member_rename_edits(symbol, new_name)
        if isinstance(symbol, VariableSymbol):
            return self.workspace.variable_rename_edits(symbol, new_name)
        name = self.name_at_request(params)
        return self.workspace.rename_edits(name, new_name) if name else {"changes": {}}

    def prepare_rename_at_request(self, params: dict) -> dict | None:
        uri = (params.get("textDocument") or {}).get("uri", "")
        pos = params.get("position") or {}
        doc = self.workspace.documents.get(uri)
        if not doc:
            return None
        line = int(pos.get("line", 0))
        col = int(pos.get("character", 0))
        ident, start, end = token_range_at(doc.text, line, col)
        if not ident:
            return None
        symbol = self.symbol_at_request(params)
        if not symbol:
            return None
        return position_to_lsp(line, start, end)

    def run(self) -> None:
        while True:
            headers: dict[str, str] = {}
            while True:
                line = sys.stdin.buffer.readline()
                if not line:
                    return
                line = line.decode("ascii", errors="replace")
                if line in ("\r\n", "\n", ""):
                    break
                key, _, value = line.partition(":")
                headers[key.lower()] = value.strip()
            length = int(headers.get("content-length", "0"))
            if length <= 0:
                continue
            body = sys.stdin.buffer.read(length).decode("utf-8")
            self.handle(json.loads(body))


def command_check(paths: list[Path]) -> int:
    workspace = Workspace()
    exit_code = 0
    for path in paths:
        doc = workspace.open_path(path)
        print(f"{path}: {len(doc.symbols)} symbols, {len(doc.diagnostics)} diagnostics")
        for symbol in doc.symbols:
            print(f"  {symbol.kind:6} {symbol.name} @ {symbol.line + 1}:{symbol.col + 1}")
        for diag in doc.diagnostics:
            exit_code = 1
            print(f"  error {diag.line + 1}:{diag.col + 1}: {diag.message}")
    return exit_code


def main() -> None:
    parser = argparse.ArgumentParser(description="Prototype language server for I.")
    parser.add_argument("--check", nargs="*", type=Path, help="Analyze files without starting LSP stdio.")
    args = parser.parse_args()

    if args.check is not None:
        raise SystemExit(command_check(args.check))

    LspServer().run()


if __name__ == "__main__":
    main()
