#!/usr/bin/env python3
"""
configcheck.py - Parse an autoconf 'configure' script (statically, without
running it) to list mandatory and optional build checks, what each checks
for, which Debian package provides it, and whether that package is
installed.

Part of deb-buildcheck-suite (see also makecheck.py).

Usage: ./configcheck.py /path/to/configure [--json]
"""

import argparse
import bisect
import contextlib
import ctypes
import gzip
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import zlib
from dataclasses import dataclass, field
from functools import lru_cache


# ---------------------------------------------------------------------------
# Opt-in diagnostics: DEB_BUILDCHECK_TIMING=1 prints, on exit, how many times
# each external command ran and how long it took in total - the quick way to
# see where a slow run is actually spending its time (a configure script can
# trigger hundreds of dpkg/apt-cache/--version calls). Off by default and has
# no effect on results.
# ---------------------------------------------------------------------------
if os.environ.get("DEB_BUILDCHECK_TIMING"):
    import atexit
    import time as _time

    _CMD_TIMES: "dict[str, list]" = {}
    _real_subprocess_run = subprocess.run

    def _timed_run(cmd, *args, **kwargs):
        start = _time.monotonic()
        try:
            return _real_subprocess_run(cmd, *args, **kwargs)
        finally:
            elapsed = _time.monotonic() - start
            argv = list(cmd) if isinstance(cmd, (list, tuple)) else [str(cmd)]
            key = os.path.basename(argv[0])
            if key == "dpkg" and len(argv) > 2 and argv[1] == "-S":
                key += " -S (glob)" if "*" in argv[2] else " -S (exact path)"
            elif len(argv) > 1 and argv[1].startswith("-"):
                key += " " + argv[1]
            elif len(argv) > 1 and key in ("apt-cache", "apt-file"):
                key += " " + argv[1]
            entry = _CMD_TIMES.setdefault(key, [0, 0.0])
            entry[0] += 1
            entry[1] += elapsed

    subprocess.run = _timed_run

    @atexit.register
    def _print_command_timing() -> None:
        total = sum(t for _, t in _CMD_TIMES.values())
        print(f"\nexternal command time: {total:.1f}s total", file=sys.stderr)
        for key, (n, t) in sorted(_CMD_TIMES.items(), key=lambda kv: -kv[1][1]):
            print(f"  {key:34s} {n:5d} calls  {t:7.1f}s  ({t / n * 1000:.0f} ms each)", file=sys.stderr)


@dataclass
class Option:
    flag: str
    var: str
    default: str          # "enabled" | "disabled"
    kind: str              # "feature" | "package"
    desc: str
    checks: list = field(default_factory=list)


@dataclass(frozen=True)
class VersionReq:
    op: str                # one of < <= = == != >= >
    version: str            # the required version string, e.g. "1.2.3"
    source: str             # where this requirement was recovered from -
                             # shown to the user so a heuristic-sourced
                             # requirement can be told apart from a
                             # pkg-config-sourced one


@dataclass(frozen=True)
class Check:
    name: str
    type: str              # "header" | "library" | "generic" | "pkgconfig"
    guard: str              # var name, or "UNCONDITIONAL"
    candidates: tuple[str, ...] | None = None  # for "generic" checks that came
                                                # from a `for ac_prog in A B C`
                                                # loop: try each in order via
                                                # `which`, same as autoconf itself
    version_req: "VersionReq | None" = None    # a <,>,<=,>=,=,!= requirement
                                                # this check places on the
                                                # thing it's looking for, if
                                                # one could be recovered


# ---------------------------------------------------------------------------
# Stage 1: pull the --help text block(s) out of the configure script.
# Autoconf embeds this as literal text under headers like
# "Optional Features:" and "Optional Packages:".
# ---------------------------------------------------------------------------
SECTION_HEADER_RE = re.compile(r'^[A-Za-z].*:$')


def extract_help_block(text: str, header: str) -> list[str]:
    header_re = re.compile(header)
    out, in_block = [], False
    for line in text.splitlines():
        if header_re.match(line):
            in_block = True
            continue
        if in_block and SECTION_HEADER_RE.match(line):
            in_block = False
        if in_block:
            out.append(line)
    return out


# ---------------------------------------------------------------------------
# Stage 2: parse each option line into flag / varname / default state.
#   --enable-foo[=X]  desc [default=yes]  -> enable_foo, per stated default
#   --disable-foo     desc                -> enable_foo, enabled (disable-* = on by default)
#   --with-foo        desc [default=yes]  -> with_foo, per stated default
#   --without-foo     desc                -> with_foo, enabled (without-* = on by default)
# ---------------------------------------------------------------------------
OPTION_LINE_RE = re.compile(r'^\s*(--[a-zA-Z0-9][a-zA-Z0-9_-]*)(\[=[^\]]*\])?\s*(.*)$')

PREFIX_RULES = [
    ("--enable-",  "enable_", "disabled"),
    ("--disable-", "enable_", "enabled"),
    ("--with-",    "with_",   "disabled"),
    ("--without-", "with_",   "enabled"),
]


def parse_options(lines: list[str], kind: str) -> list[Option]:
    options = []
    for line in lines:
        m = OPTION_LINE_RE.match(line)
        if not m:
            continue
        flag, _bracket, desc = m.groups()
        desc = (desc or "").strip()

        for prefix, varprefix, default in PREFIX_RULES:
            if flag.startswith(prefix):
                var = (varprefix + flag[len(prefix):]).replace("-", "_")
                break
        else:
            continue

        if "[default=yes]" in desc:
            default = "enabled"
        elif "[default=no]" in desc:
            default = "disabled"

        options.append(Option(flag=flag, var=var, default=default, kind=kind, desc=desc))
    return options


# ---------------------------------------------------------------------------
# Stage 3: walk the whole script tracking which --enable/--with variable(s)
# guard each "checking for X" line, via an if/fi depth stack. A line with
# several guard vars (e.g. `if test "x$enable_a" = xyes && test "x$with_b"
# = xyes`) keeps all of them, since a check nested there depends on ALL of
# them being set - real ambiguity if they conflict, but we surface every
# guard rather than silently picking one.
# ---------------------------------------------------------------------------
GUARD_VAR_RE = re.compile(r'\$\{?([A-Za-z_][A-Za-z0-9_]*)')
IF_RE = re.compile(r'^\s*if\b')
FI_RE = re.compile(r'^\s*fi\b')
CHECKING_RE = re.compile(r'checking for (.+)')
TRAILING_RE = re.compile(r'(\.\.\.|["\']).*$')
# A "checking for $2"-style line inside a reusable ac_fn_c_check_* function
# DEFINITION (autoconf >=2.71 refactored these into shared shell functions).
# $2 is only substituted at each call site, so the literal text left in the
# function body is template noise, not a real check - discard it entirely
# rather than reporting a check named "$2".
BARE_PLACEHOLDER_RE = re.compile(r'^\$\{?[A-Za-z_0-9][A-Za-z0-9_]*\}?$')
# The real check subjects for those refactored functions live in the call
# arguments instead, e.g.:
#   ac_fn_c_check_header_compile "$LINENO" "dlfcn.h" "ac_cv_header_dlfcn_h" ...
#   ac_fn_c_check_func "$LINENO" "dlopen" "ac_cv_func_dlopen"
# (only literal-argument calls are matched; loop calls that pass a $variable
# are handled via the ac_header_c_list accumulator below instead).
CALL_HEADER_RE = re.compile(r'ac_fn_c_check_header_(?:compile|mongrel)\s+"\$LINENO"\s+"([^"]+)"')
CALL_FUNC_RE = re.compile(r'ac_fn_c_check_func\s+"\$LINENO"\s+"([^"]+)"')
# AC_CHECK_HEADERS_ONCE accumulates its whole header list into one variable
# via repeated as_fn_append calls, then checks them all in a single generic
# loop - so the real header names never appear next to "checking for" text
# at all and have to be read from the accumulator lines themselves.
ACCUM_HEADER_RE = re.compile(r'as_fn_append\s+ac_header_c_list\s+"\s*([^"\s]+)')

# A handful of libtool checks announce themselves with fixed, near-universal
# message text but determine their real candidate via multi-branch shell
# logic (case statements reassigning a scratch variable across several
# conditional paths) that isn't safe to trace generically - doing so risks
# picking up a value that's just a fallback for one specific branch, not
# the actual check subject. But the message text itself is exactly as
# invariant as ac_header_c_list, generated by the same shared libtool.m4
# macros, so recognizing the message directly is safe where tracing the
# logic behind it would not be.
KNOWN_LIBTOOL_PROGRAMS: dict[str, tuple[str, ...]] = {
    "GNU ld": ("ld",),
    "non-GNU ld": ("ld",),
    "BSD- or MS-compatible name lister (nm)": ("nm",),
}
# AC_CHECK_PROGS/AC_PATH_PROGS generate `for ac_prog in NAME1 NAME2 ...` right
# before the same "$ac_word" placeholder pattern - a real, well-defined list
# of program-name candidates that autoconf itself tries in order via a PATH
# search, so we can resolve it exactly the same way (`which`, trying each in
# turn) instead of discarding it as placeholder noise.
FOR_PROG_RE = re.compile(r'^\s*for ac_prog in\s+(.+?)\s*(?:;\s*do\s*)?$')
# AC_PROG_CC/CXX and friends probe one candidate at a time (gcc, then cc,
# then cl.exe, ...) rather than looping a list - each attempt is its own
# "set dummy CANDIDATE; ac_word=$2" right before the same $ac_word
# placeholder pattern. ${ac_tool_prefix} is the cross-compiler prefix,
# empty on any native build (the overwhelming common case), so stripping
# it recovers the real candidate name directly.
SET_DUMMY_RE = re.compile(r'^set dummy\s+(.+?);\s*ac_word=\$2\s*$')


