import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: marks tests that require a live agent CLI (excluded from default suite).",
    )
