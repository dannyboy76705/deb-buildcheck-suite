#!/usr/bin/env python3
"""
configcheck-cmake.py - Parse a CMake project's CMakeLists.txt files
statically (without running cmake) to list what it checks for - mandatory
and optional - which Debian package provides each, and whether that
package is installed.

The CMake counterpart of configcheck.py (which reads autoconf `configure`
scripts). Part of deb-buildcheck-suite. It only IMPORTS configcheck.py
(package lookup, Contents scanning, table rendering) and never modifies
it, so nothing here can affect the autotools tools. Must live in the same
directory as configcheck.py.

Usage: ./configcheck-cmake.py /path/to/CMakeLists.txt-or-project-dir
                              [--json] [-o FILE] [-D NAME=VALUE ...]
"""

import argparse
import contextlib
import dataclasses
import glob
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

try:
    import configcheck as cc
    from configcheck import Check, Option, VersionReq, Column, render_table, get_term_width
except ModuleNotFoundError:
    print("error: configcheck.py not found - configcheck-cmake.py shares its package/library",
          file=sys.stderr)
    print("resolution and table code with it, so both files need to be in the same directory.",
          file=sys.stderr)
    sys.exit(1)


# ===========================================================================
# Stage 1: tokenizer. CMake's syntax is small but not regular: commands are
# `name(args)`, args may be unquoted, "quoted" (can span lines) or
# [[bracketed]], comments are `# ...` or `#[[ ... ]]`, and parentheses nest
# inside arguments (that's how `if((A OR B) AND C)` is written).
# ===========================================================================
IDENT_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
BRACKET_OPEN_RE = re.compile(r'\[(=*)\[')


@dataclass
class Arg:
    text: str
    quoted: bool


@dataclass
class Cmd:
    name: str
    args: list
    line: int
    file: str


def _skip_space_comments(text: str, i: int, line: int) -> "tuple[int, int]":
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\n":
            line += 1
            i += 1
        elif c in " \t\r\f\v":
            i += 1
        elif c == "#":
            m = BRACKET_OPEN_RE.match(text, i + 1)
            if m:
                close = "]" + m.group(1) + "]"
                j = text.find(close, m.end())
                j = n if j < 0 else j + len(close)
                line += text.count("\n", i, j)
                i = j
            else:
                j = text.find("\n", i)
                i = n if j < 0 else j
        else:
            break
    return i, line


_MAX_VALUE = 20000
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}


def _read_quoted(text: str, i: int, line: int) -> "tuple[str, int, int]":
    """text[i] is the opening quote; returns (contents, index after closing quote, line)."""
    n = len(text)
    j = i + 1
    buf = []
    while j < n and text[j] != '"':
        c = text[j]
        if c == "\\" and j + 1 < n:
            nxt = text[j + 1]
            if nxt == "\n":              # backslash-newline: line continuation
                line += 1
                j += 2
                continue
            buf.append(_ESCAPES[nxt] if nxt in _ESCAPES else "\\" + nxt)   # keeps "\;" and "\$" as written
            j += 2
            continue
        if c == "\n":
            line += 1
        buf.append(c)
        j += 1
    return "".join(buf), min(j + 1, n), line


def _parse_args(text: str, i: int, line: int) -> "tuple[list[Arg], int, int]":
    n = len(text)
    args: "list[Arg]" = []
    depth = 0
    while True:
        i, line = _skip_space_comments(text, i, line)
        if i >= n:
            return args, i, line
        c = text[i]
        if c == ")":
            i += 1
            if depth == 0:
                return args, i, line
            depth -= 1
            args.append(Arg(")", False))
            continue
        if c == "(":
            depth += 1
            args.append(Arg("(", False))
            i += 1
            continue
        if c == '"':
            s, i, line = _read_quoted(text, i, line)
            args.append(Arg(s, True))
            continue
        m = BRACKET_OPEN_RE.match(text, i)
        if m:
            close = "]" + m.group(1) + "]"
            j = text.find(close, m.end())
            body = text[m.end():] if j < 0 else text[m.end():j]
            end = n if j < 0 else j + len(close)
            line += text.count("\n", i, end)
            args.append(Arg(body.lstrip("\n"), True))
            i = end
            continue
        # unquoted argument
        buf = []
        while i < n:
            c = text[i]
            if c in " \t\r\f\v\n()#":
                break
            if c == '"':                  # legacy: -DFOO="bar" glued into one unquoted token
                s, i, line = _read_quoted(text, i, line)
                buf.append('"' + s + '"')
                continue
            if c == "\\" and i + 1 < n:
                buf.append(text[i:i + 2])
                i += 2
                continue
            buf.append(c)
            i += 1
        args.append(Arg("".join(buf), False))


def tokenize(text: str, filename: str) -> "list[Cmd]":
    cmds: "list[Cmd]" = []
    i, line, n = 0, 1, len(text)
    while i < n:
        i, line = _skip_space_comments(text, i, line)
        if i >= n:
            break
        m = IDENT_RE.match(text, i)
        if not m:
            i += 1
            continue
        start_line = line
        j, line2 = _skip_space_comments(text, m.end(), line)
        if j >= n or text[j] != "(":
            i, line = j, line2
            continue
        args, j, line = _parse_args(text, j + 1, line2)
        cmds.append(Cmd(m.group(0), args, start_line, filename))
        i = j
    return cmds


# ===========================================================================
# Stage 2: structure. if/elseif/else/endif, foreach/while, function/macro
# become nested nodes so the evaluator can decide which branches apply.
# ===========================================================================
@dataclass
class IfNode:
    branches: list           # [(Cmd, [nodes])]  -  if / elseif / else


@dataclass
class LoopNode:
    cmd: Cmd
    body: list


@dataclass
class DefNode:
    cmd: Cmd                 # function(name params...) / macro(name params...)
    body: list


_BLOCK_END = {"if": "endif", "foreach": "endforeach", "while": "endwhile",
              "function": "endfunction", "macro": "endmacro", "block": "endblock"}


def build_ast(cmds: "list[Cmd]") -> list:
    def block(i: int, stop: "set[str]"):
        nodes: list = []
        while i < len(cmds):
            cmd = cmds[i]
            name = cmd.name.lower()
            if name in stop:
                return nodes, i
            if name == "if":
                branches = []
                cur = cmd
                while True:
                    body, i = block(i + 1, {"elseif", "else", "endif"})
                    branches.append((cur, body))
                    if i >= len(cmds) or cmds[i].name.lower() == "endif":
                        break
                    cur = cmds[i]
                nodes.append(IfNode(branches))
                i += 1
            elif name in ("foreach", "while", "function", "macro", "block"):
                body, i = block(i + 1, {_BLOCK_END[name]})
                if name in ("function", "macro"):
                    nodes.append(DefNode(cmd, body))
                elif name == "block":
                    nodes.extend(body)
                else:
                    nodes.append(LoopNode(cmd, body))
                i += 1
            else:
                nodes.append(cmd)
                i += 1
        return nodes, i

    nodes, _ = block(0, set())
    return nodes


# ===========================================================================
# Stage 3: what the analysis ASSUMES about the machine. cmake is not run, so
# platform variables get Linux values (branches for Windows/macOS/BSD are
# dropped, which removes most of the noise in big cross-platform projects);
# anything the project decides at configure time (option() values, results
# of other checks, compiler IDs) stays UNKNOWN and shows up as a condition.
# ===========================================================================
PLATFORM_OFF = [
    "WIN32", "WINDOWS", "WINDOWS_STORE", "WINDOWS_PHONE", "MSVC", "MSVC_IDE", "MINGW", "MSYS", "CYGWIN",
    "APPLE", "IOS", "XCODE", "BSD", "FREEBSD", "NETBSD", "OPENBSD", "DRAGONFLY", "SOLARIS", "SUNOS", "AIX",
    "HPUX", "IRIX", "HAIKU", "BEOS", "OS2", "QNX", "ANDROID", "EMSCRIPTEN", "HURD", "RISCOS", "VITA", "PSP",
    "PS2", "N3DS", "NGAGE", "CMAKE_CROSSCOMPILING", "MSVC60", "MSVC70", "MSVC71", "MSVC80", "MSVC90", "WATCOM",
    "BORLAND", "CMAKE_COMPILER_IS_MINGW", "CMAKE_HOST_WIN32", "CMAKE_HOST_APPLE",
]
OPERATORS = {"AND", "OR", "NOT", "(", ")", "EXISTS", "COMMAND", "DEFINED", "TARGET", "POLICY", "TEST",
             "IS_DIRECTORY", "IS_SYMLINK", "IS_ABSOLUTE", "EQUAL", "LESS", "GREATER", "LESS_EQUAL",
             "GREATER_EQUAL", "STREQUAL", "STRLESS", "STRGREATER", "STRLESS_EQUAL", "STRGREATER_EQUAL",
             "VERSION_EQUAL", "VERSION_LESS", "VERSION_GREATER", "VERSION_LESS_EQUAL",
             "VERSION_GREATER_EQUAL", "MATCHES", "IN_LIST", "IS_NEWER_THAN", "PATH_EQUAL"}
