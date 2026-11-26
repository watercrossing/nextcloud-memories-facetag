"""The version guard: on an untested Memories major the app switches itself off.

A real untested Memories can't be conjured, so the installed copy of Compatibility.php is stubbed
to claim only Memories 999 was tested — exactly what a new Memories major looks like to the app.
"""

import time

import pytest

import stackctl as s

# The web server caches app config in APCu for 3 s (AppConfig::LOCAL_CACHE_TTL); occ can't clear it
APPCONFIG_CACHE_SECONDS = 4
COMPAT = "/var/www/html/custom_apps/memories_facetag/lib/Service/Compatibility.php"


def sh(cmd: str) -> None:
    s.run(["exec", "-T", "-u", "www-data", "nextcloud", "sh", "-euc", cmd])


@pytest.fixture(scope="class")
def untested_memories():
    # opcache notices a changed file by its mtime, to the second: give stub and original different ones
    sh(f"cp {COMPAT} {COMPAT}.orig && sed -i \"s/'memories' => \\[[0-9, ]*\\]/'memories' => [999]/\" {COMPAT}"
       f" && grep -q \"'memories' => \\[999\\]\" {COMPAT} && touch -d @1000000000 {COMPAT}")
    s.occ("config:app:delete", "memories_facetag", "ignore_versions")
    try:
        yield
    finally:
        sh(f"mv {COMPAT}.orig {COMPAT} && touch {COMPAT}")
        s.occ("config:app:delete", "memories_facetag", "ignore_versions")
        time.sleep(APPCONFIG_CACHE_SECONDS)


@pytest.fixture(scope="module")
def a_file(photos):
    return next(iter(photos))


class TestUntestedMemories:
    """The stub is in place for this class only."""

    def test_untested_version_switches_the_app_off(self, untested_memories, api, a_file):
        r = api("GET", f"/file/{a_file}")
        assert r.status == 503, r.body
        assert "Switched off" in r.json()["message"]
        assert api("POST", f"/file/{a_file}/locate", {"x": 0.4, "y": 0.4, "width": 0.1, "height": 0.1}).status == 503

        stats = s.occ("memories_facetag:scan", "--settle-hours", "0", "--dry-run", check=False)
        assert stats.returncode == 1 and "Switched off" in stats.stdout, stats.stdout

        check = s.occ("memories_facetag:check", check=False)
        assert check.returncode == 1 and "is not a tested version" in check.stdout, check.stdout

        setup = s.occ("setupchecks", check=False).stdout
        assert "Switched off" in setup, setup


    def test_ignore_versions_lets_it_run(self, untested_memories, api, a_file):
        s.occ("config:app:set", "memories_facetag", "ignore_versions", "--value=yes")
        time.sleep(APPCONFIG_CACHE_SECONDS)
        assert api("GET", f"/file/{a_file}").status == 200
        r = s.occ("memories_facetag:scan", "--settle-hours", "0", "--dry-run", "--limit", "1", check=False)
        assert r.returncode == 0 and "Done: 1 photos" in r.stdout, r.stdout
        setup = s.occ("setupchecks", check=False).stdout
        assert "ignore_versions is set" in setup, setup


def test_restored(api, a_file):
    # After the stub is gone, the app is back on
    assert api("GET", f"/file/{a_file}").status == 200
    assert s.occ("memories_facetag:check", check=False).returncode == 0
