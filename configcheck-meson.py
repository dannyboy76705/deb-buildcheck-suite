#!/usr/bin/env python3
"""
configcheck-meson.py - Parse a Meson project's meson.build files statically
(without running meson) to list what it checks for - mandatory and optional -
which Debian package provides each, and whether that package is installed.

The Meson counterpart of configcheck.py (autoconf) and configcheck-cmake.py.
Part of deb-buildcheck-suite. It only IMPORTS configcheck.py (package lookup,
Contents scanning, table rendering) and never modifies it, so nothing here can
affect the other tools. Must live in the same directory as configcheck.py.

Usage: ./configcheck-meson.py /path/to/meson.build-or-project-dir
                              [--json] [-o FILE] [-D NAME=VALUE ...]
"""

import argparse
import contextlib
import dataclasses
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
    print("error: configcheck.py not found - configcheck-meson.py shares its package/library",
          file=sys.stderr)
    print("resolution and table code with it, so both files need to be in the same directory.",
          file=sys.stderr)
    sys.exit(1)


# ===========================================================================
# Stage 1: lexer. Meson is a small Python-like language: newlines end
# statements except inside (), [] and {}; strings are '...' or '''...'''
# (optionally f'...'), comments start with #.
# ===========================================================================
@dataclass
class Tok:
    kind: str        # id | num | str | fstr | op | nl | eof
    val: object
    line: int


KEYWORDS = {"if", "elif", "else", "endif", "foreach", "endforeach", "and", "or", "not", "in",
            "true", "false", "break", "continue"}
OPS3 = {"not in"}
OPS2 = {"==", "!=", "<=", ">=", "+=", "-=", "*=", "/=", "%=", "//"}
OPS1 = set("()[]{},:.+-*/%=<>?")
_STR_ESC = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "'": "'", "a": "\a", "b": "\b", "f": "\f", "v": "\v"}


class ParseError(Exception):
    def __init__(self, msg: str, line: int):
        super().__init__(msg)
        self.line = line


def lex(text: str) -> "list[Tok]":
    toks: "list[Tok]" = []
    i, n, line, depth = 0, len(text), 1, 0
    while i < n:
        c = text[i]
        if c == "\n":
            if depth == 0 and toks and toks[-1].kind != "nl":
                toks.append(Tok("nl", None, line))
            line += 1
            i += 1
        elif c in " \t\r\f\v":
            i += 1
        elif c == "\\" and text[i + 1:i + 2] == "\n":
            line += 1
            i += 2
        elif c == "#":
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif c == "'" or (c in "fF" and text[i + 1:i + 2] == "'") or (c in "rR" and text[i + 1:i + 2] == "'"):
            fmt = c in "fF"
            raw = c in "rR"
            start = i + (1 if c != "'" else 0)
            if text.startswith("'''", start):
                end = text.find("'''", start + 3)
                if end < 0:
                    end = n
                body = text[start + 3:end]
                toks.append(Tok("fstr" if fmt else "str", body, line))
                line += text.count("\n", i, end)
                i = min(end + 3, n)
            else:
                j = start + 1
                buf = []
                while j < n and text[j] != "'":
                    ch = text[j]
                    if ch == "\\" and j + 1 < n and not raw:
                        nxt = text[j + 1]
                        if nxt == "\n":
                            line += 1
                            j += 2
                            continue
                        buf.append(_STR_ESC.get(nxt, "\\" + nxt))
                        j += 2
                        continue
                    if ch == "\n":
                        break
                    buf.append(ch)
                    j += 1
                toks.append(Tok("fstr" if fmt else "str", "".join(buf), line))
                i = j + 1
        elif c.isdigit():
            m = re.match(r"0[xX][0-9a-fA-F]+|0[oO][0-7]+|0[bB][01]+|\d+", text[i:])
            toks.append(Tok("num", int(m.group(0), 0) if not re.match(r"0\d", m.group(0)) else int(m.group(0)), line))
            i += m.end()
        elif c.isalpha() or c == "_":
            m = re.match(r"[A-Za-z_][A-Za-z0-9_]*", text[i:])
            word = m.group(0)
            i += m.end()
            if word == "not":
                m2 = re.match(r"\s+in\b", text[i:])
                if m2:
                    toks.append(Tok("op", "not in", line))
                    i += m2.end()
                    continue
            toks.append(Tok("kw" if word in KEYWORDS else "id", word, line))
        elif text[i:i + 2] in OPS2:
            toks.append(Tok("op", text[i:i + 2], line))
            i += 2
        elif c in OPS1:
            if c in "([{":
                depth += 1
            elif c in ")]}":
                depth = max(0, depth - 1)
            toks.append(Tok("op", c, line))
            i += 1
        else:
            i += 1
    toks.append(Tok("nl", None, line))
    toks.append(Tok("eof", None, line))
    return toks


