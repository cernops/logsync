"""Setup shared by all pytest-collected test suites."""

import random


def pytest_runtest_setup() -> None:
    """Give every test an independent, reproducible pseudorandom sequence."""
    random.seed(0)
