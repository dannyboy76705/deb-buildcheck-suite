#!/usr/bin/env python3
"""
runcheck.py - trace a running program (compiled ELF, bash, python, etc.)
to find what it actually links to / invokes at RUNTIME, and map each to
a Debian package - including dependencies that only show up once you
exercise a particular code path (a conditionally dlopen()'d plugin, an
optional backend) that a static build-time scan (configcheck.py /
makecheck.py) can never see.

Part of deb-buildcheck-suite. Shares its rendering and subprocess
helpers with configcheck.py (must be in the same directory).

Usage: ./runcheck.py <program> [args...]
"""

import contextlib
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

try:
    from configcheck import Column, render_table, get_term_width, _run, HAVE_DPKG, _fix_merged_usr, resolve_package, describe_contents_files, prefetch_contents
except ModuleNotFoundError:
    print("error: configcheck.py not found - runcheck.py shares its rendering and",
          file=sys.stderr)
    print("subprocess helpers with it, so both files need to be in the same directory.",
          file=sys.stderr)
    sys.exit(1)


HELP_TEXT = """\
runcheck - trace a running program to find its runtime dependencies

Usage: runcheck.py <program> [args...]

Run it exactly the way you'd normally invoke the program, including any
arguments it needs. While it's running, use it normally - exercise
different inputs, different code paths, whatever's relevant - so runcheck
can catch dependencies that don't show up until they're actually needed
(a conditionally-loaded plugin, an optional backend). Every new shared
library or subprocess it touches is announced on screen as it's found.

Press Ctrl+C or let the program exit normally when you're done. A
deduplicated summary of every library/program and its Debian package
prints to the screen at that point - a solid first draft of a Debian
package's Run-Depends.

Options:
  -o FILE       also save the summary report to FILE (the on-screen
                summary is always printed too - never suppressed)
  --raw FILE    also save the raw strace log to FILE, for when the
                summary misses something and you need to check by hand
  --json        emit the summary as JSON instead of a text report

Example:
  runcheck.py lame test.wav
  runcheck.py ./myscript.sh
"""

# ---------------------------------------------------------------------------
# Detection is informational only (printed up front) - strace traces an ELF
# binary or a script's interpreter process the same way either way, so
# nothing downstream actually branches on this.
# ---------------------------------------------------------------------------
def detect_kind(path: str) -> str:
    try:
        with open(path, "rb") as f:
            head = f.read(4)
    except OSError:
        return "unknown"
    if head == b"\x7fELF":
        return "elf"
    try:
        with open(path, "r", errors="replace") as f:
            first_line = f.readline()
    except OSError:
        return "unknown"
    if first_line.startswith("#!"):
        interpreter = first_line[2:].strip()
        tokens = interpreter.split()
        if tokens and os.path.basename(tokens[0]) == "env" and len(tokens) > 1:
            interpreter = tokens[1]
        elif tokens:
            interpreter = tokens[0]
        return f"script ({os.path.basename(interpreter)})"
    return "unknown"


# ---------------------------------------------------------------------------
# strace -f output parsing. A line looks like:
#   12345 openat(AT_FDCWD, "/usr/lib/x86_64-linux-gnu/libz.so.1", O_RDONLY|O_CLOEXEC) = 3
#   12346 execve("/usr/bin/lame", ["lame", "test.wav"], 0x7ffee...) = 0
# We only care about openat calls that succeeded (retval >= 0) against a
# path that looks like a shared library, and execve calls (a subprocess
# actually invoked) - both give us an exact, already-resolved absolute
# path, which is what makes package resolution here simpler and more
# precise than configcheck.py's name-guessing approach: dpkg -S on a real
# path is unambiguous, and the multiarch directory in the path itself
# already encodes i386 vs amd64 - no need to reimplement multilib logic.
# ---------------------------------------------------------------------------
LIB_PATH_RE = re.compile(r'/\S+\.so(?:\.[0-9]+)*$')
OPENAT_RE = re.compile(r'openat\([^,]*,\s*"([^"]+)"[^)]*\)\s*=\s*(-?\d+)')
EXECVE_RE = re.compile(r'execve\("([^"]+)"')


def parse_strace_log(path: str) -> "tuple[set, set, set]":
    """Returns (opened_libraries, programs, failed_basenames).
    failed_basenames is every basename that had at least one ENOENT-style
    failed open - the caller cross-references this against
    opened_libraries afterward, since ld.so routinely fails on several
    candidate directories before succeeding on the real one; only a
    basename that NEVER succeeds anywhere in the whole trace is a genuine
    problem worth reporting."""
    libraries: set = set()
    programs: set = set()
    failed_basenames: set = set()
    try:
        with open(path, "r", errors="replace") as f:
            for line in f:
                m = OPENAT_RE.search(line)
                if m:
                    p, retval = m.group(1), int(m.group(2))
                    if LIB_PATH_RE.match(p):
                        if retval >= 0:
                            libraries.add(p)
                        else:
                            failed_basenames.add(os.path.basename(p))
                    continue
                m = EXECVE_RE.search(line)
                if m:
                    programs.add(m.group(1))
    except OSError:
        pass
    return libraries, programs, failed_basenames