def _program_candidates(raw_list: str) -> tuple[str, ...]:
    try:
        tokens = shlex.split(raw_list)
    except ValueError:
        tokens = raw_list.split()
    # drop Windows-only tools (cl.exe, dumpbin, "link -lib") and anything
    # templated on an unresolved shell variable (${CC}_r) - neither is a
    # concrete Debian-packaged program name
    return tuple(t for t in tokens if " " not in t and "$" not in t and not t.endswith(".exe"))

# Very common autoconf idiom: --enable-X (or --with-X) sets $enableval /
# $withval, which then gets copied into an arbitrary custom variable name
# (often "want_X") that's what actually guards the real check later on:
#     enableval=$enable_ogg;  want_ogg=$enableval
# Without tracing this, a check guarded by $want_ogg silently detaches
# from the --enable-ogg option that actually controls it.
ALIAS_ASSIGN_RE = re.compile(
    r'(?:enableval|withval)=\$((?:enable|with)_[a-zA-Z0-9_]+);\s*([A-Za-z_][A-Za-z0-9_]*)=\$(?:enableval|withval)\b'
)


def build_guard_aliases(text: str) -> dict[str, str]:
    return {derived: real for real, derived in ALIAS_ASSIGN_RE.findall(text)}


def resolve_guard_var(varname: str, aliases: dict[str, str]) -> str | None:
    if re.match(r'^(enable|with)_', varname):
        return varname
    return aliases.get(varname)

# Some configure scripts (older AM_PATH_FOO-style macros, predating
# pkg-config) don't use AC_CHECK_HEADER/AC_CHECK_LIB at all - they just
# print "checking for X" and then compile a hand-written test program
# inline. When that happens there's no ".h" or "in -lY" text to classify
# from, but the embedded test program itself usually has real #include and
# -lXXX hints we can pull out of the enclosing if-block.
COMMON_HEADERS = {
    "stdio.h", "stdlib.h", "string.h", "unistd.h", "errno.h", "math.h",
    "ctype.h", "time.h", "fcntl.h", "signal.h", "limits.h", "stdint.h",
    "stddef.h", "stdarg.h", "assert.h", "sys/types.h", "sys/stat.h",
    "sys/time.h", "sys/socket.h", "sys/wait.h", "sys/ioctl.h", "sys/param.h",
    "netinet/in.h", "arpa/inet.h", "pthread.h", "dlfcn.h", "dirent.h",
}
INCLUDE_RE = re.compile(r'#\s*include\s*<([^>]+\.h)>')
# require -l to start a token (not glued onto a preceding word char/hyphen,
# as in the substring "-libraries" inside "--with-ogg-libraries")
LIB_FLAG_RE = re.compile(r'(?<![A-Za-z0-9_-])-l([A-Za-z0-9_+]+)')
# a bare regex can't tell "-lfoo" (link against libfoo) apart from an
# ordinary English word that happens to start with "l" right after a
# hyphen in unrelated text - "-linux" and "-link" are the two that have
# actually shown up in the wild scanning real configure output ("...PIC
# in -link", "debug options in -linux"). Not a general solution (there's
# no dictionary here), but covers the concrete false positives seen so
# far - same defensive approach makecheck.py already takes for the
# analogous -links/-lname collision with find(1).
LIB_FLAG_FALSE_POSITIVES = {"inux", "ink"}
# a genuine AC_CHECK_LIB-style message is always exactly "FUNCTION in
# -lLIBRARY" - FUNCTION is a single bare identifier, nothing else on the
# line. A plain `" in -l" in name` substring check also fires on the
# ordinary English words "-link" and "-linux" appearing anywhere in an
# unrelated message (e.g. "...option to produce PIC in -link", "...debug
# options in -linux") - anchoring the whole string closes that off.
LIBCHECK_SHAPE_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]* in -l[A-Za-z0-9_+]+$')


def enrich_generic_check(check: "Check", block_text: str) -> list["Check"]:
    headers = [h for h in INCLUDE_RE.findall(block_text) if h not in COMMON_HEADERS]
    libs = [l for l in LIB_FLAG_RE.findall(block_text) if l not in LIB_FLAG_FALSE_POSITIVES]

    out = []
    if headers:
        out.append(Check(name=headers[-1], type="header", guard=check.guard, version_req=check.version_req))
    if libs:
        out.append(Check(name=f"{check.name} in -l{libs[0]}", type="library", guard=check.guard,
                          version_req=check.version_req))
    return out or [check]


HEREDOC_START_RE = re.compile(r'<<-?\s*(["\']?)(\w+)\1')


def compute_heredoc_mask(lines: list[str]) -> list[bool]:
    """True for a line that's inside a heredoc body (e.g. the embedded C
    test program between `cat confdefs.h - <<_ACEOF` and `_ACEOF`).
    Without this, a C `if (...)` statement inside one of these bodies
    looks exactly like a shell `if` to a naive line-anchored regex and
    pushes an entry onto the if/fi guard stack that no shell `fi` will
    ever pop - silently corrupting every guard resolved for the rest of
    the file, not just checks near that one test program."""
    mask = [False] * len(lines)
    terminator = None
    for i, line in enumerate(lines):
        if terminator is not None:
            mask[i] = True
            # the terminator line can carry a trailing quote when the whole
            # heredoc sits inside a quoted argument, e.g. libtool's
            # eval 'cat <<_LTECHO_EOF ... _LTECHO_EOF' - strip that off
            # before comparing, or the mask never clears and swallows the
            # rest of the file
            if line.strip().rstrip("'\"") == terminator:
                terminator = None
            continue
        m = HEREDOC_START_RE.search(line)
        if m:
            terminator = m.group(2)
    return mask


