import pytest

from flowtool import ratelimit


@pytest.fixture(autouse=True)
def _fresh_rate_limit():
    """The limiter is process-wide state; tests share one fake client, so start each clean."""
    ratelimit.reset()
    yield
