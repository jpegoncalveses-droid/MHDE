from adapters.base import Scores


def determine_status(scores: Scores) -> str:
    """Assign final status from total score. Access=1 always rejects."""
    if scores.access <= 1:
        return "Reject for v1"
    total = scores.total()
    if total >= 28:
        return "Core"
    if total >= 21:
        return "Useful but optional"
    if total >= 14:
        return "Fallback only"
    return "Reject for v1"
