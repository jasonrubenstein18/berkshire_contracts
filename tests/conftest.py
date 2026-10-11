"""Tests run offline against a throwaway data directory: no OpenAI calls and no model downloads."""

import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="contracts-tests-")

import pytest


@pytest.fixture(scope="session", autouse=True)
def _database():
    from berkshire_contracts.db import init_db

    init_db()
