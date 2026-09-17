import configcheck as cc

print("1. GLIBC_BUILTIN_LIBS present:", hasattr(cc, "GLIBC_BUILTIN_LIBS"), getattr(cc, "GLIBC_BUILTIN_LIBS", None))
print("2. configcheck.py file location:", cc.__file__)

import shutil
print("3. ldconfig found:", shutil.which("ldconfig"))
print("4. nm found:", shutil.which("nm"))

libc = cc._libc_path()
print("5. libc path found:", libc)

if libc:
    print("6. libc provides iconv_open:", cc._libc_provides_symbol("iconv_open"))

print("7. resolve_library('iconv') result:", cc.resolve_library("iconv"))
