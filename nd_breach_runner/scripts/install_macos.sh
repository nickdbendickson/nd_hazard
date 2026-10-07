#!/bin/bash
# ND Dam Breach Runner: one-command model installer for macOS (Apple Silicon or Intel).
#
#   scripts/install_macos.sh [options]              install (or repair) LISFLOOD-FP and ANUGA
#   scripts/install_macos.sh --check                report what is installed and configured
#   scripts/install_macos.sh --uninstall            undo what this script did
#   scripts/install_macos.sh --write-settings-only --lisflood-exe FILE --anuga-python FILE
#
# WHAT IT DOES, in this order (each step is skipped when it is already done, so running the
# script again is safe and repairs a half-finished install):
#
#   1. Refuses to run while QGIS is open (QGIS rewrites its settings file when it quits and
#      would undo step 5) unless --force.
#   2. Checks the Xcode command line tools (needed to compile) and Homebrew (the LISFLOOD-FP
#      build script uses it for cmake and libomp). If one is missing it tells you the command
#      to run and stops; it never installs Xcode or Homebrew itself.
#   3. LISFLOOD-FP: builds the arrival-depth-patched executable with
#      scripts/build_lisflood_macos.sh into  PREFIX/bin/lisflood  (default PREFIX ~/.ndbreach,
#      one of the places the plugin looks by itself). The source is --lisflood-src DIR, else
#      PREFIX/src/lisflood-fp, else it is downloaded (LISFLOOD-FP 8.2, Zenodo record
#      13121102) after asking.
#   4. ANUGA: finds conda/mamba (offers 'brew install --cask miniforge' if there is none) and
#      creates the conda environment "anuga" with scripts/create_anuga_env.sh.
#   5. Runs 'ndbreach_cli.py check' on both, then WRITES their paths into QGIS's Processing
#      settings, the only file outside PREFIX it changes:
#          ~/Library/Application Support/QGIS/QGIS3/profiles/default/QGIS/QGIS3.ini
#          [Processing]
#          Configuration\NDBREACH_LISFLOOD_EXE=<PREFIX>/bin/lisflood
#          Configuration\NDBREACH_ANUGA_PYTHON=<conda env>/bin/python
#      (the same two settings as Processing > Options > Providers > ND Dam Breach Runner).
#      The edit adds or updates just those two lines, keeps everything else byte for byte,
#      first copies the file to QGIS3.ini.ndbreach-backup-<date>, and replaces it atomically.
#      Paths with non-ASCII characters, commas, quotes, backslashes or semicolons are refused
#      (QGIS would store them in an escaped form): use a plain --prefix instead.
#   6. Records what it did in PREFIX/install-manifest.txt (used by --uninstall and --check).
#
# OPTIONS
#   --prefix DIR          where the executable and sources go (default ~/.ndbreach)
#   --lisflood-src DIR    an unpacked LISFLOOD-FP source folder (instead of downloading)
#   --lisflood-exe FILE   use this patched executable instead of building one
#   --anuga-python FILE   use this ANUGA environment's python instead of creating one
#   --env NAME            conda environment name (default anuga)
#   --skip-lisflood       do not build LISFLOOD-FP
#   --skip-anuga          do not create the ANUGA environment
#   --rebuild             build LISFLOOD-FP again even if PREFIX/bin/lisflood is fine
#   --recreate-env        delete and create the conda environment again
#   --profile NAME        QGIS profile (default "default")
#   --ini FILE            the QGIS settings file to edit (default from --profile)
#   --no-settings         do not touch the QGIS settings file (print the paths instead)
#   --no-verify           write the settings even if the check of the models fails
#   --write-settings-only only write the two settings (needs --lisflood-exe / --anuga-python)
#   --force               go ahead although QGIS seems to be running, or the QGIS profile
#                         folder does not exist yet
#   --yes, -y             answer yes to the questions (download, Miniforge)
#   --dry-run             say what would be done, change nothing
#   --remove-src          with --uninstall: also delete PREFIX/src
#   --remove-env          with --uninstall: also delete the conda environment
#   -h, --help            this text
#
# EXIT STATUS  0 done / everything fine; 1 a step failed or --check found a problem;
#              2 wrong usage or refused (QGIS running, unsafe path).
#
# Compatible with macOS's bash 3.2. Environment overrides for testing (not for normal use):
#   NDBREACH_INSTALL_TEST=1   skip the macOS-only toolchain checks (Xcode tools, Homebrew)
#   NDBREACH_BUILD_SCRIPT, NDBREACH_ENV_SCRIPT   other scripts in place of the two helpers
set -eu
set -o pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PLUGIN_DIR="$(dirname "$HERE")"

