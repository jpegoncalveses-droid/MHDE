"""Load-path tests for the MHDE host Telegram credentials file.

Step 1 of the ATSRP shelving: Telegram creds now live in a gitignored MHDE
host env file (default ~/.config/mhde/telegram.env, overridable via
MHDE_ENV_FILE). These tests pin the new load path and assert MHDE no longer
references the ATSRP .env path.
"""
from __future__ import annotations

import inspect
import os

import pytest


@pytest.fixture(autouse=True)
def _clean_telegram_env():
    """Snapshot/restore the env vars these tests touch, so load_env_file's
    os.environ.setdefault side effects never leak between tests."""
    keys = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "MHDE_ENV_FILE"]
    saved = {k: os.environ.get(k) for k in keys}
    for k in keys:
        os.environ.pop(k, None)
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_load_env_file_sets_keys_from_file(tmp_path):
    env = tmp_path / "telegram.env"
    env.write_text("TELEGRAM_BOT_TOKEN=tok123\nTELEGRAM_CHAT_ID=chat456\n")
    from storage.config import load_env_file

    load_env_file(env)

    assert os.environ["TELEGRAM_BOT_TOKEN"] == "tok123"
    assert os.environ["TELEGRAM_CHAT_ID"] == "chat456"


def test_load_env_file_tolerates_comments_quotes_and_blanks(tmp_path):
    env = tmp_path / "telegram.env"
    env.write_text(
        "# a comment\n\nTELEGRAM_BOT_TOKEN=\"q-tok\"\nTELEGRAM_CHAT_ID='q-chat'\n"
    )
    from storage.config import load_env_file

    load_env_file(env)

    assert os.environ["TELEGRAM_BOT_TOKEN"] == "q-tok"
    assert os.environ["TELEGRAM_CHAT_ID"] == "q-chat"


def test_load_env_file_does_not_override_existing_env(tmp_path):
    os.environ["TELEGRAM_BOT_TOKEN"] = "from-systemd"
    env = tmp_path / "telegram.env"
    env.write_text("TELEGRAM_BOT_TOKEN=from-file\n")
    from storage.config import load_env_file

    load_env_file(env)

    assert os.environ["TELEGRAM_BOT_TOKEN"] == "from-systemd"


def test_load_env_file_missing_file_is_noop(tmp_path):
    from storage.config import load_env_file

    load_env_file(tmp_path / "does-not-exist.env")  # must not raise

    assert "TELEGRAM_BOT_TOKEN" not in os.environ


def test_load_engine_config_reads_telegram_from_mhde_env_file(tmp_path):
    """The notifications path (storage.config -> notifications.telegram) must
    get creds with NO pre-set env and WITHOUT importing the FX bot."""
    env = tmp_path / "telegram.env"
    env.write_text("TELEGRAM_BOT_TOKEN=cfg-tok\nTELEGRAM_CHAT_ID=cfg-chat\n")
    os.environ["MHDE_ENV_FILE"] = str(env)
    from storage.config import load_engine_config

    cfg = load_engine_config()

    assert cfg["telegram_bot_token"] == "cfg-tok"
    assert cfg["telegram_chat_id"] == "cfg-chat"


def test_fx_bot_credentials_load_from_mhde_env_file(tmp_path):
    env = tmp_path / "telegram.env"
    env.write_text("TELEGRAM_BOT_TOKEN=bot-tok\nTELEGRAM_CHAT_ID=bot-chat\n")
    os.environ["MHDE_ENV_FILE"] = str(env)
    from fx.bot import telegram_bot

    token, chat = telegram_bot._credentials()

    assert token == "bot-tok"
    assert chat == "bot-chat"


def test_fx_bot_source_no_longer_references_atsrp_path():
    """The whole point of step 1: MHDE must not reference /home/jpcg/ATSRP/.env."""
    from fx.bot import telegram_bot

    assert "ATSRP" not in inspect.getsource(telegram_bot)
