#!/usr/bin/env python3
"""
makecheck-meson.py - Read a CONFIGURED Meson build directory (the one you ran
`meson setup builddir` in) and report what the build actually looked for, links
against and invokes - mapped to Debian packages. Lookups that came up NOT FOUND
are called out, with the package that would fix them.

The Meson counterpart of makecheck.py / makecheck-cmake.py. Part of
deb-buildcheck-suite. It only IMPORTS configcheck.py (package lookup, Contents
scanning, table rendering) and never modifies it. Must live in the same
directory as configcheck.py.

Reads: meson-logs/meson-log.txt (every dependency/library/program/header
lookup and its result), meson-info/*.json, build.ninja and
compile_commands.json.

Usage: ./makecheck-meson.py /path/to/builddir [--json] [-o FILE]
"""

import argparse
import contextlib
import glob as _glob
import json
import os
import re
import shlex
import subprocess
import sys

try:
    import configcheck as cc
    from configcheck import Check, Column, render_table, get_term_width
except ModuleNotFoundError:
    print("error: configcheck.py not found - makecheck-meson.py shares its package/library",
          file=sys.stderr)
    print("resolution and table code with it, so both files need to be in the same directory.",
          file=sys.stderr)
    sys.exit(1)

LIB_FILE_RE = re.compile(r"^lib(.+?)\.(so(?:\.[0-9.]+)?|a)$")
ARCHIVERS = {"ar", "gcc-ar", "llvm-ar", "ranlib", "gcc-ranlib"}

LOG_PATTERNS = [
    ("dependency", re.compile(r"^(?:Run-time |Build-time )?[Dd]ependency (\S+) found: (YES|NO)\s*(.*)$")),
    ("skipped", re.compile(r"^(?:Run-time |Build-time )?[Dd]ependency (\S+) skipped: (.*)$")),
    ("library", re.compile(r"^Library (\S+) found: (YES|NO)\s*(.*)$")),
    ("program", re.compile(r"^Program (.+?) found: (YES|NO)\s*(.*)$")),
    ("program_skipped", re.compile(r"^Program (.+?) skipped: (.*)$")),
    ("header", re.compile(r'^Has header "([^"]+)" : (YES|NO)\s*(.*)$')),
    ("header_symbol", re.compile(r'^Header "([^"]+)" has symbol "[^"]+" : (YES|NO)\s*(.*)$')),
]

# dependency('name') names that are not packages, or are not found as <name>.pc
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
    "gtest": ("pc:gtest", "h:gtest/gtest.h"),
    "gmock": ("pc:gmock", "h:gmock/gmock.h"),
    "qt5": ("pc:Qt5Core",), "qt6": ("pc:Qt6Core",),
    "curses": ("pc:ncurses", "h:curses.h"),
    "gl": ("pc:gl", "h:GL/gl.h"), "glu": ("pc:glu", "h:GL/glu.h"),
    "zlib": ("pc:zlib", "h:zlib.h"), "bzip2": ("pc:bzip2", "h:bzlib.h"),
    "cups": ("prog:cups-config",), "llvm": ("prog:llvm-config",),
}


# ---------------------------------------------------------------------------
# Reading the build directory
# ---------------------------------------------------------------------------
def find_build_dirs(root: str) -> "list[str]":
    found = []
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if not d.startswith(".")]
        if os.path.isdir(os.path.join(dp, "meson-info")) and os.path.isfile(os.path.join(dp, "meson-logs", "meson-log.txt")):
            found.append(dp)
            dn[:] = []
    return sorted(found)


def load_json(path: str):
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return None


def parse_log(path: str) -> "list[dict]":
    seen: "dict[tuple, dict]" = {}
    try:
        lines = open(path, errors="replace").read().splitlines()
    except OSError:
        return []
    for line in lines:
        for kind, rx in LOG_PATTERNS:
            m = rx.match(line)
            if not m:
                continue
            g = m.groups()
            if kind in ("skipped", "program_skipped"):
                rec = dict(kind="program" if kind == "program_skipped" else "dependency", name=g[0], found=None,
                           detail="skipped: " + g[1].strip())
            else:
                k = "header" if kind == "header_symbol" else kind
                rec = dict(kind=k, name=g[0], found=g[1] == "YES", detail=g[2].strip())
            seen[(rec["kind"], rec["name"])] = rec          # last result wins
            break
    return list(seen.values())


