#!/usr/bin/env python3
"""
makecheck-cmake.py - Read a CONFIGURED CMake build directory (the one you
ran `cmake ..` in) and report what the build actually links against,
invokes, and looked up - mapped to Debian packages.

The CMake counterpart of makecheck.py. Part of deb-buildcheck-suite. It only
IMPORTS configcheck.py (package lookup, Contents scanning, table rendering)
and never modifies it, so nothing here can affect the autotools tools.
Must live in the same directory as configcheck.py.

Reads, whichever exist: CMakeCache.txt, CMakeFiles/*.dir/link.txt and
flags.make (Unix Makefiles generator), build.ninja (Ninja generator) and
compile_commands.json.

Usage: ./makecheck-cmake.py /path/to/build-dir [--json] [-o FILE]
"""

import argparse
import contextlib
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
    print("error: configcheck.py not found - makecheck-cmake.py shares its package/library",
          file=sys.stderr)
    print("resolution and table code with it, so both files need to be in the same directory.",
          file=sys.stderr)
    sys.exit(1)

ARCHIVERS = {"ar", "ranlib", "llvm-ar", "gcc-ar", "gcc-ranlib"}
LIB_FILE_RE = re.compile(r"^lib(.+?)\.(so(?:\.[0-9.]+)?|a)$")
CACHE_RE = re.compile(r"^([^#/:=\s][^:=]*):([A-Z]+)=(.*)$")
SEP_RE = re.compile(r"\s*(?:&&|;|\|\|)\s*")


# ---------------------------------------------------------------------------
# Finding the build directory and its files
# ---------------------------------------------------------------------------
def find_build_dirs(root: str) -> "list[str]":
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "CMakeFiles"]
        if "CMakeCache.txt" in filenames:
            found.append(dirpath)
            dirnames[:] = []                 # a build dir doesn't nest another one worth reporting
    return sorted(found)


def read_cache(path: str) -> "dict[str, tuple[str, str]]":
    out = {}
    try:
        for line in open(path, errors="replace"):
            m = CACHE_RE.match(line.rstrip("\n"))
            if m:
                out[m.group(1)] = (m.group(2), m.group(3))
    except OSError:
        pass
    return out


def split_commands(line: str) -> "list[list[str]]":
    cmds = []
    for part in SEP_RE.split(line.strip()):
        if not part:
            continue
        try:
            cmds.append(shlex.split(part))
        except ValueError:
            cmds.append(part.split())
    return cmds