# A trace of something like vlc touches hundreds of files, and every
# `dpkg -S` / `dpkg-query` call re-reads dpkg's whole database (~130ms and
# ~30ms on a typical install) - one call per file was ~26s of a 32s run.
# Both commands accept many arguments, and `dpkg -S` prints each path next
# to its owner, so the whole set is resolved with a handful of calls.
_DPKG_BATCH = 300                                 # args per call, far below ARG_MAX
_DPKG_S_LINE_RE = re.compile(r'^(.*?): (/.*)$')   # "pkg[:arch][, pkg...]: /path"


def _dpkg_owners(paths: "list[str]") -> "dict[str, str]":
    """path -> owning package (first one listed), for each path dpkg knows."""
    owners: "dict[str, str]" = {}
    wanted = set(paths)
    for i in range(0, len(paths), _DPKG_BATCH):
        for line in _run(["dpkg", "-S", *paths[i:i + _DPKG_BATCH]], timeout=60):
            m = _DPKG_S_LINE_RE.match(line)
            if m and m.group(2) in wanted:
                owners.setdefault(m.group(2), m.group(1).split(":", 1)[0])
    return owners


def _package_versions(packages: "set[str]") -> "dict[str, str]":
    versions: "dict[str, str]" = {}
    names = sorted(packages)
    for i in range(0, len(names), _DPKG_BATCH):
        cmd = ["dpkg-query", "-W", "-f=${Package}\t${Version}\n", *names[i:i + _DPKG_BATCH]]
        for line in _run(cmd, timeout=60):
            name, _, version = line.partition("\t")
            if version.strip():
                versions.setdefault(name, version.strip())
    return versions


def resolve_paths_to_packages(paths) -> "dict[str, tuple[str, str]]":
    """path -> (package, version) for exact, already-known-to-exist file
    paths - same answers as resolving each one separately, in a few calls."""
    uniq = list(dict.fromkeys(paths))
    if not HAVE_DPKG:
        return {p: ("?", "?") for p in uniq}

    fixed = {p: _fix_merged_usr(p) for p in uniq}
    owners = _dpkg_owners(list(dict.fromkeys(fixed.values())))
    pkg_of = {p: owners.get(fixed[p]) for p in uniq}

    # some libraries (BLAS/LAPACK being the classic case) are managed
    # through Debian's update-alternatives system: the traced path is
    # a symlink (often a two-hop chain through /etc/alternatives/)
    # that dpkg doesn't track directly - only the real file at the
    # end of the chain is actually owned by a package
    retry = {}
    for p in uniq:
        if pkg_of[p] is None:
            real_path = os.path.realpath(p)
            if real_path != p:
                retry[p] = _fix_merged_usr(real_path)
    if retry:
        owners = _dpkg_owners(list(dict.fromkeys(retry.values())))
        for p, real in retry.items():
            pkg_of[p] = owners.get(real)

    versions = _package_versions({pkg for pkg in pkg_of.values() if pkg})
    return {p: ((pkg_of[p], versions.get(pkg_of[p], "?")) if pkg_of[p] else ("?", "?")) for p in uniq}


def resolve_path_to_package(path: str) -> "tuple[str, str]":
    """(package, version) for an exact, already-known-to-exist file path."""
    return resolve_paths_to_packages([path])[path]


def tail_and_announce(log_path: str, stop_event: threading.Event, seen: set, lock: threading.Lock) -> None:
    """Runs in a background thread while the traced program is alive:
    polls the growing strace log and prints a line the moment a NEW
    shared library is successfully opened. Deliberately does NOT resolve
    packages here - that's deferred to the final summary - so a live
    announcement is instant and never stalls on a dpkg/apt-cache lookup
    mid-session."""
    pos = 0
    while not stop_event.is_set():
        try:
            with open(log_path, "r", errors="replace") as f:
                f.seek(pos)
                for line in f:
                    m = OPENAT_RE.search(line)
                    if m:
                        p, retval = m.group(1), int(m.group(2))
                        if retval >= 0 and LIB_PATH_RE.match(p):
                            with lock:
                                if p not in seen:
                                    seen.add(p)
                                    print(f"  [+] {p}")
                pos = f.tell()
        except OSError:
            pass
        time.sleep(0.2)