UNARY = {"EXISTS", "COMMAND", "DEFINED", "TARGET", "POLICY", "TEST", "IS_DIRECTORY", "IS_SYMLINK", "IS_ABSOLUTE"}
BINARY = OPERATORS - UNARY - {"AND", "OR", "NOT", "(", ")"}
KNOWN_COMMANDS = {"if", "set", "list", "string", "message", "include", "find_package", "find_library",
                  "find_path", "find_program", "option", "cmake_policy", "target_link_libraries",
                  "add_library", "add_executable", "install", "configure_file", "file", "foreach",
                  "cmake_dependent_option", "check_include_file", "check_symbol_exists",
                  "check_function_exists", "check_library_exists", "pkg_check_modules", "add_subdirectory",
                  "target_include_directories", "add_definitions", "include_directories", "link_libraries",
                  "cmake_push_check_state", "cmake_pop_check_state", "try_compile", "execute_process",
                  "install_files", "export", "add_custom_command", "add_custom_target", "enable_testing",
                  "add_test", "get_filename_component", "set_target_properties", "target_compile_options",
                  "target_compile_definitions", "target_sources", "target_link_options", "add_compile_options"}


def _const_truth(v: str) -> "bool | None":
    """CMake's constant rules for if(<constant>); None if v is no constant."""
    u = v.strip().upper()
    if u in ("1", "ON", "YES", "TRUE", "Y"):
        return True
    if u in ("0", "OFF", "NO", "FALSE", "N", "IGNORE", "NOTFOUND", "") or u.endswith("-NOTFOUND"):
        return False
    try:
        return float(u) != 0
    except ValueError:
        return None


def _version_tuple(v: str) -> tuple:
    return tuple(int(p) if p.isdigit() else 0 for p in re.split(r"[.\-+_]", v.strip()) if p != "")


@dataclass
class OptionInfo:
    var: str
    doc: str
    default_text: str
    default: "bool | None"         # None = couldn't tell
    depends: str = ""
    user: "str | None" = None


@dataclass
class Frame:
    """One unresolved condition we are currently inside."""
    text: str
    vars: tuple                     # variable names the condition mentions, in order of appearance


@dataclass
class Found:
    kind: str                       # header | library | pkgconfig | program | package | cmake | compiler
    name: str
    guard_frames: tuple
    file: str
    line: int
    candidates: "tuple | None" = None
    version_req: "VersionReq | None" = None
    extra: dict = field(default_factory=dict)


class ReturnSignal(Exception):
    pass


class Settings:
    def __init__(self, user_defs: "dict[str, str] | None" = None):
        self.user_defs = user_defs or {}
        self.cmake_version, self.cmake_version_detected = _detect_cmake_version()
        self.module_dirs = _builtin_module_dirs()
        self.module_cache: "dict[str, tuple | None]" = {}
        self.notes: "list[str]" = []

    def note(self, msg: str) -> None:
        if msg not in self.notes:
            self.notes.append(msg)


def _detect_cmake_version() -> "tuple[str, bool]":
    exe = shutil.which("cmake")
    if exe:
        try:
            out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10).stdout
            m = re.search(r"version\s+(\d+\.\d+(?:\.\d+)?)", out)
            if m:
                return m.group(1), True
        except Exception:
            pass
    return "3.31.6", False          # Debian trixie's version


def _builtin_module_dirs() -> "list[str]":
    exe = shutil.which("cmake")
    prefixes = []
    if exe:
        prefixes.append(os.path.dirname(os.path.dirname(os.path.realpath(exe))))
    prefixes += ["/usr", "/usr/local"]
    dirs = []
    for pre in prefixes:
        dirs += sorted(glob.glob(os.path.join(pre, "share", "cmake-*", "Modules")), reverse=True)
        dirs += sorted(glob.glob(os.path.join(pre, "data", "share", "cmake-*", "Modules")), reverse=True)
    return list(dict.fromkeys(dirs))


# find_package() names whose CMake module is too dynamic to read statically
# (or isn't a package at all). Tags: pc:<pkg-config module> h:<header>
# lib:<library> prog:<a,b,c> none:<why no package is needed>
FIND_PACKAGE_OVERRIDES = {
    "PkgConfig": ("prog:pkg-config,pkgconf",),
    "Threads": ("none:POSIX threads are part of glibc (libc6-dev)",),
    "PythonInterp": ("prog:python3",),
    "PythonLibs": ("h:Python.h", "pc:python3-embed"),
    "Python3": ("prog:python3", "h:Python.h"),
    "Python": ("prog:python3", "h:Python.h"),
    "Python2": ("prog:python2",),
    "Perl": ("prog:perl",),
    "Git": ("prog:git",),
    "Doxygen": ("prog:doxygen",),
    "Gettext": ("prog:msgfmt",),
    "Intl": ("none:gettext's libintl is part of glibc",),
    "Java": ("prog:java",),
    "Vala": ("prog:valac",),
    "Bash": ("prog:bash",),
}