def extract_checks(text: str) -> list[Check]:
    lines = text.splitlines()
    aliases = build_guard_aliases(text)
    shell_vars = dict(SHELL_VAR_ASSIGN_RE.findall(text))
    heredoc = compute_heredoc_mask(lines)

    # Pass 1: if/fi block boundaries.
    open_stack: list[int] = []
    block_end: dict[int, int] = {}
    for i, line in enumerate(lines):
        if heredoc[i]:
            continue
        if IF_RE.match(line):
            open_stack.append(i)
        elif FI_RE.match(line) and open_stack:
            block_end[open_stack.pop()] = i

    # Pass 2: collect raw checks with their line index, guard, and the
    # immediate enclosing block's start line (None = top level).
    stack: list[list[str]] = []
    block_stack: list[int] = []
    raw: list[tuple[int, str, str, str, "int | None", "tuple[str, ...] | None"]] = []
    pending_candidates: "tuple[int, tuple[str, ...]] | None" = None
    for_loops: list[tuple[int, "tuple[str, ...]", "int | None"]] = []  # (line_idx, candidates, block_start)
    consumed_for_loop_lines: set[int] = set()

    for i, line in enumerate(lines):
        if heredoc[i]:
            continue
        if IF_RE.match(line):
            candidates = GUARD_VAR_RE.findall(line)
            resolved = [v for v in (resolve_guard_var(c, aliases) for c in candidates) if v]
            stack.append(resolved)
            block_stack.append(i)
            continue
        if FI_RE.match(line):
            if stack:
                stack.pop()
            if block_stack:
                block_stack.pop()
            continue

        m = FOR_PROG_RE.match(line)
        if m:
            cand = _program_candidates(m.group(1))
            if cand:
                pending_candidates = (i, cand)
                for_loops.append((i, cand, block_stack[-1] if block_stack else None))
            continue

        m = SET_DUMMY_RE.match(line)
        if m:
            raw_name = m.group(1).replace("${ac_tool_prefix}", "")
            cand = _program_candidates(raw_name)
            if cand:
                pending_candidates = (i, cand)
                for_loops.append((i, cand, block_stack[-1] if block_stack else None))
            continue

        m = CHECKING_RE.search(line)
        if m:
            name = TRAILING_RE.sub("", m.group(1)).strip()
            # a pending candidate list only belongs to a $ac_word check that
            # follows within a few lines - past that it's not this check's
            # placeholder and must not leak forward onto some unrelated
            # later tool (see the backward-correlation pass for those).
            if pending_candidates and i - pending_candidates[0] > 5:
                pending_candidates = None
            if name == "$ac_word" and pending_candidates:
                guards = next((frame for frame in reversed(stack) if frame), None)
                guard = guards[0] if guards else "UNCONDITIONAL"
                loop_line, cand = pending_candidates
                display = " or ".join(cand)
                raw.append((i, guard, display, "generic", block_stack[-1] if block_stack else None, cand))
                consumed_for_loop_lines.add(loop_line)
                pending_candidates = None
            elif name and not BARE_PLACEHOLDER_RE.match(name):
                guards = next((frame for frame in reversed(stack) if frame), None)
                guard = guards[0] if guards else "UNCONDITIONAL"
                if LIBCHECK_SHAPE_RE.match(name):
                    ctype = "library"
                elif name.endswith(".h"):
                    ctype = "header"
                else:
                    ctype = "generic"
                raw.append((i, guard, name, ctype, block_stack[-1] if block_stack else None, None))
            continue

        guards = next((frame for frame in reversed(stack) if frame), None)
        guard = guards[0] if guards else "UNCONDITIONAL"
        bstart = block_stack[-1] if block_stack else None

        m = CALL_HEADER_RE.search(line)
        if m:
            raw.append((i, guard, m.group(1), "header", bstart, None))
            continue

        m = CALL_FUNC_RE.search(line)
        if m:
            # a bare libc/toolchain function check - not tied to any
            # specific Debian package, so surfaced but not resolved
            raw.append((i, guard, m.group(1), "function", bstart, None))
            continue

        m = ACCUM_HEADER_RE.search(line)
        if m:
            raw.append((i, guard, m.group(1), "header", bstart, None))
            continue

    # Pass 3: dedupe raw entries by (guard, name) FIRST - a real check
    # normally has two near-identical source lines (the $as_me log line and
    # the user-facing printf line), and treating the second one as if it
    # were a different *sibling* check would wrongly narrow the enrichment
    # window below and cut off a hint that's actually still part of this
    # same check's block.
    dedup: dict[tuple[str, str], tuple[int, str, str, str, "int | None", "tuple[str, ...] | None"]] = {}
    for entry in raw:
        key = (entry[1], entry[2])
        if key not in dedup:
            dedup[key] = entry
    entries = list(dedup.values())

    # Some AC_PATH_PROGS-style checks announce a fixed, descriptive message
    # first ("checking for a working dd") and only reveal the real
    # candidate name(s) later, in their own internal `for ac_prog in ...`
    # loop - the reverse order from the $ac_word pattern already handled
    # above. Backward-correlate: an unresolvable generic check gets paired
    # with the nearest *following*, not-yet-claimed for-loop in the same
    # enclosing block, within a modest distance (the loop is always part
    # of that same check's own implementation, so it never lives far away).
    claimed_loop_lines: set[int] = set()
    unclaimed_loops = sorted(
        (fl for fl in for_loops if fl[0] not in consumed_for_loop_lines),
        key=lambda fl: fl[0],
    )
    patched: list[tuple[int, str, str, str, "int | None", "tuple[str, ...] | None"]] = []
    for entry in sorted(entries, key=lambda e: e[0]):
        line_idx, guard, name, ctype, bstart, candidates = entry
        if ctype == "generic" and candidates is None and name in KNOWN_LIBTOOL_PROGRAMS:
            entry = (line_idx, guard, name, ctype, bstart, KNOWN_LIBTOOL_PROGRAMS[name])
        elif ctype == "generic" and candidates is None and not PLAIN_TOKEN_RE.match(name):
            best = None
            for loop_line, cand, loop_bstart in unclaimed_loops:
                if loop_line in claimed_loop_lines or loop_line <= line_idx:
                    continue
                if loop_line - line_idx > 150:
                    continue
                if best is None or loop_line < best[0]:
                    best = (loop_line, cand)
            if best is not None:
                claimed_loop_lines.add(best[0])
                entry = (line_idx, guard, name, ctype, bstart, best[1])
        patched.append(entry)
    entries = patched

    # For a check with no enclosing if-block at all (block_start=None), an
    # unbounded trailing window would run to literally the end of the
    # file if it has no later top-level sibling - risking a hint bleeding
    # in from a totally unrelated, deeply-nested block much further down.
    # Cap that specific case at the next "if" anywhere after it. This does
    # NOT apply to checks that already have a real enclosing block (like
    # ogg's, whose own nested "if enable_oggtest" sub-block legitimately
    # holds the compile-test hints it needs) - only to genuinely top-level
    # checks reaching past where any structure starts.
    all_if_starts = sorted(block_end.keys())

    def next_if_after(pos: int) -> int:
        i = bisect.bisect_right(all_if_starts, pos)
        return all_if_starts[i] - 1 if i < len(all_if_starts) else len(lines) - 1

    # Pass 4: enrich generic checks using a window bounded by the nearest
    # neighboring *distinct* check within the same enclosing block - not
    # the whole block, which can be huge (e.g. libtool's boilerplate wraps
    # dozens of unrelated checks in one if) and would otherwise leak a
    # hint from one check's test program into an unrelated sibling.
    by_block: dict["int | None", list[int]] = {}
    for idx, entry in enumerate(entries):
        by_block.setdefault(entry[4], []).append(idx)

    checks: list[Check] = []
    seen_output: set[tuple[str, str]] = set()

    for block_start, idxs in by_block.items():
        idxs.sort(key=lambda idx: entries[idx][0])
        blk_lo = block_start if block_start is not None else 0
        blk_hi = block_end.get(block_start, len(lines) - 1) if block_start is not None else None

        for pos, idx in enumerate(idxs):
            line_idx, guard, name, ctype, _, candidates = entries[idx]
            key = (guard, name)

            if ctype != "generic":
                if key not in seen_output:
                    seen_output.add(key)
                    checks.append(Check(name=name, type=ctype, guard=guard))
                continue

            if candidates is not None:
                # came from a `for ac_prog in A B C` loop - the candidate
                # list itself is already the ground truth, no window
                # scanning needed or wanted
                if key not in seen_output:
                    seen_output.add(key)
                    checks.append(Check(name=name, type=ctype, guard=guard, candidates=candidates))
                continue

            win_start = entries[idxs[pos - 1]][0] + 1 if pos > 0 else blk_lo
            if pos + 1 < len(idxs):
                win_end = entries[idxs[pos + 1]][0] - 1
            elif blk_hi is not None:
                win_end = blk_hi
            else:
                win_end = next_if_after(line_idx)
            window_text = "\n".join(lines[max(win_start, 0):win_end + 1])

            base_name, vreq = split_inline_version(name, shell_vars)
            for c in enrich_generic_check(Check(name=base_name, type=ctype, guard=guard, version_req=vreq),
                                           window_text):
                ck = (c.guard, c.name)
                if ck not in seen_output:
                    seen_output.add(ck)
                    checks.append(c)

    return [c for c in checks if c.name != "ac_nonexistent.h" and "$" not in c.name]


# ---------------------------------------------------------------------------
# Stage 3b: recover <,>,<=,>=,=,!= version requirements.
#
# PKG_CHECK_MODULES(...) itself never appears literally in a GENERATED
# configure script - autoconf fully macro-expands it before writing the
# file out. What DOES survive, verbatim, is the quoted module-spec string
# handed to the actual `$PKG_CONFIG --exists --print-errors "foo >= 1.2.3"`
# shell call the macro expands into - so that's what gets matched, not the
# macro call syntax.
#
# AX_COMPARE_VERSION is a harder case: it expands into inline shell
# comparison logic with no fixed, greppable call signature either. What
# usually IS still literal is the human-facing AC_MSG_CHECKING text next
# to it ("checking for foo >= 1.2.3", "checking whether zlib version is at
# least 1.2.3"), since that message is passed as a plain M4 string and
# survives expansion untouched. So hand-rolled comparisons are recovered
# heuristically from a check's own message text, not by pattern-matching
# the macro - lower confidence, and misses a comparison whose message
# doesn't happen to restate the requirement, but the check itself is still
# reported with installed/candidate version instead of a resolved
# requirement in that case (see resolve_all).
# ---------------------------------------------------------------------------
VERSION_OP_RE = r'<=|>=|==|!=|=|<|>'
PKGCONFIG_EXISTS_RE = re.compile(r'PKG_CONFIG\s+--exists\b[^\n"]*"([^"]+)"')
MODVER_TOKEN_RE = re.compile(
    r'(?<![A-Za-z0-9_.+-])([A-Za-z][A-Za-z0-9+._-]*?)\s*(' + VERSION_OP_RE + r')\s*([0-9][0-9A-Za-z_.+-]*)'
)
# same operator/version shape, but not anchored to a leading module name -
# for pulling a bare requirement out of a check's own "checking for ..."
# message text (Tier 2, heuristic)
INLINE_VERSION_RE = re.compile(r'(' + VERSION_OP_RE + r')\s*v?([0-9][0-9A-Za-z_.]*)\s*$')
# a checking-for message very often embeds the requirement via a shell
# variable ("checking for GTK - version >= $min_gtk_version") rather than
# a literal number - the actual value is set nearby with a plain
# `varname=1.2.3` assignment, which (unlike a macro call) is real shell
# and survives into the generated script verbatim
INLINE_VERSION_VAR_RE = re.compile(r'(' + VERSION_OP_RE + r')\s*\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?\s*$')
SHELL_VAR_ASSIGN_RE = re.compile(r'^[ \t]*([A-Za-z_][A-Za-z0-9_]*)=([0-9][0-9A-Za-z_.]*)[ \t]*$', re.MULTILINE)


def split_inline_version(name: str, shell_vars: dict) -> "tuple[str, VersionReq | None]":
    """If `name` ends in an OP + version (literal, or a $var that resolves
    to one via `shell_vars`), returns (name with that stripped, the
    VersionReq) - otherwise returns (name, None) unchanged."""
    m = INLINE_VERSION_RE.search(name)
    if m:
        op, ver = m.groups()
        base = name[:m.start()].strip()
        if base:
            return base, VersionReq(op=op, version=ver, source="check message text")

    m = INLINE_VERSION_VAR_RE.search(name)
    if m:
        op, varname = m.groups()
        ver = shell_vars.get(varname)
        base = name[:m.start()].strip()
        if ver and base:
            return base, VersionReq(op=op, version=ver, source=f"check message text (${varname})")

    return name, None