class Facts:
    def __init__(self, build_dir: str):
        self.build_dir = os.path.abspath(build_dir)
        self.libs: "dict[str, dict]" = {}
        self.programs: "dict[str, set]" = {}
        self.includes: "dict[str, set]" = {}
        self.roots = [self.build_dir]

    def project_path(self, p: str) -> bool:
        return any(p == r or p.startswith(r.rstrip("/") + "/") for r in self.roots)

    def add_program(self, tok: str, where: str) -> None:
        if not tok or tok.startswith(("-", "$")) or "=" in os.path.basename(tok):
            return
        if tok.startswith(("./", "../")) or (os.path.isabs(tok) and self.project_path(tok)):
            return
        self.programs.setdefault(os.path.basename(tok), set()).add(where)

    def add_lib(self, tok: str, target: str) -> None:
        name = path = None
        static = False
        if tok.startswith("-l") and len(tok) > 2 and not tok.startswith("-l:"):
            name = tok[2:]
        elif os.path.isabs(tok) and not self.project_path(tok):
            m = LIB_FILE_RE.match(os.path.basename(tok))
            if m:
                name, path, static = m.group(1), tok, m.group(2) == "a"
        if name:
            e = self.libs.setdefault(name, {"targets": set(), "paths": set(), "static": False})
            e["targets"].add(target)
            if path:
                e["paths"].add(path)
            e["static"] = e["static"] or static

    def add_flags(self, toks: "list[str]", target: str) -> None:
        for i, t in enumerate(toks):
            inc = None
            if t in ("-I", "-isystem", "-iquote") and i + 1 < len(toks):
                inc = toks[i + 1]
            elif t.startswith("-isystem") and len(t) > 8:
                inc = t[8:]
            elif t.startswith("-I") and len(t) > 2:
                inc = t[2:]
            if inc and os.path.isabs(inc) and not self.project_path(inc):
                self.includes.setdefault(inc, set()).add(target)


def _split(v: str) -> "list[str]":
    try:
        return shlex.split(v)
    except ValueError:
        return v.split()


def collect_ninja(f: Facts) -> None:
    path = os.path.join(f.build_dir, "build.ninja")
    if not os.path.isfile(path):
        return
    target, in_build, rule = None, False, None
    for raw in open(path, errors="replace"):
        line = raw.rstrip("\n")
        if line.startswith("rule "):
            rule, in_build = line[5:].strip(), False
        elif line.startswith("build "):
            m = re.match(r"build\s+(\S+?)(?::|\s)", line)
            target, in_build = (os.path.basename(m.group(1)) if m else "?"), True
        elif line[:1] in (" ", "\t"):
            k, _, v = line.strip().partition(" = ")
            if not in_build and k == "command":
                for part in re.split(r"\s*(?:&&|;)\s*", v):
                    toks = _split(part)
                    if toks and not toks[0].startswith("$") and toks[0] not in ("rm", "cd", "true", "cp", "mv"):
                        f.add_program(toks[0], rule or "rule")
            elif in_build and k in ("LINK_ARGS", "LINK_LIBRARIES"):
                toks = _split(v)
                if toks and toks[0] in ("csrDT", "csr"):
                    continue                                   # an archiver's flags, not libraries
                for t in toks:
                    f.add_lib(t, target or "?")
            elif in_build and k == "ARGS":
                f.add_flags(_split(v), target or "?")


def collect_compile_commands(f: Facts) -> None:
    data = load_json(os.path.join(f.build_dir, "compile_commands.json"))
    for e in data or []:
        argv = e.get("arguments") or (_split(e["command"]) if e.get("command") else [])
        if argv:
            f.add_program(argv[0], "compile")
            f.add_flags(argv[1:], "compile")


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



GLIBC_LIBS = {"dl", "rt", "pthread", "util", "m", "c", "resolv", "anl"}


