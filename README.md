# deb-buildcheck-suite

A small suite of Debian CLI tools that answer one question: **what do I
need to install to build (and run) this software, and what's the exact
Debian package for that?**

Build systems assume you already know that `curses not found` means
`libncurses-dev`, that `-lz` means `zlib1g-dev`, and that a stray
`libmagic.h` check might resolve to three different packages depending
on what else you have installed. deb-buildcheck-suite does that
translation for you - statically, before you ever run `./configure`,
`cmake` or `meson setup` for real, and dynamically, by watching a program
actually run.

It covers the three build systems you're likely to meet - **GNU
autotools**, **CMake** and **Meson** - each with a static tool that reads
the project's sources and a second tool that reads what a configured build
actually decided. The suite is feature complete; see [Roadmap](#roadmap).

## Tools

| | Reads the source (nothing to configure first) | Reads a configured build |
|---|---|---|
| **autotools** | `configcheck.py` - a generated `configure` script | `makecheck.py` - a Makefile (hand-written or generated) |
| **CMake** | `configcheck-cmake.py` - a `CMakeLists.txt` tree | `makecheck-cmake.py` - a configured build directory |
| **Meson** | `configcheck-meson.py` - a `meson.build` tree | `makecheck-meson.py` - a configured build directory |
| **any** | `runcheck.py` - a running program | |

- **configcheck-\*** answer: *what does this project check for, mandatory
  and optional, which Debian package provides each, and do I have it?*
  Optional checks are grouped under the option that gates them
  (`--enable-x`, `-DNAME`, `-Dname`); the CMake and Meson reports end with
  how to flip an option.
- **makecheck-\*** answer: *what did the build actually link against,
  invoke and look up - and what came up NOT FOUND, with the package that
  would fix it?*
- **runcheck.py** answers: *what shared libraries/packages does this
  actually load at runtime - including ones only touched once you exercise
  a particular code path?*

Every tool imports its package lookup, Contents scanning and table
rendering from `configcheck.py`, so keep all the files in the same
directory (they don't need to be in your working directory - just next to
each other).

The CMake and Meson tools are deliberately separate scripts that only
*import* `configcheck.py`: they never modify it and share no code with
each other, so a change or bug in one build system's tools can never affect
another's.

## Requirements

- Python 3
- Debian/Ubuntu (or a derivative) - resolution is built on `dpkg`,
  `apt-cache`, and `ldconfig`
- `strace`, for `runcheck.py` only (`sudo apt install strace`)
- The `*-cmake` and `*-meson` tools do **not** need cmake or meson
  installed (the static tools read the project files themselves; the
  makecheck tools read the files a configure run left behind). If cmake or
  meson is installed, its version is shown in the report; otherwise
  Debian trixie's is assumed.
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

## configcheck-cmake.py

Reads `CMakeLists.txt` **statically** - cmake is never run. It tokenizes
the CMake language, follows `include()` / `add_subdirectory()`, expands
the project's own functions and macros, and evaluates `if()` conditions
with three outcomes (true / false / depends on a user choice). Branches
that are definitely off on Linux (`WIN32`, `APPLE`, ...) are dropped;
checks under an undecided condition are grouped under the `option()` that
controls them, like configcheck groups by `--enable-x`.

```
./configcheck-cmake.py /path/to/project            # or .../CMakeLists.txt
./configcheck-cmake.py /path/to/project --json
./configcheck-cmake.py /path/to/project -DENABLE_FOO=ON -o report.txt
```

`find_package(X)` is resolved by reading what X's Find module actually
looks for (the project's own `FindX.cmake` first, then cmake's built-in
modules) and mapping that to a `-dev` package; config-file packages fall
back to `XConfig.cmake`. Rows say which route they used and whether the
package is `REQUIRED`. Known gaps: `while()` loops are skipped, and
names built from variables that can't be resolved statically are dropped
(listed under Notes).

## configcheck-meson.py

Reads `meson.build` **statically** - `meson setup` is not needed. It has its
own parser and small interpreter for the Meson language: it follows
`subdir()`, evaluates what it can (Linux, gcc, string/list methods, `if`
on known values) and keeps anything that depends on a user option or an
installed package as "unknown", remembering which option it came from -
so a check under `if get_option('enable_docs')` is listed under
`-Denable_docs`.

```
./configcheck-meson.py /path/to/project            # or .../meson.build
./configcheck-meson.py /path/to/project -Denable_asm=false --json -o report.txt
```

`dependency('x')` is looked up as `x.pc` (pkg-config), then as an `x-config`
tool; a few names (`threads`, `dl`, `boost`, `gtest`...) have built-in
handling. `find_program()`, `cc.find_library()`, `cc.has_header()` and the
headers named in `cc.compiles()` / `has_function(prefix: ...)` probes are
reported too. Lookups that only happen when a compile probe fails are
marked as such. Gaps (shown under Notes): subprojects are noted, not
followed; names built from unresolvable variables are dropped.

## makecheck-meson.py

Reads a **configured** build directory (`meson setup builddir` first).
`meson-log.txt` records every dependency, library, program and header lookup
and its result, so NOT FOUND ones come with the Debian package that would
fix them. It also lists what `build.ninja` links (with full-path and static
`.a` linking flagged) and invokes.

```
./makecheck-meson.py /path/to/builddir
```

Both Meson tools import only `configcheck.py`.

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

## makecheck-cmake.py

Reads a **configured** CMake build directory (run `cmake ..` first) and
reports what the build really uses: libraries linked (from `link.txt` or
`build.ninja`, including ones linked by full path - system `.a` files are
flagged as static), programs invoked (compilers, `ar`, `ninja`/`make`...),
pkg-config modules and `find_library`/`find_path`/package results recorded
in `CMakeCache.txt` - with **NOT FOUND** ones called out - and include
paths outside the project. Works with the Unix Makefiles and Ninja
generators.

```
./makecheck-cmake.py /path/to/build
./makecheck-cmake.py /path/to/build --json -o report.txt
```

Like `configcheck-cmake.py`, it only imports `configcheck.py`.

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

All the tools share one resolver, tried in order, for anything that
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
   `DEB_BUILDCHECK_CONTENTS_FILE=/path/to/file1:/path/to/file2`. Every
   tool prints which files they found (or that none were found) before
   they start. The files are **streamed**, never loaded: a full Debian
   release's Contents files are ~800MB uncompressed, but they're read in
   1MB pieces and everything a report needs is looked up in a single
   pass, so memory use stays at a few tens of MB and a typical run spends
   only a couple of seconds here. If you've enabled a foreign
   architecture (e.g. `i386` for wine on an `amd64` system), its
   `Contents-i386` file is picked up automatically; packages found *only*
   there (`lib64ncurses-dev` and the like - things you can't install on
   your own architecture) are set aside whenever a native match exists,
   so they don't show up as false "ambiguous" candidates. (`.lz4` files are decompressed by
   dynamically loading the system's own `liblz4.so.1` - nothing is
   bundled.)
