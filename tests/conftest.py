"""Configuration shared by the tests."""

import pytest

from axion_wizard.render.console import console, error_console


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path, monkeypatch):
    """No test runs with the repository's real working directory.

    Without this, a test that invokes the CLI without `--project-dir` and with
    incomplete mocking — failing before it reaches where it meant to, but not
    before some step has already written something — leaves stray files in the
    repo root. This really happened: `test_no_elevate_flag_skips_elevation`
    and one or two others wrote `axion/.axion-wizard-state.json` and
    `axion/nginx/certs/` into the project directory itself, during ordinary
    runs of the suite.

    Every test that genuinely needs a specific path already uses its own
    `tmp_path`; isolating the cwd on top changes nothing for those.
    """
    monkeypatch.chdir(tmp_path)

#: A fixed width for Rich's panels during the tests.
#:
#: Without this, the width is decided by the environment: Rich falls back to
#: 80 columns when the output is not a terminal, and there a long path —
#: pytest's `tmp_path`, for one — wraps across several lines. Assertions like
#: `assert "docker-compose.yml" in result.stderr` passed on Windows and failed
#: on Linux purely because of the temp directory's length, which has nothing
#: to do with what the test is checking.
TEST_CONSOLE_WIDTH = 200


@pytest.fixture(autouse=True, scope="session")
def _wide_consoles():
    """Pin the shared consoles' width for the whole session."""
    console.width = TEST_CONSOLE_WIDTH
    error_console.width = TEST_CONSOLE_WIDTH
    yield