class Analyzer:
    def __init__(self, settings: Settings, root_dir: str, module_mode: bool = False):
        self.s = settings
        self.module_mode = module_mode
        self.root_dir = root_dir
        self.cur_dir = root_dir
        self.cur_file = ""
        self.vars: "dict[str, str]" = {}
        self.options: "dict[str, OptionInfo]" = {}
        self.option_order: "list[str]" = []
        self.result_vars: "set[str]" = set()
        self.targets: "set[str]" = set()
        self.funcs: "dict[str, tuple]" = {}
        self.frames: "list[Frame]" = []
        self.found: "list[Found]" = []
        self.parent_scopes: "list[dict]" = []
        self.call_depth = 0
        self.include_stack: "list[str]" = []
        self.steps = 0
        self.languages: "list[str]" = []
        self.min_version: "str | None" = None
        self._init_vars()

    # ---- variables -------------------------------------------------------
    def _init_vars(self) -> None:
        v = self.vars
        for name in PLATFORM_OFF:
            v[name] = "OFF"
        v.update({
            "UNIX": "ON", "LINUX": "ON", "CMAKE_SYSTEM_NAME": "Linux", "CMAKE_HOST_SYSTEM_NAME": "Linux",
            "CMAKE_HOST_UNIX": "ON", "CMAKE_SYSTEM_PROCESSOR": "x86_64", "CMAKE_SIZEOF_VOID_P": "8",
            "CMAKE_VERSION": self.s.cmake_version, "CMAKE_MAJOR_VERSION": self.s.cmake_version.split(".")[0],
            "CMAKE_MINOR_VERSION": self.s.cmake_version.split(".")[1],
            "CMAKE_SOURCE_DIR": self.root_dir, "CMAKE_CURRENT_SOURCE_DIR": self.root_dir,
            "CMAKE_CURRENT_LIST_DIR": self.root_dir, "PROJECT_SOURCE_DIR": self.root_dir,
            "CMAKE_BINARY_DIR": "<build>", "CMAKE_CURRENT_BINARY_DIR": "<build>", "PROJECT_BINARY_DIR": "<build>",
            "CMAKE_MODULE_PATH": "", "CMAKE_PREFIX_PATH": "", "CMAKE_INSTALL_PREFIX": "/usr/local",
        })
        for name, val in self.s.user_defs.items():
            self.vars[name] = val

    # ---- expansion ---------------------------------------------------------
    _REF_RE = re.compile(r"\$\{([^${}]*)\}")
    _ENV_RE = re.compile(r"\$ENV\{([^${}]*)\}")

    def expand(self, s: str) -> str:
        for _ in range(12):
            if "$" not in s:
                break

            def sub(m: "re.Match") -> str:
                name = m.group(1)
                return self.vars[name] if name in self.vars else m.group(0)

            new = self._REF_RE.sub(sub, s)
            new = self._ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), new)
            if new == s:
                break
            s = new[:_MAX_VALUE]          # self-referencing variables can double on every pass
        return s

    @staticmethod
    def split_list(s: str) -> "list[str]":
        return [p.replace("\\;", ";") for p in re.split(r"(?<!\\);", s) if p != ""]

    def args_of(self, cmd: Cmd) -> "list[str]":
        out: "list[str]" = []
        for a in cmd.args:
            s = self.expand(a.text)
            if a.quoted:
                out.append(s)
            else:
                out.extend(self.split_list(s))
        return out

    # ---- condition evaluation (three-valued: True / False / None=unknown) ---
    def eval_cond(self, args: "list[Arg]") -> "tuple[bool | None, list[str]]":
        toks = [(self.expand(a.text), a.quoted) for a in args]
        used: "list[str]" = []
        pos = 0

        def peek():
            return toks[pos] if pos < len(toks) else None

        def is_op(tok, names) -> bool:
            return tok is not None and not tok[1] and tok[0] in names

        def truth(tok) -> "bool | None":
            text, quoted = tok
            if "${" in text:
                return None
            if quoted:
                c = _const_truth(text)
                return c if c is not None else False
            if text.upper() in ("1", "ON", "YES", "TRUE", "Y", "0", "OFF", "NO", "FALSE", "N", "IGNORE", "NOTFOUND"):
                return _const_truth(text)
            c = _const_truth(text) if re.fullmatch(r"[0-9.+-]+", text) else None
            if c is not None:
                return c
            used.append(text)
            if text in self.vars:
                c = _const_truth(self.vars[text])
                return True if c is None else c
            return None

        UNKNOWN = object()

        def value(tok):
            text, quoted = tok
            if "${" in text:
                return UNKNOWN
            if quoted or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.+-]*", text):
                return text
            if text in self.vars:
                used.append(text)
                return self.vars[text]
            if text.isupper() and "_" in text:        # looks like a variable we know nothing about
                used.append(text)
                return UNKNOWN
            return text                                 # a bare word: a string literal

        def compare(op: str, a, b) -> "bool | None":
            if a is UNKNOWN or b is UNKNOWN:
                return None
            try:
                if op == "STREQUAL":
                    return a == b
                if op in ("EQUAL", "LESS", "GREATER", "LESS_EQUAL", "GREATER_EQUAL"):
                    x, y = float(a), float(b)
                    return {"EQUAL": x == y, "LESS": x < y, "GREATER": x > y,
                            "LESS_EQUAL": x <= y, "GREATER_EQUAL": x >= y}[op]
                if op.startswith("STR"):
                    return {"STRLESS": a < b, "STRGREATER": a > b,
                            "STRLESS_EQUAL": a <= b, "STRGREATER_EQUAL": a >= b}[op]
                if op.startswith("VERSION_"):
                    x, y = _version_tuple(a), _version_tuple(b)
                    return {"VERSION_EQUAL": x == y, "VERSION_LESS": x < y, "VERSION_GREATER": x > y,
                            "VERSION_LESS_EQUAL": x <= y, "VERSION_GREATER_EQUAL": x >= y}[op]
                if op == "MATCHES":
                    return re.search(b, a) is not None
                if op == "IN_LIST":
                    return a in self.split_list(b)
            except (ValueError, re.error):
                return None
            return None

        def primary() -> "bool | None":
            nonlocal pos
            tok = peek()
            if tok is None:
                return None
            if is_op(tok, {"("}):
                pos += 1
                r = parse_or()
                if is_op(peek(), {")"}):
                    pos += 1
                return r
            if is_op(tok, UNARY) and pos + 1 < len(toks):
                op, arg = tok[0], toks[pos + 1]
                nxt = toks[pos + 2] if pos + 2 < len(toks) else None
                if not (nxt is not None and is_op(nxt, BINARY) and op not in ("DEFINED", "COMMAND")):
                    pos += 2
                    return unary(op, arg)
            pos += 1
            nxt = peek()
            if is_op(nxt, BINARY) and pos + 1 <= len(toks) - 1:
                op = nxt[0]
                right = toks[pos + 1]
                pos += 2
                return compare(op, value(tok), value(right) if op != "MATCHES" else (right[0] if "${" not in right[0] else UNKNOWN))
            return truth(tok)

        def unary(op: str, arg) -> "bool | None":
            name = arg[0]
            if op == "DEFINED":
                if name.startswith("ENV{"):
                    return name[4:-1] in os.environ
                if name.startswith("CACHE{"):
                    return None
                used.append(name)
                return name in self.vars or name in self.options or name in self.result_vars
            if op == "COMMAND":
                return True if (name.lower() in self.funcs or name.lower() in KNOWN_COMMANDS) else None
            if op == "TARGET":
                return True if name in self.targets else None
            if op == "POLICY":
                return True
            if op in ("EXISTS", "IS_DIRECTORY", "IS_SYMLINK"):
                if "${" in name or "<build>" in name:
                    return None
                if op == "EXISTS":
                    return os.path.exists(name)
                return os.path.isdir(name) if op == "IS_DIRECTORY" else os.path.islink(name)
            if op == "IS_ABSOLUTE":
                return None if "${" in name else name.startswith("/")
            return None

        def parse_not() -> "bool | None":
            nonlocal pos
            if is_op(peek(), {"NOT"}):
                pos += 1
                r = parse_not()
                return None if r is None else not r
            return primary()

        def parse_and() -> "bool | None":
            nonlocal pos
            r = parse_not()
            while is_op(peek(), {"AND"}):
                pos += 1
                r2 = parse_not()
                r = False if (r is False or r2 is False) else (None if (r is None or r2 is None) else True)
            return r

        def parse_or() -> "bool | None":
            nonlocal pos
            r = parse_and()
            while is_op(peek(), {"OR"}):
                pos += 1
                r2 = parse_and()
                r = True if (r is True or r2 is True) else (None if (r is None or r2 is None) else False)
            return r

        result = parse_or()
        ordered = [u for u in dict.fromkeys(used) if u not in OPERATORS]
        return result, ordered

    # ---- execution -----------------------------------------------------------
    def run_file(self, path: str) -> bool:
        try:
            text = open(path, "r", errors="replace").read()
        except OSError:
            return False
        saved_file, saved_dir_var = self.cur_file, self.vars.get("CMAKE_CURRENT_LIST_DIR")
        self.cur_file = path
        self.vars["CMAKE_CURRENT_LIST_DIR"] = os.path.dirname(path)
        self.vars["CMAKE_CURRENT_LIST_FILE"] = path
        try:
            self.exec_nodes(build_ast(tokenize(text, path)))
        except ReturnSignal:
            pass
        finally:
            self.cur_file = saved_file
            if saved_dir_var is not None:
                self.vars["CMAKE_CURRENT_LIST_DIR"] = saved_dir_var
        return True

    def exec_nodes(self, nodes: list) -> None:
        for node in nodes:
            self.steps += 1
            if self.steps > 3_000_000:
                self.s.note("analysis stopped early: step limit reached (a loop or macro recursion?)")
                raise ReturnSignal()
            if isinstance(node, Cmd):
                self.exec_cmd(node)
            elif isinstance(node, IfNode):
                self.exec_if(node)
            elif isinstance(node, LoopNode):
                self.exec_loop(node)
            elif isinstance(node, DefNode):
                args = [a.text for a in node.cmd.args]
                if args:
                    self.funcs[args[0].lower()] = (node.cmd.name.lower(), args[1:], node.body)

    def exec_if(self, node: IfNode) -> None:
        before = dict(self.vars)
        try:
            self._exec_if(node)
        finally:
            if self._if_was_uncertain:
                self._forget_uncertain(before)

    def _forget_uncertain(self, before: "dict[str, str]") -> None:
        """A branch ran only 'maybe', so whatever it assigned is not a known value afterwards.
        Appends (lists growing) are kept, as an over-approximation."""
        for k in set(before) | set(self.vars):
            old, new = before.get(k), self.vars.get(k)
            if old == new:
                continue
            if k == "CMAKE_MODULE_PATH" and new:
                continue
            if old and new and new.startswith(old):
                continue
            self.vars.pop(k, None)

    _if_was_uncertain = False

    def _exec_if(self, node: IfNode) -> None:
        self._if_was_uncertain = False
        taken = False
        unknown_vars: "list[str]" = []
        any_unknown = False
        for cmd, body in node.branches:
            if taken:
                break
            if cmd.name.lower() == "else":
                tri, used = True, []
            else:
                tri, used = self.eval_cond(cmd.args)
            if tri is False:
                continue
            if tri is True:
                if any_unknown:
                    self.frames.append(Frame("else", tuple(unknown_vars)))
                    try:
                        self.exec_nodes(body)
                    finally:
                        self.frames.pop()
                else:
                    self.exec_nodes(body)
                taken = True
                continue
            text = " ".join(a.text if not a.quoted else f'"{a.text}"' for a in cmd.args)[:90]
            self.frames.append(Frame(f"{cmd.name.lower()}({text})", tuple(used)))
            try:
                self.exec_nodes(body)
            finally:
                self.frames.pop()
            any_unknown = True
            unknown_vars += [u for u in used if u not in unknown_vars]
        self._if_was_uncertain = any_unknown

    def exec_loop(self, node: LoopNode) -> None:
        cmd = node.cmd
        if cmd.name.lower() != "foreach":
            return                                  # while(): can't know how often it runs; skipped
        args = [a for a in cmd.args]
        if not args:
            return
        var = args[0].text
        rest = self.args_of(Cmd(cmd.name, args[1:], cmd.line, cmd.file))
        items: "list[str] | None" = None
        if rest and rest[0] == "IN":
            items = []
            mode = None
            for t in rest[1:]:
                if t in ("ITEMS", "LISTS", "ZIP_LISTS"):
                    mode = t
                elif mode == "LISTS":
                    if t in self.vars:
                        items += self.split_list(self.vars[t])
                    else:
                        items = None
                        break
                else:
                    items.append(t)
        elif rest and rest[0] == "RANGE":
            nums = [int(x) for x in rest[1:] if x.lstrip("-").isdigit()]
            items = [str(i) for i in (range(nums[0] + 1) if len(nums) == 1 else range(nums[0], nums[1] + 1))] if nums else None
        else:
            items = rest
        if items is not None and any("${" in i for i in items):
            items = None
        saved = self.vars.get(var)
        try:
            if items is None or len(items) > 200:
                self.vars.pop(var, None)
                self.exec_nodes(node.body)
            else:
                for it in items:
                    self.vars[var] = it
                    self.exec_nodes(node.body)
        finally:
            if saved is None:
                self.vars.pop(var, None)
            else:
                self.vars[var] = saved

    def call_user(self, name: str, cmd: Cmd) -> None:
        kind, params, body = self.funcs[name]
        if self.call_depth >= 8:
            return
        args = self.args_of(cmd)
        local = {"ARGC": str(len(args)), "ARGV": ";".join(args), "ARGN": ";".join(args[len(params):])}
        for i, a in enumerate(args):
            local[f"ARGV{i}"] = a
        for i, p in enumerate(params):
            local[p] = args[i] if i < len(args) else ""
        self.call_depth += 1
        try:
            if kind == "function":
                saved = self.vars
                self.parent_scopes.append(saved)
                self.vars = dict(saved)
                self.vars.update(local)
                try:
                    self.exec_nodes(body)
                except ReturnSignal:
                    pass
                finally:
                    self.vars = saved
                    self.parent_scopes.pop()
            else:
                backup = {k: self.vars.get(k) for k in local}
                self.vars.update(local)
                try:
                    self.exec_nodes(body)
                except ReturnSignal:
                    pass
                finally:
                    for k, old in backup.items():
                        if old is None:
                            self.vars.pop(k, None)
                        else:
                            self.vars[k] = old
        finally:
            self.call_depth -= 1

    # ---- recording a finding -------------------------------------------------------
    def record(self, kind: str, name: str, cmd: Cmd, **kw) -> None:
        if "${" in name or not name.strip():
            self.s.note(f"skipped {cmd.name}() on {os.path.basename(cmd.file)}:{cmd.line}: "
                        f"its argument depends on a variable that couldn't be resolved statically")
            return
        self.found.append(Found(kind=kind, name=name, guard_frames=tuple(self.frames),
                                file=cmd.file, line=cmd.line, **kw))

    # ---- commands -------------------------------------------------------------
    def exec_cmd(self, cmd: Cmd) -> None:
        name = cmd.name.lower()
        if name in self.funcs:
            self.call_user(name, cmd)
            return
        handler = getattr(self, "cmd_" + name, None)
        if handler is not None:
            handler(cmd)

    def cmd_return(self, cmd: Cmd) -> None:
        raise ReturnSignal()

    def cmd_set(self, cmd: Cmd) -> None:
        a = self.args_of(cmd)
        if not a:
            return
        var, vals = a[0], a[1:]
        scope = self.vars
        if "PARENT_SCOPE" in vals:
            vals = [v for v in vals if v != "PARENT_SCOPE"]
            if not self.parent_scopes:
                return
            scope = self.parent_scopes[-1]
        if "CACHE" in vals:
            vals = vals[:vals.index("CACHE")]
            if var in self.s.user_defs or var in self.options:
                return                      # user/option value wins
            if var in scope:
                return
        if not vals:
            scope.pop(var, None)
        else:
            scope[var] = ";".join(vals)[:_MAX_VALUE]

    def cmd_unset(self, cmd: Cmd) -> None:
        a = self.args_of(cmd)
        if a:
            self.vars.pop(a[0], None)

    def cmd_list(self, cmd: Cmd) -> None:
        a = self.args_of(cmd)
        if len(a) < 2:
            return
        op, var = a[0].upper(), a[1]
        if op == "APPEND":
            cur = self.vars.get(var, "")
            self.vars[var] = ";".join(([cur] if cur else []) + a[2:])[:_MAX_VALUE]
        elif op == "PREPEND":
            cur = self.vars.get(var, "")
            self.vars[var] = ";".join(a[2:] + ([cur] if cur else []))
        elif op in ("REMOVE_ITEM", "REMOVE_DUPLICATES", "SORT", "REVERSE", "FILTER", "REMOVE_AT", "TRANSFORM",
                    "INSERT", "POP_BACK", "POP_FRONT"):
            if op == "REMOVE_ITEM" and var in self.vars:
                drop = set(a[2:])
                self.vars[var] = ";".join(x for x in self.split_list(self.vars[var]) if x not in drop)
            elif op in ("REMOVE_DUPLICATES", "SORT", "REVERSE") and var in self.vars:
                pass
            else:
                self.vars.pop(var, None)
        elif op in ("LENGTH", "GET", "JOIN", "FIND", "SUBLIST", "FILTER"):
            out = a[-1]
            if op == "LENGTH" and var in self.vars:
                self.vars[out] = str(len(self.split_list(self.vars[var])))
            else:
                self.vars.pop(out, None)

    def cmd_string(self, cmd: Cmd) -> None:
        a = self.args_of(cmd)
        if len(a) < 2:
            return
        op = a[0].upper()
        if op == "APPEND" and len(a) >= 2:
            self.vars[a[1]] = self.vars.get(a[1], "") + "".join(a[2:])
        elif op in ("TOLOWER", "TOUPPER") and len(a) >= 3:
            self.vars[a[2]] = a[1].lower() if op == "TOLOWER" else a[1].upper()
        elif op == "REPLACE" and len(a) >= 5:
            self.vars[a[3]] = "".join(a[4:]).replace(a[1], a[2])
        elif op == "CONCAT" and len(a) >= 2:
            self.vars[a[1]] = "".join(a[2:])
        elif a[-1] and op not in ("APPEND",):
            self.vars.pop(a[-1], None)       # some other string() mode: result unknown

    def cmd_option(self, cmd: Cmd) -> None:
        a = self.args_of(cmd)
        if not a:
            return
        var = a[0]
        doc = a[1] if len(a) > 1 else ""
        default_text = a[2] if len(a) > 2 else "OFF"
        self._declare_option(var, doc, default_text, "")

    def cmd_cmake_dependent_option(self, cmd: Cmd) -> None:
        a = self.args_of(cmd)
        if len(a) < 3:
            return
        self._declare_option(a[0], a[1], a[2], a[3] if len(a) > 3 else "")

    def _declare_option(self, var: str, doc: str, default_text: str, depends: str) -> None:
        if var in self.options:
            return
        c = _const_truth(default_text) if "${" not in default_text else None
        info = OptionInfo(var, doc, default_text, c, depends)
        if var in self.s.user_defs:
            info.user = self.s.user_defs[var]
        self.options[var] = info
        self.option_order.append(var)
        if info.user is None:
            self.vars.pop(var, None)         # value is the user's choice: unknown

    def cmd_project(self, cmd: Cmd) -> None:
        a = self.args_of(cmd)
        if not a:
            return
        self.vars["PROJECT_NAME"] = a[0]
        self.vars[f"{a[0]}_SOURCE_DIR"] = self.cur_dir
        langs: "list[str]" = []
        mode = None
        for t in a[1:]:
            if t in ("VERSION", "DESCRIPTION", "HOMEPAGE_URL"):
                mode = t
                continue
            if t == "LANGUAGES":
                mode = "LANGUAGES"
                continue
            if mode == "VERSION":
                self.vars["PROJECT_VERSION"] = t
                mode = None
            elif mode == "LANGUAGES" or mode is None:
                langs.append(t)
        if not any(x == "LANGUAGES" for x in a):
            langs = [t for t in a[1:] if t in ("C", "CXX", "CUDA", "Fortran", "ASM", "OBJC", "OBJCXX")] or ["C", "CXX"]
        self._add_languages(langs, cmd)

    def cmd_enable_language(self, cmd: Cmd) -> None:
        self._add_languages(self.args_of(cmd), cmd)

    def _add_languages(self, langs: "list[str]", cmd: Cmd) -> None:
        for lang in langs:
            if lang in ("NONE",) or lang in self.languages:
                continue
            self.languages.append(lang)
            progs = {"C": ("cc", "gcc", "clang"), "CXX": ("c++", "g++", "clang++"),
                     "Fortran": ("gfortran",), "ASM": ("cc", "gcc"), "CUDA": ("nvcc",),
                     "OBJC": ("cc", "gcc"), "OBJCXX": ("c++", "g++")}.get(lang)
            if progs and not self.module_mode:
                self.found.append(Found(kind="compiler", name=f"{lang} compiler", guard_frames=tuple(self.frames),
                                        file=cmd.file, line=cmd.line, candidates=progs))

    def cmd_cmake_minimum_required(self, cmd: Cmd) -> None:
        a = self.args_of(cmd)
        if "VERSION" in a and a.index("VERSION") + 1 < len(a) and self.min_version is None:
            v = re.split(r"\.\.\.?", a[a.index("VERSION") + 1])[0]
            self.min_version = v
            if not self.module_mode:
                self.found.append(Found(kind="cmake", name="cmake", guard_frames=(), file=cmd.file, line=cmd.line,
                                        version_req=VersionReq(op=">=", version=v, source="cmake_minimum_required")))

    def cmd_include(self, cmd: Cmd) -> None:
        a = self.args_of(cmd)
        if not a:
            return
        target = a[0]
        if target == "CTest":
            self._declare_option("BUILD_TESTING", "Build the testing tree (from CTest)", "ON", "")
            return
        path = None
        if target.endswith(".cmake") or "/" in target:
            path = target if os.path.isabs(target) else os.path.join(self.vars.get("CMAKE_CURRENT_LIST_DIR", self.cur_dir), target)
        elif "${" not in target:
            for d in self.split_list(self.vars.get("CMAKE_MODULE_PATH", "")):
                cand = os.path.join(d, target + ".cmake")
                if os.path.isfile(cand):
                    path = cand
                    break
        if path and "${" not in path and os.path.isfile(path) and path not in self.include_stack and len(self.include_stack) < 12:
            self.include_stack.append(path)
            try:
                self.run_file(path)
            finally:
                self.include_stack.pop()

    def cmd_add_subdirectory(self, cmd: Cmd) -> None:
        a = self.args_of(cmd)
        if not a or "${" in a[0]:
            return
        d = a[0] if os.path.isabs(a[0]) else os.path.join(self.cur_dir, a[0])
        f = os.path.join(d, "CMakeLists.txt")
        if not os.path.isfile(f):
            self.s.note(f"add_subdirectory({a[0]}): no CMakeLists.txt there (not part of this source tree?)")
            return
        if self.call_depth > 12:
            return
        saved_vars, saved_dir = self.vars, self.cur_dir
        self.vars = dict(saved_vars)
        self.cur_dir = d
        self.vars["CMAKE_CURRENT_SOURCE_DIR"] = d
        self.call_depth += 1
        try:
            self.run_file(f)
        finally:
            self.call_depth -= 1
            self.vars, self.cur_dir = saved_vars, saved_dir

    def cmd_add_library(self, cmd: Cmd) -> None:
        a = self.args_of(cmd)
        if a:
            self.targets.add(a[0])

    cmd_add_executable = cmd_add_library

    def cmd_fetchcontent_declare(self, cmd: Cmd) -> None:
        a = self.args_of(cmd)
        if a:
            self.s.note(f"FetchContent_Declare({a[0]}) downloads source at configure time "
                        f"({os.path.basename(cmd.file)}:{cmd.line}) - needs network, not a Debian package")

    # ---- the commands that matter: what the project checks for -------------------------
    def cmd_check_include_file(self, cmd: Cmd) -> None:
        a = self.args_of(cmd)
        if a:
            self.record("header", a[0], cmd)
            if len(a) > 1:
                self.result_vars.add(a[1])

    cmd_check_include_file_cxx = cmd_check_include_file

    def cmd_check_include_files(self, cmd: Cmd) -> None:
        a = self.args_of(cmd)
        for h in a[:1]:
            for hdr in self.split_list(h):
                self.record("header", hdr, cmd)
        if len(a) > 1:
            self.result_vars.add(a[1])

    def cmd_check_symbol_exists(self, cmd: Cmd) -> None:
        a = self.args_of(cmd)
        if len(a) >= 2:
            for hdr in self.split_list(a[1]):
                self.record("header", hdr, cmd)
        if len(a) >= 3:
            self.result_vars.add(a[2])

    cmd_check_cxx_symbol_exists = cmd_check_symbol_exists

    def cmd_check_function_exists(self, cmd: Cmd) -> None:
        a = self.args_of(cmd)
        if len(a) >= 2:
            self.result_vars.add(a[1])

    def cmd_check_library_exists(self, cmd: Cmd) -> None:
        a = self.args_of(cmd)
        if len(a) >= 2:
            lib, func = a[0], a[1]
            if not (lib.endswith(".a") or "/" in lib):
                self.record("library", f"{func} in -l{lib}", cmd)
            if len(a) >= 4:
                self.result_vars.add(a[3])

    _FIND_KEYWORDS = {"NAMES", "HINTS", "PATHS", "PATH_SUFFIXES", "DOC", "REQUIRED", "NO_DEFAULT_PATH", "QUIET",
                      "NO_CMAKE_PATH", "NO_CMAKE_ENVIRONMENT_PATH", "NO_SYSTEM_ENVIRONMENT_PATH", "NO_CMAKE_SYSTEM_PATH",
                      "NO_CMAKE_FIND_ROOT_PATH", "CMAKE_FIND_ROOT_PATH_BOTH", "ONLY_CMAKE_FIND_ROOT_PATH",
                      "NO_PACKAGE_ROOT_PATH", "NAMES_PER_DIR", "PATH_SUFFIXES", "VALIDATOR", "REGISTRY_VIEW",
                      "ENV", "DOC"}

    def _find_names(self, a: "list[str]") -> "list[str]":
        names: "list[str]" = []
        if "NAMES" in a:
            for t in a[a.index("NAMES") + 1:]:
                if t in self._FIND_KEYWORDS:
                    break
                names.append(t)
        elif len(a) > 1 and a[1] not in self._FIND_KEYWORDS:
            names.append(a[1])
        return [n for n in names if "${" not in n]

    def cmd_find_library(self, cmd: Cmd) -> None:
        a = self.args_of(cmd)
        if not a:
            return
        self.result_vars.add(a[0])
        names = [n for n in self._find_names(a) if not n.endswith((".a", ".lib", ".dll", ".so"))]
        if names:
            self.record("library", names[0], cmd)

    def cmd_find_path(self, cmd: Cmd) -> None:
        a = self.args_of(cmd)
        if not a:
            return
        self.result_vars.add(a[0])
        names = self._find_names(a)
        if names:
            self.record("header", names[0], cmd)

    def cmd_find_program(self, cmd: Cmd) -> None:
        a = self.args_of(cmd)
        if not a:
            return
        self.result_vars.add(a[0])
        names = [n for n in self._find_names(a) if "/" not in n and not n.endswith(".exe")]
        if names:
            self.record("program", " or ".join(names), cmd, candidates=tuple(names))

    _PKG_FLAGS = {"REQUIRED", "QUIET", "NO_CMAKE_PATH", "NO_CMAKE_ENVIRONMENT_PATH", "IMPORTED_TARGET", "GLOBAL",
                  "NO_POLICY_SCOPE", "EXACT"}
    _SPEC_RE = re.compile(r"^([^<>=!\s]+)\s*(>=|<=|!=|=|>|<)\s*(\S+)$")

    def _module_specs(self, tokens: "list[str]") -> "list[tuple[str, VersionReq | None]]":
        specs = []
        i = 0
        while i < len(tokens):
            t = tokens[i]
            if t in self._PKG_FLAGS or t.startswith("IMPORTED_TARGET"):
                i += 1
                continue
            m = self._SPEC_RE.match(t)
            if m:
                specs.append((m.group(1), VersionReq(op=m.group(2), version=m.group(3), source="pkg_check_modules")))
            elif i + 2 < len(tokens) and tokens[i + 1] in (">=", "<=", "=", ">", "<", "!=") \
                    and re.match(r"^\d", tokens[i + 2]):
                specs.append((t, VersionReq(op=tokens[i + 1], version=tokens[i + 2], source="pkg_check_modules")))
                i += 2
            elif re.match(r"^[A-Za-z0-9][A-Za-z0-9+._-]*$", t):
                specs.append((t, None))
            i += 1
        return specs

    def cmd_pkg_check_modules(self, cmd: Cmd) -> None:
        a = self.args_of(cmd)
        if len(a) < 2:
            return
        prefix = a[0]
        self.result_vars.update({f"{prefix}_FOUND", prefix})
        for mod, req in self._module_specs(a[1:]):
            if "${" in mod:
                continue
            self.record("pkgconfig", mod, cmd, version_req=req)

    cmd_pkg_search_module = cmd_pkg_check_modules

    def cmd_find_package(self, cmd: Cmd) -> None:
        a = self.args_of(cmd)
        if not a or self.module_mode:
            return
        name, rest = a[0], a[1:]
        self.result_vars.update({f"{name}_FOUND", f"{name.upper()}_FOUND"})
        if name == "PkgConfig":
            self.result_vars.add("PKG_CONFIG_FOUND")
        version = next((t for t in rest[:1] if re.match(r"^\d+(\.\d+)*$", t)), None)
        req = None
        if version:
            req = VersionReq(op="=" if "EXACT" in rest else ">=", version=version, source="find_package")
        config_only = any(t in rest for t in ("CONFIG", "NO_MODULE"))
        comps: "list[str]" = []
        mode = None
        for t in rest:
            if t in ("COMPONENTS", "REQUIRED_COMPONENTS", "OPTIONAL_COMPONENTS"):
                mode = t
            elif t in ("REQUIRED", "QUIET", "CONFIG", "NO_MODULE", "MODULE", "EXACT", "GLOBAL", "NO_POLICY_SCOPE",
                       "NAMES", "CONFIGS", "HINTS", "PATHS", "PATH_SUFFIXES"):
                mode = None if t not in ("NAMES", "CONFIGS", "HINTS", "PATHS", "PATH_SUFFIXES") else "skip"
            elif mode in ("COMPONENTS", "REQUIRED_COMPONENTS", "OPTIONAL_COMPONENTS"):
                comps.append(t)
        tags, source = self._package_tags(name, config_only)
        self.found.append(Found(kind="package", name=name, guard_frames=tuple(self.frames), file=cmd.file,
                                line=cmd.line, candidates=tuple(tags), version_req=req,
                                extra={"source": source, "components": comps, "required": "REQUIRED" in rest}))

    # ---- find_package(): what does the package's Find module / config file look for? ----
    def _package_tags(self, name: str, config_only: bool) -> "tuple[list[str], str]":
        if name in FIND_PACKAGE_OVERRIDES and not config_only:
            return list(FIND_PACKAGE_OVERRIDES[name]), "built-in knowledge"
        if not config_only:
            for d in self.split_list(self.vars.get("CMAKE_MODULE_PATH", "")):
                p = os.path.join(d, f"Find{name}.cmake")
                if os.path.isfile(p):
                    tags = self._module_tags(p, name)
                    if tags:
                        return tags, os.path.relpath(p, self.root_dir)
            for d in self.s.module_dirs:
                p = os.path.join(d, f"Find{name}.cmake")
                if os.path.isfile(p):
                    tags = self._module_tags(p, name)
                    if tags:
                        return tags + [f"cfg:{name}Config.cmake"], f"cmake's Find{name}.cmake"
                    break
        return [f"cfg:{name}Config.cmake", f"cfg:{name.lower()}-config.cmake"], "config-file package"

    def _module_tags(self, path: str, name: str) -> "list[str]":
        if path in self.s.module_cache:
            cached = self.s.module_cache[path]
            return list(cached) if cached else []
        sub = Analyzer(self.s, self.root_dir, module_mode=True)
        sub.vars["CMAKE_FIND_PACKAGE_NAME"] = name
        sub.vars["CMAKE_CURRENT_LIST_DIR"] = os.path.dirname(path)
        sub.run_file(path)
        pcs, hdrs, libs, progs = [], [], [], []
        for f in sub.found:
            if f.kind == "pkgconfig":
                pcs.append(f"pc:{f.name}")
            elif f.kind == "header":
                hdrs.append(f"h:{f.name}")
            elif f.kind == "library":
                libs.append(f"lib:{f.name}")
            elif f.kind == "program":
                progs.append("prog:" + ",".join(f.candidates or (f.name,)))
        tags = list(dict.fromkeys(pcs + hdrs + libs + progs))[:12]
        self.s.module_cache[path] = tuple(tags)
        return tags