# ===========================================================================
# Stage 2: parser -> small AST of tuples.
# ===========================================================================
class Parser:
    def __init__(self, toks: "list[Tok]"):
        self.t = toks
        self.p = 0

    def peek(self) -> Tok:
        return self.t[self.p]

    def next(self) -> Tok:
        tok = self.t[self.p]
        self.p += 1
        return tok

    def is_op(self, v: str) -> bool:
        t = self.peek()
        return t.kind == "op" and t.val == v

    def is_kw(self, v: str) -> bool:
        t = self.peek()
        return t.kind == "kw" and t.val == v

    def expect_op(self, v: str) -> Tok:
        t = self.next()
        if t.kind != "op" or t.val != v:
            raise ParseError(f"expected '{v}'", t.line)
        return t

    def skip_nl(self) -> None:
        while self.peek().kind == "nl":
            self.p += 1

    def parse_all(self) -> list:
        body = []
        self.skip_nl()
        while self.peek().kind != "eof":
            body.append(self.safe_stmt())
            self.skip_nl()
        return [b for b in body if b]

    def safe_stmt(self):
        start = self.p
        try:
            return self.stmt()
        except ParseError:
            # resync: skip to the next top-level newline
            self.p = max(start, self.p)
            depth = 0
            while self.peek().kind != "eof":
                t = self.next()
                if t.kind == "op" and t.val in "([{":
                    depth += 1
                elif t.kind == "op" and t.val in ")]}":
                    depth = max(0, depth - 1)
                elif t.kind == "nl" and depth == 0:
                    break
            return None

    def block(self, stop: "set[str]") -> list:
        body = []
        self.skip_nl()
        while not (self.peek().kind == "kw" and self.peek().val in stop) and self.peek().kind != "eof":
            s = self.safe_stmt()
            if s:
                body.append(s)
            self.skip_nl()
        return body

    def stmt(self):
        t = self.peek()
        if t.kind == "kw":
            if t.val == "if":
                self.next()
                branches = []
                cond = self.expr()
                body = self.block({"elif", "else", "endif"})
                branches.append((cond, body))
                else_body = None
                while True:
                    k = self.peek()
                    if k.kind == "kw" and k.val == "elif":
                        self.next()
                        cond = self.expr()
                        branches.append((cond, self.block({"elif", "else", "endif"})))
                    elif k.kind == "kw" and k.val == "else":
                        self.next()
                        else_body = self.block({"endif"})
                    else:
                        break
                if self.is_kw("endif"):
                    self.next()
                return ("if", t.line, branches, else_body)
            if t.val == "foreach":
                self.next()
                names = [self.next().val]
                while self.is_op(","):
                    self.next()
                    names.append(self.next().val)
                self.expect_op(":")
                it = self.expr()
                body = self.block({"endforeach"})
                if self.is_kw("endforeach"):
                    self.next()
                return ("foreach", t.line, names, it, body)
            if t.val in ("break", "continue"):
                self.next()
                return (t.val, t.line)
        e = self.expr()
        nt = self.peek()
        if nt.kind == "op" and nt.val in ("=", "+=", "-=", "*=", "/=", "%="):
            self.next()
            val = self.expr()
            return ("assign", t.line, e, nt.val, val)
        return ("expr", t.line, e)

    # ---- expressions (lowest to highest precedence) ----
    def expr(self):
        return self.ternary()

    def ternary(self):
        c = self.or_()
        if self.is_op("?"):
            self.next()
            a = self.ternary()
            self.expect_op(":")
            b = self.ternary()
            return ("tern", c, a, b)
        return c

    def or_(self):
        l = self.and_()
        while self.is_kw("or"):
            self.next()
            l = ("bin", "or", l, self.and_())
        return l

    def and_(self):
        l = self.not_()
        while self.is_kw("and"):
            self.next()
            l = ("bin", "and", l, self.not_())
        return l

    def not_(self):
        if self.is_kw("not"):
            self.next()
            return ("un", "not", self.not_())
        return self.cmp()

    def cmp(self):
        l = self.add()
        while True:
            t = self.peek()
            if t.kind == "op" and t.val in ("==", "!=", "<", ">", "<=", ">=", "not in"):
                self.next()
                l = ("bin", t.val, l, self.add())
            elif t.kind == "kw" and t.val == "in":
                self.next()
                l = ("bin", "in", l, self.add())
            else:
                return l

    def add(self):
        l = self.mul()
        while self.peek().kind == "op" and self.peek().val in ("+", "-"):
            op = self.next().val
            l = ("bin", op, l, self.mul())
        return l

    def mul(self):
        l = self.unary()
        while self.peek().kind == "op" and self.peek().val in ("*", "/", "%", "//"):
            op = self.next().val
            l = ("bin", op, l, self.unary())
        return l

    def unary(self):
        if self.is_op("-"):
            self.next()
            return ("un", "-", self.unary())
        return self.postfix()

    def postfix(self):
        e = self.atom()
        while True:
            if self.is_op("."):
                self.next()
                name = self.next()
                if name.kind not in ("id", "kw"):
                    raise ParseError("method name expected", name.line)
                self.expect_op("(")
                args, kw = self.args(")")
                e = ("method", name.line, e, name.val, args, kw)
            elif self.is_op("["):
                self.next()
                idx = self.expr()
                self.expect_op("]")
                e = ("index", e, idx)
            else:
                return e

    def args(self, close: str):
        args, kw = [], {}
        while not self.is_op(close):
            if self.peek().kind == "eof":
                raise ParseError("unterminated argument list", self.peek().line)
            if self.peek().kind == "id" and self.t[self.p + 1].kind == "op" and self.t[self.p + 1].val == ":":
                name = self.next().val
                self.next()
                kw[name] = self.expr()
            else:
                args.append(self.expr())
            if self.is_op(","):
                self.next()
            elif not self.is_op(close):
                raise ParseError("',' or closing bracket expected", self.peek().line)
        self.next()
        return args, kw

    def atom(self):
        t = self.next()
        if t.kind == "num":
            return ("lit", t.val)
        if t.kind == "str":
            return ("lit", t.val)
        if t.kind == "fstr":
            return ("fstr", t.val)
        if t.kind == "kw" and t.val in ("true", "false"):
            return ("lit", t.val == "true")
        if t.kind == "id":
            if self.is_op("("):
                self.next()
                args, kw = self.args(")")
                return ("call", t.line, t.val, args, kw)
            return ("id", t.val)
        if t.kind == "op" and t.val == "(":
            e = self.expr()
            self.expect_op(")")
            return e
        if t.kind == "op" and t.val == "[":
            items = []
            while not self.is_op("]"):
                if self.peek().kind == "eof":
                    raise ParseError("unterminated list", t.line)
                items.append(self.expr())
                if self.is_op(","):
                    self.next()
            self.next()
            return ("list", items)
        if t.kind == "op" and t.val == "{":
            pairs = []
            while not self.is_op("}"):
                if self.peek().kind == "eof":
                    raise ParseError("unterminated dict", t.line)
                k = self.expr()
                self.expect_op(":")
                pairs.append((k, self.expr()))
                if self.is_op(","):
                    self.next()
            self.next()
            return ("dict", pairs)
        raise ParseError(f"unexpected {t.val!r}", t.line)


def parse(text: str) -> list:
    return Parser(lex(text)).parse_all()


# ===========================================================================
# Stage 3: values. Meson is evaluated "as far as it can be": host facts
# (Linux, gcc) are assumed, but user options and anything that depends on
# what is installed stay UNKNOWN - and an UNKNOWN remembers which options it
# came from, so a check inside `if get_option('tests')` is grouped under
# that option, exactly like configcheck groups by --enable-x.
# ===========================================================================
class Unk:
    __slots__ = ("opts",)

    def __init__(self, opts=()):
        self.opts = frozenset(opts)

    def __repr__(self):
        return f"Unk({sorted(self.opts)})"

    def __bool__(self):
        raise TypeError("truth value of unknown")


def unk(*vals) -> Unk:
    o = set()
    for v in vals:
        if isinstance(v, Unk):
            o |= v.opts
        elif isinstance(v, FeatureVal) and v.value is None:
            o.add(v.name)
        elif isinstance(v, (list, tuple)):
            for x in v:
                if isinstance(x, Unk):
                    o |= x.opts
                elif isinstance(x, FeatureVal) and x.value is None:
                    o.add(x.name)
        elif isinstance(v, dict):
            for x in v.values():
                if isinstance(x, Unk):
                    o |= x.opts
    return Unk(o)


@dataclass
class FeatureVal:
    name: str
    value: "str | None"          # 'enabled' | 'disabled' | 'auto' | None (= the user's choice, unknown)


class Opaque:
    """Targets, files, include dirs, run results... - things whose only use is being passed around."""
    pass


@dataclass
class DepObj:
    names: tuple
    required: "bool | None"      # True: configure fails without it. None: unknown/optional.
    found_known: "bool | None" = None


@dataclass
class ProgObj:
    name: str
    required: "bool | None"


class CompilerObj:
    def __init__(self, lang: str):
        self.lang = lang


class MachineObj:
    def __init__(self, kind: str):
        self.kind = kind


class MesonObj:
    pass


class CfgData:
    def __init__(self):
        self.d: dict = {}


class ModuleObj:
    def __init__(self, name: str):
        self.name = name


class VersionStr(str):
    pass


@dataclass
class OptionInfo:
    name: str
    otype: str
    value: object
    choices: list
    desc: str
    user: "str | None" = None


@dataclass
class Frame:
    text: str
    opts: tuple


@dataclass
class Found:
    kind: str                       # header | library | pkgconfig | program | dependency | compiler | meson
    name: str
    frames: tuple
    file: str
    line: int
    candidates: "tuple | None" = None
    version_req: "VersionReq | None" = None
    extra: dict = field(default_factory=dict)


class StopSignal(Exception):
    pass


class BreakSignal(Exception):
    pass


class ContinueSignal(Exception):
    pass


class SubdirDone(Exception):
    pass


class FatalError(Exception):
    pass


LANG_PROGS = {"c": ("cc", "gcc", "clang"), "cpp": ("c++", "g++", "clang++"), "fortran": ("gfortran",),
              "objc": ("cc", "gcc"), "objcpp": ("c++", "g++"), "cuda": ("nvcc",), "rust": ("rustc",),
              "vala": ("valac",), "d": ("gdc", "ldc2"), "cython": ("cython", "cython3"), "nasm": ("nasm",),
              "java": ("javac",), "swift": ("swiftc",), "cs": ("mcs",)}