def compute_line_guards(lines: list[str]) -> list[str]:
    """A lighter-weight guard resolver than extract_checks' full multi-pass
    one, used only for the version-requirement scan below: which
    --enable/--with var (if any) is in force at each line. Good enough for
    this purpose even where it's less precise than the main resolver."""
    aliases = build_guard_aliases("\n".join(lines))
    heredoc = compute_heredoc_mask(lines)
    stack: list[list[str]] = []
    guards = []
    for i, line in enumerate(lines):
        if not heredoc[i]:
            if IF_RE.match(line):
                candidates = GUARD_VAR_RE.findall(line)
                resolved = [v for v in (resolve_guard_var(c, aliases) for c in candidates) if v]
                stack.append(resolved)
            elif FI_RE.match(line) and stack:
                stack.pop()
        guards.append(next((f[0] for f in reversed(stack) if f), "UNCONDITIONAL"))
    return guards


def extract_pkgconfig_version_checks(text: str) -> list[Check]:
    lines = text.splitlines()
    line_guards = compute_line_guards(lines)
    results: list[Check] = []
    seen: set[tuple[str, str]] = set()

    for m in PKGCONFIG_EXISTS_RE.finditer(text):
        line_idx = min(text.count("\n", 0, m.start()), len(line_guards) - 1)
        guard = line_guards[line_idx] if line_guards else "UNCONDITIONAL"
        spec = m.group(1)

        versioned_mods = set()
        for tok_m in MODVER_TOKEN_RE.finditer(spec):
            mod, op, ver = tok_m.groups()
            key = (guard, mod)
            versioned_mods.add(mod)
            if key in seen:
                continue
            seen.add(key)
            results.append(Check(name=mod, type="pkgconfig", guard=guard,
                                  version_req=VersionReq(op=op, version=ver, source="pkg-config")))

        for tok in spec.split():
            # a module name always starts with a letter; a version number
            # never does, so this also excludes the version half of every
            # "mod OP ver" triple already captured above without needing
            # to track its exact token position
            if tok in versioned_mods or not re.match(r'^[A-Za-z][A-Za-z0-9+._-]*$', tok):
                continue
            key = (guard, tok)
            if key in seen:
                continue
            seen.add(key)
            results.append(Check(name=tok, type="pkgconfig", guard=guard))

    return results


def attach_inline_version_reqs(checks: list[Check], shell_vars: dict) -> list[Check]:
    """Tier 2: if a check's own message text already states the version
    requirement (e.g. name == 'zlib >= 1.2.3'), split it out into a real
    VersionReq instead of leaving it embedded in the display name."""
    from dataclasses import replace
    out = []
    for c in checks:
        if c.version_req is not None:
            out.append(c)
            continue
        base, vreq = split_inline_version(c.name, shell_vars)
        out.append(replace(c, name=base, version_req=vreq) if vreq else c)
    return out


# ---------------------------------------------------------------------------
# Stage 4: turn a checked name into a filesystem search term apt-file/dpkg
# can actually match against. Returns None for names that aren't files at
# all (bare function names, free-text like "GNU ld").
# ---------------------------------------------------------------------------
PLAIN_TOKEN_RE = re.compile(r'^[A-Za-z0-9_.+-]+$')


def search_term_for(check: Check) -> str | None:
    if check.type == "header":
        # a header check's raw message sometimes carries a leading
        # descriptive word ("checking for working alloca.h"), and the
        # classifier's guarantee (ctype == "header" only when the name
        # ends in ".h") means the trailing token is always the real
        # filename - stripping the rest keeps a message like that from
        # searching for the literal, unmatchable string "working alloca.h"
        return check.name.split()[-1]
    if check.type == "library":
        libname = check.name.rsplit(" in -l", 1)[-1]
        return f"lib{libname}.so"
    if check.type == "pkgconfig":
        return f"{check.name}.pc"
    if check.type == "function":
        # a bare libc/toolchain function check - there's no single Debian
        # package to point at (it's testing the existing compiler/libc),
        # so this is intentionally left unresolved rather than guessed.
        return None
    if check.type == "generic":
        # only useful if it's a plain program name (single token, no
        # spaces) - e.g. "gcc". Anchor to bin/ so a short name doesn't
        # substring-match unrelated packages.
        if PLAIN_TOKEN_RE.match(check.name):
            return f"bin/{check.name}"
    return None


# ---------------------------------------------------------------------------
# Stage 5: resolve a search term to a Debian package + install status.
# dpkg -S first (fast, catches anything already installed), apt-file as
# fallback for undiscovered packages, both glob-anchored on a path
# separator so "zlib.h" doesn't also match ".../bzlib.h".
# ---------------------------------------------------------------------------
HAVE_DPKG = shutil.which("dpkg") is not None
HAVE_APTFILE = shutil.which("apt-file") is not None
# ldconfig normally lives in /sbin or /usr/sbin, which a regular (non-root)
# user's $PATH on Debian typically does NOT include - only root's does. A
# plain shutil.which("ldconfig") silently returns None for most people
# running this tool, which would otherwise skip the entire ldconfig-based
# library search (falling back to the weaker dpkg -S path for everything,
# with no clear sign that happened) - so check the well-known absolute
# locations too, not just $PATH.
LDCONFIG_PATH = (
    shutil.which("ldconfig")
    or next((p for p in ("/sbin/ldconfig", "/usr/sbin/ldconfig", "/usr/bin/ldconfig")
             if os.path.isfile(p) and os.access(p, os.X_OK)), None)
)


def _run(cmd: list[str], timeout: int) -> list[str]:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return [l for l in out.stdout.splitlines() if l.strip()]
    except Exception:
        return []


def _format_ambiguous(pkgs: list[str], max_chars: int = 32) -> str:
    """Join candidate package names for display, capped at a fixed max
    length regardless of how many/how long the names are. Column widths
    in the report are uniform per-column, so an unbounded list here would
    force every OTHER row in the same table to pay for this one row's
    width - like a `-` package value getting squeezed to make room for a
    long ambiguous list on a totally unrelated check."""
    joined = " or ".join(pkgs)
    if len(joined) <= max_chars:
        return joined
    out = pkgs[0]
    for p in pkgs[1:]:
        candidate = f"{out} or {p}"
        if len(candidate) > max_chars - 12:  # leave room for the "(+N more)" tail
            break
        out = candidate
    shown_count = out.count(" or ") + 1
    remaining = len(pkgs) - shown_count
    return out + (f" (+{remaining} more)" if remaining > 0 else "")


# ---------------------------------------------------------------------------
# Direct Contents-file parsing - an alternative to shelling out to the
# apt-file CLI program for resolving a package that ISN'T currently
# installed. apt-file is a separate package with its own runtime
# dependencies (a compiled libapt-pkg-perl binding, tied to one specific
# libapt-pkg SONAME and Perl ABI) that don't always travel well even
# across versions of the SAME distro family - confirmed the hard way: a
# Contents file fetched and decompressed on one machine can be handed to
# a completely different machine and searched directly with nothing but
# Python's stdlib and a near-universal system library (liblz4), whereas
# the actual apt-file program failed to even install across a
# Debian-Trixie-vs-Ubuntu gap. The Contents file itself is just a plain
# text index (optionally gzip- or lz4-compressed) of "path  package[,
# package...]" lines - the exact same data apt-file itself searches,
# with no other moving parts. This tier is tried BEFORE the apt-file CLI
# tier below: it needs nothing installed beyond what's almost always
# already on a Debian/Ubuntu system, so it's the more portable path when
# it has data to work with, falling through to apt-file (if present) only
# when no Contents file can be found at all.
# ---------------------------------------------------------------------------
CONTENTS_ENV_VAR = "DEB_BUILDCHECK_CONTENTS_FILE"
# newest apt (Debian ~bullseye+/Trixie, integrated straight into the main
# list cache) through classic apt-file's own separate cache directory
CONTENTS_SEARCH_DIRS = (
    "/var/lib/apt/lists",
    "/var/cache/apt/apt-file",
    os.path.expanduser("~/.cache/apt-file"),
)


def _find_contents_files() -> list:
    """An explicit DEB_BUILDCHECK_CONTENTS_FILE env var (one path, or several
    joined with ':') always wins and skips the directory scan - useful
    when the relevant Contents file was fetched on a different machine
    and dropped in an arbitrary location, exactly like tonight's case."""
    override = os.environ.get(CONTENTS_ENV_VAR)
    if override:
        return [p for p in override.split(":") if p and os.path.isfile(p)]

    found = []
    for d in CONTENTS_SEARCH_DIRS:
        if not os.path.isdir(d):
            continue
        try:
            for name in os.listdir(d):
                if "contents-" in name.lower():
                    found.append(os.path.join(d, name))
        except OSError:
            continue
    return found


@lru_cache(maxsize=1)
def _contents_files() -> tuple:
    return tuple(_find_contents_files())


_LZ4 = None