def fix_glibc_lib(name: str, pkg: str, inst: str, note: str) -> "tuple[str, str, str]":
    """libdl/librt/libpthread live inside libc since glibc 2.34 (only a stub .a remains), so the
    'no unversioned dev symlink' warning is wrong for them: libc6-dev is all you need."""
    if name in GLIBC_LIBS and (pkg.startswith("libc6") or "no unversioned dev symlink" in note):
        return "libc6-dev", "yes" if inst != "no" else inst, f"lib{name} is part of glibc (libc6-dev)"
    return pkg, inst, note


def narrow_header(name: str, row: dict) -> dict:
    """'xxhash.h' is shipped by 15 packages; exactly one is named after it."""
    stem = re.sub(r"[^a-z0-9]", "", os.path.splitext(os.path.basename(name))[0].lower())
    if len(stem) < 3:
        return row
    try:
        pkgs = list(cc.search_contents_files(name, False)) or list(cc._local_package_lookup(name, False))
    except Exception:
        return row
    hits = [p for p in pkgs if stem in re.sub(r"[^a-z0-9]", "", p.lower()) and p.endswith("-dev")]
    if len(hits) == 1:
        row = dict(row, package=hits[0], note=f"{len(pkgs)} packages ship a file named this; picked the one named after the header")
    return row


# ---------------------------------------------------------------------------
# Resolving
# ---------------------------------------------------------------------------
def owner_of(path: str) -> "tuple[str, str] | None":
    if os.path.exists(path):
        pkgs = cc._local_package_lookup(path, True)
        if pkgs:
            return (pkgs[0] if len(pkgs) == 1 else cc._format_ambiguous(list(pkgs)), "yes")
    return None


def resolve_tags(tags: "list[str]") -> "tuple[str, str, str, str]":
    """-> (package, installed, note, tag-used)"""
    tried = []
    for tag in tags:
        kind, _, val = tag.partition(":")
        if kind == "none":
            return "-", "yes", val, tag
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
            return r[0], r[1], r[2], tag
    return "?", "unknown", ("no match for " + ", ".join(tried)) if tried else "", ""


def lookup_row(rec: dict) -> dict:
    name, kind = rec["name"], rec["kind"]
    if kind == "dependency":
        tags = list(DEP_OVERRIDES.get(name, (f"pc:{name}", f"prog:{name}-config")))
    elif kind == "library":
        tags = [f"lib:{name}"]
    elif kind == "header":
        tags = [f"h:{name}"]
    else:
        tags = [f"prog:{name}"]
    pkg, inst, note, via = resolve_tags(tags)
    if kind == "program" and rec["found"]:
        m = re.match(r"\((.+)\)", rec["detail"])
        if m and os.path.isabs(m.group(1)):
            own = owner_of(m.group(1))
            if own:
                pkg, inst, note = own[0], own[1], ""
    if via and not via.startswith("none:") and kind == "dependency":
        note = (f"via {via}" + (f"; {note}" if note else ""))
    if rec["found"] is None:
        result = rec["detail"]
        inst = inst if inst else "-"
    elif rec["found"]:
        ver = re.match(r"^([0-9][^\s(]*)", rec["detail"])
        result = "found" + (f" {ver.group(1)}" if ver and kind == "dependency" else "")
    else:
        result = "NOT FOUND"
        if inst == "yes":
            note = (note + "; " if note else "") + "package is installed but meson did not find it (check -dev package / pkg-config path)"
    chk = Check(name=name, type="pkgconfig" if via.startswith("pc:") else "generic", guard="UNCONDITIONAL")
    have = cc.have_cell(Check(name=via.split(":", 1)[1] if via.startswith("pc:") else name,
                              type="pkgconfig" if via.startswith("pc:") else "generic", guard="UNCONDITIONAL"),
                        pkg) if pkg not in ("?", "-") else "-"
    if kind == "library":
        pkg, inst, note = fix_glibc_lib(name, pkg, inst, note)
    if kind == "header":
        own = system_include_owner(name)
        if own:
            pkg, inst, note = own
            have = cc.have_cell(Check(name=name, type="generic", guard="UNCONDITIONAL"), pkg.split(" ")[0]) \
                if inst == "yes" else "-"
            if rec["found"] is False and inst == "yes":
                note = "package is installed but meson did not find it (check the include path)"
        else:
            inst = inst
    row = dict(kind=kind, name=name, result=result, package=pkg, installed=inst, have=have, note=note)
    if kind == "header" and note.startswith("ambiguous"):
        row = narrow_header(name, row)
    return row