BUILTIN_OPTIONS = {
    "prefix": "/usr/local", "bindir": "bin", "sbindir": "sbin", "libdir": "lib", "libexecdir": "libexec",
    "datadir": "share", "includedir": "include", "sysconfdir": "etc", "localstatedir": "var",
    "sharedstatedir": "com", "mandir": "share/man", "infodir": "share/info", "localedir": "share/locale",
    "buildtype": "debug", "default_library": "shared", "warning_level": "1", "werror": False, "debug": True,
    "optimization": "0", "strip": False, "unity": "off", "layout": "mirror", "wrap_mode": "default",
    "b_ndebug": "false", "b_lto": False, "b_sanitize": "none", "b_pie": False, "b_coverage": False,
    "b_staticpic": True, "b_lundef": True, "b_asneeded": True, "b_pch": True, "b_colorout": "always",
    "c_std": "none", "cpp_std": "none", "errorlogs": True, "install_umask": "preserve",
    "pkg_config_path": [], "cmake_prefix_path": [], "backend": "ninja", "auto_features": "auto",
    "libdir_cross": "lib", "python.install_env": "prefix", "python.platlibdir": "", "python.purelibdir": "",
    "force_fallback_for": [], "genvslite": "vs2022", "vsenv": False, "stdsplit": True, "licensedir": "",
}

# dependency('name') names that are not packages at all, or need a different lookup than pkg-config name.pc
DEP_OVERRIDES = {
    "threads": ("none:POSIX threads are part of glibc (libc6-dev)",),
    "dl": ("none:libdl is part of glibc (libc6-dev)",),
    "m": ("none:libm is part of glibc (libc6-dev)",),
    "rt": ("none:librt is part of glibc (libc6-dev)",),
    "iconv": ("none:iconv is part of glibc (libc6-dev)",),
    "intl": ("none:gettext's libintl is part of glibc (libc6-dev)",),
    "openmp": ("none:OpenMP comes with the compiler (libgomp1 at runtime)",),
    "boost": ("h:boost/version.hpp",),
    "python3": ("prog:python3", "pc:python3-embed", "h:Python.h"),
    "python": ("prog:python3", "pc:python3-embed", "h:Python.h"),
    "gtest": ("pc:gtest", "h:gtest/gtest.h"),
    "gmock": ("pc:gmock", "h:gmock/gmock.h"),
    "qt5": ("pc:Qt5Core",),
    "qt6": ("pc:Qt6Core",),
    "curses": ("pc:ncurses", "h:curses.h"),
    "cups": ("prog:cups-config",),
    "llvm": ("prog:llvm-config",),
    "sdl2": ("pc:sdl2",),
    "bzip2": ("pc:bzip2", "h:bzlib.h"),
    "zlib": ("pc:zlib", "h:zlib.h"),
    "gl": ("pc:gl", "h:GL/gl.h"),
    "glu": ("pc:glu", "h:GL/glu.h"),
    "pcap": ("prog:pcap-config", "h:pcap.h"),
    "libgcrypt": ("prog:libgcrypt-config", "h:gcrypt.h"),
    "wxwidgets": ("prog:wx-config",),
}


def _version_tuple(v: str) -> tuple:
    return tuple(int(p) if p.isdigit() else 0 for p in re.split(r"[.\-+_~]", str(v).strip()) if p != "")


def _cmp_versions(have: str, req: str) -> "bool | None":
    m = re.match(r"^\s*(>=|<=|==|!=|<|>|=)?\s*(.+?)\s*$", req)
    if not m:
        return None
    op, want = m.group(1) or "==", m.group(2)
    a, b = _version_tuple(have), _version_tuple(want)
    return {">=": a >= b, "<=": a <= b, "==": a == b, "=": a == b, "!=": a != b, "<": a < b, ">": a > b}[op]


class Settings:
    def __init__(self, user_defs: "dict[str, str]"):
        self.user_defs = user_defs
        self.notes: "list[str]" = []
        self.meson_version, self.meson_version_detected = _detect_meson_version()

    def note(self, msg: str) -> None:
        if msg not in self.notes:
            self.notes.append(msg)


def _detect_meson_version() -> "tuple[str, bool]":
    exe = shutil.which("meson")
    if exe:
        try:
            out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
            if re.match(r"^\d+\.\d+", out):
                return out, True
        except Exception:
            pass
    return "1.7.0", False          # Debian trixie's version


# ===========================================================================
# Stage 4: interpreter
# ===========================================================================
INCLUDE_RE = re.compile(r'#\s*include\s*[<"]([^>"]+)[>"]')