def _lz4_lib():
    """liblz4.so.1 is the LZ4 runtime library itself, not the standalone
    `lz4` CLI tool or a pip-installed binding - it's a near-universal
    transitive dependency on Debian/Ubuntu (pulled in by systemd, apt
    itself, and many others), so driving it directly via ctypes avoids
    needing anything additional installed just to read a modern apt
    Contents file, which is lz4-compressed by default."""
    global _LZ4
    if _LZ4 is None:
        try:
            lib = ctypes.CDLL("liblz4.so.1")
            lib.LZ4F_createDecompressionContext.restype = ctypes.c_size_t
            lib.LZ4F_createDecompressionContext.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint]
            lib.LZ4F_freeDecompressionContext.restype = ctypes.c_size_t
            lib.LZ4F_freeDecompressionContext.argtypes = [ctypes.c_void_p]
            lib.LZ4F_isError.restype = ctypes.c_uint
            lib.LZ4F_isError.argtypes = [ctypes.c_size_t]
            lib.LZ4F_decompress.restype = ctypes.c_size_t
            lib.LZ4F_decompress.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                ctypes.c_void_p,
            ]
            _LZ4 = lib
        except OSError:
            _LZ4 = False
    return _LZ4 or None


_LZ4_READ_SIZE = 1 << 18     # compressed bytes fed to liblz4 per read
_LZ4_OUT_SIZE = 1 << 20      # decompressed bytes produced per call
_BLOCK_READ_SIZE = 1 << 20   # bytes per read for gzip/plain Contents files


def _lz4_stream_chunks(path: str):
    """Yield a LZ4-framed file's decompressed content in ~1MB pieces.

    Streaming matters here: the Debian main Contents-all file alone is
    ~590MB decompressed, and the whole set for one release is ~800MB.
    The previous approach decompressed each file into one bytes object and
    then turned every line into a Python str that stayed alive for the
    life of the process - well over a gigabyte of resident memory for a
    lookup table that only ever answers "which package ships file X". Here
    only the current input chunk and one 1MB output buffer exist at a time
    (~20MB peak RSS regardless of how large the Contents files are)."""
    lib = _lz4_lib()
    if lib is None:
        raise RuntimeError("liblz4.so.1 not available")
    ctx = ctypes.c_void_p()
    if lib.LZ4F_isError(lib.LZ4F_createDecompressionContext(ctypes.byref(ctx), 100)):
        raise RuntimeError("failed to create LZ4 decompression context")
    out_buf = ctypes.create_string_buffer(_LZ4_OUT_SIZE)
    frame_done = True    # nothing in flight yet; flips False mid-frame
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(_LZ4_READ_SIZE)
                if not chunk:
                    break
                src_buf = ctypes.create_string_buffer(chunk, len(chunk))
                src_base = ctypes.addressof(src_buf)
                src_off = 0
                while src_off < len(chunk):
                    src_size = ctypes.c_size_t(len(chunk) - src_off)
                    dst_size = ctypes.c_size_t(_LZ4_OUT_SIZE)
                    result = lib.LZ4F_decompress(ctx, out_buf, ctypes.byref(dst_size),
                                                  ctypes.c_void_p(src_base + src_off),
                                                  ctypes.byref(src_size), None)
                    if lib.LZ4F_isError(result):
                        raise RuntimeError("LZ4F_decompress failed (corrupt or unsupported LZ4 data)")
                    if src_size.value == 0 and dst_size.value == 0:
                        raise RuntimeError("LZ4F_decompress made no progress")
                    src_off += src_size.value
                    frame_done = (result == 0)   # 0 = a complete frame has been fully decoded
                    if dst_size.value:
                        yield ctypes.string_at(out_buf, dst_size.value)
        if not frame_done:
            raise RuntimeError("truncated LZ4 file (ends mid-frame)")
    finally:
        lib.LZ4F_freeDecompressionContext(ctx)


def _raw_contents_chunks(path: str):
    """Decompressed bytes of a Contents file in arbitrary-sized pieces,
    whether it is plain text, gzip-compressed (classic apt-file), or
    lz4-compressed (apt's own default since Debian ~bullseye/Trixie)."""
    if path.endswith(".lz4"):
        yield from _lz4_stream_chunks(path)
        return
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rb") as f:
        while True:
            chunk = f.read(_BLOCK_READ_SIZE)
            if not chunk:
                break
            yield chunk


def _iter_contents_blocks(path: str):
    """Yield the file as blocks of COMPLETE lines (each block ends on a
    newline), so a caller never has to deal with a line split across two
    reads."""
    tail = b""
    for chunk in _raw_contents_chunks(path):
        data = tail + chunk if tail else chunk
        cut = data.rfind(b"\n")
        if cut < 0:
            tail = data
            continue
        tail = data[cut + 1:]
        yield data[:cut + 1]
    if tail.strip():
        yield tail + b"\n"


# (term, exact) -> sorted tuple of package names, for every term already
# scanned for. This is the ONLY thing kept in memory from the Contents
# files - a few bytes per distinct term, not the files' contents.
_CONTENTS_CACHE: "dict[tuple[str, bool], tuple[str, ...]]" = {}


_CONTENTS_ARCH_RE = re.compile(r'contents-([a-z0-9]+)(?:\.|$)', re.IGNORECASE)


@lru_cache(maxsize=1)
def _native_arch() -> "str | None":
    out = _run(["dpkg", "--print-architecture"], timeout=5) if HAVE_DPKG else []
    return out[0].strip() if out else None


def _is_foreign_contents_file(path: str) -> bool:
    """True for a Contents file of a different CPU architecture than this
    machine's (e.g. Contents-i386 on an amd64 system with 32-bit multiarch
    enabled). 'Contents-all' and anything whose name doesn't say are never
    foreign. Foreign files list packages that only exist in that other
    architecture's archive - lib64ncurses-dev, binutils-x86-64-linux-gnu,
    libc6-dev-amd64 on i386 - which can't be installed here and would
    otherwise show up as false 'ambiguous' candidates. Their ordinary
    packages (libncurses-dev, ...) have the SAME name as the native ones,
    so nothing real is lost by setting the foreign-only names aside."""
    m = _CONTENTS_ARCH_RE.search(os.path.basename(path))
    native = _native_arch()
    return bool(m and native and m.group(1).lower() not in (native.lower(), "all"))


def _packages_in(pkg_field: bytes) -> "set[str]":
    """'section/pkg,section/pkg' -> {'pkg', ...}"""
    pkgs = set()
    for entry in pkg_field.decode("utf-8", errors="replace").split(","):
        pkg = entry.rsplit("/", 1)[-1]
        if pkg:
            pkgs.add(pkg)
    return pkgs


def _scan_contents(keys: "list[tuple[str, bool]]") -> None:
    """Resolve every (term, exact) key in `keys` against every Contents
    file in ONE streaming pass, and record each result in _CONTENTS_CACHE.

    The matching rules are exactly the ones the old per-term regex used:
    non-exact = the path ends in '/' + term (term is the final path
    component, or a trailing run of components like 'bin/gcc'); exact =
    the path ends in term. What changed is only how lines are found. The
    final path component is looked up in a dict built from the wanted
    terms, so a line costs one rsplit + one rpartition + one dict probe
    no matter how many terms are being searched for - a pass over all of
    Debian's Contents files (~8M lines) takes a couple of seconds
    whether it's resolving 1 term or 200. The suffix check afterwards is
    what makes a basename hit a real match."""
    # hits from this machine's own architecture (and Contents-all), kept
    # apart from hits seen only in a foreign architecture's file - see
    # _is_foreign_contents_file for why
    native_found: "dict[tuple[str, bool], set]" = {k: set() for k in keys}
    foreign_found: "dict[tuple[str, bool], set]" = {k: set() for k in keys}

    loose: "dict[bytes, list[tuple[tuple[str, bool], bytes]]]" = {}   # final component -> [(key, '/'+term)]
    exact_suffixes: "dict[bytes, list[tuple[str, bool]]]" = {}       # term -> [key]
    for key in keys:
        term, exact = key
        raw = term.encode("utf-8", errors="replace")
        if exact:
            exact_suffixes.setdefault(raw, []).append(key)
        else:
            loose.setdefault(raw.rpartition(b"/")[2], []).append((key, b"/" + raw))
    exact_tuple = tuple(exact_suffixes)   # bytes.endswith() takes a tuple, tested in C

    for path in _contents_files():
        found = foreign_found if _is_foreign_contents_file(path) else native_found
        try:
            for block in _iter_contents_blocks(path):
                for line in block.split(b"\n"):
                    parts = line.rsplit(None, 1)
                    if len(parts) != 2:
                        continue
                    file_path, pkg_field = parts
                    hits = loose.get(file_path.rpartition(b"/")[2])
                    if hits:
                        for key, suffix in hits:
                            if file_path.endswith(suffix):
                                found[key] |= _packages_in(pkg_field)
                    if exact_tuple and file_path.endswith(exact_tuple):
                        for suffix, ekeys in exact_suffixes.items():
                            if file_path.endswith(suffix):
                                for key in ekeys:
                                    found[key] |= _packages_in(pkg_field)
        except (OSError, RuntimeError, EOFError, zlib.error) as e:
            # whatever was read before the problem is still valid, so keep
            # it, but say so - silently treating a damaged download as
            # "no matches" would look exactly like a genuinely missing package
            print(f"warning: couldn't read all of Contents file {path}: {e}", file=sys.stderr)

    for key in keys:
        # foreign-architecture names only count when nothing native matched
        _CONTENTS_CACHE[key] = tuple(sorted(native_found[key] or foreign_found[key]))


