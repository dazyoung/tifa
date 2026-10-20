"""Fixtures for the operator tools' tests: synthetic recordings written to a temporary directory."""

import pytest

from tests.tools import recorder


@pytest.fixture
def recording(tmp_path):
    """The standard local recording (tests/tools/recorder.py): four hours, a 30-minute gap, half an hour more."""
    directory = tmp_path / "recording"
    recorder.record(directory, recorder.LOCAL_POLLS)
    return directory