class Analyzer:
    def __init__(self, settings: Settings, root_dir: str):
        self.s = settings
        self.root_dir = root_dir
        self.cur_dir = root_dir
        self.cur_file = ""
        self.env: "dict[str, object]" = {}
        self.options: "dict[str, OptionInfo]" = {}
        self.option_order: "list[str]" = []
        self.frames: "list[Frame]" = []
        self.found: "list[Found]" = []
        self.languages: "list[str]" = []
        self.project_defaults: "dict[str, object]" = {}
        self.project_name = ""
        self.project_version = ""
        self.meson_min: "str | None" = None
        self.steps = 0
        self.pending_frames: "list[Frame]" = []
        self.subdir_depth = 0
        self.aborted = False
        self.compilers: "dict[str, CompilerObj]" = {}

    # ---- helpers -------------------------------------------------------------
    def record(self, kind: str, name: str, line: int, extra_opts=(), **kw) -> None:
        frames = tuple(self.frames)
        if extra_opts:
            frames += (Frame("required:", tuple(sorted(extra_opts))),)
        self.found.append(Found(kind=kind, name=name, frames=frames, file=self.cur_file, line=line, **kw))

    _hdr_cache: "set[str] | None" = None

    def is_project_header(self, h: str) -> bool:
        """True if this header ships inside the project's own source tree (not something to install)."""
        if self._hdr_cache is None:
            names = set()
            for dp, dn, fn in os.walk(self.root_dir):
                dn[:] = [d for d in dn if not d.startswith(".") and d not in ("builddir", "build", "subprojects")]
                rel = os.path.relpath(dp, self.root_dir)
                for f in fn:
                    if f.endswith((".h", ".hpp", ".hh", ".inc")):
                        names.add(os.path.normpath(os.path.join(rel, f)))
            self._hdr_cache = names
        return any(n == h or n.endswith("/" + h) for n in self._hdr_cache)

    def where(self, line: int) -> str:
        return f"{os.path.basename(os.path.dirname(self.cur_file)) or '.'}/{os.path.basename(self.cur_file)}:{line}"

    def req_state(self, v) -> "tuple[bool | None, tuple]":
        """required: kwarg -> (known value, option names it depends on). None known = optional-or-unknown."""
        if v is None:
            return True, ()
        if isinstance(v, bool):
            return v, ()
        if isinstance(v, FeatureVal):
            if v.value == "enabled":
                return True, ()
            if v.value == "disabled":
                return False, ()
            if v.value == "auto":
                return False, ()
            return None, (v.name,)
        if isinstance(v, Unk):
            return None, tuple(sorted(v.opts))
        return True, ()

    # ---- running files -----------------------------------------------------------
    def run_file(self, path: str) -> bool:
        try:
            text = open(path, "r", errors="replace").read()
        except OSError:
            return False
        saved = self.cur_file
        self.cur_file = path
        try:
            body = parse(text)
            self.exec_block(body)
        except SubdirDone:
            pass
        finally:
            self.cur_file = saved
        return True

    def exec_block(self, body: list) -> None:
        self._popcount.append(0)
        try:
            self._run_stmts(body)
        finally:
            for _ in range(self._popcount.pop()):
                self.frames.pop()

    _popcount: list = []

    def _run_stmts(self, body: list) -> None:
        for st in body:
            if self.aborted:
                return
            while self.pending_frames:
                self.frames.append(self.pending_frames.pop())
                self._popcount[-1] += 1
            self.steps += 1
            if self.steps > 2_000_000:
                self.s.note("analysis stopped early: step limit reached")
                self.aborted = True
                return
            try:
                self.exec_stmt(st)
            except (BreakSignal, ContinueSignal, SubdirDone, FatalError):
                raise
            except RecursionError:
                self.s.note(f"{self.where(st[1])}: expression too deeply nested - skipped")
            except Exception as e:                      # one odd statement must not sink the whole analysis
                self.s.note(f"{self.where(st[1]) if len(st) > 1 and isinstance(st[1], int) else self.cur_file}: "
                            f"could not evaluate ({type(e).__name__}: {e})")

    def exec_stmt(self, st) -> None:
        kind = st[0]
        if kind == "expr":
            self.eval(st[2])
        elif kind == "assign":
            _, line, target, op, valexpr = st
            val = self.eval(valexpr)
            if op != "=":
                cur = self.eval(target)
                val = self.binop(op[0], cur, val)
            if target[0] == "id":
                self.env[target[1]] = val
        elif kind == "if":
            self.exec_if(st)
        elif kind == "foreach":
            self.exec_foreach(st)
        elif kind == "break":
            raise BreakSignal()
        elif kind == "continue":
            raise ContinueSignal()

    def truth(self, v) -> "bool | None":
        if isinstance(v, Unk):
            return None
        if isinstance(v, FeatureVal):
            return None if v.value is None else v.value == "enabled"
        if isinstance(v, (CfgData, Opaque, DepObj, ProgObj, CompilerObj)):
            return True
        try:
            return bool(v)
        except Exception:
            return None

    def exec_if(self, st) -> None:
        _, line, branches, else_body = st
        before = dict(self.env)
        uncertain = False
        taken = False
        seen_opts: "list[str]" = []
        for cond, body in branches:
            if taken:
                break
            v = self.eval(cond)
            t = self.truth(v)
            if t is False:
                continue
            if t is True:
                if uncertain:
                    self.frames.append(Frame("else", tuple(seen_opts)))
                    try:
                        self.exec_block(body)
                    finally:
                        self.frames.pop()
                else:
                    self.exec_block(body)
                taken = True
                break
            opts = tuple(sorted(v.opts)) if isinstance(v, Unk) else \
                ((v.name,) if isinstance(v, FeatureVal) else ())
            self.frames.append(Frame("if", opts))
            try:
                self.exec_block(body)
            except SubdirDone:
                # `if not X: subdir_done()` - everything after this point only happens when the condition is false
                self.pending_frames.append(Frame("not", opts))
            finally:
                self.frames.pop()
            uncertain = True
            seen_opts += [o for o in opts if o not in seen_opts]
        if not taken and else_body is not None:
            if uncertain:
                self.frames.append(Frame("else", tuple(seen_opts)))
                try:
                    self.exec_block(else_body)
                finally:
                    self.frames.pop()
            else:
                self.exec_block(else_body)
        if uncertain:
            # whatever was assigned only 'maybe' is not a known value afterwards; it remembers which options decided it
            for k in set(before) | set(self.env):
                if before.get(k, _MISSING) is not self.env.get(k, _MISSING):
                    old, new = before.get(k, _MISSING), self.env.get(k, _MISSING)
                    if isinstance(new, list) and isinstance(old, list) and new[:len(old)] == old:
                        continue                         # a list that only grew: keep it (over-approximation)
                    self.env[k] = Unk(seen_opts)

    def exec_foreach(self, st) -> None:
        _, line, names, itexpr, body = st
        it = self.eval(itexpr)
        if isinstance(it, dict):
            items = [(k, v) for k, v in it.items()]
        elif isinstance(it, list):
            items = list(it)
        else:
            items = None
        saved = {n: self.env.get(n, _MISSING) for n in names}
        try:
            if items is None or len(items) > 200:
                for n in names:
                    self.env[n] = unk(it)
                try:
                    self.exec_block(body)
                except (BreakSignal, ContinueSignal):
                    pass
            else:
                for x in items:
                    if len(names) == 2 and isinstance(x, tuple):
                        self.env[names[0]], self.env[names[1]] = x
                    else:
                        self.env[names[0]] = x
                    try:
                        self.exec_block(body)
                    except ContinueSignal:
                        continue
                    except BreakSignal:
                        break
        finally:
            for n, v in saved.items():
                if v is _MISSING:
                    self.env.pop(n, None)
                else:
                    self.env[n] = v

    # ---- expressions -------------------------------------------------------------------
    def eval(self, e):
        k = e[0]
        if k == "lit":
            return e[1]
        if k == "id":
            return self.env.get(e[1], Unk())
        if k == "list":
            out = []
            for x in e[1]:
                v = self.eval(x)
                out.append(v)
            return out
        if k == "dict":
            d = {}
            for kk, vv in e[1]:
                key = self.eval(kk)
                try:
                    d[key] = self.eval(vv)
                except TypeError:
                    pass
            return d
        if k == "fstr":
            return re.sub(r"@([A-Za-z_][A-Za-z0-9_]*)@", lambda m: self._fmt(self.env.get(m.group(1), "@@")), e[1])
        if k == "un":
            v = self.eval(e[2])
            if e[1] == "not":
                t = self.truth(v)
                return unk(v) if t is None else (not t)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return -v
            return unk(v)
        if k == "bin":
            return self.eval_bin(e)
        if k == "tern":
            c = self.eval(e[1])
            t = self.truth(c)
            if t is True:
                return self.eval(e[2])
            if t is False:
                return self.eval(e[3])
            a, b = self.eval(e[2]), self.eval(e[3])
            return unk(c, a, b)
        if k == "index":
            o, i = self.eval(e[1]), self.eval(e[2])
            try:
                if isinstance(o, (list, str, dict)) and not isinstance(i, Unk):
                    return o[i]
            except (IndexError, KeyError, TypeError):
                pass
            return unk(o, i)
        if k == "call":
            return self.call_function(e)
        if k == "method":
            return self.call_method(e)
        return Unk()

    @staticmethod
    def _fmt(v) -> str:
        if isinstance(v, Unk):
            return "<unknown>"
        if isinstance(v, bool):
            return "true" if v else "false"
        return str(v)

    def eval_bin(self, e):
        op = e[1]
        if op in ("and", "or"):
            l = self.eval(e[2])
            tl = self.truth(l)
            if op == "and":
                if tl is False:
                    return False
                r = self.eval(e[3])
                tr = self.truth(r)
                if tr is False:
                    return False
                if tl is True and tr is True:
                    return True
                return unk(l, r)
            if tl is True:
                return True
            r = self.eval(e[3])
            tr = self.truth(r)
            if tr is True:
                return True
            if tl is False and tr is False:
                return False
            return unk(l, r)
        return self.binop(op, self.eval(e[2]), self.eval(e[3]))

    def binop(self, op: str, l, r):
        if isinstance(l, FeatureVal) or isinstance(r, FeatureVal):
            lv = l.value if isinstance(l, FeatureVal) else l
            rv = r.value if isinstance(r, FeatureVal) else r
            if lv is None or rv is None:
                return unk(l, r)
            l, r = lv, rv
        if isinstance(l, Unk) or isinstance(r, Unk):
            return unk(l, r)
        try:
            if op == "==":
                return l == r
            if op == "!=":
                return l != r
            if op == "<":
                return l < r
            if op == ">":
                return l > r
            if op == "<=":
                return l <= r
            if op == ">=":
                return l >= r
            if op == "in":
                return l in r
            if op == "not in":
                return l not in r
            if op == "+":
                if isinstance(l, list):
                    return l + (r if isinstance(r, list) else [r])
                if isinstance(l, dict) and isinstance(r, dict):
                    return {**l, **r}
                return l + r
            if op == "-":
                return l - r
            if op == "*":
                return l * r
            if op == "/":
                if isinstance(l, str) and isinstance(r, str):
                    return os.path.join(l, r) if not r.startswith("/") else r
                return l // r
            if op == "//":
                return l // r
            if op == "%":
                return l % r
        except Exception:
            pass
        return unk(l, r)

    # ---- functions ------------------------------------------------------------------------
    def args(self, e_args, e_kw):
        return [self.eval(a) for a in e_args], {k: self.eval(v) for k, v in e_kw.items()}

    def call_function(self, e):
        _, line, name, eargs, ekw = e
        args, kw = self.args(eargs, ekw)
        h = getattr(self, "f_" + name, None)
        if h is not None:
            return h(line, args, kw)
        if name in OPAQUE_FUNCS:
            return Opaque()
        return Unk()

    def flat_strs(self, v) -> "list[str]":
        if isinstance(v, str):
            return [v]
        if isinstance(v, list):
            out = []
            for x in v:
                out += self.flat_strs(x)
            return out
        return []

    def f_project(self, line, args, kw):
        if args and isinstance(args[0], str):
            self.project_name = args[0]
        v = kw.get("version")
        if isinstance(v, str):
            self.project_version = v
        for lang in self.flat_strs(args[1:]):
            self.add_language(lang, line)
        mv = kw.get("meson_version")
        if isinstance(mv, str) and self.meson_min is None:
            self.meson_min = mv
            m = re.match(r"^\s*(>=|>|==|=|<=|<)?\s*(.+)$", mv)
            if m:
                self.record("meson", "meson", line, version_req=VersionReq(op=m.group(1) or ">=", version=m.group(2),
                                                                         source="meson_version"))
        d = kw.get("default_options")
        items = d if isinstance(d, list) else ([f"{k}={v}" for k, v in d.items()] if isinstance(d, dict) else [])
        for it in items:
            if isinstance(it, str) and "=" in it:
                k, _, val = it.partition("=")
                self.project_defaults[k.strip()] = val.strip()
        return Opaque()

    def add_language(self, lang: str, line: int) -> None:
        lang = lang.lower()
        if lang in self.languages:
            return
        self.languages.append(lang)
        self.compilers.setdefault(lang, CompilerObj(lang))
        progs = LANG_PROGS.get(lang)
        if progs:
            self.record("compiler", f"{lang.upper() if lang == 'c' else lang} compiler", line, candidates=progs)

    def f_add_languages(self, line, args, kw):
        for lang in self.flat_strs(args):
            self.add_language(lang, line)
        return True

    def f_option(self, line, args, kw):
        if not args or not isinstance(args[0], str):
            return Opaque()
        name = args[0]
        otype = kw.get("type") if isinstance(kw.get("type"), str) else "string"
        info = OptionInfo(name, otype, kw.get("value"), kw.get("choices") if isinstance(kw.get("choices"), list) else [],
                          kw.get("description") if isinstance(kw.get("description"), str) else "")
        if name in self.s.user_defs:
            info.user = self.s.user_defs[name]
        if name not in self.options:
            self.option_order.append(name)
        self.options[name] = info
        return Opaque()

    def f_get_option(self, line, args, kw):
        if not args or not isinstance(args[0], str):
            return Unk()
        name = args[0]
        info = self.options.get(name)
        if info is not None:
            u = info.user
            if info.otype == "feature":
                return FeatureVal(name, u if u in ("enabled", "disabled", "auto") else None)
            if u is not None:
                if info.otype == "boolean":
                    return u.lower() in ("true", "1", "yes", "on")
                if info.otype == "integer":
                    try:
                        return int(u)
                    except ValueError:
                        return Unk({name})
                if info.otype == "array":
                    return [x for x in re.split(r"[,\s]+", u.strip("[]")) if x]
                return u
            return Unk({name})
        if name in self.s.user_defs:
            return self.s.user_defs[name]
        if name in self.project_defaults:
            v = self.project_defaults[name]
            return v.lower() == "true" if v.lower() in ("true", "false") else v
        if name == "buildtype" or name in BUILTIN_OPTIONS:
            return BUILTIN_OPTIONS.get(name, Unk())
        return Unk()

    def f_meson_dummy(self, *a):                     # (placeholder, keeps the f_ namespace readable)
        return Unk()

    def f_join_paths(self, line, args, kw):
        strs = [a for a in args if isinstance(a, str)]
        if len(strs) == len(args):
            out = ""
            for s in strs:
                out = s if s.startswith("/") or not out else os.path.join(out, s)
            return out
        return unk(args)

    def f_files(self, line, args, kw):
        return Opaque()

    def f_subdir(self, line, args, kw):
        if not args or not isinstance(args[0], str):
            return Opaque()
        d = os.path.join(self.cur_dir, args[0])
        f = os.path.join(d, "meson.build")
        if not os.path.isfile(f):
            self.s.note(f"{self.where(line)}: subdir('{args[0]}') - no meson.build there")
            return Opaque()
        if self.subdir_depth > 12:
            return Opaque()
        saved_dir = self.cur_dir
        self.cur_dir = d
        self.subdir_depth += 1
        try:
            self.run_file(f)
        finally:
            self.subdir_depth -= 1
            self.cur_dir = saved_dir
        return Opaque()

    def f_subdir_done(self, line, args, kw):
        raise SubdirDone()

    def f_subproject(self, line, args, kw):
        if args and isinstance(args[0], str):
            self.s.note(f"{self.where(line)}: uses subproject '{args[0]}' - bundled/fetched at configure time "
                        f"(see subprojects/), not a Debian package")
        return Opaque()

    def f_error(self, line, args, kw):
        msg = " ".join(str(a) for a in args if isinstance(a, str))[:100]
        if not self.frames:
            self.s.note(f"{self.where(line)}: error('{msg}') is reached unconditionally - the analysis stops here")
            self.aborted = True
            raise FatalError()
        # a guarded error() is a constraint on the configuration, not a dependency
        return Opaque()

    def f_message(self, line, args, kw):
        return Opaque()

    f_warning = f_message
    f_summary = f_message

    def f_assert(self, line, args, kw):
        return Opaque()

    def f_import(self, line, args, kw):
        name = args[0] if args and isinstance(args[0], str) else ""
        if name == "python" or name == "python3":
            return ModuleObj("python")
        return ModuleObj(name)

    def f_configuration_data(self, line, args, kw):
        return CfgData()

    def f_declare_dependency(self, line, args, kw):
        return DepObj(names=("<declared>",), required=True, found_known=True)

    def f_find_program(self, line, args, kw):
        names = self.flat_strs(args)
        req, ropts = self.req_state(kw.get("required"))
        names = [n for n in names if n]
        cand = []
        for n in names:
            if "/" in n or os.path.isfile(os.path.join(self.cur_dir, n)) or os.path.isfile(os.path.join(self.root_dir, n)):
                continue                      # a script that ships in the source tree
            cand.append(n)
        if cand:
            ver = kw.get("version")
            vr = _first_version_req(ver, "find_program")
            self.record("program", " or ".join(cand), line, extra_opts=ropts, candidates=tuple(cand),
                        version_req=vr, extra={"optional": req is not True})
        return ProgObj(names[0] if names else "", req)

    def f_dependency(self, line, args, kw):
        names = [n for n in self.flat_strs(args) if n]
        req, ropts = self.req_state(kw.get("required"))
        if not names:
            return DepObj((), req)
        vr = _first_version_req(kw.get("version"), "dependency")
        mods = self.flat_strs(kw.get("modules"))
        method = kw.get("method") if isinstance(kw.get("method"), str) else "auto"
        fb = kw.get("fallback")
        if fb or "default_options" in kw and isinstance(kw.get("allow_fallback"), bool) is False and False:
            pass
        if fb:
            self.s.note(f"{self.where(line)}: dependency('{names[0]}') has a bundled subproject fallback "
                        f"(used only when the system package is missing)")
        self.record("dependency", names[0], line, extra_opts=ropts, candidates=tuple(names), version_req=vr,
                    extra={"optional": req is not True, "modules": mods, "method": method,
                           "static": kw.get("static") is True})
        return DepObj(tuple(names), req)

    def f_run_command(self, line, args, kw):
        return Opaque()

    def f_get_variable(self, line, args, kw):
        return Unk()

    def f_is_variable(self, line, args, kw):
        return Unk()

    def f_set_variable(self, line, args, kw):
        if len(args) >= 2 and isinstance(args[0], str):
            self.env[args[0]] = args[1]
        return Opaque()

    def f_import_dummy(self):
        return None

    # ---- methods ------------------------------------------------------------------------------
    def call_method(self, e):
        _, line, oexpr, name, eargs, ekw = e
        obj = self.eval(oexpr)
        args, kw = self.args(eargs, ekw)
        return self.dispatch_method(line, obj, name, args, kw)

    def _headers_in(self, line: int, text, extra_opts=()) -> None:
        if isinstance(text, str):
            for h in INCLUDE_RE.findall(text):
                self.record("header", h, line, extra_opts=extra_opts)

    def dispatch_method(self, line, obj, name, args, kw):
        if isinstance(obj, Unk):
            return unk(obj, args)
        if isinstance(obj, FeatureVal):
            v = obj.value
            if name in ("enabled", "disabled", "auto", "allowed"):
                if v is None:
                    return Unk({obj.name})
                return {"enabled": v == "enabled", "disabled": v == "disabled", "auto": v == "auto",
                        "allowed": v != "disabled"}[name]
            if name == "require":
                return obj
            if name == "disable_auto_if":
                return obj
            return Unk({obj.name})
        if isinstance(obj, CompilerObj):
            return self.compiler_method(line, obj, name, args, kw)
        if isinstance(obj, MachineObj):
            return {"system": "linux", "cpu_family": "x86_64", "cpu": "x86_64", "endian": "little",
                    "kernel": "linux", "subsystem": "linux"}.get(name, Unk())
        if isinstance(obj, MesonObj):
            return self.meson_method(line, name, args, kw)
        if isinstance(obj, DepObj):
            if name == "found":
                return True if obj.found_known or obj.required is True else Unk()
            if name in ("partial_dependency", "as_system", "as_link_whole"):
                return obj
            return Unk()
        if isinstance(obj, ProgObj):
            if name == "found":
                return True if obj.required is True else Unk()
            return Unk()
        if isinstance(obj, CfgData):
            if name in ("set", "set10", "set_quoted"):
                if args and isinstance(args[0], str) and len(args) > 1:
                    obj.d[args[0]] = args[1]
                return Opaque()
            if name in ("get", "has", "get_unquoted", "keys", "merge_from"):
                return Unk()
            return Opaque()
        if isinstance(obj, ModuleObj):
            return self.module_method(line, obj, name, args, kw)
        if isinstance(obj, str):
            return self.str_method(obj, name, args)
        if isinstance(obj, list):
            return self.list_method(obj, name, args)
        if isinstance(obj, dict):
            return self.dict_method(obj, name, args)
        if isinstance(obj, bool):
            if name == "to_string":
                return "true" if obj else "false"
            if name == "to_int":
                return int(obj)
        if isinstance(obj, int):
            if name == "to_string":
                return str(obj)
            if name in ("is_even", "is_odd"):
                return (obj % 2 == 0) == (name == "is_even")
        return Unk()

    def str_method(self, s: str, name: str, args):
        try:
            if name == "format":
                out = s
                for i, a in enumerate(args):
                    out = out.replace(f"@{i}@", self._fmt(a))
                return out
            if name == "split":
                return s.split(args[0]) if args and isinstance(args[0], str) else s.split()
            if name == "strip":
                return s.strip(args[0]) if args else s.strip()
            if name == "to_upper":
                return s.upper()
            if name == "to_lower":
                return s.lower()
            if name == "to_int":
                return int(s, 0)
            if name == "startswith":
                return isinstance(args[0], str) and s.startswith(args[0])
            if name == "endswith":
                return isinstance(args[0], str) and s.endswith(args[0])
            if name == "contains":
                return isinstance(args[0], str) and args[0] in s
            if name == "replace":
                return s.replace(args[0], args[1])
            if name == "join":
                items = args[0] if args and isinstance(args[0], list) else list(args)
                if all(isinstance(x, str) for x in items):
                    return s.join(items)
                return unk(items)
            if name == "substring":
                lo = args[0] if args else 0
                hi = args[1] if len(args) > 1 else len(s)
                return s[lo:hi]
            if name == "underscorify":
                return re.sub(r"[^A-Za-z0-9]", "_", s)
            if name == "version_compare":
                if args and isinstance(args[0], str):
                    r = _cmp_versions(s, args[0])
                    return Unk() if r is None else r
            if name == "to_string":
                return s
        except (IndexError, ValueError, TypeError):
            pass
        return Unk()

    def list_method(self, l: list, name: str, args):
        try:
            if name == "contains":
                if any(isinstance(x, Unk) for x in l) or isinstance(args[0], Unk):
                    return unk(l, args)
                return args[0] in l
            if name == "get":
                i = args[0]
                if isinstance(i, int) and -len(l) <= i < len(l):
                    return l[i]
                return args[1] if len(args) > 1 else Unk()
            if name == "length":
                return len(l)
        except (IndexError, TypeError):
            pass
        return Unk()

    def dict_method(self, d: dict, name: str, args):
        try:
            if name == "has_key":
                return args[0] in d
            if name == "get":
                return d.get(args[0], args[1] if len(args) > 1 else Unk())
            if name == "keys":
                return list(d.keys())
            if name == "values":
                return list(d.values())
        except TypeError:
            pass
        return Unk()

    def meson_method(self, line, name, args, kw):
        if name == "project_version":
            return self.project_version or Unk()
        if name == "project_name":
            return self.project_name or Unk()
        if name == "version":
            return VersionStr(self.s.meson_version)
        if name in ("current_source_dir", "project_source_root", "source_root"):
            return self.cur_dir if name == "current_source_dir" else self.root_dir
        if name in ("current_build_dir", "project_build_root", "build_root"):
            return "<build>"
        if name == "is_cross_build":
            return False
        if name == "is_subproject":
            return False
        if name == "can_run_host_binaries":
            return True
        if name == "get_compiler":
            lang = args[0] if args and isinstance(args[0], str) else "c"
            return self.compilers.setdefault(lang, CompilerObj(lang))
        if name == "get_external_property":
            return args[1] if len(args) > 1 else Unk()
        return Unk()

    def module_method(self, line, mod: ModuleObj, name: str, args, kw):
        if mod.name == "python" and name == "find_installation":
            names = self.flat_strs(args[:1]) or ["python3"]
            req, ropts = self.req_state(kw.get("required"))
            self.record("program", names[0], line, extra_opts=ropts, candidates=(names[0],),
                        extra={"optional": req is not True})
            for m in self.flat_strs(kw.get("modules")):
                self.s.note(f"{self.where(line)}: needs the Python module '{m}' (python3-{m.lower()} on Debian)")
            return ProgObj(names[0], req)
        if mod.name == "i18n":
            self.s.note(f"{self.where(line)}: i18n module - needs gettext's msgfmt/xgettext/msgmerge (package: gettext)")
        if mod.name in ("gnome", "qt5", "qt6", "wayland", "rust", "cmake", "hotdoc", "sourceset", "windows"):
            if mod.name not in ("windows",):
                self.s.note(f"{self.where(line)}: uses the '{mod.name}' module - it runs helper tools "
                            f"(glib-compile-resources, moc, ... ) that are not tracked here")
        return Opaque()

    def compiler_method(self, line, comp: CompilerObj, name: str, args, kw):
        req, ropts = self.req_state(kw.get("required"))
        if name in ("has_header", "check_header", "has_header_symbol", "check_header_symbol"):
            if args and isinstance(args[0], str):
                self.record("header", args[0], line, extra_opts=ropts, extra={"optional": req is not True})
            self._headers_in(line, kw.get("prefix"))
            return Unk()
        if name == "find_library":
            lib = args[0] if args and isinstance(args[0], str) else ""
            if lib:
                self.record("library", lib, line, extra_opts=ropts, extra={"optional": req is not True})
            for h in self.flat_strs(kw.get("has_headers")):
                self.record("header", h, line, extra_opts=ropts, extra={"optional": req is not True})
            return DepObj((lib,), req)
        if name in ("has_function", "has_member", "has_members", "has_type", "sizeof", "alignment", "get_define",
                    "check_header_symbol", "compute_int", "has_function_attribute", "has_multi_arguments"):
            self._headers_in(line, kw.get("prefix"))
            return Unk()
        if name in ("compiles", "links", "run", "preprocess"):
            self._headers_in(line, args[0] if args else None)
            return Opaque() if name == "run" else Unk()
        if name == "get_id":
            return "gcc"
        if name == "get_linker_id":
            return "ld.bfd"
        if name == "get_argument_syntax":
            return "gcc"
        if name == "version":
            return Unk()
        if name == "get_supported_arguments" or name == "get_supported_link_arguments":
            return Unk()
        if name == "has_argument" or name == "has_link_argument":
            return Unk()
        if name == "cmd_array":
            return ["cc"]
        return Unk()


