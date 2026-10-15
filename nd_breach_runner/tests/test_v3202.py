"""3.20.2: the ANUGA one-click setup downloads through the proxy set in QGIS (damsuite.netproxy):
micromamba itself through netproxy.urlopen, and micromamba's own package downloads through
HTTPS_PROXY in the environment it is started with. QGIS's settings are stood in for; the
machine's QGIS profile is not touched."""
import os

from nd_breach_runner.tests.registry import test


@test
def v3202_anuga_setup_downloads_through_the_proxy_set_in_qgis():
    from nd_breach_runner.core import anuga_setup as A
    from nd_breach_runner.damsuite import netproxy as NP
    real = NP.qgis_settings
    try:
        NP.qgis_settings = lambda: {"proxyEnabled": "true", "proxyType": "HttpProxy",
                                    "proxyHost": "proxy.nd.gov", "proxyPort": "8080"}
        env = A._clean_env("/tmp/anuga_home")
        assert env["HTTPS_PROXY"] == env["https_proxy"] == "http://proxy.nd.gov:8080"
        assert env["MAMBA_ROOT_PREFIX"] == os.path.join("/tmp/anuga_home", "root")
        NP.qgis_settings = lambda: {}                       # no proxy in QGIS: the computer's
        env = A._clean_env("/tmp/anuga_home")
        assert env.get("HTTPS_PROXY") == os.environ.get("HTTPS_PROXY")
    finally:
        NP.qgis_settings = real
    calls = []
    real_open = NP.urlopen
    try:
        def fake(url, timeout=60):
            calls.append(url)
            raise OSError("offline")
        NP.urlopen = fake
        try:
            A.download("https://github.com/x/micromamba", "/tmp/anuga_home_dl/mm", "0" * 64)
        except A.SetupError as e:
            assert "Settings > Options > Network" in str(e)
        assert calls == ["https://github.com/x/micromamba"] * 3, calls
    finally:
        NP.urlopen = real_open
