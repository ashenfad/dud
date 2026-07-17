import pytest

from dud import Session


@pytest.fixture
def session():
    """The conformance seam: everything tested through this fixture is
    the guest contract, backend-agnostic. VM rungs parameterize here."""
    s = Session()
    yield s
    s.close()