# ---------------------------------------------------------------------------
# Collecting facts. Everything lands in one Facts object so the three
# generators (Makefiles, Ninja, compile_commands.json) feed the same report.
# ---------------------------------------------------------------------------
class Facts:
    def __init__(self, build_dir: str, cache: dict):
        self.build_dir = os.path.abspath(build_dir)
        self.cache = cache
        self.libs: "dict[str, dict]" = {}       # name -> {"targets": set, "paths": set, "static": bool}
        self.programs: "dict[str, set]" = {}    # basename -> where seen
        self.includes: "dict[str, set]" = {}
        self.sources: "list[str]" = []
        self.project_roots = [self.build_dir]
        home = cache.get("CMAKE_HOME_DIRECTORY", ("", ""))[1]
        cdir = cache.get("CMAKE_CACHEFILE_DIR", ("", ""))[1]
        self.project_roots += [p for p in (home, cdir) if p]

    def is_project_path(self, p: str) -> bool:
        return any(p == r or p.startswith(r.rstrip("/") + "/") for r in self.project_roots)

    def add_program(self, tok: str, where: str) -> None:
        if not tok or tok.startswith("-") or "=" in tok.split("/")[-1] or "$" in tok:
            return
        name = os.path.basename(tok)
        if not name or name in (".", ".."):
            return
        if tok.startswith(("./", "../")) or (os.path.isabs(tok) and self.is_project_path(tok)):
            return
        self.programs.setdefault(name, set()).add(where)

    def add_include(self, path: str, where: str) -> None:
        if path and not self.is_project_path(path):
            self.includes.setdefault(path, set()).add(where)

    def add_lib_token(self, tok: str, target: str) -> None:
        name = path = None
        static = False
        if tok.startswith("-l") and len(tok) > 2 and not tok.startswith("-l:"):
            name = tok[2:]
        elif tok.endswith((".o", ".obj", ".lo")) or tok.startswith(("-", "$", "@")):
            return
        elif os.path.isabs(tok) and LIB_FILE_RE.match(os.path.basename(tok)) and not self.is_project_path(tok):
            m = LIB_FILE_RE.match(os.path.basename(tok))
            name, path, static = m.group(1), tok, m.group(2) == "a"
        if not name:
            return
        e = self.libs.setdefault(name, {"targets": set(), "paths": set(), "static": False})
        e["targets"].add(target)
        if path:
            e["paths"].add(path)
        e["static"] = e["static"] or static

    def scan_flags(self, toks: "list[str]", target: str) -> None:
        for i, t in enumerate(toks):
            if t in ("-I", "-isystem", "-iquote") and i + 1 < len(toks):
                self.add_include(toks[i + 1], target)
            elif t.startswith("-isystem") and len(t) > 8:
                self.add_include(t[8:], target)
            elif t.startswith("-I") and len(t) > 2:
                self.add_include(t[2:], target)

    def scan_command(self, argv: "list[str]", target: str) -> None:
        if not argv:
            return
        if argv[0] in ("cd", "cmake", "/usr/bin/cmake") and len(argv) > 1 and argv[1] == "-E":
            return
        self.add_program(argv[0], target)
        if os.path.basename(argv[0]) in ARCHIVERS:
            return
        self.scan_flags(argv[1:], target)
        i = 1
        while i < len(argv):
            t = argv[i]
            if t.startswith("-l") or LIB_FILE_RE.match(os.path.basename(t)):
                self.add_lib_token(t, target)
            i += 1


def collect_makefile_generator(f: Facts) -> None:
    for dirpath, dirnames, filenames in os.walk(f.build_dir):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        base = os.path.basename(dirpath)
        if not base.endswith(".dir"):
            continue
        target = base[:-4]
        if "link.txt" in filenames:
            f.sources.append(os.path.join(dirpath, "link.txt"))
            for line in open(os.path.join(dirpath, "link.txt"), errors="replace"):
                for argv in split_commands(line):
                    f.scan_command(argv, target)
        if "flags.make" in filenames:
            f.sources.append(os.path.join(dirpath, "flags.make"))
            for line in open(os.path.join(dirpath, "flags.make"), errors="replace"):
                m = re.match(r"^#\s*compile \w+ with (\S+)", line)
                if m:
                    f.add_program(m.group(1), target)
                    continue
                if re.match(r"^\w+_(INCLUDES|FLAGS)\s*=", line):
                    f.scan_flags(split_commands(line.split("=", 1)[1])[0] if line.split("=", 1)[1].strip() else [],
                                 target)


def collect_ninja(f: Facts) -> None:
    path = os.path.join(f.build_dir, "build.ninja")
    if not os.path.isfile(path):
        return
    f.sources.append(path)
    cur_rule = None
    cur_target = None
    in_build = False
    for raw in open(path, errors="replace"):
        line = raw.rstrip("\n")
        if line.startswith("rule "):
            cur_rule, in_build = line[5:].strip(), False
        elif line.startswith("build "):
            m = re.match(r"build\s+(\S+?)(?:\s|:)", line)
            cur_target, in_build = (os.path.basename(m.group(1)) if m else "?"), True
        elif line.startswith(("  ", "\t")):
            k, _, v = line.strip().partition("=")
            k, v = k.strip(), v.strip()
            if not in_build and k == "command":
                for argv in split_commands(v):
                    if argv and not argv[0].startswith("$"):
                        f.add_program(argv[0], cur_rule or "rule")
            elif in_build and k in ("LINK_LIBRARIES", "LINK_FLAGS"):
                for t in (shlex.split(v) if v else []):
                    f.add_lib_token(t, cur_target or "?")
            elif in_build and k in ("INCLUDES", "FLAGS"):
                try:
                    f.scan_flags(shlex.split(v), cur_target or "?")
                except ValueError:
                    f.scan_flags(v.split(), cur_target or "?")