# ===========================================================================
# Stage 4: turn the findings into configcheck-style Checks, grouped under the
# option that controls them (like configcheck groups by --enable-x).
# ===========================================================================
TRANSPARENT_PREFIXES = ("CMAKE_",)


def guard_for(an: Analyzer, frames: tuple) -> str:
    """Innermost declared option mentioned by an enclosing condition; failing
    that, innermost project-level variable; otherwise UNCONDITIONAL.
    Conditions on the RESULT of another check (FOO_FOUND, HAVE_X) or on
    toolchain variables (CMAKE_*) are transparent: they say "only if the
    earlier lookup worked", not "only if the user asks for it"."""
    for fr in reversed(frames):
        for v in fr.vars:
            if v in an.options:
                return v
    for fr in reversed(frames):
        for v in fr.vars:
            if v in an.result_vars or v.startswith(TRANSPARENT_PREFIXES) or re.match(r"^\d", v):
                continue
            if v.endswith(("_FOUND", "_Found")) or v.upper() == v and v.startswith("HAVE_"):
                continue
            return v
    return "UNCONDITIONAL"


def build_model(an: Analyzer) -> "tuple[list[Option], list[Check]]":
    checks: "list[Check]" = []
    seen: "set[tuple]" = set()
    by_guard: "dict[str, list[Check]]" = {}
    mandatory: "list[Check]" = []

    for f in an.found:
        guard = guard_for(an, f.guard_frames)
        if f.kind == "header":
            ck = Check(name=f.name, type="header", guard=guard)
        elif f.kind == "library":
            ck = Check(name=f.name, type="library", guard=guard)
        elif f.kind == "pkgconfig":
            ck = Check(name=f.name, type="pkgconfig", guard=guard, version_req=f.version_req)
        elif f.kind in ("program", "compiler"):
            ck = Check(name=f.name, type="generic", guard=guard, candidates=f.candidates)
        elif f.kind == "cmake":
            ck = Check(name="cmake", type="generic", guard=guard, version_req=f.version_req)
        elif f.kind == "package":
            ck = Check(name=f.name, type="cmakepkg", guard=guard, candidates=f.candidates, version_req=f.version_req)
            PACKAGE_EXTRA[(guard, f.name)] = f.extra
        else:
            continue
        key = (guard, ck.type, ck.name)
        if key in seen:
            if ck.type == "cmakepkg":     # same package looked up on two branches (e.g. CONFIG vs module mode): merge ways to find it
                lst = mandatory if guard == "UNCONDITIONAL" else by_guard[guard]
                idx = next(i for i, c in enumerate(lst) if c.type == "cmakepkg" and c.name == ck.name)
                prev = lst[idx]
                merged = list(ck.candidates or ()) + list(prev.candidates or ())
                tags = tuple(dict.fromkeys(t for t in merged if not t.startswith("cfg:"))) + \
                    tuple(dict.fromkeys(t for t in merged if t.startswith("cfg:")))
                lst[idx] = dataclasses.replace(prev, candidates=tags,
                                               version_req=prev.version_req or ck.version_req)
            continue
        seen.add(key)
        (mandatory if guard == "UNCONDITIONAL" else by_guard.setdefault(guard, [])).append(ck)

    options: "list[Option]" = []
    listed: "set[str]" = set()

    def make_option(var: str) -> Option:
        info = an.options.get(var)
        if info:
            val = info.user if info.user is not None else None
            c = _const_truth(info.user) if info.user is not None else info.default
            default = "enabled" if c else "disabled" if c is False else "unknown"
            desc = info.doc + (f" [depends on: {info.depends}]" if info.depends else "")
            return Option(flag=f"-D{var}", var=var, default=default, kind="feature", desc=desc)
        return Option(flag=var, var=var, default="conditional", kind="condition",
                      desc="condition set elsewhere in the project")

    for var in an.option_order:
        o = make_option(var)
        o.checks = by_guard.get(var, [])
        options.append(o)
        listed.add(var)
    for var, cks in by_guard.items():
        if var not in listed:
            o = make_option(var)
            o.checks = cks
            options.append(o)
    return options, mandatory


