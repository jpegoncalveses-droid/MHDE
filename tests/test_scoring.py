from adapters.base import Scores
from runner.scoring import determine_status


def test_core_threshold():
    s = Scores(5, 5, 4, 5, 5, 5, 5)   # total=34
    assert determine_status(s) == "Core"


def test_useful_but_optional():
    s = Scores(4, 3, 3, 3, 3, 4, 4)   # total=24
    assert determine_status(s) == "Useful but optional"


def test_fallback_only():
    s = Scores(3, 2, 2, 2, 3, 3, 3)   # total=18
    assert determine_status(s) == "Fallback only"


def test_reject_for_v1_low_score():
    s = Scores(2, 2, 1, 2, 2, 2, 2)   # total=13
    assert determine_status(s) == "Reject for v1"


def test_reject_for_v1_access_1():
    s = Scores(1, 5, 5, 5, 5, 5, 5)   # total=31 but access=1
    assert determine_status(s) == "Reject for v1"