def collect_compile_commands(f: Facts) -> None:
    path = os.path.join(f.build_dir, "compile_commands.json")
    if not os.path.isfile(path):
        return
    try:
        entries = json.load(open(path))
    except (OSError, ValueError):
        return
    f.sources.append(path)
    for e in entries:
        argv = e.get("arguments")
        if not argv and e.get("command"):
            argv = split_commands(e["command"])[0]
        if argv:
            f.add_program(argv[0], "compile")
            f.scan_flags(argv[1:], "compile")


def collect_cache_programs(f: Facts) -> None:
    for key in ("CMAKE_C_COMPILER", "CMAKE_CXX_COMPILER", "CMAKE_AR", "CMAKE_RANLIB", "CMAKE_LINKER",
                "CMAKE_MAKE_PROGRAM", "CMAKE_COMMAND", "PKG_CONFIG_EXECUTABLE", "CMAKE_NM", "CMAKE_STRIP",
                "CMAKE_OBJCOPY", "CMAKE_OBJDUMP", "CMAKE_ASM_COMPILER", "CMAKE_Fortran_COMPILER"):
        v = f.cache.get(key)
        if v and v[1] and not v[1].endswith("-NOTFOUND"):
            f.add_program(v[1], "CMakeCache.txt")
    for key, (typ, val) in f.cache.items():
        if re.search(r"_(EXECUTABLE|PROGRAM)$", key) and typ in ("FILEPATH", "PATH") and val \
                and not val.endswith("-NOTFOUND") and os.path.isabs(val) and not f.is_project_path(val):
            f.add_program(val, "CMakeCache.txt")


# ---------------------------------------------------------------------------
# Configure-time lookups recorded in CMakeCache.txt
# ---------------------------------------------------------------------------
def cache_lookups(f: Facts) -> "tuple[list[dict], list[dict]]":
    """(find_* results, pkg-config modules)"""
    finds = []
    for key, (typ, val) in sorted(f.cache.items()):
        if typ not in ("FILEPATH", "PATH"):
            continue
        if not re.search(r"(_LIBRARY|_LIBRARY_RELEASE|_LIBRARIES|_INCLUDE_DIR|_INCLUDE_DIRS|_DIR)$", key):
            continue
        if key.startswith(("CMAKE_", "_", "FETCHCONTENT_")) or key in ("CMAKE_INSTALL_INCLUDEDIR",):
            continue
        if key.endswith("_DIR") and not (val.endswith("-NOTFOUND") or "/cmake" in val.lower()):
            continue                          # *_DIR that isn't a package-config directory
        if not val:
            continue
        finds.append({"name": key, "value": val, "found": not val.endswith("-NOTFOUND")})

    mods = []
    checked = [k[len("__pkg_config_checked_"):] for k in f.cache if k.startswith("__pkg_config_checked_")]
    for prefix in sorted(checked):
        found = f.cache.get(f"{prefix}_FOUND", ("", ""))[1] not in ("", "0", "FALSE", "OFF") \
            or bool(f.cache.get(f"{prefix}_VERSION", ("", ""))[1])
        names = set()
        for k in f.cache:
            if k.startswith(prefix + "_"):
                m = re.match(r"^(.+)_(INCLUDEDIR|LIBDIR|PREFIX|VERSION|LDFLAGS|CFLAGS|LIBRARIES|LINK_LIBRARIES)$",
                             k[len(prefix) + 1:])
                if m and m.group(1) not in ("STATIC",):
                    names.add(m.group(1))
        for name in sorted(names) or [prefix]:
            ver = f.cache.get(f"{prefix}_{name}_VERSION", ("", ""))[1] or f.cache.get(f"{prefix}_VERSION", ("", ""))[1]
            mods.append({"name": name, "prefix": prefix, "found": found, "version": ver})
    return finds, mods