PACKAGE_EXTRA: "dict[tuple[str, str], dict]" = {}


# ===========================================================================
# Stage 5: resolve. Everything configcheck already knows how to resolve is
# handed to configcheck.resolve_all unchanged; only find_package() ("cmakepkg")
# needs its own logic, since one find_package() stands for a whole bundle of
# header/library/pkg-config lookups.
# ===========================================================================
def _contents_terms(checks: "list[Check]") -> "list[str]":
    terms = cc.contents_terms_for([c for c in checks if c.type != "cmakepkg"])
    for c in checks:
        if c.type != "cmakepkg":
            continue
        for tag in c.candidates or ():
            kind, _, val = tag.partition(":")
            if kind == "pc":
                terms.append(f"{val}.pc")
            elif kind == "h":
                terms.append(val)
            elif kind == "lib":
                t = cc.contents_term_for_library(val)
                if t:
                    terms.append(t)
            elif kind == "prog":
                names = val.split(",")
                if not any(shutil.which(n) for n in names):
                    terms.append(f"bin/{names[0]}")
            elif kind == "cfg":
                terms.append(val)
    return terms


def resolve_cmakepkg(c: Check) -> dict:
    tried: "list[str]" = []
    pkg, installed, note, via = "?", "unknown", "", ""
    extra = PACKAGE_EXTRA.get((c.guard, c.name), {})
    for tag in c.candidates or ():
        kind, _, val = tag.partition(":")
        if kind == "none":
            pkg, installed, note, via = "-", "yes", val, tag
            break
        if kind == "pc":
            r = cc.resolve_package(f"{val}.pc")
        elif kind == "h":
            r = resolve_header(val.split()[-1])
        elif kind == "lib":
            r = cc.resolve_library(val)
        elif kind == "prog":
            r = cc.resolve_program(tuple(val.split(",")))
        elif kind == "cfg":
            r = cc.resolve_package(val)
        else:
            continue
        tried.append(tag)
        if r[0] not in ("?", "-"):
            pkg, installed, note, via = r[0], r[1], r[2], tag
            break
    if via.startswith("none:"):
        pass
    elif via:
        src = extra.get("source", "")
        prefix = f"via {src}: {via}" if src else f"via {via}"
        note = f"{prefix}; {note}" if note else prefix
    else:
        note = ("no match for " + ", ".join(tried)) if tried else "no way found to look this package up"
    if extra.get("required") is False:
        note += "; not REQUIRED - the project builds without it"
    comps = extra.get("components")
    if comps:
        note += f"; components: {' '.join(comps)}"

    kind = via.split(":", 1)[0]
    if kind == "pc":
        chk = Check(name=via.split(":", 1)[1], type="pkgconfig", guard=c.guard, version_req=c.version_req)
    else:
        chk = Check(name=c.name, type="generic", guard=c.guard, version_req=None)
    have = cc.have_cell(chk, pkg) if pkg not in ("?", "-") else "-"
    req = f"{c.version_req.op}{c.version_req.version}" if c.version_req else "-"
    return {"name": c.name, "type": "package", "package": pkg, "installed": installed, "note": note,
            "want": req, "have": have, "version_req": "" if req == "-" else req}