_MISSING = object()

OPAQUE_FUNCS = {"executable", "library", "static_library", "shared_library", "both_libraries", "shared_module",
                "custom_target", "run_target", "alias_target", "install_headers", "install_data", "install_subdir",
                "install_man", "install_symlink", "install_emptydir", "test", "benchmark", "generator", "vcs_tag",
                "include_directories", "configure_file", "add_project_arguments", "add_global_arguments",
                "add_project_link_arguments", "add_global_link_arguments", "add_test_setup", "environment",
                "files", "jar", "build_target", "gettext", "structured_sources", "range"}


def _first_version_req(v, source: str) -> "VersionReq | None":
    items = [v] if isinstance(v, str) else [x for x in v if isinstance(x, str)] if isinstance(v, list) else []
    for s in items:
        m = re.match(r"^\s*(>=|<=|==|!=|>|<|=)?\s*(\S+)\s*$", s)
        if m:
            return VersionReq(op=m.group(1) or "==", version=m.group(2), source=source)
    return None


def _make_root_env(an: Analyzer) -> None:
    an.env["host_machine"] = MachineObj("host")
    an.env["build_machine"] = MachineObj("build")
    an.env["target_machine"] = MachineObj("target")
    an.env["meson"] = MesonObj()


# ===========================================================================
# Stage 5: findings -> configcheck-style Options and Checks
# ===========================================================================
def guard_for(an: Analyzer, frames: tuple) -> str:
    for fr in reversed(frames):
        for o in fr.opts:
            if o in an.options:
                return o
    return "UNCONDITIONAL"