3. **`apt-file search`**, if installed - a last-resort fallback; in
   practice the Contents-file tier above already covers the same ground
   `apt-file` itself reads from, and more portably (`apt-file` is a
   separate package with its own compiled Perl bindings tied to a
   specific `libapt-pkg`/Perl ABI, which don't always travel well even
   between versions of the same distro)
4. Otherwise: an honest `?` - never a guess

**Headers** (the CMake and Meson tools) are matched against the compiler's
default include path first - `/usr/include/<h>`, the multiarch directory and
the installed gcc's own include directory - so `xxhash.h` doesn't resolve to
the private copy LLVM happens to ship, and `stdbool.h` names the compiler
package instead of listing every package that has a file of that name. Only
if that finds nothing does the looser "any path ending in `/<h>`" search run.

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
- The CMake and Meson static tools are interpreters-by-approximation: they
  assume Linux and gcc, treat anything that depends on a user option or an
  installed package as "unknown" (and show it under the option that decides
  it), and skip what can't be known statically - CMake `while()` loops,
  Meson subprojects and helper-tool modules (gnome, qt, ...), and names
  built from variables they can't resolve. These are listed under *Notes*
  in the report. A lookup guarded by a compile/link probe is marked as
  such, since it can't be decided without running the compiler.
- The makecheck-cmake / makecheck-meson tools report what the configure run
  decided, so they need a configured build directory; a package they name
  for a NOT FOUND entry is inferred from the Find module / dependency name
  and should be treated as a strong hint, not a guarantee.
- A pkg-config module referenced only by its macro's variable-prefix
  name (e.g. `PKG_CHECK_MODULES([SNDFILE], ...)`) can show up as a
  separate, unresolved-looking entry alongside the real module it
  actually maps to

## Roadmap

The suite is **feature complete**: every supported build system (autotools,
CMake, Meson) has both a static tool and a configured-build tool, plus
`runcheck.py` for runtime tracing.

Possible future work, none of it required:
- `runcheck.py` portability beyond Linux x86_64 (currently the initial
  target platform)
- Following Meson subprojects and CMake `FetchContent` dependencies
- Further build systems (e.g. SCons, Bazel) if there is demand

## License

MIT - see [LICENSE](LICENSE).
