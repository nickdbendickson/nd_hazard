#!/usr/bin/env python3
"""Verify (and re-vendor) the vendored `damsuite` package against the shared pin.

`damsuite` is a single shared package that BOTH forks vendor from: the DamSafety_* plugins in the
DWR_Plugins repo, and the nd_* plugins here. The canonical source lives in DWR_Plugins
(`shared/damsuite/`); a content fingerprint of it is pinned in `tools/damsuite_pin.json`, committed
byte-identical into both repos. This tool checks each nd plugin's vendored copy against that pin,
so NORMAL CI on every PR can prove the copies match THIS repo's committed pin (and that the pin is
internally consistent) WITHOUT a checkout of the other repo and WITHOUT network access. Keeping
this repo's pin byte-identical to DamSafety's canonical pin is a separate, cross-repo mirror step
(a maintainer copies tools/damsuite_pin.json); no single repo's CI can verify that across repos.

    python3 tools/sync_damsuite.py --check                 # verify: exit 1 on any drift from the pin
    python3 tools/sync_damsuite.py --from <DWR>/shared/damsuite   # re-vendor from the pinned source

Each plugin here vendors the SAME subset of the shared package (every pinned module except the
DamSafety-only modules listed in ND_NOT_VENDORED). Three modules are per-fork and NOT compared by
content (the pin's `fork_specific` list): __init__.py (module manifest + SUITE_VERSION), versions.py
(branding + releases) and suite_home.py (DamSafety couples it to the DamSafety-only structure_flags
module; nd ships the same file minus that feature until that port is decided). Pure stdlib; no
QGIS/numpy/scipy needed.
"""
import argparse
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.dirname(os.path.abspath(__file__))
PIN_PATH = os.path.join(TOOLS, "damsuite_pin.json")
FILES_NAME = "FILES.json"

# the nd plugins that vendor the shared damsuite package
ND_PLUGINS = ("ND_Dam_Hazard_Helper", "nd_breach_runner", "nd_dam_screening", "nd_data_updater")

# Pinned modules that nd does NOT vendor - DamSafety-only extensions nd has not adopted (the
# end-state decision - one product + a branding skin vs. an extracted shared package - is still
# open). Listing them here keeps CI from flagging them as "missing" while making the gap explicit.
ND_NOT_VENDORED = frozenset({
    "health.py", "layout.py", "pack_convert.py", "record.py",
    "servers.py", "structure_flags.py", "suite_config.py", "suite_fill.py",
})


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _read_pin():
    if not os.path.isfile(PIN_PATH):
        return None
    with open(PIN_PATH, encoding="utf-8") as f:
        return json.load(f)


def _vendored_py(folder):
    return sorted(n for n in os.listdir(folder)
                  if n.endswith(".py") and os.path.isfile(os.path.join(folder, n)))


def _manifest(folder):
    return {n: _sha(os.path.join(folder, n)) for n in _vendored_py(folder)}


def _manifest_text(folder):
    return json.dumps(_manifest(folder), indent=1, sort_keys=True) + "\n"


def _expected(pin):
    """The .py set each nd plugin should vendor: every pinned module except the DamSafety-only ones."""
    return set(pin["files"]) - ND_NOT_VENDORED


