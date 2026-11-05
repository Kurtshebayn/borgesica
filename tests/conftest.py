"""Test-wide isolation.

Providers read a refreshable price cache from ``~/.borgesica/prices.json``. A
developer's real cache must never leak into tests (it would change prices and
make results machine-dependent), so every test points the cache at a path that
does not exist unless it opts in with its own file.
"""

from __future__ import annotations

import pytest

from borgesica.adapters.providers.price_cache import ENV_AUTO_REFRESH, ENV_PATH


@pytest.fixture(autouse=True)
def _isolated_price_cache(tmp_path_factory, monkeypatch):
    monkeypatch.setenv(ENV_PATH, str(tmp_path_factory.mktemp("prices") / "prices.json"))
    monkeypatch.setenv(ENV_AUTO_REFRESH, "0")  # no network from the test suite
