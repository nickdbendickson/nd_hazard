"""Tests of core/anuga_setup.py (no network, no conda): platform choice, folders, the pinned
checksums, download verification, the environment's python being a candidate."""
import hashlib
import io
import os
import sys
import tempfile

from nd_breach_runner.core import anuga_setup as AS, nd_data
from nd_breach_runner.tests.registry import test


@test
def anuga_setup_platform_key():
    assert AS.platform_key("linux", "x86_64") == "linux-64"
    assert AS.platform_key("win32", "AMD64") == "win-64"
    assert AS.platform_key("darwin", "arm64") == "osx-arm64"
    assert AS.platform_key("darwin", "x86_64") == "osx-64"
    for system, machine in (("linux", "aarch64"), ("win32", "ARM64"), ("freebsd13", "x86_64")):
        try:
            AS.platform_key(system, machine)
        except AS.SetupError as e:
            assert str(e)
        else:
            raise AssertionError((system, machine))


@test
def anuga_setup_checksums_pinned_for_every_platform():
    assert set(AS.MICROMAMBA_SHA256) == {"linux-64", "osx-64", "osx-arm64", "win-64"}
    for v in AS.MICROMAMBA_SHA256.values():
        assert len(v) == 64 and int(v, 16) >= 0
    assert AS.URL.format(v=AS.MICROMAMBA_VERSION, p="win-64").endswith("/micromamba-win-64")


@test
def anuga_setup_folders_and_candidate():
    with tempfile.TemporaryDirectory() as home:
        old = os.environ.get("NDBREACH_ANUGA_HOME")
        os.environ["NDBREACH_ANUGA_HOME"] = home
        try:
            assert AS.data_dir() == os.path.abspath(home)
            py = AS.env_python()
            assert py.startswith(os.path.join(os.path.abspath(home), "env"))
            assert nd_data.anuga_python_candidates() == [] or py not in nd_data.anuga_python_candidates()
            os.makedirs(os.path.dirname(py), exist_ok=True)
            open(py, "w").close()
            assert py in nd_data.anuga_python_candidates()
        finally:
            if old is None:
                os.environ.pop("NDBREACH_ANUGA_HOME")
            else:
                os.environ["NDBREACH_ANUGA_HOME"] = old


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


@test
def anuga_setup_download_checks_sha256():
    data = b"not really micromamba" * 1000
    good = hashlib.sha256(data).hexdigest()
    with tempfile.TemporaryDirectory() as d:
        dest = os.path.join(d, "bin", "mm")
        AS.download("https://x/y", dest, good, opener=lambda url, timeout=0: _Resp(data))
        assert open(dest, "rb").read() == data and not os.path.exists(dest + ".part")
        bad = os.path.join(d, "bin", "bad")
        try:
            AS.download("https://x/y", bad, "0" * 64, opener=lambda url, timeout=0: _Resp(data))
        except AS.SetupError as e:
            assert "SHA-256" in str(e)
        else:
            raise AssertionError("a wrong checksum was accepted")
        assert not os.path.exists(bad) and not os.path.exists(bad + ".part")

        def boom(url, timeout=0):
            raise OSError("no route")
        lines = []
        try:
            AS.download("https://x/y", bad, good, log=lines.append, opener=boom)
        except AS.SetupError as e:
            assert "internet" in str(e) and "no route" in str(e)
        else:
            raise AssertionError("a failed download was not reported")
        assert sum("attempt" in m for m in lines) == 3


@test
def anuga_setup_cancel_stops_the_download():
    data = b"x" * 5000
    with tempfile.TemporaryDirectory() as d:
        dest = os.path.join(d, "mm")
        try:
            AS.download("https://x/y", dest, "0" * 64, cancel=lambda: True,
                        opener=lambda url, timeout=0: _Resp(data))
        except AS.Cancelled:
            pass
        else:
            raise AssertionError("not cancelled")
        assert not os.path.exists(dest) and not os.path.exists(dest + ".part")


@test
def anuga_setup_remove_and_clean_env():
    with tempfile.TemporaryDirectory() as d:
        home = os.path.join(d, "anuga")
        assert AS.remove(home) is False
        os.makedirs(os.path.join(home, "env"))
        assert AS.remove(home) is True and not os.path.exists(home)
    os.environ["CONDA_PREFIX"] = "/somewhere"
    try:
        env = AS._clean_env("/h")
        assert "CONDA_PREFIX" not in env and env["MAMBA_ROOT_PREFIX"] == os.path.join("/h", "root")
    finally:
        os.environ.pop("CONDA_PREFIX")