def check(root=ROOT):
    """Return a list of drift problems against the pin; [] means every nd copy matches the pinned
    shared source (content-checked modules) and its FILES.json is current."""
    problems = []
    pin = _read_pin()
    if pin is None:
        return ["pin missing: tools/{}".format(os.path.basename(PIN_PATH))]
    pin_files = pin["files"]
    fork_specific = set(pin.get("fork_specific") or ())
    # the committed pin must be internally consistent: pin_sha256 is a fingerprint over the
    # content-checked (non-fork-specific) files, computed exactly as DamSafety's --write-pin does.
    # This catches a corrupt or hand-edited pin (e.g. a file hash bumped without rewriting
    # pin_sha256) before it is trusted. Keeping this repo's pin identical to DamSafety's canonical
    # pin is a separate, cross-repo step (mirror tools/damsuite_pin.json); no single repo's CI can
    # verify that without reaching the other repo.
    content = {k: v for k, v in pin_files.items() if k not in fork_specific}
    want_sha = hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    if pin.get("pin_sha256") != want_sha:
        problems.append("pin is internally inconsistent: pin_sha256 does not match its files map "
                        "(tools/damsuite_pin.json is corrupt or was hand-edited; re-mirror it from "
                        "DWR_Plugins)")
    expected = _expected(pin)
    seen = {}                                                   # name -> {sha: [plugins]} for consistency
    for plugin in ND_PLUGINS:
        folder = os.path.join(root, plugin, "damsuite")
        if not os.path.isdir(folder):
            problems.append("{}: no damsuite/".format(plugin))
            continue
        have = set(_vendored_py(folder))
        for name in sorted(expected - have):
            problems.append("{}/damsuite/{} is missing (pinned in the shared source)".format(
                plugin, name))
        for name in sorted(have - expected):
            why = ("DamSafety-only; not vendored by nd" if name in ND_NOT_VENDORED
                   else "not in the shared pin")
            problems.append("{}/damsuite/{} should not be vendored ({})".format(plugin, name, why))
        for name in sorted(have & expected):
            sha = _sha(os.path.join(folder, name))
            seen.setdefault(name, {}).setdefault(sha, []).append(plugin)
            if name in fork_specific:
                continue                                        # per-fork content; not compared
            if sha != pin_files.get(name):
                problems.append("{}/damsuite/{} differs from the shared pin ({})".format(
                    plugin, name, pin["pin"]))
        mpath = os.path.join(folder, FILES_NAME)
        if not os.path.isfile(mpath):
            problems.append("{}/damsuite/{} missing".format(plugin, FILES_NAME))
        else:
            with open(mpath, encoding="utf-8") as f:
                on_disk = f.read()
            if on_disk != _manifest_text(folder):
                problems.append("{}/damsuite/{} is stale (does not match the files)".format(
                    plugin, FILES_NAME))
    # every nd copy must agree with the others, even on the per-fork files
    for name in sorted(seen):
        if len(seen[name]) > 1:
            problems.append("nd copies of {} disagree with each other: {}".format(
                name, {k[:12]: v for k, v in seen[name].items()}))
    return problems


def revendor(from_dir, root=ROOT):
    """Copy the pinned, content-shared modules from a DWR `shared/damsuite` directory into each nd
    plugin, and rewrite FILES.json. Leaves the per-fork files (fork_specific) untouched. This is the
    occasional 'advance to a new pin' step; it needs the other repo checked out (CI does not)."""
    pin = _read_pin()
    if pin is None:
        print("pin missing: tools/{}".format(os.path.basename(PIN_PATH)))
        return 1
    fork_specific = set(pin.get("fork_specific") or ())
    to_copy = sorted(_expected(pin) - fork_specific)            # content-shared modules nd carries
    changes = []
    for plugin in ND_PLUGINS:
        folder = os.path.join(root, plugin, "damsuite")
        if not os.path.isdir(folder):
            continue
        for name in to_copy:
            src = os.path.join(from_dir, name)
            if not os.path.isfile(src):
                print("source missing: {}".format(src))
                return 1
            with open(src, "rb") as f:
                want = f.read()
            dst = os.path.join(folder, name)
            have = None
            if os.path.isfile(dst):
                with open(dst, "rb") as f:
                    have = f.read()
            if want != have:
                with open(dst, "wb") as f:
                    f.write(want)
                changes.append("{} {}/damsuite/{}".format(
                    "added" if have is None else "updated", plugin, name))
        mpath = os.path.join(folder, FILES_NAME)
        want_m = _manifest_text(folder)
        have_m = ""
        if os.path.isfile(mpath):
            with open(mpath, encoding="utf-8") as f:
                have_m = f.read()
        if want_m != have_m:
            with open(mpath, "w", encoding="utf-8") as f:
                f.write(want_m)
            changes.append("refreshed {}/damsuite/{}".format(plugin, FILES_NAME))
    for c in changes:
        print(c)
    if not changes:
        print("damsuite: already matches the source; nothing to re-vendor")
    print("note: run --check to confirm the copies now match the pin, and update "
          "tools/damsuite_pin.json from DWR if the pin itself advanced")
    return 0


def main(argv):
    ap = argparse.ArgumentParser(description="Verify the vendored damsuite copies against the shared pin.")
    ap.add_argument("--check", action="store_true",
                    help="verify only; exit 1 if any vendored copy, FILES.json or the pin drifted")
    ap.add_argument("--from", dest="from_dir", metavar="DIR",
                    help="re-vendor the content-shared modules from a DWR shared/damsuite directory")
    args = ap.parse_args(argv)
    if args.from_dir:
        return revendor(args.from_dir)
    if args.check:
        problems = check()
        if problems:
            print("damsuite drift (against the shared pin):")
            for p in problems:
                print("  - " + p)
            print("re-vendor with: python3 tools/sync_damsuite.py --from <DWR_Plugins>/shared/damsuite")
            return 1
        pin = _read_pin()
        print("damsuite: all {} nd copies match the shared pin {} ({} modules each)".format(
            len(ND_PLUGINS), pin["pin"], len(_expected(pin))))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