def build_report(build_dir: str) -> dict:
    f = Facts(build_dir)
    info = os.path.join(build_dir, "meson-info")
    proj = load_json(os.path.join(info, "intro-projectinfo.json")) or {}
    comps = load_json(os.path.join(info, "intro-compilers.json")) or {}
    for d in (load_json(os.path.join(info, "intro-dependencies.json")) or []):
        for t in d.get("link_args", []):
            f.add_lib(t, d.get("name", "?"))
    collect_ninja(f)
    collect_compile_commands(f)
    for side in comps.values():
        for lang, c in side.items():
            if c.get("exelist"):
                f.add_program(c["exelist"][0], f"{lang} compiler")
    lookups = parse_log(os.path.join(build_dir, "meson-logs", "meson-log.txt"))
    lookups = [r for r in lookups if not (r["kind"] == "program" and ("/" in r["name"] or r["name"].endswith((".py", ".sh", ".pl"))
                                                                     and not r["found"] is False))]
    for r in lookups:
        if r["kind"] == "program" and r["found"]:
            m = re.match(r"\((.+)\)", r["detail"])
            if m:
                f.add_program(m.group(1), "configure")

    terms = [t for t in map(cc.contents_term_for_library, f.libs) if t]
    terms += [f"bin/{p}" for p in sorted(f.programs) if not cc.shutil.which(p)]
    for r in lookups:
        if r["kind"] == "dependency":
            terms += [f"{r['name']}.pc"]
        elif r["kind"] == "header":
            terms.append(r["name"])
        elif r["kind"] == "library":
            t = cc.contents_term_for_library(r["name"])
            if t:
                terms.append(t)
        elif r["kind"] == "program" and not r["found"]:
            terms.append(f"bin/{r['name']}")
    cc.prefetch_contents(terms)
    cc.prefetch_contents([p for r in lookups if r["kind"] == "header" for p in system_include_paths(r["name"])],
                         exact=True)

    lib_rows = []
    for name in sorted(f.libs):
        e = f.libs[name]
        res = next((o for o in (owner_of(p) for p in sorted(e["paths"])) if o), None)
        if res:
            pkg, inst, note = res[0], res[1], "linked by full path: " + sorted(e["paths"])[0]
        else:
            pkg, inst, note = cc.resolve_library(name)
        pkg, inst, note = fix_glibc_lib(name, pkg, inst, note)
        if "no unversioned dev symlink" in note:
            dev = _glob.glob(f"/usr/lib/*/lib{name}.so") + _glob.glob(f"/usr/lib/lib{name}.so") + _glob.glob(f"/lib/*/lib{name}.so")
            own = owner_of(dev[0]) if dev else None
            if own:
                pkg, inst, note = own[0], "yes", f"dev file present ({dev[0]})"
        if e["static"]:
            note = (note + "; " if note else "") + "linked STATICALLY (.a) - prefer the shared library if one exists"
        lib_rows.append(dict(name=name, package=pkg, installed=inst, note=note,
                             have=cc.have_cell(Check(name=name, type="library", guard="UNCONDITIONAL"), pkg),
                             used_by=_targets(e["targets"])))

    prog_rows = []
    for name in sorted(f.programs):
        pkg, inst, note = cc.resolve_program(name)
        prog_rows.append(dict(name=name, package=pkg, installed=inst, note=note,
                              have=cc.have_cell(Check(name=name, type="generic", guard="UNCONDITIONAL"), pkg,
                                                resolved_prog_path=cc.shutil.which(name))))

    look_rows = [lookup_row(r) for r in lookups if r["kind"] != "program" or r["found"] is not True]
    look_rows += [lookup_row(r) for r in lookups if r["kind"] == "program" and r["found"] is True]
    inc_rows = [{"path": p, "exists": os.path.isdir(p), "used_by": _targets(t)} for p, t in sorted(f.includes.items())]
    return {"build_dir": build_dir, "project": proj.get("descriptive_name", ""), "version": proj.get("version", ""),
            "libraries": lib_rows, "programs": prog_rows, "lookups": look_rows, "includes": inc_rows}