def prefetch_contents(terms, exact: bool = False) -> None:
    """Look up many search terms in a single pass over the Contents files.

    Optional, but it is what makes a report fast: without it each
    not-yet-seen term triggers its own pass (still low-memory, just
    repeated - a couple of seconds each on the full Debian set). A caller
    that knows its terms up front - all the checks of a configure script,
    all the libraries of a Makefile - calls this once before resolving
    them one by one. Passing extra terms is harmless; terms that dpkg
    already answers locally are skipped, since resolve_package would never
    consult the Contents files for them."""
    if not _contents_files():
        return
    wanted = []
    for term in dict.fromkeys(terms):    # de-duplicated, order kept
        if not term or (term, exact) in _CONTENTS_CACHE:
            continue
        if _local_package_lookup(term, exact):
            continue
        wanted.append((term, exact))
    if wanted:
        _scan_contents(wanted)


def search_contents_files(term: str, exact: bool) -> list:
    """Returns candidate package names found by searching every reachable
    Contents file directly - the same data `apt-file search` itself
    would consult. Mirrors apt-file's own -x regex anchoring: exact
    matches the path ending in exactly `term`; non-exact matches `term`
    as the final path component (preceded by a '/'), same as the
    apt-file CLI tier below."""
    if not _contents_files():
        return []
    key = (term, exact)
    if key not in _CONTENTS_CACHE:
        _scan_contents([key])
    return list(_CONTENTS_CACHE[key])


def describe_contents_files() -> str:
    """One-line-or-more status message naming exactly which Contents
    files were found for the not-currently-installed lookup tier - since
    Debian keeps several (one per component: main, contrib, non-free,
    non-free-firmware, sometimes per-architecture too), it's not obvious
    at a glance which ones are actually in play without this."""
    files = _contents_files()
    if not files:
        return ("No local Contents files found (checked $DEB_BUILDCHECK_CONTENTS_FILE, "
                 "/var/lib/apt/lists, /var/cache/apt/apt-file, ~/.cache/apt-file) - "
                 "lookups for anything not already installed will rely on 'apt-file "
                 "search' if it's installed, or show '?' otherwise.")
    listing = "\n".join(
        f"  {p}" + ("   [foreign architecture - only used when nothing native matches]"
                    if _is_foreign_contents_file(p) else "")
        for p in files)
    return f"Using {len(files)} Contents file(s) for package lookup:\n{listing}"


@lru_cache(maxsize=None)
def _local_package_lookup(term: str, exact: bool) -> tuple:
    """Tier 1 of resolve_package: packages that already OWN a matching
    file on this machine, per `dpkg -S`. Split out and cached so that
    prefetch_contents() can ask the same question ("does the local dpkg
    database already answer this?") before deciding what needs a Contents
    scan, without the later resolve_package call repeating the subprocess."""
    if not HAVE_DPKG:
        return ()
    dpkg_pattern = term if exact else f"*/{term}"
    hits = _run(["dpkg", "-S", dpkg_pattern], timeout=10)
    return tuple(sorted({h.split(":", 1)[0] for h in hits}))


@lru_cache(maxsize=None)
def resolve_package(term: str | None, exact: bool = False) -> tuple[str, str, str]:
    if not term:
        return ("-", "unknown", "not a file-based check")

    if HAVE_DPKG:
        pkgs = list(_local_package_lookup(term, exact))
        if pkgs:
            if len(pkgs) > 1 and not exact:
                # dpkg -S's ordering isn't meaningful - it's whatever order
                # the local database happens to return, not which package
                # is the "real" dependency. Confidently picking the first
                # one can be flatly wrong (e.g. a C build's stdio.h getting
                # attributed to libstdc++-dev just because a C++ toolchain
                # happens to also be installed). List every candidate
                # instead of guessing.
                pkg = _format_ambiguous(pkgs)
                note = f"ambiguous: {len(pkgs)} packages ship a file named this - verify which applies"
            else:
                pkg = pkgs[0]
                note = ""
            return (pkg, "yes", note)

    contents_pkgs = search_contents_files(term, exact)
    if contents_pkgs:
        if len(contents_pkgs) > 1:
            pkg = _format_ambiguous(contents_pkgs)
            note = f"ambiguous: {len(contents_pkgs)} packages ship a file named this - verify which applies"
        else:
            pkg = contents_pkgs[0]
            note = ""
        return (pkg, "no", note)

    # apt-file is only a stand-in for when there are no Contents files to
    # read directly. If we DID just search them and found nothing, asking
    # apt-file means searching the same data again (apt-file reads the same
    # Contents indexes) - and it's slow, ~9s per call, which for a typical
    # configure script's handful of genuinely-nonexistent names (e.g.
    # minix/config.h) was ~75s of an otherwise few-second run.
    if HAVE_APTFILE and not _contents_files():
        pattern = f"{term}$" if exact else f"/{term}$"
        hits = _run(["apt-file", "search", "-x", pattern], timeout=30)
        if hits:
            pkgs = sorted({h.split(":", 1)[0] for h in hits})
            if len(pkgs) > 1:
                pkg = _format_ambiguous(pkgs)
                note = f"ambiguous: {len(pkgs)} packages ship a file named this - verify which applies"
            else:
                pkg = pkgs[0]
                note = ""
            return (pkg, "no", note)

    return ("?", "unknown",
            "no match (need a Contents file under /var/lib/apt/lists, or apt-file installed + updated)")


def _fix_merged_usr(path: str) -> str:
    """Resolve only a merged-/usr directory symlink (/lib -> /usr/lib), not
    the file itself - a library's own unversioned .so is normally a
    symlink to its versioned .so.N, and fully resolving THAT would
    collapse the exact dev-vs-runtime distinction resolve_library relies
    on. dpkg's database records the canonical /usr/... directory even
    though /bin, /lib etc. still work as legacy compatibility symlinks."""
    d = os.path.realpath(os.path.dirname(path))
    return os.path.join(d, os.path.basename(path))


def resolve_program(names: tuple[str, ...] | str) -> tuple[str, str, str]:
    """A program check is best answered by just asking the shell where the
    program actually lives on THIS machine (same idea as `command -v`),
    then handing dpkg the exact real path - no bin/ glob-guessing, no
    ambiguity about /bin vs /usr/bin vs /usr/sbin. When given several
    candidate names (from a `for ac_prog in A B C` loop), tries each in
    order via `which`, exactly mirroring what autoconf itself does - the
    first one that exists on this machine is the real answer. Falls back
    to the glob heuristic on the first candidate only when NONE are
    present here at all, purely to still surface what would need
    installing."""
    if isinstance(names, str):
        names = (names,)

    for name in names:
        path = shutil.which(name)
        if not path:
            continue
        path = _fix_merged_usr(path)
        if HAVE_DPKG:
            hits = _run(["dpkg", "-S", path], timeout=10)
            if hits:
                return (hits[0].split(":", 1)[0], "yes", "")

            # Debian's update-alternatives manages some commands (cc, mt,
            # javac, ...) as a symlink dpkg doesn't own directly - the
            # package owns the real target underneath, not the alternatives
            # symlink itself. Unlike the library case, there's no dev-vs-
            # runtime distinction to lose here, so fully resolving the
            # symlink chain is safe and often finds the real owner.
            real = os.path.realpath(path)
            if real != path:
                hits = _run(["dpkg", "-S", real], timeout=10)
                if hits:
                    return (hits[0].split(":", 1)[0], "yes", "")

            return ("?", "yes", f"found at {path} but no package owns it (locally built or manually installed?)")
        return ("?", "yes", f"found at {path} (dpkg not available to identify the owning package)")

    return resolve_package(f"bin/{names[0]}")


# A handful of libraries have no standalone .so file to find on a glibc
# system at all - not because they're missing, but because their
# functionality has always lived directly inside libc.so.6. iconv is the
# canonical case: GNU libc has provided iconv_open/iconv/iconv_close
# natively since day one; the separate FSF libiconv package only exists
# for non-glibc platforms (musl, macOS, *BSD) that don't bundle it. A
# plain ldconfig/dpkg search for "libiconv.so" correctly finds nothing on
# such a system - there genuinely is no such file - so without this,
# resolve_library reports a bare "no match" that reads as a real gap
# rather than what it actually is: satisfied already, just not as a
# separate file.
GLIBC_BUILTIN_LIBS: dict = {"iconv": "iconv_open"}


def _libc_path() -> "str | None":
    if not LDCONFIG_PATH:
        return None
    for line in _run([LDCONFIG_PATH, "-p"], timeout=10):
        if "=>" not in line:
            continue
        name_part, path_part = line.split("=>", 1)
        tokens = name_part.split()
        if tokens and tokens[0].startswith("libc.so"):
            return path_part.strip()
    return None


@lru_cache(maxsize=None)
def _libc_provides_symbol(symbol: str) -> bool:
    libc = _libc_path()
    if not libc or not shutil.which("nm"):
        return False
    pattern = re.compile(r'\b' + re.escape(symbol) + r'(@@?[A-Za-z0-9_.]+)?$')
    for line in _run(["nm", "-D", "--defined-only", libc], timeout=15):
        if pattern.search(line.strip()):
            return True
    return False


@lru_cache(maxsize=1)
def _ldconfig_names() -> frozenset:
    """Every library name the dynamic linker knows about (`ldconfig -p`),
    fetched once. resolve_library does its own live lookup; this is only
    for contents_term_for_library's cheap "is it even worth searching?"."""
    names = set()
    if LDCONFIG_PATH:
        for line in _run([LDCONFIG_PATH, "-p"], timeout=10):
            if "=>" in line:
                tokens = line.split("=>", 1)[0].split()
                if tokens:
                    names.add(tokens[0])
    return frozenset(names)