def dep_tags(f: Found) -> "list[str]":
    names = list(f.candidates or (f.name,))
    for n in names:
        if n in DEP_OVERRIDES:
            tags = list(DEP_OVERRIDES[n])
            if n == "boost":
                tags += [f"lib:boost_{m}" for m in f.extra.get("modules", [])]
            return tags
    tags: "list[str]" = []
    for n in names:
        tags.append(f"pc:{n}")
    for n in names:
        tags.append(f"prog:{n}-config")
    return tags


def build_model(an: Analyzer) -> "tuple[list[Option], list[Check]]":
    by_guard: "dict[str, list[Check]]" = {}
    mandatory: "list[Check]" = []
    seen: "set[tuple]" = set()
    EXTRA.clear()

    def add(guard: str, ck: Check, extra: dict) -> None:
        key = (guard, ck.type, ck.name)
        lst = mandatory if guard == "UNCONDITIONAL" else by_guard.setdefault(guard, [])
        if key in seen:
            return
        seen.add(key)
        lst.append(ck)
        EXTRA[(guard, ck.type, ck.name)] = extra

    add("UNCONDITIONAL", Check(name="meson", type="generic", guard="UNCONDITIONAL", candidates=("meson",)), {})
    add("UNCONDITIONAL", Check(name="ninja", type="generic", guard="UNCONDITIONAL", candidates=("ninja", "samu")),
        {"why": "meson's default backend"})
    for f in an.found:
        guard = guard_for(an, f.frames)
        extra = dict(f.extra)
        if any(not fr.opts and fr.text == "if" for fr in f.frames):
            extra["probe"] = True
        if f.kind == "header" and an.is_project_header(f.name):
            continue
        extra["where"] = f"{os.path.relpath(f.file, an.root_dir)}:{f.line}"
        if f.kind == "header":
            ck = Check(name=f.name, type="header", guard=guard)
        elif f.kind == "library":
            ck = Check(name=f.name, type="library", guard=guard)
        elif f.kind in ("program", "compiler"):
            ck = Check(name=f.name, type="generic", guard=guard, candidates=f.candidates, version_req=f.version_req)
        elif f.kind == "meson":
            ck = Check(name="meson", type="generic", guard=guard, candidates=("meson",), version_req=f.version_req)
            key = ("UNCONDITIONAL", "generic", "meson")
            if key in seen:
                idx = next(i for i, c in enumerate(mandatory) if c.name == "meson")
                mandatory[idx] = dataclasses.replace(mandatory[idx], version_req=f.version_req)
                continue
        elif f.kind == "dependency":
            ck = Check(name=f.name, type="mesondep", guard=guard, candidates=tuple(dep_tags(f)),
                       version_req=f.version_req)
        else:
            continue
        add(guard, ck, extra)

    options: "list[Option]" = []
    listed: "set[str]" = set()
    for name in an.option_order:
        info = an.options[name]
        options.append(Option(flag=f"-D{name}", var=name, default=_default_text(info), kind="feature",
                              desc=info.desc, checks=by_guard.get(name, [])))
        listed.add(name)
    return options, mandatory


