"""One-time ANUGA setup without a conda install by hand.

Downloads micromamba (one file, no administrator rights, no conda needed) from its official
GitHub release, checks it against a pinned SHA-256, and uses it to create a private
environment with ANUGA from conda-forge in the plugin's own data folder. The Python
interpreter of that environment is what the Runner's "ANUGA Python" setting wants; this module
returns it, and `nd_data.anuga_python_candidates` also finds it on its own.

Needs the internet once (a few hundred MB to download, about 1.6 GB on disk afterwards).
Windows 64-bit, Linux x86-64, macOS Intel and macOS Apple silicon are covered; Linux ARM has no
ANUGA on conda-forge. No Qt or QGIS imports: the command line uses it too.
"""
import hashlib
import os
import shutil
import subprocess
import sys

from ..damsuite import netproxy                  # QGIS's proxy setting applies (3.20.2)

MICROMAMBA_VERSION = "2.9.0-0"
URL = ("https://github.com/mamba-org/micromamba-releases/releases/download/{v}/"
       "micromamba-{p}")
# SHA-256 of each file of release 2.9.0-0 (the .sha256 files of that release)
MICROMAMBA_SHA256 = {
    "linux-64": "366cd9cd8be14df1ab8ed50352a82111082a36686b2d389fdb79a92c3fafb3e3",
    "osx-64": "1e71054bb3ac9a076e21f7ec48acfef536f9b3f1408f371a942784bf5ef83d8a",
    "osx-arm64": "ec2a072f028e1a7cf20f3e2e74d5a8127cf5a5f27636375b5359811565f4e5be",
    "win-64": "a6d804394b2418991c4e29562853eaace2f2ce9d9da661a98e74e02e8dbb44b0",
}
PACKAGES = ["python=3.12", "anuga", "numpy", "scipy", "matplotlib"]
CHANNEL = "conda-forge"


class SetupError(RuntimeError):
    pass


class Cancelled(SetupError):
    pass


def platform_key(system=None, machine=None):
    """micromamba's platform name for this computer, or raises SetupError."""
    system = (system or sys.platform).lower()
    import platform as _pf
    machine = (machine or _pf.machine()).lower()
    arm = machine in ("arm64", "aarch64")
    if system.startswith("win"):
        if arm:
            raise SetupError("Windows on ARM: conda-forge has no ANUGA for it. Skip ANUGA "
                             "(LISFLOOD-FP or the built-in engine run everything else).")
        return "win-64"
    if system == "darwin":
        return "osx-arm64" if arm else "osx-64"
    if system.startswith("linux"):
        if arm:
            raise SetupError("Linux on ARM: conda-forge has no ANUGA for it. Skip ANUGA "
                             "(LISFLOOD-FP or the built-in engine run everything else).")
        return "linux-64"
    raise SetupError("Unsupported system {!r}".format(system))


def data_dir():
    """The plugin's data folder for ANUGA (NDBREACH_ANUGA_HOME overrides it)."""
    env = os.environ.get("NDBREACH_ANUGA_HOME")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        return os.path.join(base, "ndbreach", "anuga")
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support",
                            "ndbreach", "anuga")
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "ndbreach", "anuga")


def env_dir(home=None):
    return os.path.join(home or data_dir(), "env")


def env_python(home=None):
    e = env_dir(home)
    return os.path.join(e, "python.exe") if sys.platform.startswith("win") else os.path.join(e, "bin", "python")


def micromamba_path(home=None):
    name = "micromamba.exe" if sys.platform.startswith("win") else "micromamba"
    return os.path.join(home or data_dir(), "bin", name)


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url, dest, expected_sha256, log=None, cancel=None, opener=None):
    """Download `url` to `dest` (written as dest + '.part' first) and check its SHA-256."""
    say = log or (lambda m: None)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = dest + ".part"
    say("Downloading {}".format(url))
    last = None
    for attempt in range(3):
        try:
            with (opener or netproxy.urlopen)(url, timeout=60) as r, open(part, "wb") as f:
                while True:
                    if cancel and cancel():
                        raise Cancelled("cancelled")
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
            last = None
            break
        except Cancelled:
            _rm(part)
            raise
        except Exception as e:                                   # noqa
            last = e
            say("  attempt {} failed: {}".format(attempt + 1, e))
    if last is not None:
        _rm(part)
        raise SetupError("Could not download {}: {}. This needs the internet once (github.com "
                         "and conda.anaconda.org must be reachable; behind a proxy set it in QGIS "
                         "(Settings > Options > Network) or in the HTTPS_PROXY environment "
                         "variable).".format(url, last))
    got = sha256_of(part)
    if got != expected_sha256:
        _rm(part)
        raise SetupError("The downloaded file does not match its expected SHA-256 ({} != {}); "
                         "it was deleted.".format(got, expected_sha256))
    os.replace(part, dest)