def contents_term_for_library(libname: str) -> "str | None":
    """The Contents search term resolve_library(libname) would fall back to
    ('libfoo.so'), or None when it can answer from this machine alone (the
    linker already knows the library, or it's glibc-builtin) - so batch
    prefetching never spends a glob-style `dpkg -S` on a library the
    original lookup resolved with a cheap exact-path query."""
    soname = f"lib{libname}.so"
    names = _ldconfig_names()
    if soname in names or any(n.startswith(soname + ".") for n in names):
        return None
    symbol = GLIBC_BUILTIN_LIBS.get(libname)
    if symbol and _libc_provides_symbol(symbol):
        return None
    return soname


def resolve_library(libname: str) -> tuple[str, str, str]:
    """A library check needs the equivalent of `which`, but `shutil.which`
    only walks $PATH for executables - that mechanism doesn't apply to
    shared libraries at all. The real equivalent is `ldconfig -p`, which
    lists every shared library the dynamic linker actually knows about on
    this machine. One wrinkle worth respecting: `-lfoo` at link time needs
    the *unversioned* libfoo.so symlink, which normally only exists if the
    -dev package is installed - the runtime package alone only ships the
    versioned libfoo.so.N. So a plain "is it in ldconfig" check isn't
    enough; distinguish the two rather than reporting a false "yes"."""
    soname = f"lib{libname}.so"
    if LDCONFIG_PATH:
        exact_path = None
        versioned_path = None
        for line in _run([LDCONFIG_PATH, "-p"], timeout=10):
            if "=>" not in line:
                continue
            name_part, path_part = line.split("=>", 1)
            tokens = name_part.split()
            if not tokens:
                continue
            name_token, path = tokens[0], path_part.strip()
            if name_token == soname:
                exact_path = path
                break
            if name_token.startswith(soname + "."):
                versioned_path = versioned_path or path

        target = exact_path or versioned_path
        if target:
            target = _fix_merged_usr(target)
        if target and HAVE_DPKG:
            hits = _run(["dpkg", "-S", target], timeout=10)
            if hits:
                pkg = hits[0].split(":", 1)[0]
                if exact_path:
                    return (pkg, "yes", "")
                return (pkg, "yes",
                        f"runtime lib present ({target}) but no unversioned dev symlink - "
                        f"the -dev package (for linking) may still be missing")

    expected_symbol = GLIBC_BUILTIN_LIBS.get(libname)
    if expected_symbol and _libc_provides_symbol(expected_symbol):
        libc = _libc_path()
        pkg = "libc6"
        if libc and HAVE_DPKG:
            hits = _run(["dpkg", "-S", _fix_merged_usr(libc)], timeout=10)
            if hits:
                pkg = hits[0].split(":", 1)[0]
        return (pkg, "yes",
                f"no separate lib{libname}.so on this system - {libname} is provided directly "
                f"by glibc itself (symbol {expected_symbol} found in libc), not a standalone library")

    return resolve_package(soname)


# ---------------------------------------------------------------------------
# Stage 5b: version lookup, comparison, and display for a resolved check.
# Three ways to learn "what version do I actually have / can I get":
#   1. ask the thing itself (`prog --version`, `pkg-config --modversion`) -
#      the real upstream version, comparable directly against a version_req
#   2. fall back to the Debian PACKAGE's version (dpkg-query / apt-cache
#      policy) when there's no direct way to ask - close enough to answer
#      "what do I have / can I get" even though it's not always safely
#      comparable to an upstream version_req (Debian appends its own
#      suffixes), so is reported as-is without a pass/fail verdict
#   3. nothing resolvable at all -> "-"
# ---------------------------------------------------------------------------
VERSION_LIKE_RE = re.compile(r'\d+(?:\.\d+){0,3}[A-Za-z0-9]*')
_OP_TO_DPKG = {"=": "eq", "==": "eq", "!=": "ne", "<": "lt", "<=": "le", ">": "gt", ">=": "ge"}