EXTRA: "dict[tuple, dict]" = {}


def _default_text(info: OptionInfo) -> str:
    v = info.user if info.user is not None else info.value
    if info.otype == "boolean":
        if isinstance(v, bool):
            return "enabled" if v else "disabled"
        return "enabled" if str(v).lower() == "true" else "disabled" if v is not None else "enabled"
    if info.otype == "feature":
        return str(v) if v is not None else "auto"
    if info.otype == "array":
        return "-"
    return str(v) if v is not None else "-"


# ===========================================================================
# Stage 6: resolving. Everything configcheck understands is handed to
# configcheck.resolve_all unchanged; dependency() ("mesondep") has its own
# logic, since one dependency() can be satisfied several ways.
# ===========================================================================
def _contents_terms(checks: "list[Check]") -> "list[str]":
    terms = cc.contents_terms_for([c for c in checks if c.type != "mesondep"])
    for c in checks:
        if c.type != "mesondep":
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
                if not shutil.which(val):
                    terms.append(f"bin/{val}")
    return terms


def resolve_mesondep(c: Check) -> dict:
    tried, pkg, installed, note, via = [], "?", "unknown", "", ""
    extra = EXTRA.get((c.guard, c.type, c.name), {})
    for tag in c.candidates or ():
        kind, _, val = tag.partition(":")
        if kind == "none":
            pkg, installed, note, via = "-", "yes", val, tag
            break
        if kind == "pc":
            r = cc.resolve_package(f"{val}.pc")
        elif kind == "h":
            r = cc.resolve_package(val)
        elif kind == "lib":
            r = cc.resolve_library(val)
        elif kind == "prog":
            r = cc.resolve_program(val)
        else:
            continue
        tried.append(tag)
        if r[0] not in ("?", "-"):
            pkg, installed, note, via = r[0], r[1], r[2], tag
            break
    if via.startswith("none:"):
        pass
    elif via:
        note = f"via {via}" + (f"; {note}" if note else "")
    else:
        note = ("no match for " + ", ".join(tried)) if tried else "no way found to look this dependency up"
    if extra.get("probe"):
        note += "; depends on a compile/link probe"
    if extra.get("optional"):
        note += "; optional (required: false)" if note else "optional (required: false)"
    if extra.get("modules"):
        note += f"; modules: {' '.join(extra['modules'])}"
    if extra.get("method") not in (None, "auto", "pkg-config", "system"):
        note += f"; method: {extra['method']}"
    kind = via.split(":", 1)[0]
    if kind == "pc":
        chk = Check(name=via.split(":", 1)[1], type="pkgconfig", guard=c.guard, version_req=c.version_req)
    else:
        chk = Check(name=c.name, type="generic", guard=c.guard)
    have = cc.have_cell(chk, pkg) if pkg not in ("?", "-") else "-"
    req = f"{c.version_req.op}{c.version_req.version}" if c.version_req else "-"
    return {"name": c.name, "type": "dependency", "package": pkg, "installed": installed, "note": note.lstrip("; "),
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


def system_include_paths(h: str) -> "list[str]":
    """Where `#include <h>` is satisfied from the compiler's DEFAULT search path (Contents-style, no leading /).
    A plain 'xxhash.h' also matches e.g. LLVM's private copy under /usr/lib/llvm-19/..., which is not on that path."""
    return [f"usr/include/{h}", f"usr/include/{_multiarch()}/{h}"]


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



def _narrow_header(c: Check, r: dict) -> dict:
    """'xxhash.h' is shipped by 15 packages; exactly one of them is called *xxhash*-dev. When the header's
    stem appears in precisely one candidate's name, that is almost certainly the intended one."""
    stem = re.sub(r"[^a-z0-9]", "", os.path.splitext(os.path.basename(c.name))[0].lower())
    if len(stem) < 3:
        return r
    try:
        pkgs = list(cc.search_contents_files(c.name, False)) or list(cc._local_package_lookup(c.name, False))
    except Exception:
        return r
    hits = [p for p in pkgs if stem in re.sub(r"[^a-z0-9]", "", p.lower()) and p.endswith("-dev")]
    if len(hits) == 1:
        r = dict(r)
        r["package"] = hits[0]
        r["installed"] = r["installed"]
        r["note"] = f"{len(pkgs)} packages ship a file named this; picked the one named after the header"
    return r


def resolve_checks(checks: "list[Check]") -> "list[dict]":
    rows = []
    for c in checks:
        r = resolve_mesondep(c) if c.type == "mesondep" else cc.resolve_all([c])[0]
        extra = EXTRA.get((c.guard, c.type, c.name), {})
        if c.type == "library" and c.name in ("dl", "rt", "pthread", "util", "m", "resolv") \
                and (str(r.get("package", "")).startswith("libc6") or "no unversioned dev symlink" in str(r.get("note", ""))):
            r = dict(r, package="libc6-dev", installed=r.get("installed", "yes"),
                     note=f"lib{c.name} is part of glibc (libc6-dev)")
        if c.type == "header":
            own = system_include_owner(c.name)
            if own:
                r = dict(r, package=own[0], installed=own[1], note=own[2])
                if own[1] == "yes":
                    r["have"] = cc.have_cell(Check(name=c.name, type="generic", guard=c.guard), own[0].split(" ")[0])
            elif str(r.get("note", "")).startswith("ambiguous"):
                r = _narrow_header(c, r)
        if c.type != "mesondep":
            bits = []
            if extra.get("optional"):
                bits.append("optional (required: false)")
            if extra.get("why"):
                bits.append(extra["why"])
            if extra.get("probe"):
                bits.append("only if an earlier compile/link probe fails or passes (not decidable statically)")
            if bits:
                r["note"] = ((r.get("note") or "") + ("; " if r.get("note") else "") + "; ".join(bits))
        rows.append(r)
    return rows


# ===========================================================================
# Stage 7: report
# ===========================================================================
def print_report(an: Analyzer, options: "list[Option]", mandatory: "list[Check]", path: str,
                 width: "int | None" = None) -> None:
    tw = width if width is not None else get_term_width()
    bar = "=" * min(78, tw)
    ver = an.s.meson_version + ("" if an.s.meson_version_detected else " (assumed - meson not installed)")
    print(f"Parsed: {path}")
    print(f"Assuming: Linux, gcc, Meson {ver}; option defaults as declared (override with -DNAME=VALUE)")
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
    print(render_table(mand_cols, resolve_checks(mandatory), width=tw))
    if not mandatory:
        print("  (none detected)")

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
        for i, row in enumerate(resolve_checks(opt.checks)):
            rows.append({"flag": opt.flag if i == 0 else "", "default": opt.default if i == 0 else "",
                         "name": row["name"], "package": row["package"], "installed": row["installed"],
                         "want": row["want"], "have": row["have"], "note": row.get("note", "")})
    opt_cols = [
        Column("FLAG", "flag", min_width=8, flexible=True, shrink_priority=2),
        Column("DEFAULT", "default", min_width=7),
        Column("CHECKS FOR", "name", min_width=12, flexible=True, shrink_priority=0),
        Column("PACKAGE", "package", min_width=14, flexible=True, shrink_priority=1),
        Column("INST?", "installed", min_width=5),
        Column("WANT", "want", min_width=6, flexible=True, shrink_priority=4),
        Column("HAVE", "have", min_width=10, flexible=True, shrink_priority=3),
        Column("NOTE", "note", min_width=0, flexible=True, shrink_priority=5),
    ]
    print(render_table(opt_cols, rows, width=tw))
    if an.s.notes:
        print()
        print(bar)
        print(" Notes")
        print(bar)
        for n in an.s.notes[:30]:
            print(f"  - {n}")
        if len(an.s.notes) > 30:
            print(f"  ... and {len(an.s.notes) - 30} more")
    print()
    print("Note: to enable or disable a feature, pass its option to meson, e.g.")
    print("        meson setup builddir -Dname=true       (boolean:  true / false)")
    print("        meson setup builddir -Dname=enabled    (feature:  enabled / disabled / auto)")
    print("      On an existing build directory:  meson configure builddir -Dname=value")


def to_json(an: Analyzer, options: "list[Option]", mandatory: "list[Check]") -> str:
    return json.dumps({
        "mandatory": resolve_checks(mandatory),
        "optional": [{"flag": o.flag, "default": o.default, "kind": o.kind, "description": o.desc,
                      "checks": resolve_checks(o.checks)} for o in options],
        "notes": an.s.notes,
    }, indent=2)


def main() -> int:
    ap = argparse.ArgumentParser(description="Parse a Meson project's meson.build files and map what they check "
                                             "for to Debian packages.")
    ap.add_argument("path", help="path to a meson.build, or the project directory containing one")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of the text report")
    ap.add_argument("-o", "--output", metavar="FILE",
                    help="write the report to FILE instead of the terminal (columns are never truncated)")
    ap.add_argument("-D", dest="defs", action="append", default=[], metavar="NAME=VALUE",
                    help="set an option as `meson setup -DNAME=VALUE` would; repeatable")
    args = ap.parse_args()

    path = args.path
    if os.path.isdir(path):
        path = os.path.join(path, "meson.build")
    if not os.path.isfile(path):
        print(f"error: no such file: {path}", file=sys.stderr)
        return 1
    if os.path.basename(path) != "meson.build":
        print(f"warning: '{os.path.basename(path)}' isn't named meson.build", file=sys.stderr)
        print("configcheck-meson.py parses Meson syntax; use configcheck.py for configure scripts, "
              "configcheck-cmake.py for CMakeLists.txt.", file=sys.stderr)
        if sys.stdin.isatty() and input("Continue anyway? [y/N] ").strip().lower() not in ("y", "yes"):
            print("aborted.", file=sys.stderr)
            return 1

    defs = {}
    for d in args.defs:
        k, sep, v = d.partition("=")
        defs[k] = v if sep else "true"

    print(cc.describe_contents_files(), file=sys.stderr)
    root = os.path.dirname(os.path.abspath(path))
    an = Analyzer(Settings(defs), root)
    _make_root_env(an)
    for opt_file in ("meson.options", "meson_options.txt"):
        p = os.path.join(root, opt_file)
        if os.path.isfile(p):
            an.cur_dir = root
            an.run_file(p)
            break
    try:
        an.run_file(os.path.abspath(path))
    except FatalError:
        pass
    options, mandatory = build_model(an)
    all_checks = mandatory + [c for o in options for c in o.checks]
    cc.prefetch_contents(_contents_terms(all_checks))
    cc.prefetch_contents([p for c in all_checks if c.type == "header" for p in system_include_paths(c.name)],
                         exact=True)

    if args.output:
        with open(args.output, "w") as f, contextlib.redirect_stdout(f):
            print(to_json(an, options, mandatory) if args.json else "", end="")
            if not args.json:
                print_report(an, options, mandatory, path, width=1000)
        print(f"Report written to {args.output}")
    elif args.json:
        print(to_json(an, options, mandatory))
    else:
        print_report(an, options, mandatory, path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