# ---------------------------------------------------------------------------
# Guessing the package behind a NOT FOUND cache entry. The cache only holds
# names like ALSA_LIBRARY-NOTFOUND, so we read the Find module that produced
# it (project-local FindX.cmake first, then cmake's own) and see what it was
# searching for. A deliberately small, regex-based reader - this tool keeps
# no code in common with configcheck-cmake.py so neither can break the other.
# ---------------------------------------------------------------------------
import glob as _glob

_FIND_CMD_RE = re.compile(r"\b(find_path|find_library|find_file|find_program|pkg_check_modules|pkg_search_module)"
                          r"\s*\(([^)]*)\)", re.I)
_STOP_WORDS = {"HINTS", "PATHS", "PATH_SUFFIXES", "DOC", "REQUIRED", "NO_DEFAULT_PATH", "QUIET", "NAMES_PER_DIR",
               "NO_CMAKE_PATH", "NO_CMAKE_ENVIRONMENT_PATH", "NO_SYSTEM_ENVIRONMENT_PATH", "NO_CMAKE_SYSTEM_PATH",
               "IMPORTED_TARGET", "NO_CMAKE_FIND_ROOT_PATH", "ENV", "VALIDATOR", "REGISTRY_VIEW"}


def _module_search_dirs(f: "Facts") -> "list[str]":
    dirs = []
    for root in dict.fromkeys(f.project_roots + [os.path.dirname(f.build_dir)]):
        for sub in ("cmake", "cmake/Modules", "CMake", "cmake/modules", "."):
            d = os.path.join(root, sub)
            if os.path.isdir(d):
                dirs.append(d)
    exe = cc.shutil.which("cmake")
    prefixes = ["/usr", "/usr/local"] + ([os.path.dirname(os.path.dirname(os.path.realpath(exe)))] if exe else [])
    for pre in prefixes:
        dirs += sorted(_glob.glob(os.path.join(pre, "share", "cmake-*", "Modules")), reverse=True)
    return list(dict.fromkeys(dirs))


def _find_module_file(prefix: str, dirs: "list[str]") -> "str | None":
    want = f"find{prefix}.cmake".lower()
    for d in dirs:
        try:
            for name in os.listdir(d):
                if name.lower() == want:
                    return os.path.join(d, name)
        except OSError:
            pass
    return None


def _module_searches(path: str) -> "list[tuple[str, str, list[str]]]":
    """[(command, variable-or-prefix, [names searched for])]"""
    try:
        text = open(path, errors="replace").read()
    except OSError:
        return []
    text = re.sub(r"#\[=*\[.*?\]=*\]", "", text, flags=re.S)
    text = re.sub(r"#[^\n]*", "", text)
    out = []
    for m in _FIND_CMD_RE.finditer(text):
        cmd = m.group(1).lower()
        try:
            toks = shlex.split(m.group(2))
        except ValueError:
            toks = m.group(2).split()
        if not toks:
            continue
        var, rest = toks[0], toks[1:]
        names: "list[str]" = []
        if cmd.startswith("pkg_"):
            for t in rest:
                mm = re.match(r"^([A-Za-z0-9][A-Za-z0-9+._-]*?)(?:[<>=!]=?.*)?$", t)
                if t not in _STOP_WORDS and mm and not re.match(r"^[<>=!]", t) and not re.match(r"^\d", t):
                    names.append(mm.group(1))
        else:
            if "NAMES" in rest:
                for t in rest[rest.index("NAMES") + 1:]:
                    if t in _STOP_WORDS:
                        break
                    names.append(t)
            elif rest and rest[0] not in _STOP_WORDS:
                names.append(rest[0])
        names = [n for n in names if "${" not in n and not n.endswith((".lib", ".dll", ".a"))]
        if names:
            out.append((cmd, var, names))
    return out


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
    A plain 'xxhash.h' would also match e.g. LLVM's private copy under /usr/lib/llvm-19/, which is not on that path."""
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