MODE=install
PREFIX="${HOME}/.ndbreach"
LISFLOOD_SRC=""
LISFLOOD_EXE_OPT=""
ANUGA_PYTHON_OPT=""
ENV_NAME="anuga"
SKIP_LISFLOOD=0
SKIP_ANUGA=0
REBUILD=0
RECREATE_ENV=0
PROFILE="default"
INI=""
NO_SETTINGS=0
NO_VERIFY=0
FORCE=0
YES=0
DRY=0
REMOVE_SRC=0
REMOVE_ENV=0
ZENODO_RECORD="13121102"
BUILD_SCRIPT="${NDBREACH_BUILD_SCRIPT:-$HERE/build_lisflood_macos.sh}"
ENV_SCRIPT="${NDBREACH_ENV_SCRIPT:-$HERE/create_anuga_env.sh}"
TEST_MODE="${NDBREACH_INSTALL_TEST:-0}"

LF_FINAL=""
AN_FINAL=""
CONDA_CMD=""
PROBLEMS=0

say() { printf '%s\n' "$*"; }
note() { printf '  %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit "${2:-1}"; }
step() { printf '\n== %s\n' "$*"; }
problem() { printf 'PROBLEM: %s\n' "$*"; PROBLEMS=$((PROBLEMS + 1)); }

usage() { sed -n '2,/^set -eu/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; }

# ------------------------------------------------------------------------------------------
# the settings editor: a small Python program, so the QSettings INI file is edited line by
# line (a real INI parser would rewrite the whole file, drop comments and reorder keys)
# ------------------------------------------------------------------------------------------
IFS= read -r -d '' PY_INI <<'PYEOF' || true
import os, sys, shutil, tempfile, time

SECTION = "Processing"
BAD = set(',\\"\';')


def refuse(msg):
    sys.stderr.write("ERROR: " + msg + "\n")
    sys.exit(2)


def check_value(key, value):
    for ch in value:
        if ord(ch) < 32 or ord(ch) > 126 or ch in BAD:
            refuse("the value for {0} contains a character QGIS would store in an escaped "
                   "form ({1!r}); use a plain path (for example with --prefix): {2}".format(
                       key, ch, value))
    if value != value.strip():
        refuse("the value for {0} has leading or trailing spaces: {1!r}".format(key, value))
    if not value:
        refuse("the value for {0} is empty".format(key))


def read(path):
    if not os.path.exists(path):
        return None, "\n"
    with open(path, "rb") as f:
        raw = f.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    eol = "\r\n" if "\r\n" in text else "\n"
    return text.replace("\r\n", "\n"), eol


def split(text):
    """Line indexes of the [Processing] section: (start of body, end)."""
    lines = text.split("\n")
    start = end = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("[") and s.endswith("]"):
            if start is not None and end is None:
                end = i
            if s == "[" + SECTION + "]" and start is None:
                start = i + 1
    if start is not None and end is None:
        end = len(lines)
    return lines, start, end


def find_keys(lines, start, end, key):
    return [i for i in range(start, end) if lines[i].split("=", 1)[0].strip() == key
            and "=" in lines[i]]


def value_of(lines, i):
    return lines[i].split("=", 1)[1].strip()


def get(path, key):
    text, _eol = read(path)
    if text is None:
        return ""
    lines, start, end = split(text)
    if start is None:
        return ""
    hits = find_keys(lines, start, end, key)
    return value_of(lines, hits[0]) if hits else ""


def write(path, text, eol, existed):
    d = os.path.dirname(path) or "."
    backup = ""
    if existed:
        backup = path + ".ndbreach-backup-" + time.strftime("%Y%m%d-%H%M%S")
        n = 1
        while os.path.exists(backup):
            n += 1
            backup = path + ".ndbreach-backup-" + time.strftime("%Y%m%d-%H%M%S") + "-" + str(n)
        shutil.copy2(path, backup)
    fd, tmp = tempfile.mkstemp(prefix=".QGIS3.ini.", dir=d)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(text.replace("\n", eol).encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        if existed:
            shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return backup


def do_set(path, pairs):
    text, eol = read(path)
    existed = text is not None
    if not existed:
        text, eol = "", "\n"
    lines, start, end = split(text)
    report, changed = [], False
    if start is None:
        while lines and lines[-1] == "":
            lines.pop()
        if lines:
            lines.append("")
        lines.append("[" + SECTION + "]")
        start = len(lines)
        end = start
        lines.append("")                      # keeps the file ending with a newline
    for key, value in pairs:
        hits = find_keys(lines, start, end, key)
        new = "{0}={1}".format(key, value)
        if hits:
            if value_of(lines, hits[0]) == value and len(hits) == 1:
                report.append("{0}: unchanged".format(key))
                continue
            lines[hits[0]] = new
            for j in reversed(hits[1:]):        # a duplicate would shadow ours
                del lines[j]
                end -= 1
            report.append("{0}: changed".format(key))
        else:
            at = end
            while at > start and lines[at - 1].strip() == "":
                at -= 1
            lines.insert(at, new)
            end += 1
            report.append("{0}: added".format(key))
        changed = True
    if changed:
        backup = write(path, "\n".join(lines), eol, existed)
        if backup:
            report.append("backup: " + backup)
        elif not existed:
            report.append("created: " + path)
    return report


def do_unset(path, pairs):
    text, eol = read(path)
    report = []
    if text is None:
        return ["{0}: absent (no settings file)".format(k) for k, _v in pairs]
    lines, start, end = split(text)
    changed = False
    for key, expect in pairs:
        hits = find_keys(lines, start, end, key) if start is not None else []
        if not hits:
            report.append("{0}: absent".format(key))
            continue
        if expect != "*" and value_of(lines, hits[0]) != expect:
            report.append("{0}: kept (it points somewhere else now: {1})".format(
                key, value_of(lines, hits[0])))
            continue
        for j in reversed(hits):
            del lines[j]
            end -= 1
        report.append("{0}: removed".format(key))
        changed = True
    if changed:
        backup = write(path, "\n".join(lines), eol, True)
        report.append("backup: " + backup)
    return report


def main():
    if len(sys.argv) < 3:
        refuse("usage: ACTION FILE [KEY VALUE ...]")
    action, path, rest = sys.argv[1], sys.argv[2], sys.argv[3:]
    if action == "get":
        sys.stdout.write(get(path, rest[0]) + "\n")
        return
    if len(rest) % 2:
        refuse("keys and values must come in pairs")
    pairs = [(rest[i], rest[i + 1]) for i in range(0, len(rest), 2)]
    if action == "set":
        for k, v in pairs:
            check_value(k, v)
        out = do_set(path, pairs)
    elif action == "unset":
        out = do_unset(path, pairs)
    else:
        refuse("unknown action " + action)
    sys.stdout.write("\n".join(out) + "\n")


try:
    main()
except OSError as e:
    sys.stderr.write("ERROR: cannot edit {0}: {1}\n".format(sys.argv[2], e))
    sys.exit(3)
PYEOF

KEY_LF='Configuration\NDBREACH_LISFLOOD_EXE'
KEY_AN='Configuration\NDBREACH_ANUGA_PYTHON'

ini_edit() { python3 -c "$PY_INI" "$@"; }

# ------------------------------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------------------------------
need_arg() { [ "$1" -ge 2 ] || die "$2 needs a value" 2; }

confirm() {
  # confirm "question": yes with --yes; otherwise ask on a terminal; no when not interactive
  if [ "$YES" -eq 1 ]; then return 0; fi
  if [ ! -t 0 ]; then
    say "$1 (not asked: no terminal; run again with --yes to agree)"
    return 1
  fi
  printf '%s [y/N] ' "$1"
  read -r answer || answer=""
  case "$answer" in y | Y | yes | YES) return 0 ;; *) return 1 ;; esac
}

run() {
  # run a command, or just print it with --dry-run
  if [ "$DRY" -eq 1 ]; then
    printf '  [dry run] %s\n' "$*"
    return 0
  fi
  "$@"
}

is_macos() { [ "$(uname -s)" = "Darwin" ]; }

qgis_running() {
  pgrep -x QGIS >/dev/null 2>&1 && return 0
  pgrep -f 'QGIS[^ /]*\.app/Contents/MacOS/QGIS' >/dev/null 2>&1 && return 0
  return 1
}

default_ini() {
  printf '%s/Library/Application Support/QGIS/QGIS3/profiles/%s/QGIS/QGIS3.ini' "$HOME" "$PROFILE"
}

lisflood_ok() {
  # a patched build: executable, and it knows the arrival-depth keyword
  [ -n "$1" ] && [ -f "$1" ] && [ -x "$1" ] && grep -q inittm_depth "$1"
}

anuga_ok() {
  [ -n "$1" ] && [ -x "$1" ] && "$1" -c "import anuga" >/dev/null 2>&1
}

manifest_get() {
  [ -f "$PREFIX/install-manifest.txt" ] || return 0
  awk -F= -v k="$1" '$1 == k { sub(/^[^=]*=/, ""); print; exit }' "$PREFIX/install-manifest.txt"
}

need_toolchain() {
  if [ "$TEST_MODE" = "1" ]; then return 0; fi
  is_macos || die "this installer is for macOS (found $(uname -s)); build LISFLOOD-FP as in the README instead"
  local dev
  if ! dev="$(xcode-select -p 2>/dev/null)" || [ ! -x "$dev/usr/bin/clang" ]; then
    say "The Xcode command line tools are not installed. Install them with:"
    say "    xcode-select --install"
    say "wait for it to finish, then run this script again."
    exit 1
  fi
  note "Xcode command line tools: $dev"
  if ! command -v brew >/dev/null 2>&1; then
    local b
    for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do
      if [ -x "$b" ]; then eval "$("$b" shellenv)"; break; fi
    done
  fi
  if ! command -v brew >/dev/null 2>&1; then
    say "Homebrew is not installed (the LISFLOOD-FP build needs it for cmake and libomp)."
    say "Install it from https://brew.sh (one command), open a new terminal, and run this again."
    exit 1
  fi
  note "Homebrew: $(command -v brew)"
}

find_conda() {
  # sets CONDA_CMD to conda (preferred) or mamba, and puts its folder on PATH for the helpers
  local c d
  CONDA_CMD=""
  for c in conda mamba; do
    if command -v "$c" >/dev/null 2>&1; then CONDA_CMD="$(command -v "$c")"; return 0; fi
  done
  for d in "$HOME/miniforge3" "$HOME/mambaforge" "$HOME/miniconda3" "$HOME/anaconda3" \
    /opt/homebrew/Caskroom/miniforge/base /usr/local/Caskroom/miniforge/base \
    /opt/homebrew/anaconda3 /usr/local/anaconda3; do
    for c in conda mamba; do
      if [ -x "$d/bin/$c" ]; then
        CONDA_CMD="$d/bin/$c"
        PATH="$d/bin:$PATH"
        export PATH
        return 0
      fi
    done
  done
  return 1
}

need_conda() {
  if find_conda; then note "conda: $CONDA_CMD"; return 0; fi
  say "conda/mamba was not found (ANUGA runs in its own conda environment)."
  if [ "$TEST_MODE" != "1" ] && command -v brew >/dev/null 2>&1 &&
    confirm "Install Miniforge with 'brew install --cask miniforge'?"; then
    run brew install --cask miniforge
    if [ "$DRY" -eq 1 ]; then return 0; fi
    find_conda || die "Miniforge was installed but conda was not found; open a new terminal and run this again"
    note "conda: $CONDA_CMD"
    return 0
  fi
  say "Install Miniforge (https://github.com/conda-forge/miniforge, or: brew install --cask miniforge),"
  say "open a new terminal and run this script again; or use --skip-anuga to skip the ANUGA tier."
  exit 1
}

env_python() {
  # the interpreter of conda environment $1, or nothing when it does not exist
  local p
  p="$("$CONDA_CMD" run -n "$1" python -c 'import sys; print(sys.executable)' 2>/dev/null | tail -n 1)" || p=""
  if [ -n "$p" ] && [ -x "$p" ]; then printf '%s\n' "$p"; fi
}

# ------------------------------------------------------------------------------------------
# step 3: LISFLOOD-FP
# ------------------------------------------------------------------------------------------
find_source() {
  # prints an unpacked source folder (has CMakeLists.txt), downloading when needed
  local d
  if [ -n "$LISFLOOD_SRC" ]; then
    [ -f "$LISFLOOD_SRC/CMakeLists.txt" ] || die "--lisflood-src $LISFLOOD_SRC has no CMakeLists.txt: give the folder of the unpacked LISFLOOD-FP source"
    printf '%s\n' "$LISFLOOD_SRC"
    return 0
  fi
  for d in "$PREFIX/src/lisflood-fp" "$PREFIX/src"/*/; do
    d="${d%/}"
    if [ -f "$d/CMakeLists.txt" ]; then printf '%s\n' "$d"; return 0; fi
  done
  say "The LISFLOOD-FP source is not on this computer." >&2
  confirm "Download LISFLOOD-FP 8.2 (University of Bristol, Zenodo record $ZENODO_RECORD) into $PREFIX/src?" >&2 ||
    die "give the unpacked source with --lisflood-src DIR (download it from https://zenodo.org/records/$ZENODO_RECORD), or allow the download with --yes"
  if [ "$DRY" -eq 1 ]; then
    printf '  [dry run] download and unpack the source into %s/src/lisflood-fp\n' "$PREFIX" >&2
    printf '%s\n' "$PREFIX/src/lisflood-fp"
    return 0
  fi
  command -v curl >/dev/null 2>&1 || die "curl is needed for the download"
  local tmp url name top
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/ndbreach-src.XXXXXX")"
  url="$(curl -fsSL "https://zenodo.org/api/records/$ZENODO_RECORD" | python3 -c '
import json, sys
d = json.load(sys.stdin)
for f in d.get("files", []):
    k = f.get("key", "")
    if k.endswith((".zip", ".tar.gz", ".tgz")):
        print(f["links"]["self"])
        break
')" || url=""
  [ -n "$url" ] || die "could not find the source archive of Zenodo record $ZENODO_RECORD (offline?); download it by hand and pass --lisflood-src"
  name="$tmp/source-archive"
  say "Downloading $url" >&2
  curl -fL --progress-bar -o "$name" "$url" || die "download failed: $url"
  mkdir -p "$tmp/x" "$PREFIX/src"
  case "$url" in
    *.zip*) unzip -q "$name" -d "$tmp/x" ;;
    *) tar -xzf "$name" -C "$tmp/x" ;;
  esac
  top="$(dirname "$(find "$tmp/x" -maxdepth 3 -name CMakeLists.txt | head -n 1)")"
  [ -f "$top/CMakeLists.txt" ] || die "the downloaded archive has no CMakeLists.txt"
  rm -rf "$PREFIX/src/lisflood-fp"
  mv "$top" "$PREFIX/src/lisflood-fp"
  rm -rf "$tmp"
  printf '%s\n' "$PREFIX/src/lisflood-fp"
}

step_lisflood() {
  step "LISFLOOD-FP"
  if [ "$SKIP_LISFLOOD" -eq 1 ]; then
    note "skipped (--skip-lisflood)"
    if lisflood_ok "$PREFIX/bin/lisflood"; then LF_FINAL="$PREFIX/bin/lisflood"; fi
    return 0
  fi
  if [ -n "$LISFLOOD_EXE_OPT" ]; then
    lisflood_ok "$LISFLOOD_EXE_OPT" || die "$LISFLOOD_EXE_OPT is not an executable patched LISFLOOD-FP (it does not know the arrival-depth keyword)"
    LF_FINAL="$LISFLOOD_EXE_OPT"
    note "using $LF_FINAL"
    return 0
  fi
  if lisflood_ok "$PREFIX/bin/lisflood" && [ "$REBUILD" -eq 0 ]; then
    LF_FINAL="$PREFIX/bin/lisflood"
    note "already built: $LF_FINAL (use --rebuild to build it again)"
    return 0
  fi
  need_toolchain
  local src
  src="$(find_source)"
  note "source: $src"
  [ -f "$BUILD_SCRIPT" ] || die "missing $BUILD_SCRIPT"
  run mkdir -p "$PREFIX/bin"
  run bash "$BUILD_SCRIPT" "$src" "$PREFIX/bin"
  if [ "$DRY" -eq 0 ]; then
    lisflood_ok "$PREFIX/bin/lisflood" || die "the build finished but $PREFIX/bin/lisflood is missing or is not the patched build"
  fi
  LF_FINAL="$PREFIX/bin/lisflood"
  note "built: $LF_FINAL"
}

# ------------------------------------------------------------------------------------------
# step 4: ANUGA
# ------------------------------------------------------------------------------------------
step_anuga() {
  step "ANUGA (conda environment '$ENV_NAME')"
  if [ "$SKIP_ANUGA" -eq 1 ]; then
    note "skipped (--skip-anuga)"
    return 0
  fi
  if [ -n "$ANUGA_PYTHON_OPT" ]; then
    anuga_ok "$ANUGA_PYTHON_OPT" || die "$ANUGA_PYTHON_OPT cannot 'import anuga'"
    AN_FINAL="$ANUGA_PYTHON_OPT"
    note "using $AN_FINAL"
    return 0
  fi
  need_conda
  local py=""
  if [ "$DRY" -eq 0 ]; then py="$(env_python "$ENV_NAME")"; fi
  if [ -n "$py" ] && [ "$RECREATE_ENV" -eq 0 ] && anuga_ok "$py"; then
    AN_FINAL="$py"
    note "environment exists and imports ANUGA: $AN_FINAL (use --recreate-env to build it again)"
    return 0
  fi
  if [ -n "$py" ]; then
    if [ "$RECREATE_ENV" -eq 1 ]; then note "removing the old environment"; else note "the environment exists but cannot import ANUGA: recreating it"; fi
    run "$CONDA_CMD" env remove -y -n "$ENV_NAME"
  fi
  [ -f "$ENV_SCRIPT" ] || die "missing $ENV_SCRIPT"
  run bash "$ENV_SCRIPT" "$ENV_NAME"
  if [ "$DRY" -eq 1 ]; then return 0; fi
  py="$(env_python "$ENV_NAME")"
  [ -n "$py" ] || die "the environment '$ENV_NAME' was created but its python was not found"
  anuga_ok "$py" || die "$py cannot import anuga after the install"
  AN_FINAL="$py"
  note "created: $AN_FINAL"
}

# ------------------------------------------------------------------------------------------
# step 5: verify and write the QGIS settings
# ------------------------------------------------------------------------------------------
step_verify() {
  step "Checking the models"
  local py=""
  if [ -z "$LF_FINAL" ] && [ -z "$AN_FINAL" ]; then
    note "nothing to check"
    return 0
  fi
  if [ "$DRY" -eq 1 ]; then
    note "[dry run] run ndbreach_cli.py check on the installed models"
    return 0
  fi
  if [ -n "$AN_FINAL" ] && [ -x "$AN_FINAL" ]; then
    py="$AN_FINAL"
  elif command -v python3 >/dev/null 2>&1 && python3 -c 'import numpy' >/dev/null 2>&1; then
    py="python3"
  fi
  if [ -z "$py" ]; then
    note "no Python with numpy to run the plugin's check with: only the file checks were done"
    return 0
  fi
  local ok=1
  if [ -n "$LF_FINAL" ] && [ -n "$AN_FINAL" ]; then
    "$py" "$HERE/ndbreach_cli.py" check --lisflood "$LF_FINAL" --anuga-python "$AN_FINAL" || ok=0
  elif [ -n "$LF_FINAL" ]; then
    "$py" "$HERE/ndbreach_cli.py" check --lisflood "$LF_FINAL" || ok=0
  else
    "$py" "$HERE/ndbreach_cli.py" check --anuga-python "$AN_FINAL" || ok=0
  fi
  if [ "$ok" -eq 1 ]; then return 0; fi
  if [ "$NO_VERIFY" -eq 1 ]; then warn "the check failed; going on because of --no-verify"; return 0; fi
  die "the check of the installed models failed (see above); the QGIS settings were not written. Fix it and run this script again, or use --no-verify."
}

write_settings() {
  step "QGIS settings"
  local ini="$INI"
  [ -n "$ini" ] || ini="$(default_ini)"
  if [ -z "$LF_FINAL" ] && [ -z "$AN_FINAL" ]; then
    note "nothing to write"
    return 0
  fi
  if [ "$NO_SETTINGS" -eq 1 ]; then
    note "not writing $ini (--no-settings). Set these in Processing > Options > Providers > ND Dam Breach Runner:"
    [ -z "$LF_FINAL" ] || note "  LISFLOOD-FP executable = $LF_FINAL"
    [ -z "$AN_FINAL" ] || note "  ANUGA Python interpreter = $AN_FINAL"
    return 0
  fi
  if qgis_running && [ "$FORCE" -eq 0 ]; then
    die "QGIS is running: it would overwrite the settings when it quits. Quit QGIS and run this again (or use --force)." 2
  fi
  if [ ! -d "$(dirname "$ini")" ] && [ "$FORCE" -eq 0 ]; then
    die "the QGIS profile folder $(dirname "$ini") does not exist: start QGIS once, or pass --profile / --ini (or --force to create it)." 2
  fi
  note "file: $ini"
  if [ "$DRY" -eq 1 ]; then
    [ -z "$LF_FINAL" ] || note "[dry run] $KEY_LF=$LF_FINAL"
    [ -z "$AN_FINAL" ] || note "[dry run] $KEY_AN=$AN_FINAL"
    return 0
  fi
  mkdir -p "$(dirname "$ini")"
  local out
  if [ -n "$LF_FINAL" ] && [ -n "$AN_FINAL" ]; then
    out="$(ini_edit set "$ini" "$KEY_LF" "$LF_FINAL" "$KEY_AN" "$AN_FINAL")" || exit $?
  elif [ -n "$LF_FINAL" ]; then
    out="$(ini_edit set "$ini" "$KEY_LF" "$LF_FINAL")" || exit $?
  else
    out="$(ini_edit set "$ini" "$KEY_AN" "$AN_FINAL")" || exit $?
  fi
  printf '%s\n' "$out" | sed 's/^/  /'
  WRITTEN_INI="$ini"
}

write_manifest() {
  [ "$DRY" -eq 0 ] || return 0
  mkdir -p "$PREFIX"
  {
    printf 'prefix=%s\n' "$PREFIX"
    printf 'lisflood_exe=%s\n' "$LF_FINAL"
    printf 'anuga_python=%s\n' "$AN_FINAL"
    printf 'anuga_env=%s\n' "$ENV_NAME"
    printf 'ini=%s\n' "${WRITTEN_INI:-}"
    printf 'date=%s\n' "$(date '+%Y-%m-%d %H:%M:%S')"
  } >"$PREFIX/install-manifest.txt"
}

WRITTEN_INI=""

# ------------------------------------------------------------------------------------------
# modes
# ------------------------------------------------------------------------------------------
do_install() {
  say "ND Dam Breach Runner model installer (prefix $PREFIX)"
  [ "$DRY" -eq 0 ] || say "(dry run: nothing is changed)"
  if [ "$NO_SETTINGS" -eq 0 ] && [ "$FORCE" -eq 0 ] && qgis_running; then
    die "QGIS is running. Quit QGIS first (it rewrites its settings when it quits, which would undo this install), or use --force / --no-settings." 2
  fi
  step_lisflood
  step_anuga
  step_verify
  write_settings
  write_manifest
  step "Done"
  [ -z "$LF_FINAL" ] || say "LISFLOOD-FP: $LF_FINAL"
  [ -z "$AN_FINAL" ] || say "ANUGA:       $AN_FINAL"
  say "Start QGIS, then run  Processing > ND Dam Breach Runner > Self-test the installation in this QGIS."
  say "To undo: $0 --uninstall"
}

do_write_only() {
  [ -n "$LISFLOOD_EXE_OPT$ANUGA_PYTHON_OPT" ] || die "--write-settings-only needs --lisflood-exe and/or --anuga-python" 2
  if [ -n "$LISFLOOD_EXE_OPT" ]; then
    [ -f "$LISFLOOD_EXE_OPT" ] && [ -x "$LISFLOOD_EXE_OPT" ] || die "$LISFLOOD_EXE_OPT is not an executable file"
    LF_FINAL="$LISFLOOD_EXE_OPT"
  fi
  if [ -n "$ANUGA_PYTHON_OPT" ]; then
    [ -f "$ANUGA_PYTHON_OPT" ] && [ -x "$ANUGA_PYTHON_OPT" ] || die "$ANUGA_PYTHON_OPT is not an executable file"
    AN_FINAL="$ANUGA_PYTHON_OPT"
  fi
  write_settings
  write_manifest
}

do_check() {
  local ini="$INI" v exe py dev
  [ -n "$ini" ] || ini="$(default_ini)"
  say "ND Dam Breach Runner installation check"
  step "Tools"
  if [ "$TEST_MODE" = "1" ]; then
    note "(test mode: macOS toolchain checks skipped)"
  elif is_macos; then
    if dev="$(xcode-select -p 2>/dev/null)" && [ -x "$dev/usr/bin/clang" ]; then note "OK Xcode command line tools"; else problem "Xcode command line tools missing: xcode-select --install"; fi
    if command -v brew >/dev/null 2>&1 || [ -x /opt/homebrew/bin/brew ] || [ -x /usr/local/bin/brew ]; then note "OK Homebrew"; else problem "Homebrew missing (needed to build LISFLOOD-FP): https://brew.sh"; fi
  else
    note "not macOS ($(uname -s)): toolchain checks skipped"
  fi
  if find_conda; then note "OK conda: $CONDA_CMD"; else note "NOTE conda not found (only needed for ANUGA)"; fi
  if qgis_running; then note "NOTE QGIS is running now"; fi

  step "QGIS settings ($ini)"
  exe=""
  py=""
  if [ -f "$ini" ]; then
    exe="$(ini_edit get "$ini" "$KEY_LF")"
    py="$(ini_edit get "$ini" "$KEY_AN")"
  else
    problem "the settings file does not exist yet"
  fi
  if [ -n "$exe" ]; then
    if lisflood_ok "$exe"; then note "OK LISFLOOD-FP setting: $exe"; else problem "LISFLOOD-FP setting points to $exe, which is missing or is not the patched build"; fi
  else
    problem "no LISFLOOD-FP executable is set (run this script without --check)"
  fi
  if [ -n "$py" ]; then
    if anuga_ok "$py"; then note "OK ANUGA setting: $py"; else problem "ANUGA setting points to $py, which is missing or cannot import anuga"; fi
  else
    note "NOTE no ANUGA Python interpreter is set (only needed for the ANUGA tier)"
  fi

  step "Files"
  v="$(manifest_get lisflood_exe)"
  if [ -f "$PREFIX/install-manifest.txt" ]; then
    note "manifest: $PREFIX/install-manifest.txt (installed $(manifest_get date))"
    if [ -n "$v" ] && [ "$v" != "$exe" ]; then problem "the QGIS setting ($exe) is not the executable this script installed ($v)"; fi
  else
    note "NOTE no install manifest under $PREFIX (the models were set up some other way, or not yet)"
  fi
  if lisflood_ok "$PREFIX/bin/lisflood"; then note "OK $PREFIX/bin/lisflood"; else note "NOTE no patched LISFLOOD-FP at $PREFIX/bin/lisflood"; fi

  if [ -n "$exe" ] && lisflood_ok "$exe"; then
    step "Model check"
    local runner=""
    if [ -n "$py" ] && [ -x "$py" ]; then runner="$py"; elif command -v python3 >/dev/null 2>&1 && python3 -c 'import numpy' >/dev/null 2>&1; then runner="python3"; fi
    if [ -n "$runner" ]; then
      if [ -n "$py" ]; then
        "$runner" "$HERE/ndbreach_cli.py" check --lisflood "$exe" --anuga-python "$py" || problem "the plugin's model check failed"
      else
        "$runner" "$HERE/ndbreach_cli.py" check --lisflood "$exe" || problem "the plugin's model check failed"
      fi
    else
      note "no Python with numpy found to run the plugin's check"
    fi
  fi
  say ""
  if [ "$PROBLEMS" -eq 0 ]; then say "Everything looks fine."; return 0; fi
  say "$PROBLEMS problem(s). Run $0 to fix them."
  return 1
}

do_uninstall() {
  local ini="$INI" lf an env_name
  [ -n "$ini" ] || ini="$(default_ini)"
  say "Removing what the ND Dam Breach Runner installer added (prefix $PREFIX)"
  [ "$DRY" -eq 0 ] || say "(dry run: nothing is changed)"
  lf="$(manifest_get lisflood_exe)"
  an="$(manifest_get anuga_python)"
  env_name="$(manifest_get anuga_env)"
  [ -n "$env_name" ] || env_name="$ENV_NAME"
  if [ -z "$lf" ] && [ -f "$PREFIX/bin/lisflood" ]; then lf="$PREFIX/bin/lisflood"; fi
  step "QGIS settings"
  if [ -f "$ini" ] && [ "$NO_SETTINGS" -eq 0 ]; then
    if qgis_running && [ "$FORCE" -eq 0 ]; then
      die "QGIS is running: it would overwrite the settings when it quits. Quit QGIS first (or use --force)." 2
    fi
    # remove a setting only while it still holds the value this script wrote
    if [ "$DRY" -eq 1 ]; then
      note "[dry run] remove $KEY_LF and $KEY_AN from $ini if they still hold the installed paths"
    else
      local out
      out="$(ini_edit unset "$ini" "$KEY_LF" "${lf:-*none*}" "$KEY_AN" "${an:-*none*}")" || exit $?
      printf '%s\n' "$out" | sed 's/^/  /'
    fi
  else
    note "no settings file to change ($ini)"
  fi
  step "Files"
  if [ -f "$PREFIX/bin/lisflood" ]; then run rm -f "$PREFIX/bin/lisflood"; note "removed $PREFIX/bin/lisflood"; fi
  if [ "$REMOVE_SRC" -eq 1 ] && [ -d "$PREFIX/src" ]; then
    if [ -f "$PREFIX/install-manifest.txt" ]; then
      run rm -rf "$PREFIX/src"
      note "removed $PREFIX/src"
    else
      warn "not removing $PREFIX/src: there is no install manifest, so this folder was not created by the installer"
    fi
  fi
  if [ "$REMOVE_ENV" -eq 1 ]; then
    if find_conda; then
      run "$CONDA_CMD" env remove -y -n "$env_name"
      note "removed conda environment $env_name"
    else
      warn "conda not found: the environment $env_name was not removed"
    fi
  else
    note "kept the conda environment '$env_name' and the sources (--remove-env / --remove-src remove them)"
  fi
  if [ "$DRY" -eq 0 ]; then
    rm -f "$PREFIX/install-manifest.txt"
    rmdir "$PREFIX/bin" "$PREFIX/src" "$PREFIX" 2>/dev/null || true
  fi
  say ""
  say "Done."
}

# ------------------------------------------------------------------------------------------
# command line
# ------------------------------------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --check) MODE=check; shift ;;
    --uninstall) MODE=uninstall; shift ;;
    --write-settings-only) MODE=write; shift ;;
    --prefix) need_arg $# "$1"; PREFIX="$2"; shift 2 ;;
    --prefix=*) PREFIX="${1#*=}"; shift ;;
    --lisflood-src) need_arg $# "$1"; LISFLOOD_SRC="$2"; shift 2 ;;
    --lisflood-src=*) LISFLOOD_SRC="${1#*=}"; shift ;;
    --lisflood-exe) need_arg $# "$1"; LISFLOOD_EXE_OPT="$2"; shift 2 ;;
    --lisflood-exe=*) LISFLOOD_EXE_OPT="${1#*=}"; shift ;;
    --anuga-python) need_arg $# "$1"; ANUGA_PYTHON_OPT="$2"; shift 2 ;;
    --anuga-python=*) ANUGA_PYTHON_OPT="${1#*=}"; shift ;;
    --env) need_arg $# "$1"; ENV_NAME="$2"; shift 2 ;;
    --env=*) ENV_NAME="${1#*=}"; shift ;;
    --profile) need_arg $# "$1"; PROFILE="$2"; shift 2 ;;
    --profile=*) PROFILE="${1#*=}"; shift ;;
    --ini) need_arg $# "$1"; INI="$2"; shift 2 ;;
    --ini=*) INI="${1#*=}"; shift ;;
    --skip-lisflood) SKIP_LISFLOOD=1; shift ;;
    --skip-anuga) SKIP_ANUGA=1; shift ;;
    --rebuild) REBUILD=1; shift ;;
    --recreate-env) RECREATE_ENV=1; shift ;;
    --no-settings) NO_SETTINGS=1; shift ;;
    --no-verify) NO_VERIFY=1; shift ;;
    --force) FORCE=1; shift ;;
    --yes | -y) YES=1; shift ;;
    --dry-run) DRY=1; shift ;;
    --remove-src) REMOVE_SRC=1; shift ;;
    --remove-env) REMOVE_ENV=1; shift ;;
    -h | --help) usage; exit 0 ;;
    *) printf 'Unknown option: %s\n\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$PREFIX" in
  "~") PREFIX="$HOME" ;;
  "~/"*) PREFIX="$HOME/${PREFIX#\~/}" ;;
esac
case "$PREFIX" in
  /*) ;;
  *) die "--prefix must be an absolute path: $PREFIX" 2 ;;
esac
[ "$PREFIX" != "/" ] || die "--prefix / is not allowed" 2
command -v python3 >/dev/null 2>&1 || die "python3 is needed to edit the QGIS settings (install the Xcode command line tools: xcode-select --install)" 2

case "$MODE" in
  install) do_install ;;
  check) do_check ;;
  uninstall) do_uninstall ;;
  write) do_write_only ;;
esac