def _rm(p):
    try:
        os.remove(p)
    except OSError:
        pass


def get_micromamba(home=None, log=None, cancel=None, opener=None):
    """The path of a verified micromamba, downloading it when needed."""
    plat = platform_key()
    mm = micromamba_path(home)
    want = MICROMAMBA_SHA256[plat]
    if os.path.isfile(mm) and sha256_of(mm) == want:
        return mm
    download(URL.format(v=MICROMAMBA_VERSION, p=plat), mm, want,
             log=log, cancel=cancel, opener=opener)
    if not sys.platform.startswith("win"):
        os.chmod(mm, 0o755)
    return mm


def _clean_env(home):
    env = dict(os.environ)
    for k in list(env):
        if k.startswith(("CONDA", "MAMBA")) and k != "CONDA_OVERRIDE_OSX":
            env.pop(k)
    for k in ("PYTHONHOME", "PYTHONPATH", "QGIS_PREFIX_PATH"):
        env.pop(k, None)
    env["MAMBA_ROOT_PREFIX"] = os.path.join(home, "root")
    return netproxy.env_for("https://conda.anaconda.org/", env)     # 3.20.2: QGIS's proxy


def _run(cmd, env, log, cancel):
    """Run `cmd`, passing each output line to `log`; stop it when `cancel()` is true."""
    kw = {}
    if sys.platform.startswith("win"):
        kw["creationflags"] = 0x08000000                         # CREATE_NO_WINDOW
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                         universal_newlines=True, errors="replace", **kw)
    tail = []
    try:
        for line in p.stdout:
            line = line.rstrip()
            if line:
                tail = (tail + [line])[-25:]
                log(line)
            if cancel and cancel():
                p.kill()
                raise Cancelled("cancelled")
        p.wait()
    finally:
        if p.poll() is None:
            p.kill()
        p.stdout.close()
    if p.returncode != 0:
        raise SetupError("micromamba failed (exit {}):\n{}".format(p.returncode, "\n".join(tail[-12:])))


def check(python, workdir=None):
    """(ok, message): ANUGA imports in `python`."""
    from . import anuga_model
    import tempfile
    wd = workdir or tempfile.mkdtemp(prefix="ndbreach_anuga_check_")
    try:
        r = anuga_model.check_anuga(python, wd)
    finally:
        if not workdir:
            shutil.rmtree(wd, ignore_errors=True)
    return r["ok"], r["message"]


def installed(home=None):
    """The private environment's python when ANUGA imports in it, else ''."""
    py = env_python(home)
    if os.path.isfile(py) and check(py)[0]:
        return py
    return ""


def setup(home=None, reinstall=False, log=None, cancel=None, opener=None):
    """Create the private ANUGA environment and return its python. Already there and working:
    returned as is (unless `reinstall`)."""
    say = log or (lambda m: None)
    home = home or data_dir()
    plat = platform_key()
    py = env_python(home)
    if not reinstall and os.path.isfile(py):
        ok, msg = check(py)
        if ok:
            say("ANUGA is already set up: {} ({})".format(py, msg))
            return py
        say("The existing environment does not work ({}); creating it again.".format(msg.strip()[:200]))
    mm = get_micromamba(home, log=say, cancel=cancel, opener=opener)
    env = _clean_env(home)
    os.makedirs(env["MAMBA_ROOT_PREFIX"], exist_ok=True)
    shutil.rmtree(env_dir(home), ignore_errors=True)
    say("Creating the ANUGA environment with micromamba (a few minutes; downloads a few hundred MB)...")
    cmd = [mm, "create", "-y", "--no-rc", "-p", env_dir(home), "-c", CHANNEL, "--override-channels"] + PACKAGES
    try:
        _run(cmd, env, say, cancel)
    except SetupError:
        shutil.rmtree(env_dir(home), ignore_errors=True)
        raise
    try:                      # the package cache is a second copy of the environment (~0.5 GB)
        _run([mm, "clean", "--all", "-y", "--no-rc"], env, lambda m: None, cancel)
    except SetupError:
        pass
    if not os.path.isfile(py):
        raise SetupError("micromamba finished but {} does not exist".format(py))
    ok, msg = check(py)
    if not ok:
        raise SetupError("The environment was created but ANUGA does not import in it:\n" + msg)
    say("Done: {} ({})".format(py, msg))
    return py


def remove(home=None):
    """Delete everything this module created. True when something was deleted."""
    home = home or data_dir()
    if not os.path.isdir(home):
        return False
    shutil.rmtree(home)
    return True