_MULTIARCH = None


def _multiarch() -> str:
    global _MULTIARCH
    if _MULTIARCH is None:
        try:
            _MULTIARCH = subprocess.run(["dpkg-architecture", "-qDEB_HOST_MULTIARCH"], capture_output=True,
                                        text=True, timeout=10).stdout.strip() or "x86_64-linux-gnu"
        except Exception:
            _MULTIARCH = "x86_64-linux-gnu"
    return _MULTIARCH


_GCC_MAJOR = None


def _gcc_major() -> str:
    global _GCC_MAJOR
    if _GCC_MAJOR is None:
        _GCC_MAJOR = ""
        try:
            out = subprocess.run(["gcc", "-dumpversion"], capture_output=True, text=True, timeout=10).stdout.strip()
            _GCC_MAJOR = out.split(".")[0] if re.match(r"^\d+", out) else ""
        except Exception:
            pass
    return _GCC_MAJOR


WINDOWS_ONLY_HEADERS = {"direct.h", "io.h", "windows.h", "winsock2.h", "ws2tcpip.h", "conio.h", "process.h",
                        "shlobj.h", "tchar.h", "objbase.h", "mmsystem.h", "winbase.h"}


def system_include_paths(h: str) -> "list[str]":
    """Where `#include <h>` is satisfied from the compiler's DEFAULT search path (Contents-style, no leading /).
    A plain 'xxhash.h' would also match e.g. LLVM's private copy under /usr/lib/llvm-19/, which is not on that path."""
    paths = [f"usr/include/{h}", f"usr/include/{_multiarch()}/{h}"]
    gv = _gcc_major()
    if gv:                                   # stddef.h, stdbool.h, immintrin.h ... ship with the compiler itself
        paths.append(f"usr/lib/gcc/{_multiarch()}/{gv}/include/{h}")
    return paths