def build_summary(libraries: set, programs: set, failed_basenames: set) -> dict:
    owners = resolve_paths_to_packages(sorted(libraries | programs))
    lib_rows = [dict(zip(("path", "package", "version"), (path, *owners[path])))
                for path in sorted(libraries)]
    prog_rows = [dict(zip(("path", "package", "version"), (path, *owners[path])))
                 for path in sorted(programs)]

    opened_basenames = {os.path.basename(p) for p in libraries}
    missing_basenames = sorted(failed_basenames - opened_basenames)
    prefetch_contents(missing_basenames)   # one Contents pass for all of them
    missing_rows = []
    for name in missing_basenames:
        pkg, installed, note = resolve_package(name, exact=False)
        missing_rows.append({"name": name, "package": pkg, "installed": installed, "note": note})

    return {"libraries": lib_rows, "programs": prog_rows, "missing": missing_rows}


def render_summary(summary: dict, width: "int | None" = None) -> str:
    term_width = width if width is not None else get_term_width()
    cols = [
        Column("PATH", "path", min_width=20, flexible=True, shrink_priority=1),
        Column("PACKAGE", "package", min_width=14, flexible=True, shrink_priority=2),
        Column("VERSION", "version", min_width=10, flexible=True, shrink_priority=0),
    ]
    missing_cols = [
        Column("WANTED", "name", min_width=12, flexible=True, shrink_priority=1),
        Column("PACKAGE", "package", min_width=14, flexible=True, shrink_priority=2),
        Column("INST?", "installed", min_width=5),
        Column("NOTE", "note", min_width=0, flexible=True, shrink_priority=0),
    ]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        for title, rows, columns in (
            (f"Shared libraries loaded ({len(summary['libraries'])})", summary["libraries"], cols),
            (f"Programs invoked ({len(summary['programs'])})", summary["programs"], cols),
            (f"Wanted but never found ({len(summary['missing'])})", summary["missing"], missing_cols),
        ):
            print("=" * min(78, term_width))
            print(f" {title}")
            print("=" * min(78, term_width))
            if not rows:
                print(render_table(columns, [], width=term_width))
                print("  (none found)")
            else:
                print(render_table(columns, rows, width=term_width))
            print()
    return buf.getvalue()


def parse_args(argv: list) -> "tuple[str | None, str | None, bool, list]":
    out_file = None
    raw_file = None
    as_json = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            print(HELP_TEXT)
            sys.exit(0)
        elif a == "-o" and i + 1 < len(argv):
            out_file = argv[i + 1]
            i += 2
        elif a == "--raw" and i + 1 < len(argv):
            raw_file = argv[i + 1]
            i += 2
        elif a == "--json":
            as_json = True
            i += 1
        else:
            return out_file, raw_file, as_json, argv[i:]
    return out_file, raw_file, as_json, []


def main() -> int:
    argv = sys.argv[1:]
    if not argv:
        print(HELP_TEXT)
        return 1

    out_file, raw_file, as_json, command = parse_args(argv)
    if not command:
        print(HELP_TEXT)
        return 1

    if not shutil.which("strace"):
        print("error: strace not found - install it with: sudo apt install strace", file=sys.stderr)
        return 1

    target = shutil.which(command[0]) or command[0]
    kind = detect_kind(target) if os.path.isfile(target) else "unknown"
    print(describe_contents_files(), file=sys.stderr)
    print(f"runcheck: tracing '{' '.join(command)}' ({kind})")
    print("Use the program normally - runcheck is watching in the background.")
    print("New libraries are announced below as they're found.")
    print("Press Ctrl+C or exit the program normally when done.")
    print()

    tmp_log = None
    log_path = raw_file
    if log_path is None:
        tmp_log = tempfile.NamedTemporaryFile(prefix="runcheck-", suffix=".straceout", delete=False)
        tmp_log.close()
        log_path = tmp_log.name

    strace_cmd = ["strace", "-f", "-e", "trace=openat,execve", "-o", log_path, "--"] + command

    try:
        proc = subprocess.Popen(strace_cmd)
    except OSError as e:
        print(f"error: couldn't start strace: {e}", file=sys.stderr)
        return 1

    seen: set = set()
    lock = threading.Lock()
    stop_event = threading.Event()
    tailer = threading.Thread(target=tail_and_announce, args=(log_path, stop_event, seen, lock), daemon=True)
    tailer.start()

    try:
        proc.wait()
    except KeyboardInterrupt:
        print("\nStopping trace...")
        try:
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    stop_event.set()
    tailer.join(timeout=2)

    print()
    print("Trace ended - resolving packages...")
    print()

    libraries, programs, failed_basenames = parse_strace_log(log_path)
    summary = build_summary(libraries, programs, failed_basenames)

    if as_json:
        text = json.dumps(summary, indent=2)
        print(text)
    else:
        text = render_summary(summary)
        print(text, end="")

    if out_file:
        with open(out_file, "w") as f:
            f.write(text)
        print(f"Summary also written to {out_file}")

    if tmp_log is not None:
        try:
            os.unlink(tmp_log.name)
        except OSError:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