def _targets(ts: set) -> str:
    ts = sorted(ts)
    return ", ".join(ts[:3]) + (f" (+{len(ts) - 3})" if len(ts) > 3 else "")


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def print_report(rep: dict, width: "int | None" = None) -> None:
    tw = width if width is not None else get_term_width()
    bar = "=" * min(78, tw)

    def section(title: str, cols: list, rows: list) -> None:
        print(bar)
        print(f" {title}")
        print(bar)
        print(render_table(cols, rows, width=tw))
        if not rows:
            print("  (none found)")
        print()

    std = [Column("PACKAGE", "package", min_width=14, flexible=True, shrink_priority=2),
           Column("INST?", "installed", min_width=5),
           Column("HAVE", "have", min_width=10, flexible=True, shrink_priority=3),
           Column("NOTE", "note", min_width=0, flexible=True, shrink_priority=0)]
    print(f"Parsed: {rep['build_dir']}  ({rep['project']} {rep['version']})".rstrip())
    print()
    section("Configure-time lookups  (dependency() / find_library() / find_program() / header checks, from meson-log.txt)",
            [Column("KIND", "kind", min_width=6),
             Column("NAME", "name", min_width=8, flexible=True, shrink_priority=1),
             Column("RESULT", "result", min_width=8, flexible=True, shrink_priority=4)] + std,
            rep["lookups"])
    section("Libraries linked", [Column("LIBRARY", "name", min_width=8, flexible=True, shrink_priority=1),
                                 Column("USED BY", "used_by", min_width=8, flexible=True, shrink_priority=4)] + std,
            rep["libraries"])
    section("Programs invoked by the build", [Column("PROGRAM", "name", min_width=8, flexible=True, shrink_priority=1)] + std,
            rep["programs"])
    section("-I / -isystem include paths outside the project (informational - not package-resolved)",
            [Column("PATH", "path", min_width=12, flexible=True, shrink_priority=0),
             Column("EXISTS HERE?", "exists_str", min_width=12),
             Column("USED BY", "used_by", min_width=8, flexible=True, shrink_priority=1)],
            [{"path": i["path"], "exists_str": "yes" if i["exists"] else "no", "used_by": i["used_by"]}
             for i in rep["includes"]])
    print("Note: this reads what meson decided when it ran. A NOT FOUND above means a dependency was absent")
    print("at configure time - install the package, then `meson setup --reconfigure builddir`")
    print("(or `meson configure builddir -Dname=enabled` to turn a feature on). See configcheck-meson.py")
    print("for the options each lookup belongs to.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Read a configured Meson build directory and map what it looked for, "
                                             "links and invokes to Debian packages.")
    ap.add_argument("path", help="a Meson build directory (contains meson-info/ and meson-logs/), or a directory "
                                 "to search for build directories")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of the text report")
    ap.add_argument("-o", "--output", metavar="FILE",
                    help="write the report to FILE instead of the terminal (columns are never truncated)")
    args = ap.parse_args()

    if not os.path.isdir(args.path):
        print(f"error: no such directory: {args.path}", file=sys.stderr)
        return 1
    dirs = find_build_dirs(args.path)
    if not dirs:
        print(f"error: no configured Meson build directory under {args.path}", file=sys.stderr)
        print("(looked for meson-info/ and meson-logs/meson-log.txt). Run `meson setup builddir` first, or use "
              "configcheck-meson.py on the source tree before configuring.", file=sys.stderr)
        return 1

    print(cc.describe_contents_files(), file=sys.stderr)
    reports = [build_report(d) for d in dirs]

    def emit() -> None:
        if args.json:
            print(json.dumps(reports[0] if len(reports) == 1 else {"builds": reports}, indent=2))
            return
        for i, r in enumerate(reports):
            if i:
                print()
            print_report(r, width=1000 if args.output else None)

    if args.output:
        with open(args.output, "w") as fh, contextlib.redirect_stdout(fh):
            emit()
        print(f"Report written to {args.output}")
    else:
        emit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
