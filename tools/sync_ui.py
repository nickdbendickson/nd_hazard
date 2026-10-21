#!/usr/bin/env python3
"""Single source of truth for the shared window files (the GUI grammar's widgets and tokens).

The plugins are installed separately, so each ships its own copy of the window files it shares
with the others. The canonical copies live in `shared/ui/` at the repo root; this tool writes
them into each plugin. Edit `shared/ui/` ONLY, never a plugin's copy, then run this tool.

    python3 tools/sync_ui.py           # write: copy shared/ui/ into each plugin that carries a file
    python3 tools/sync_ui.py --check   # verify: exit 1 if any plugin copy differs or is missing

tools/ci_checks.py runs the check on every PR, so a hand-edited copy fails the build instead of
drifting. `shared/ui/` is kept apart from the vendored `damsuite` package on purpose: damsuite is
pinned from DWR_Plugins (tools/sync_damsuite.py), these window files are this repo's own; the same
files exist in DWR_Plugins/shared/ui and are kept alike by hand. docs/gui-grammar.md says what
each widget is for. Pure stdlib.
"""
import argparse
import hashlib
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MASTER = os.path.join(ROOT, "shared", "ui")

_HRS = ("ND_Dam_Hazard_Helper", "nd_breach_runner", "nd_dam_screening")
# {file in shared/ui/: plugins that ship a copy at their root}
SHARED = {
    "tokens.py": _HRS + ("nd_data_updater",),
    "ui_widgets.py": _HRS,
    "ui_fit.py": _HRS + ("nd_data_updater",),
    "suite_settings.py": _HRS,
    "suite_settings_tools.py": _HRS,
    "suite_styles.py": _HRS,
    "suite_home.py": _HRS,
    "tool_dialog.py": _HRS,
}


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def check(root=ROOT):
    """Problems (strings); empty when every copy matches its canonical file."""
    master = os.path.join(root, "shared", "ui")
    problems = []
    for name in sorted(SHARED):
        src = os.path.join(master, name)
        if not os.path.isfile(src):
            problems.append("shared/ui/{} missing".format(name))
            continue
        want = _sha(src)
        for plugin in SHARED[name]:
            if not os.path.isdir(os.path.join(root, plugin)):
                continue
            dst = os.path.join(root, plugin, name)
            if not os.path.isfile(dst):
                problems.append("{}/{} missing - run tools/sync_ui.py".format(plugin, name))
            elif _sha(dst) != want:
                problems.append("{}/{} differs from shared/ui/{} - edit shared/ui/, then run "
                                "tools/sync_ui.py".format(plugin, name, name))
    return problems


def sync(root=ROOT):
    master = os.path.join(root, "shared", "ui")
    n = 0
    for name in sorted(SHARED):
        src = os.path.join(master, name)
        for plugin in SHARED[name]:
            pdir = os.path.join(root, plugin)
            if not os.path.isdir(pdir):
                continue
            dst = os.path.join(pdir, name)
            if os.path.isfile(dst) and _sha(dst) == _sha(src):
                continue
            shutil.copyfile(src, dst)
            n += 1
            print("wrote {}/{}".format(plugin, name))
    print("shared/ui: {} copy(ies) written; {} file(s) in sync".format(n, len(SHARED)))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="verify only; exit 1 on drift")
    args = ap.parse_args(argv)
    if args.check:
        problems = check()
        for p in problems:
            print("STALE: " + p)
        print("shared/ui: {}".format("{} problem(s)".format(len(problems)) if problems else "ok"))
        return 1 if problems else 0
    sync()
    return 0


if __name__ == "__main__":
    sys.exit(main())
