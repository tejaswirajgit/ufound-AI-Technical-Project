"""The .env loader: PowerShell has no `source`, so the scripts read the file themselves."""
from __future__ import annotations

import os

import pytest

from reference import localenv

SAMPLE = """\
# a comment

export GOOGLE_MAPS_API_KEY=AIzaTEST123
MAKE_WEBHOOK_URL="https://example.test/hook"
   export SPACED   =   padded value
QUOTED='single'
EMPTY=
not_a_pair
WITH_EQUALS=a=b=c
"""


@pytest.fixture
def env_file(tmp_path):
    path = tmp_path / "dotenv"
    path.write_text(SAMPLE, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def clean_environment():
    keys = ["GOOGLE_MAPS_API_KEY", "MAKE_WEBHOOK_URL", "SPACED", "QUOTED", "EMPTY", "WITH_EQUALS"]
    saved = {k: os.environ.pop(k, None) for k in keys}
    yield
    for k, v in saved.items():
        os.environ.pop(k, None)
        if v is not None:
            os.environ[k] = v


def test_reads_values_with_and_without_export(env_file):
    localenv.load(env_file)
    assert os.environ["GOOGLE_MAPS_API_KEY"] == "AIzaTEST123"
    assert os.environ["MAKE_WEBHOOK_URL"] == "https://example.test/hook"


def test_strips_padding_and_matching_quotes(env_file):
    localenv.load(env_file)
    assert os.environ["SPACED"] == "padded value"
    assert os.environ["QUOTED"] == "single"
    assert os.environ["EMPTY"] == ""


def test_keeps_equals_signs_inside_the_value(env_file):
    localenv.load(env_file)
    assert os.environ["WITH_EQUALS"] == "a=b=c"


def test_skips_comments_blanks_and_malformed_lines(env_file):
    applied = localenv.load(env_file)
    assert "not_a_pair" not in applied and "not_a_pair" not in os.environ


def test_a_real_environment_variable_wins(env_file):
    os.environ["GOOGLE_MAPS_API_KEY"] = "set-in-the-shell"
    applied = localenv.load(env_file)
    assert os.environ["GOOGLE_MAPS_API_KEY"] == "set-in-the-shell"
    assert "GOOGLE_MAPS_API_KEY" not in applied


def test_a_missing_file_is_not_an_error(tmp_path):
    assert localenv.load(tmp_path / "nothing-here") == {}


def test_default_path_is_the_project_root(env_file):
    assert localenv.DEFAULT_PATH.name == ".env"
    assert (localenv.DEFAULT_PATH.parent / "env.example").is_file()
