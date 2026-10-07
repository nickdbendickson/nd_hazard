#!/usr/bin/env python3
"""Apply the ND Dam Breach Runner changes to a LISFLOOD-FP 8.x source tree.

    python3 patch_lisflood.py /path/to/LISFLOOD-FP

1. `inittm_depth <m>` keyword: the .inittm output records the first time the
   depth exceeds this value (arrival time at an arrival depth) instead of the
   first wetting. Four small edits.
2. NUMA becomes optional on Linux and is skipped on macOS, so the code builds
   with Apple clang + Homebrew libomp.
3. (3.11.0) Aligned memory through _aligned_malloc on Windows compilers other than
   MSVC / Intel, so it builds with MinGW-w64.

Safe to run twice (already-applied edits are skipped). Stops with a clear
message if the source does not look as expected. Line endings are kept."""
import os
import sys

EDITS = [
    ("lisflood.h",
     "\tNUMERIC_TYPE reset_timeinit_time;",
     "\tNUMERIC_TYPE reset_timeinit_time;\n\tNUMERIC_TYPE inittm_depth; "
     "// arrival-depth threshold for .inittm (nd-breach-runner patch)",
     "inittm_depth"),
    ("lisflood.cpp",
     "\tParams.reset_timeinit_time = 0;",
     "\tParams.reset_timeinit_time = 0;\n\tParams.inittm_depth = 0;",
     "Params.inittm_depth"),
    ("pars.cpp",
     "\tif (read_numeric_param(param_name, line_number, param_value_ptr, \"resettimeinit\", ",
     "\t// nd-breach-runner patch: .inittm records the first time depth exceeds this value (m)\n"
     "\tif (read_numeric_param(param_name, line_number, param_value_ptr, \"inittm_depth\", "
     "&Parptr->inittm_depth, verbose, mode))\n\t\treturn;\n"
     "\tif (read_numeric_param(param_name, line_number, param_value_ptr, \"resettimeinit\", ",
     "\"inittm_depth\""),
    ("iterateq.cpp",
     "if (Arrptr->initHtm[ptr] == NULLVAL /*&& (Arrptr->H[ptr]>C(0.01)*/)",
     "if (Arrptr->initHtm[ptr] == NULLVAL && Arrptr->H[ptr] > Parptr->inittm_depth)",
     "Parptr->inittm_depth"),
    ("lisflood2/lisflood_processing.cpp",
     "#ifdef __unix__",
     "#if defined(__unix__) && defined(LISFLOOD_HAVE_NUMA)",
     "LISFLOOD_HAVE_NUMA"),
    ("CMakeLists.txt",
     "if (UNIX)\n\tfind_package(NUMA REQUIRED)\n"
     "\ttarget_include_directories(lisflood-base INTERFACE ${NUMA_INCLUDE_DIRS})\n"
     "\ttarget_link_libraries(lisflood-base INTERFACE ${NUMA_LIBRARIES})\nendif()",
     "if (UNIX AND NOT APPLE)\n\tfind_package(NUMA)\n\tif (NUMA_FOUND)\n"
     "\t\ttarget_include_directories(lisflood-base INTERFACE ${NUMA_INCLUDE_DIRS})\n"
     "\t\ttarget_link_libraries(lisflood-base INTERFACE ${NUMA_LIBRARIES})\n"
     "\t\ttarget_compile_definitions(lisflood-base INTERFACE LISFLOOD_HAVE_NUMA=1)\n"
     "\tendif()\nendif()",
     "LISFLOOD_HAVE_NUMA"),
    # 3.11.0: builds with MinGW-w64 (the Windows executable bundled with the plugin is
    # cross-compiled with it): aligned allocation through _aligned_malloc / _aligned_free.
    ("utility.cpp",
     '#include "utility.h"\n',
     '#include "utility.h"\n#ifdef _WIN32\n#include <malloc.h> // nd-breach-runner patch: MinGW\n'
     '#endif\n',
     "<malloc.h> // nd-breach-runner"),
    ("utility.cpp",
     "\tvoid* memory = _mm_malloc(size, 64);\n#else",
     "\tvoid* memory = _mm_malloc(size, 64);\n#elif defined(_WIN32)\n"
     "\tvoid* memory = _aligned_malloc(size, 64); // nd-breach-runner patch: MinGW build\n#else",
     "_aligned_malloc(size"),
    ("utility.cpp",
     "\t_mm_free(*memory);\n#else",
     "\t_mm_free(*memory);\n#elif defined(_WIN32)\n"
     "\t_aligned_free(*memory); // nd-breach-runner patch: MinGW build\n#else",
     "_aligned_free(*memory)"),
]


def main(src):
    ok = True
    for rel, old, new, marker in EDITS:
        path = os.path.join(src, rel)
        if not os.path.exists(path):
            print("MISSING  {} (is this a LISFLOOD-FP 8.x source folder?)".format(rel))
            ok = False
            continue
        raw = open(path, "rb").read()
        crlf = b"\r\n" in raw
        text = raw.decode("utf-8", errors="surrogateescape").replace("\r\n", "\n")
        if marker in text:
            print("already  {}".format(rel))
            continue
        n = text.count(old)
        if n == 0:
            print("NOT FOUND in {}:\n    {}".format(rel, old.splitlines()[0]))
            ok = False
            continue
        text = text.replace(old, new)
        if crlf:
            text = text.replace("\n", "\r\n")
        open(path, "wb").write(text.encode("utf-8", errors="surrogateescape"))
        print("patched  {} ({} place{})".format(rel, n, "" if n == 1 else "s"))
    if not ok:
        print("\nSome edits could not be applied. The source may be a different LISFLOOD-FP "
              "version; see scripts/lisflood-nd-breach.patch for the intended changes.")
        return 1
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
