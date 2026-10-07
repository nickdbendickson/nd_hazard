LISFLOOD-FP bundled with the ND Dam Breach Runner
==================================================

What is here
  windows-x64/lisflood.exe   Windows 64-bit (Windows 10/11), no DLLs needed
  linux-x64/lisflood         Linux 64-bit (glibc 2.38 or newer, e.g. Ubuntu 24.04)
  source/                    the complete source of exactly these builds
  COPYING-GPL-2.txt          GNU General Public License, version 2

macOS has no bundled build; there the Runner uses its built-in engine, or you
can build LISFLOOD-FP with scripts/install_macos.sh.

Origin
  LISFLOOD-FP 8.2 (reports itself as 8.1.0), University of Bristol, Zenodo
  record 13121102. Licence: GPL-2.0 (the 3rd-party notices are in the source zip).
  Built in double precision, rectangular grid, no NetCDF/CUDA:
  see nd-breach.cmake in the source.

Our change
  One patch, scripts/patch_lisflood.py: the keyword inittm_depth (arrival time is
  recorded only once the depth passes that value), plus three small edits so it
  compiles with MinGW for Windows. The patched source zip is the corresponding
  source for these binaries, as the GPL requires.

How they were built
  Linux:   cmake -S . -B build -D_CONFIG=nd-breach.cmake -DCMAKE_BUILD_TYPE=Release \
             "-DCMAKE_EXE_LINKER_FLAGS=-static-libstdc++ -static-libgcc" \
             -DOpenMP_gomp_LIBRARY=<path to libgomp.a>
           cmake --build build --target lisflood
  Windows: cross-compiled with mingw-w64 (posix threads), static libgomp and
           winpthread, linker flag -static.

The Runner finds these on its own. To use another LISFLOOD-FP, set its path in
Processing > Options > Providers > ND Dam Breach Runner.
