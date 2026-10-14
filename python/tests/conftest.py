"""Shared fixtures and Hypothesis profiles."""

import os

import pytest
from hypothesis import HealthCheck, settings

from inputs import CASES

settings.register_profile(
    "default", max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)
settings.register_profile("thorough", max_examples=1000, deadline=None)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "default"))


@pytest.fixture(params=list(CASES))
def case(request):
    return CASES[request.param]
