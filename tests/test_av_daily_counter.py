"""Tests for Alpha Vantage daily call counter."""
import datetime
import json

import pytest

from ingestion._av_daily_counter import AV_DAILY_CAP, get_remaining_calls, record_call, is_cap_reached


def test_default_remaining_is_cap(tmp_path, monkeypatch):
    monkeypatch.setattr("ingestion._av_daily_counter._COUNTER_PATH", str(tmp_path / "av.json"))
    assert get_remaining_calls() == AV_DAILY_CAP


def test_record_call_decrements(tmp_path, monkeypatch):
    monkeypatch.setattr("ingestion._av_daily_counter._COUNTER_PATH", str(tmp_path / "av.json"))
    record_call()
    assert get_remaining_calls() == AV_DAILY_CAP - 1


def test_record_multiple_calls(tmp_path, monkeypatch):
    monkeypatch.setattr("ingestion._av_daily_counter._COUNTER_PATH", str(tmp_path / "av.json"))
    record_call(5)
    assert get_remaining_calls() == AV_DAILY_CAP - 5


def test_cap_resets_on_new_day(tmp_path, monkeypatch):
    path = str(tmp_path / "av.json")
    monkeypatch.setattr("ingestion._av_daily_counter._COUNTER_PATH", path)
    with open(path, "w") as f:
        json.dump({"date": "2020-01-01", "calls": 25}, f)
    assert get_remaining_calls() == AV_DAILY_CAP


def test_is_cap_reached_when_exhausted(tmp_path, monkeypatch):
    monkeypatch.setattr("ingestion._av_daily_counter._COUNTER_PATH", str(tmp_path / "av.json"))
    record_call(AV_DAILY_CAP)
    assert is_cap_reached() is True


def test_is_cap_not_reached_when_calls_remain(tmp_path, monkeypatch):
    monkeypatch.setattr("ingestion._av_daily_counter._COUNTER_PATH", str(tmp_path / "av.json"))
    record_call(10)
    assert is_cap_reached() is False


def test_record_call_persists_to_file(tmp_path, monkeypatch):
    path = str(tmp_path / "av.json")
    monkeypatch.setattr("ingestion._av_daily_counter._COUNTER_PATH", path)
    record_call(3)
    with open(path) as f:
        data = json.load(f)
    assert data["calls"] == 3
    assert data["date"] == str(datetime.date.today())
