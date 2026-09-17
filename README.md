# deb-buildcheck-suite

A small suite of Debian CLI tools that answer one question: **what do I
need to install to build (and run) this software, and what's the exact
Debian package for that?**

Autotools-based projects assume you already know that `curses not found`
means `libncurses-dev`, that `-lz` means `zlib1g-dev`, and that a stray
`libmagic.h` check might resolve to three different packages depending
on what else you have installed. deb-buildcheck-suite does that
translation for you - statically, before you ever run `./configure` or
`make` for real, and dynamically, by watching a program actually run.

The suite is geared specifically toward **GNU autotools** projects
(`configure` + Makefiles). CMake and Meson support is planned - see
[Roadmap](#roadmap).

## Tools

| Tool | Reads | Answers |
|---|---|---|
| `configcheck.py` | a generated `configure` script | What does this project check for, mandatory and optional, and do I have it? |
| `makecheck.py` | a Makefile (hand-written or generated) | What does the concrete build recipe actually link against, invoke, and query via pkg-config? |
| `runcheck.py` | a running program | What shared libraries/packages does this actually load at runtime - including ones only touched once you exercise a particular code path? |

`makecheck.py` and `runcheck.py` both import shared resolution logic
from `configcheck.py`, so keep all three files in the same directory
(they don't need to be in your working directory - just next to each
other).

## Requirements

- Python 3
- Debian/Ubuntu (or a derivative) - resolution is built on `dpkg`,
  `apt-cache`, and `ldconfig`
- `strace`, for `runcheck.py` only (`sudo apt install strace`)
- Optional but recommended: a Contents file for your archive (see
  [Package resolution](#package-resolution)) or `apt-file`, for looking
  up a package that isn't installed on this machine at all

Nothing else needs to be installed for a tool to check for something -
that's the whole point.

## configcheck.py

Parses an autoconf-generated `configure` script **statically** (it never
executes the script - a real run can fail partway through and leave you
with an incomplete picture). It reports:

- Every **mandatory** check the script performs unconditionally
- Every **optional** check, grouped under the `--enable-x`/`--with-x`
  flag that guards it
- For each: what it's actually looking for (a header, a library symbol,
  a program), the Debian package that provides it, whether it's
  installed, and - where the script states a version requirement
  (`>= 1.2.3` and friends) - what's required versus what you have or
  could get

```
./configcheck.py /path/to/configure
./configcheck.py /path/to/configure --json
./configcheck.py /path/to/configure -o report.txt
```

If the file you point it at isn't literally named `configure`, it warns
you first (and asks for confirmation on an interactive terminal) - a
`configure.ac` or a Makefile fed to configcheck.py will just produce
noise, since it's parsing shell syntax specifically.

### Output columns

| Column | Meaning |
|---|---|
| `CHECKS FOR` | The header/library/program/function being checked |
| `PACKAGE` | The Debian package that provides it (`?` = no match found; a name with `(+N more)` means the match was ambiguous - see NOTE) |
| `INST?` | Is that package installed right now |
| `WANT` | A version requirement recovered from the script, if any |
| `HAVE` | What version you actually have installed, or could install |
| `NOTE` | Anything else worth knowing - ambiguous matches, dev-vs-runtime symlink gaps, glibc-builtin functionality with no separate package, etc. |

## makecheck.py

Parses a Makefile - hand-written, or the one autoconf/automake generates
after you run `./configure` - to find what it **actually** links
against, invokes, and queries via `pkg-config`. This is the concrete,
resolved build recipe, not a speculative reading of what a `configure`
option might enable.

```
./makecheck.py /path/to/Makefile
./makecheck.py /path/to/project-dir      # recursively finds every Makefile
./makecheck.py /path/to/Makefile --json
./makecheck.py /path/to/Makefile -o report.txt
```

Point it at a directory and it walks the whole tree, reporting each
Makefile it finds in turn (skipping hidden directories like `.git`), then
prints a final **deduplicated summary** across every Makefile in the
tree - useful since the same handful of toolchain programs (`gcc`, `ar`,
`ranlib`...) tend to show up in every subdirectory's Makefile.

If the file you point it at isn't named `Makefile`, `makefile`, or
`GNUmakefile`, it warns you first (and asks for confirmation on an
interactive terminal) - the same mixup in the other direction:
`configure` fed to makecheck.py will just produce noise.

Reports four sections: libraries linked (`-l` flags), programs invoked,
pkg-config modules queried, and `-I` include paths referenced
(informational only - an arbitrary path doesn't map reliably to one
package). Same `WANT`/`HAVE`/`NOTE` columns as configcheck.py, using the
same shared resolver.

**A blank pkg-config section is often correct, not a miss.** In an
autotools-generated Makefile, `./configure` already resolved pkg-config
queries once and baked the results in as plain `-l`/`-I` flags - there's
nothing left for the Makefile itself to query live, so nothing shows up
there. A hand-written Makefile with no configure step has to shell out to
`pkg-config` itself at build time, and that's exactly what this section
is for.

## runcheck.py

Traces a **running** program - compiled, bash, Python, whatever - to
catch dependencies that a static build-time scan can never see: anything
loaded conditionally at runtime (`dlopen()`'d plugins, optional codec or
database backends, a code path only reached when you feed it a
particular kind of input).

```
./runcheck.py lame test.wav
./runcheck.py ./myscript.sh
```

It runs the program normally - your input, your output, exactly as if it
weren't being traced - while watching for every new library or program it
touches, printing each one live as it's discovered:

```
  [+] /lib/x86_64-linux-gnu/libmp3lame.so.0
  [+] /lib/x86_64-linux-gnu/libogg.so.0
```

Exercise the program as much as you want (different files, different
code paths, whatever's relevant); when you're done, exit normally or
press Ctrl+C, and a deduplicated summary of every library and program
involved prints to the screen - a solid first draft of a Debian
package's `Run-Depends`.

### Options

| Flag | Behavior |
|---|---|
| `-o FILE` | Also save the summary report to `FILE` (the on-screen summary is never suppressed - unlike configcheck/makecheck, `-o` here is additive, not a replacement, since you're meant to be watching the program run) |
| `--raw FILE` | Also save the raw `strace` log to `FILE`, for when the summary misses something and you need to check by hand |
| `--json` | Emit the summary as JSON |

Running it with no arguments prints a full usage explanation rather than
a terse error, since the workflow (run it, use the program, then read
the summary at the end) isn't guessable from a one-line usage string.

### Output sections

- **Shared libraries loaded** - every `.so` file the traced process (or
  any of its children) actually opened successfully
- **Programs invoked** - every subprocess it `execve()`'d
- **Wanted but never found** - a library the program tried to load and
  never succeeded on *any* candidate path. This is distinct from normal
  `ld.so` search-path probing, where failing on the first couple of
  directories before succeeding on the real one is completely routine -
  only a library that fails everywhere shows up here, resolved against
  the same package it's normally provided by even if the file's
  currently missing (e.g. you moved it aside on purpose to test this)

Path resolution here is more precise than configcheck's/makecheck's:
since a trace gives an exact, already-resolved absolute path rather than
a bare name to guess at, it queries `dpkg -S` directly on that path -
automatically correct for multilib (the path itself encodes
`x86_64-linux-gnu` vs `i386-linux-gnu`), and it follows the full symlink
chain when the direct path isn't recognized (catches Debian's
`update-alternatives`-managed libraries, like BLAS/LAPACK, where the
traced path is a symlink indirection rather than the real file).

Initial scope is Linux (Debian) x86_64, with 32-bit multilib libraries
supported where reachable; broader portability is a roadmap item, not a
launch requirement.

## Package resolution

All three tools share one resolver, tried in order, for anything that
isn't already installed:

1. **`dpkg -S`** - is it already installed right now
2. **A Contents file, parsed directly** - Debian publishes a full index
   of every file in every package (`Contents-<arch>`, optionally
   `.gz`/`.lz4`-compressed) for exactly this purpose.
   deb-buildcheck-suite reads these itself rather than shelling out to
   a separate tool, so it works even where that tool doesn't (see
   below). Auto-discovered from `/var/lib/apt/lists`,
   `/var/cache/apt/apt-file`, or `~/.cache/apt-file`; or point it at
   specific files directly with
   `DEB_BUILDCHECK_CONTENTS_FILE=/path/to/file1:/path/to/file2`. All three
   tools print which files they found (or that none were found) before
   they start
3. **`apt-file search`**, if installed - a last-resort fallback; in
   practice the Contents-file tier above already covers the same ground
   `apt-file` itself reads from, and more portably (`apt-file` is a
   separate package with its own compiled Perl bindings tied to a
   specific `libapt-pkg`/Perl ABI, which don't always travel well even
   between versions of the same distro)
4. Otherwise: an honest `?` - never a guess

## Known limitations

- Compiler-property checks (compiler version, executable suffix, and
  the like) are correctly left unresolved - they aren't package
  questions, not a parsing gap
- Version-requirement detection is best-effort: a clean `pkg-config`
  call is resolved with high confidence; a hand-rolled shell comparison
  (or a hand-rolled Makefile version gate) is recovered heuristically
  from context and can miss a comparison that doesn't happen to name its
  requirement in a recognizable way
- An `--enable-x`/`--with-x` option's checks are grouped by the guard
  variable that controls them, but the tool doesn't currently trace the
  script's `if`/`else`/`||` logic - so it can't yet tell you whether
  multiple checks under one flag are alternate detection routes (one
  success is enough) or independent requirements (you need all of them)
- Makefile `-I` include paths are reported with an existence check but
  not package-resolved, since an arbitrary path doesn't map reliably to
  one package
- A pkg-config module referenced only by its macro's variable-prefix
  name (e.g. `PKG_CHECK_MODULES([SNDFILE], ...)`) can show up as a
  separate, unresolved-looking entry alongside the real module it
  actually maps to

## Roadmap

### configcheck (autoconf `configure` scripts)
- CMake equivalent (parsing `CMakeLists.txt`-style checks -
  `check_include_file`, `find_package`, etc.)
- Meson equivalent (parsing `meson.build` checks)

### makecheck (Makefiles)
- CMake equivalent (parsing the generated build files CMake produces)
- Meson equivalent (parsing the generated Ninja build file)

### runcheck (runtime dependency tracing)
- Portability beyond Linux x86_64 (currently the initial target
  platform)

### Planned additions to the suite
- (none currently)

## License

MIT - see [LICENSE](LICENSE).