def resolve_header(h: str) -> "tuple[str, str, str]":
    """Exact default-include-path owner first; the old name-only search as a fallback (compiler-private headers
    like stdatomic.h live outside /usr/include)."""
    own = system_include_owner(h)
    return own if own else cc.resolve_package(h)


def guess_package(varname: str, f: "Facts", dirs: "list[str]") -> "tuple[str, str, str] | None":
    prefix = re.sub(r"_(LIBRARY(_RELEASE|_DEBUG)?|LIBRARIES|INCLUDE_DIRS?|INCLUDEDIR|DIR|EXECUTABLE|PROGRAM)$", "", varname)
    path = _find_module_file(prefix, dirs)
    if not path:
        return None
    searches = _module_searches(path)
    exact = [x for x in searches if x[1] == varname]
    chosen = exact or searches
    if exact and exact[0][0] == "find_library":
        # a library name on its own maps to the RUNTIME package; the header from the same module
        # points at the -dev package, which is what a build actually needs
        chosen = [x for x in searches if x[0] in ("find_path", "find_file")] + chosen
    tried = []
    for cmd, var, names in chosen:
        for n in names[:4]:
            if cmd in ("find_path", "find_file"):
                r = resolve_header(n)
            elif cmd == "find_library":
                r = cc.resolve_library(n)
            elif cmd == "find_program":
                r = cc.resolve_program(n)
            else:
                r = cc.resolve_package(f"{n}.pc")
            tried.append(n)
            if r[0] not in ("?", "-"):
                where = os.path.basename(path)
                return (r[0], r[1], f"from {where}: looks for {n}" + ("; " + r[2] if r[2] else ""))
    return None


# ---------------------------------------------------------------------------
# Resolving
# ---------------------------------------------------------------------------
def owner_of(path: str) -> "tuple[str, str] | None":
    if os.path.exists(path):
        pkgs = cc._local_package_lookup(path, True)
        if pkgs:
            return (pkgs[0] if len(pkgs) == 1 else cc._format_ambiguous(list(pkgs)), "yes")
    return None