def system_include_owner(h: str) -> "tuple[str, str, str] | None":
    paths = system_include_paths(h)
    for base in paths:                        # installed packages first - the answer for this machine
        local = cc._local_package_lookup("/" + base, True)
        if local:
            pkg = local[0] if len(local) == 1 else cc._format_ambiguous(list(local))
            return pkg, "yes", ("ambiguous: %d packages ship this exact path" % len(local)) if len(local) > 1 else ""
    for base in paths:
        found = cc.search_contents_files(base, True)
        if found:
            pkg = found[0] if len(found) == 1 else cc._format_ambiguous(list(found))
            return pkg, "no", ("ambiguous: %d packages ship this exact path" % len(found)) if len(found) > 1 else ""
    return None


def resolve_header(h: str) -> "tuple[str, str, str]":
    """Exact default-include-path owner first; the old name-only search as a fallback (compiler-private headers
    like stdatomic.h live outside /usr/include)."""
    own = system_include_owner(h)
    return own if own else cc.resolve_package(h)


GLIBC_LIBS = {"m", "dl", "rt", "pthread", "util", "resolv", "anl", "c"}


def _lib_basename(name: str) -> str:
    m = re.search(r"-l(\S+)", name)
    return m.group(1) if m else name


def dev_library_owner(lib: str) -> "tuple[str, str] | None":
    """The package that ships the unversioned lib<lib>.so - the one `-l<lib>` needs at link time."""
    paths = [f"usr/lib/{_multiarch()}/lib{lib}.so", f"usr/lib/lib{lib}.so", f"lib/{_multiarch()}/lib{lib}.so"]
    for base in paths:
        local = cc._local_package_lookup("/" + base, True)
        if local:
            return (local[0] if len(local) == 1 else cc._format_ambiguous(list(local))), "yes"
    for base in paths:
        found = cc.search_contents_files(base, True)
        if found:
            return (found[0] if len(found) == 1 else cc._format_ambiguous(list(found))), "no"
    return None