def dpkg_compare_versions(installed: str, op: str, required: str) -> "bool | None":
    """None means "couldn't determine" (no dpkg, or a non-version-looking
    string) - never guess a pass/fail in that case."""
    if not shutil.which("dpkg") or not installed or not required:
        return None
    dop = _OP_TO_DPKG.get(op)
    if not dop:
        return None
    try:
        r = subprocess.run(["dpkg", "--compare-versions", installed, dop, required],
                            capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return None


def installed_version_via_program(prog_path: str) -> "str | None":
    for flag in ("--version", "-version", "-V"):
        try:
            out = subprocess.run([prog_path, flag], capture_output=True, text=True, timeout=5)
        except Exception:
            continue
        blob = (out.stdout or "") + (out.stderr or "")
        m = VERSION_LIKE_RE.search(blob)
        if m:
            return m.group(0)
    return None


def installed_version_via_pkgconfig(mod: str) -> "str | None":
    if not shutil.which("pkg-config"):
        return None
    out = _run(["pkg-config", "--modversion", mod], timeout=5)
    return out[0].strip() if out and out[0].strip() else None


@lru_cache(maxsize=None)
def package_version_info(pkg: str) -> "tuple[str | None, str | None]":
    """(installed package version, candidate/available package version),
    via dpkg-query and apt-cache policy - the fallback when there's no
    direct way to ask the thing itself for its upstream version."""
    installed = None
    if shutil.which("dpkg-query"):
        out = _run(["dpkg-query", "-W", "-f=${Version}", pkg], timeout=5)
        if out and out[0].strip():
            installed = out[0].strip()
    candidate = None
    if shutil.which("apt-cache"):
        for line in _run(["apt-cache", "policy", pkg], timeout=10):
            line = line.strip()
            if line.startswith("Candidate:"):
                v = line.split(":", 1)[1].strip()
                if v and v != "(none)":
                    candidate = v
                break
    return installed, candidate


def first_candidate_package(pkg: str) -> str:
    """_format_ambiguous always puts pkgs[0] first, so the display string's
    leading name (before ' or ' or ' (+N more)') is a real, valid package
    name we can actually query - just not necessarily the *right* one."""
    pkg = re.sub(r'\s*\(\+\d+ more\)$', '', pkg)
    return pkg.split(' or ', 1)[0].strip()


def have_cell(check: Check, pkg: str, resolved_prog_path: "str | None" = None) -> str:
    """What's actually installed/available - deliberately independent of
    whether check.version_req exists or pkg resolved cleanly, so a
    resolution failure (unknown package, or an ambiguous 'pkgA (+2 more)'
    string that isn't a real dpkg/apt-cache argument) can never make the
    WANT requirement disappear along with it."""
    direct: "str | None" = None
    if check.type == "pkgconfig":
        direct = installed_version_via_pkgconfig(check.name)
    elif resolved_prog_path:
        direct = installed_version_via_program(resolved_prog_path)

    if direct:
        if check.version_req:
            ok = dpkg_compare_versions(direct, check.version_req.op, check.version_req.version)
            verdict = "ok" if ok is True else "too old/new" if ok is False else "unverified"
            return f"{direct} ({verdict})"
        return direct

    if pkg and pkg not in ("-", "?"):
        ambiguous = "(+" in pkg or " or " in pkg
        lookup_pkg = first_candidate_package(pkg) if ambiguous else pkg
        installed_pkgver, candidate_pkgver = package_version_info(lookup_pkg)
        tag = " (first of several - see note)" if ambiguous else ""
        if installed_pkgver:
            return f"pkg: {installed_pkgver}{tag}"
        if candidate_pkgver:
            return f"can get: {candidate_pkgver}{tag}"
    return "-"


# Bare single-word "generic" checks that are libtool/autoconf internal
# configuration concepts, not real invokable programs - resolve_all's
# generic-check handling below otherwise treats any plain token as a
# program-name candidate and wastes a real lookup on something that was
# never going to resolve to a package.
NOT_A_PROGRAM = {"sysroot", "objdir", "inline"}

# A small, fixed, well-known set of tool names libtool's own boilerplate
# checks for on every single autotools project, regardless of target
# platform (dumpbin/mt are Windows-only, nmedit/lipo/otool/otool64 are
# macOS-only) - on Linux these can never resolve to a real package, so
# there's no point spending a real lookup (Contents-file scan or
# apt-file call) confirming that every time.
NOT_ON_LINUX = {"dumpbin", "mt", "nmedit", "lipo", "otool", "otool64"}


def contents_terms_for(checks: list[Check]) -> list[str]:
    """The search terms resolve_all() MAY end up sending to the Contents
    tier for these checks, so they can be looked up together in a single
    pass (prefetch_contents) instead of one pass each. Deliberately a
    superset - it mirrors the term each branch of resolve_all would hand
    to resolve_package, skipping programs/libraries the machine already
    has (the cheap checks resolve_all itself starts with); prefetch_contents
    additionally drops anything `dpkg -S` answers. An extra term in a batch
    costs next to nothing, so a rare over-inclusion is harmless."""
    terms: list[str] = []
    for c in checks:
        if c.type == "generic":
            if c.name in NOT_A_PROGRAM or c.name in NOT_ON_LINUX:
                continue
            if c.candidates:
                names = c.candidates
            elif PLAIN_TOKEN_RE.match(c.name):
                names = (c.name,)
            else:
                continue
            # resolve_program only reaches the Contents tier when none of
            # the candidates exists on this machine, and then uses the first
            if not any(shutil.which(n) for n in names):
                terms.append(f"bin/{names[0]}")
        elif c.type == "library":
            term = contents_term_for_library(c.name.rsplit(" in -l", 1)[-1])
            if term:
                terms.append(term)
        elif c.type == "pkgconfig":
            terms.append(f"{c.name}.pc")
        else:
            term = search_term_for(c)
            if term:
                terms.append(term)
    return terms


def prefetch_report_contents(options: list[Option], mandatory: list[Check]) -> None:
    prefetch_contents(contents_terms_for(mandatory + [c for opt in options for c in opt.checks]))


def resolve_all(checks: list[Check]) -> list[dict]:
    results = []
    for c in checks:
        prog_path = None
        if c.type == "generic" and c.name in NOT_A_PROGRAM:
            pkg, installed, note = "-", "unknown", "not a real dependency - an internal libtool/autoconf configuration concept, not a program"
        elif c.type == "generic" and c.name in NOT_ON_LINUX:
            pkg, installed, note = "-", "no", "Windows/macOS-only tool from libtool's boilerplate - never applicable on Linux"
        elif c.type == "generic" and c.candidates:
            pkg, installed, note = resolve_program(c.candidates)
            prog_path = next((shutil.which(n) for n in c.candidates if shutil.which(n)), None)
        elif c.type == "generic" and PLAIN_TOKEN_RE.match(c.name):
            pkg, installed, note = resolve_program(c.name)
            prog_path = shutil.which(c.name)
        elif c.type == "library":
            pkg, installed, note = resolve_library(c.name.rsplit(" in -l", 1)[-1])
        elif c.type == "pkgconfig":
            pkg, installed, note = resolve_package(f"{c.name}.pc")
        else:
            pkg, installed, note = resolve_package(search_term_for(c))
        version = have_cell(c, pkg, resolved_prog_path=prog_path)
        req = f"{c.version_req.op}{c.version_req.version}" if c.version_req else "-"
        results.append({"name": c.name, "type": c.type, "package": pkg, "installed": installed,
                         "note": note, "want": req, "have": version,
                         "version_req": "" if req == "-" else req})
    return results


# ---------------------------------------------------------------------------
# Stage 6: bucket checks into mandatory vs. per-option, then render.
# ---------------------------------------------------------------------------
def build_report(configure_path: str) -> tuple[list[Option], list[Check]]:
    text = open(configure_path, "r", errors="replace").read()

    features = parse_options(extract_help_block(text, r'^Optional Features:'), "feature")
    packages = parse_options(extract_help_block(text, r'^Optional Packages:'), "package")
    options = features + packages

    checks = extract_checks(text) + extract_pkgconfig_version_checks(text)
    checks = attach_inline_version_reqs(checks, dict(SHELL_VAR_ASSIGN_RE.findall(text)))
    by_var: dict[str, list[Check]] = {}
    mandatory: list[Check] = []
    for c in checks:
        if c.guard == "UNCONDITIONAL":
            mandatory.append(c)
        else:
            by_var.setdefault(c.guard, []).append(c)

    for opt in options:
        opt.checks = by_var.get(opt.var, [])

    return options, mandatory


# ---------------------------------------------------------------------------
# Terminal-width-aware table rendering. Columns marked flexible=True give up
# width first (in shrink_priority order) when the terminal is too narrow for
# natural content width; everything else stays at its natural size on wide
# terminals instead of sitting at a fixed width that wastes space.
# ---------------------------------------------------------------------------
@dataclass
class Column:
    header: str
    key: str
    min_width: int = 4
    flexible: bool = False
    shrink_priority: int = 0   # lower = shrinks first


def get_term_width(default: int = 100) -> int:
    return shutil.get_terminal_size(fallback=(default, 24)).columns


def truncate(s: str, width: int) -> str:
    if width <= 1:
        return s[:max(width, 0)]
    return s if len(s) <= width else s[: width - 1] + "\u2026"


def render_table(columns: list[Column], rows: list[dict], sep: str = "  ", width: int | None = None) -> str:
    widths = {}
    for col in columns:
        w = len(col.header)
        for r in rows:
            w = max(w, len(str(r.get(col.key, ""))))
        widths[col.header] = max(w, col.min_width)

    term_width = width if width is not None else get_term_width()
    total = sum(widths.values()) + len(sep) * (len(columns) - 1)

    if total > term_width:
        deficit = total - term_width
        # shrink flexible columns first, least-important (highest priority
        # number... actually lowest number = shrinks first) down to a floor
        for col in sorted((c for c in columns if c.flexible), key=lambda c: c.shrink_priority):
            if deficit <= 0:
                break
            floor = col.min_width
            shrinkable = widths[col.header] - floor
            if shrinkable <= 0:
                continue
            take = min(shrinkable, deficit)
            widths[col.header] -= take
            deficit -= take
        # last resort on extremely narrow terminals: shrink anything left
        if deficit > 0:
            for col in columns:
                if deficit <= 0:
                    break
                shrinkable = widths[col.header] - col.min_width
                if shrinkable <= 0:
                    continue
                take = min(shrinkable, deficit)
                widths[col.header] -= take
                deficit -= take

    lines = [sep.join(col.header.ljust(widths[col.header]) for col in columns).rstrip()]
    for r in rows:
        cells = [truncate(str(r.get(col.key, "")), widths[col.header]).ljust(widths[col.header]) for col in columns]
        lines.append(sep.join(cells).rstrip())
    return "\n".join(lines)


def print_report(options: list[Option], mandatory: list[Check], configure_path: str, width: int | None = None) -> None:
    term_width = width if width is not None else get_term_width()
    prefetch_report_contents(options, mandatory)
    print("=" * min(78, term_width))
    print(f" Mandatory checks  (performed unconditionally by {configure_path})")
    print("=" * min(78, term_width))

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
        print(render_table(mand_cols, resolve_all(mandatory), width=term_width))

    print()
    print("=" * min(78, term_width))
    print(" Optional flags  (default state / what they check / package / installed?)")
    print("=" * min(78, term_width))

    opt_rows = []
    for opt in options:
        if not opt.checks:
            opt_rows.append({"flag": opt.flag, "default": opt.default,
                              "name": "(no file/lib check found)", "package": "-", "installed": "-"})
            continue
        for i, row in enumerate(resolve_all(opt.checks)):
            opt_rows.append({
                "flag": opt.flag if i == 0 else "",
                "default": opt.default if i == 0 else "",
                "name": row["name"], "package": row["package"], "installed": row["installed"],
                "want": row["want"], "have": row["have"],
            })

    opt_cols = [
        Column("FLAG", "flag", min_width=8, flexible=True, shrink_priority=2),
        Column("DEFAULT", "default", min_width=7),
        Column("CHECKS FOR", "name", min_width=12, flexible=True, shrink_priority=0),
        Column("PACKAGE", "package", min_width=14, flexible=True, shrink_priority=1),
        Column("INST?", "installed", min_width=5),
        Column("WANT", "want", min_width=6, flexible=True, shrink_priority=4),
        Column("HAVE", "have", min_width=10, flexible=True, shrink_priority=3),
    ]
    print(render_table(opt_cols, opt_rows, width=term_width))

    if not HAVE_APTFILE:
        print()
        print("Note: apt-file not found - package discovery for anything not already")
        print("installed will show '?'. Install with: sudo apt install apt-file && sudo apt-file update")


def to_json(options: list[Option], mandatory: list[Check]) -> str:
    prefetch_report_contents(options, mandatory)
    return json.dumps({
        "mandatory": resolve_all(mandatory),
        "optional": [
            {
                "flag": opt.flag,
                "default": opt.default,
                "kind": opt.kind,
                "description": opt.desc,
                "checks": resolve_all(opt.checks),
            }
            for opt in options
        ],
    }, indent=2)


def main() -> int:
    ap = argparse.ArgumentParser(description="Parse a configure script and map its checks to Debian packages.")
    ap.add_argument("configure", help="path to the configure script")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of the text report")
    ap.add_argument("-o", "--output", metavar="FILE",
                     help="write the report to FILE instead of the terminal - columns are never "
                          "truncated when writing to a file, since there's no terminal width to fit")
    args = ap.parse_args()

    basename = os.path.basename(args.configure)
    if basename != "configure":
        print(f"warning: '{basename}' doesn't look like a generated configure script "
              f"(expected the file to be named exactly 'configure')", file=sys.stderr)
        print("configcheck.py parses the SHELL SCRIPT autoconf generates, not configure.ac "
              "(the M4 source) or a Makefile - either of those will just produce noise. "
              "Use makecheck.py for Makefiles.", file=sys.stderr)
        if sys.stdin.isatty():
            answer = input("Continue anyway? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                print("aborted.", file=sys.stderr)
                return 1
        else:
            print("(non-interactive input - proceeding anyway; pass the actual configure script to skip this warning)",
                  file=sys.stderr)

    print(describe_contents_files(), file=sys.stderr)

    try:
        options, mandatory = build_report(args.configure)
    except FileNotFoundError:
        print(f"error: no such file: {args.configure}", file=sys.stderr)
        return 1

    if args.output:
        with open(args.output, "w") as f:
            with contextlib.redirect_stdout(f):
                if args.json:
                    print(to_json(options, mandatory))
                else:
                    # no terminal to fit, so use a generous fixed width
                    # instead of truncating anything with '...'
                    print_report(options, mandatory, args.configure, width=1000)
        print(f"Report written to {args.output}")
    elif args.json:
        print(to_json(options, mandatory))
    else:
        print_report(options, mandatory, args.configure)
    return 0


if __name__ == "__main__":
    sys.exit(main())