def build_report(build_dir: str) -> dict:
    cache_path = os.path.join(build_dir, "CMakeCache.txt")
    cache = read_cache(cache_path)
    f = Facts(build_dir, cache)
    collect_makefile_generator(f)
    collect_ninja(f)
    collect_compile_commands(f)
    collect_cache_programs(f)
    finds, mods = cache_lookups(f)

    prefetch_terms = [t for t in map(cc.contents_term_for_library, f.libs) if t]
    prefetch_terms += [f"bin/{p}" for p in sorted(f.programs) if not cc.shutil.which(p)]
    prefetch_terms += [f"{m['name']}.pc" for m in mods]
    cc.prefetch_contents(prefetch_terms)

    lib_rows = []
    for name in sorted(f.libs):
        e = f.libs[name]
        res = None
        for p in sorted(e["paths"]):
            res = owner_of(p)
            if res:
                break
        if res:
            pkg, inst, note = res[0], res[1], "linked by full path: " + sorted(e["paths"])[0]
        else:
            pkg, inst, note = cc.resolve_library(name)
        if "no unversioned dev symlink" in note:
            # libm.so & co. are linker scripts/files, not symlinks, so configcheck's symlink test can misfire;
            # if the unversioned file is really there, the -dev package is installed
            import glob as _glob
            dev = (_glob.glob(f"/usr/lib/*/lib{name}.so") + _glob.glob(f"/usr/lib/lib{name}.so")
                   + _glob.glob(f"/lib/*/lib{name}.so"))
            own = owner_of(dev[0]) if dev else None
            if own:
                pkg, inst, note = own[0], "yes", f"dev file present ({dev[0]})"
        if e["static"]:
            note = (note + "; " if note else "") + "linked STATICALLY (.a) - prefer the shared library if one exists"
        check = Check(name=name, type="library", guard="UNCONDITIONAL")
        lib_rows.append(dict(name=name, package=pkg, installed=inst, note=note, want="-",
                             have=cc.have_cell(check, pkg), used_by=_targets(e["targets"])))

    prog_rows = []
    for name in sorted(f.programs):
        pkg, inst, note = cc.resolve_program(name)
        check = Check(name=name, type="generic", guard="UNCONDITIONAL")
        prog_rows.append(dict(name=name, package=pkg, installed=inst, note=note, want="-",
                              have=cc.have_cell(check, pkg, resolved_prog_path=cc.shutil.which(name))))

    module_dirs = _module_search_dirs(f)
    # one Contents pass for every header the not-found lookups will ask about
    hdrs: "list[str]" = []
    for fr in finds:
        if not fr["found"]:
            prefix = re.sub(r"_(LIBRARY(_RELEASE|_DEBUG)?|LIBRARIES|INCLUDE_DIRS?|INCLUDEDIR|DIR|EXECUTABLE|PROGRAM)$",
                            "", fr["name"])
            path = _find_module_file(prefix, module_dirs)
            for cmd, var, names in (_module_searches(path) if path else []):
                if cmd in ("find_path", "find_file"):
                    hdrs += names[:4]
    cc.prefetch_contents([p for h in hdrs for p in system_include_paths(h)], exact=True)
    find_rows = []
    for fr in finds:
        if fr["found"]:
            vals = [v for v in re.split(r"(?<!\\);", fr["value"]) if v and os.path.isabs(v)]
            res = next((r for r in (owner_of(v) for v in vals) if r), None)
            if res:
                pkg, inst, note = res[0], res[1], ""
            else:
                pkg, inst, note = "?", "unknown", ("not on this machine" if vals and not os.path.exists(vals[0])
                                                  else "not owned by any installed package")
            result = fr["value"]
        else:
            pkg, inst, note = "?", "no", "NOT FOUND when cmake ran - the dev package for this is probably missing"
            g = guess_package(fr["name"], f, module_dirs)
            if g:
                pkg, inst, note = g[0], "no", "NOT FOUND when cmake ran; " + g[2]
            result = "NOT FOUND"
        find_rows.append(dict(name=fr["name"], result=result, package=pkg, installed=inst, note=note))

    mod_rows = []
    for m in mods:
        pkg, inst, note = cc.resolve_package(f"{m['name']}.pc")
        check = Check(name=m["name"], type="pkgconfig", guard="UNCONDITIONAL")
        mod_rows.append(dict(name=m["name"], package=pkg, installed=inst,
                             result=("found " + m["version"]).strip() if m["found"] else "NOT FOUND",
                             note=note, want="-", have=cc.have_cell(check, pkg)))

    inc_rows = [{"path": p, "exists": os.path.isdir(p) or os.path.isfile(p), "used_by": _targets(t)}
                for p, t in sorted(f.includes.items())]

    gen = cache.get("CMAKE_GENERATOR", ("", "unknown"))[1]
    ver = ".".join(cache.get(f"CMAKE_CACHE_{k}_VERSION", ("", ""))[1] for k in ("MAJOR", "MINOR", "PATCH")
                   if cache.get(f"CMAKE_CACHE_{k}_VERSION", ("", ""))[1])
    return {"build_dir": build_dir, "generator": gen, "cmake_version": ver, "sources": f.sources,
            "libraries": lib_rows, "programs": prog_rows, "lookups": find_rows, "pkgconfig": mod_rows,
            "includes": inc_rows}


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
    print(f"Parsed: {rep['build_dir']}  (generator: {rep['generator']}, cmake {rep['cmake_version'] or '?'})")
    print()
    section("Libraries linked", [Column("LIBRARY", "name", min_width=8, flexible=True, shrink_priority=1),
                                 Column("USED BY", "used_by", min_width=8, flexible=True, shrink_priority=4)] + std,
            rep["libraries"])
    section("Programs invoked", [Column("PROGRAM", "name", min_width=8, flexible=True, shrink_priority=1)] + std,
            rep["programs"])
    section("pkg-config modules queried at configure time",
            [Column("MODULE", "name", min_width=8, flexible=True, shrink_priority=1),
             Column("RESULT", "result", min_width=8, flexible=True, shrink_priority=4)] + std,
            rep["pkgconfig"])
    section("Other configure-time lookups (CMakeCache.txt find_library / find_path / find_package results)",
            [Column("VARIABLE", "name", min_width=8, flexible=True, shrink_priority=1),
             Column("RESULT", "result", min_width=8, flexible=True, shrink_priority=4)] +
            [c for c in std if c.key != "have"], rep["lookups"])
    inc_rows = [{"path": i["path"], "exists_str": "yes" if i["exists"] else "no", "used_by": i["used_by"]}
                for i in rep["includes"]]
    section("-I / -isystem include paths outside the project (informational - not package-resolved)",
            [Column("PATH", "path", min_width=12, flexible=True, shrink_priority=0),
             Column("EXISTS HERE?", "exists_str", min_width=12),
             Column("USED BY", "used_by", min_width=8, flexible=True, shrink_priority=1)], inc_rows)
    print("Note: this reads what cmake decided when it ran. A NOT FOUND above means a dependency was absent")
    print("at configure time. After installing the package, configure again from scratch:")
    print("    cmake -S <source-dir> -B <build-dir> --fresh        (or delete CMakeCache.txt first)")
    print("To enable or disable a feature while configuring, pass its option with ON or OFF:")
    print("    cmake -S <source-dir> -B <build-dir> -DNAME=ON      (or -DNAME=OFF)")
    print("See configcheck-cmake.py for which option each lookup belongs to.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Read a configured CMake build directory and map what it links/invokes/"
                                             "looked up to Debian packages.")
    ap.add_argument("path", help="a CMake build directory (contains CMakeCache.txt), or a directory to search "
                                 "for build directories")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of the text report")
    ap.add_argument("-o", "--output", metavar="FILE",
                    help="write the report to FILE instead of the terminal (columns are never truncated)")
    args = ap.parse_args()

    if os.path.isfile(args.path) and os.path.basename(args.path) == "CMakeCache.txt":
        args.path = os.path.dirname(os.path.abspath(args.path))
    if not os.path.isdir(args.path):
        print(f"error: no such directory: {args.path}", file=sys.stderr)
        return 1
    dirs = find_build_dirs(args.path)
    if not dirs:
        print(f"error: no CMakeCache.txt under {args.path} - this tool reads a CONFIGURED build directory.",
              file=sys.stderr)
        print("", file=sys.stderr)
        print("To configure one (then run this tool on the build directory):", file=sys.stderr)
        print("    cmake -S <source-dir> -B <build-dir>                 (add --fresh to start from scratch)", file=sys.stderr)
        print("To enable or disable a feature while configuring, pass its option with ON or OFF:", file=sys.stderr)
        print("    cmake -S <source-dir> -B <build-dir> -DNAME=ON       (or -DNAME=OFF)", file=sys.stderr)
        print("Use configcheck-cmake.py on the source tree to see the options and what each one needs", file=sys.stderr)
        print("- no configure step required.", file=sys.stderr)
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