def resolve_cmake_checks(checks: "list[Check]") -> "list[dict]":
    rows = []
    for c in checks:
        if c.type == "cmakepkg":
            rows.append(resolve_cmakepkg(c))
            continue
        r = cc.resolve_all([c])[0]
        if c.type == "library" and "no unversioned dev symlink" in str(r.get("note", "")):
            lib = _lib_basename(c.name)
            if lib in GLIBC_LIBS:
                r = dict(r, package="libc6-dev", installed="yes", note=f"lib{lib} is part of glibc (libc6-dev)")
            else:
                dev = dev_library_owner(lib)
                if dev:
                    r = dict(r, package=dev[0], installed=dev[1],
                             note=f"linking with -l{lib} needs the -dev package"
                                  + ("" if dev[1] == "yes" else " (not installed)"))
                    r["have"] = cc.have_cell(Check(name=lib, type="generic", guard=c.guard), dev[0].split(" ")[0])
        if c.type == "header":
            own = system_include_owner(c.name)
            if not own and c.name in WINDOWS_ONLY_HEADERS:
                r = dict(r, package="-", installed="-", have="-",
                         note="Windows-only header - the check simply fails on Linux; nothing to install")
            elif own:
                r = dict(r, package=own[0], installed=own[1], note=own[2])
                if own[1] == "yes":
                    r["have"] = cc.have_cell(Check(name=c.name, type="generic", guard=c.guard), own[0].split(" ")[0])
                else:
                    r["have"] = cc.have_cell(Check(name=c.name, type="generic", guard=c.guard), own[0].split(" ")[0])
        rows.append(r)
    return rows


# ===========================================================================
# Stage 6: report (same layout as configcheck.py's).
# ===========================================================================
def print_report(an: Analyzer, options: "list[Option]", mandatory: "list[Check]", path: str,
                 width: "int | None" = None) -> None:
    term_width = width if width is not None else get_term_width()
    bar = "=" * min(78, term_width)
    print(f"Parsed: {path}")
    cm = an.s.cmake_version + ("" if an.s.cmake_version_detected else " (assumed - cmake not installed)")
    print(f"Assuming: Linux, CMake {cm}; option defaults as declared (override with -DNAME=VALUE)")
    print()
    print(bar)
    print(f" Mandatory checks  (performed unconditionally by {os.path.basename(path)})")
    print(bar)
    mand_cols = [
        Column("TYPE", "type", min_width=7),
        Column("CHECKS FOR", "name", min_width=12, flexible=True, shrink_priority=1),
        Column("PACKAGE", "package", min_width=14, flexible=True, shrink_priority=2),
        Column("INST?", "installed", min_width=5),
        Column("WANT", "want", min_width=6, flexible=True, shrink_priority=4),
        Column("HAVE", "have", min_width=10, flexible=True, shrink_priority=3),
        Column("NOTE", "note", min_width=0, flexible=True, shrink_priority=0),
    ]
    if not mandatory:
        print(render_table(mand_cols, [], width=term_width))
        print("  (none detected)")
    else:
        print(render_table(mand_cols, resolve_cmake_checks(mandatory), width=term_width))

    print()
    print(bar)
    print(" Optional flags  (default state / what they check / package / installed?)")
    print(bar)
    rows = []
    for opt in options:
        if not opt.checks:
            rows.append({"flag": opt.flag, "default": opt.default, "name": "(no file/lib check found)",
                         "package": "-", "installed": "-"})
            continue
        for i, row in enumerate(resolve_cmake_checks(opt.checks)):
            rows.append({"flag": opt.flag if i == 0 else "", "default": opt.default if i == 0 else "",
                         "name": row["name"], "package": row["package"], "installed": row["installed"],
                         "want": row["want"], "have": row["have"]})
    opt_cols = [
        Column("FLAG", "flag", min_width=8, flexible=True, shrink_priority=2),
        Column("DEFAULT", "default", min_width=7),
        Column("CHECKS FOR", "name", min_width=12, flexible=True, shrink_priority=0),
        Column("PACKAGE", "package", min_width=14, flexible=True, shrink_priority=1),
        Column("INST?", "installed", min_width=5),
        Column("WANT", "want", min_width=6, flexible=True, shrink_priority=4),
        Column("HAVE", "have", min_width=10, flexible=True, shrink_priority=3),
    ]
    print(render_table(opt_cols, rows, width=term_width))
    if an.s.notes:
        print()
        print(bar)
        print(" Notes")
        print(bar)
        for n in an.s.notes[:25]:
            print(f"  - {n}")
        if len(an.s.notes) > 25:
            print(f"  ... and {len(an.s.notes) - 25} more")
    print()
    print("Note: to enable or disable a feature, pass its flag to cmake with ON or OFF,")
    print("      e.g.  cmake -DNAME=ON ..   or   cmake -DNAME=OFF ..   (same syntax works with -D here)")


def to_json(an: Analyzer, options: "list[Option]", mandatory: "list[Check]") -> str:
    return json.dumps({
        "mandatory": resolve_cmake_checks(mandatory),
        "optional": [{"flag": o.flag, "default": o.default, "kind": o.kind, "description": o.desc,
                      "checks": resolve_cmake_checks(o.checks)} for o in options],
        "notes": an.s.notes,
    }, indent=2)


def main() -> int:
    ap = argparse.ArgumentParser(description="Parse a CMake project's CMakeLists.txt files and map what they check "
                                             "for to Debian packages.")
    ap.add_argument("path", help="path to a CMakeLists.txt, or the project directory containing one")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of the text report")
    ap.add_argument("-o", "--output", metavar="FILE",
                    help="write the report to FILE instead of the terminal (columns are never truncated)")
    ap.add_argument("-D", dest="defs", action="append", default=[], metavar="NAME=VALUE",
                    help="set an option/variable as `cmake -DNAME=VALUE` would; repeatable")
    args = ap.parse_args()

    path = args.path
    if os.path.isdir(path):
        path = os.path.join(path, "CMakeLists.txt")
    if not os.path.isfile(path):
        print(f"error: no such file: {path}", file=sys.stderr)
        return 1
    base = os.path.basename(path)
    if base != "CMakeLists.txt" and not base.endswith(".cmake"):
        print(f"warning: '{base}' doesn't look like a CMake file (expected CMakeLists.txt)", file=sys.stderr)
        print("configcheck-cmake.py parses CMake language; use configcheck.py for configure scripts and "
              "makecheck.py for Makefiles.", file=sys.stderr)
        if sys.stdin.isatty():
            if input("Continue anyway? [y/N] ").strip().lower() not in ("y", "yes"):
                print("aborted.", file=sys.stderr)
                return 1

    defs = {}
    for d in args.defs:
        k, _, v = d.partition("=")
        defs[k.split(":")[0]] = v if _ else "ON"

    print(cc.describe_contents_files(), file=sys.stderr)
    root = os.path.dirname(os.path.abspath(path))
    settings = Settings(defs)
    an = Analyzer(settings, root)
    an.run_file(os.path.abspath(path))
    options, mandatory = build_model(an)
    all_checks = mandatory + [c for o in options for c in o.checks]
    cc.prefetch_contents(_contents_terms(all_checks))
    hdrs = [c.name for c in all_checks if c.type == "header"]
    for c in all_checks:
        if c.type == "cmakepkg":
            hdrs += [t.partition(":")[2].split()[-1] for t in (c.candidates or ()) if t.startswith("h:")]
    cc.prefetch_contents([p for h in hdrs for p in system_include_paths(h)], exact=True)
    libs = [_lib_basename(c.name) for c in all_checks if c.type == "library"]
    cc.prefetch_contents([p for l in libs if l not in GLIBC_LIBS
                          for p in (f"usr/lib/{_multiarch()}/lib{l}.so", f"usr/lib/lib{l}.so",
                                    f"lib/{_multiarch()}/lib{l}.so")], exact=True)

    if args.output:
        with open(args.output, "w") as f:
            with contextlib.redirect_stdout(f):
                if args.json:
                    print(to_json(an, options, mandatory))
                else:
                    print_report(an, options, mandatory, path, width=1000)
        print(f"Report written to {args.output}")
    elif args.json:
        print(to_json(an, options, mandatory))
    else:
        print_report(an, options, mandatory, path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
