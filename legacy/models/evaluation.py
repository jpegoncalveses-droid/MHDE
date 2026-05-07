from __future__ import annotations

import logging

logger = logging.getLogger("mhde.models.evaluation")


def evaluate(model, X_test, y_test) -> dict:
    try:
        import numpy as np
        preds = model.predict(X_test)
        proba = model.predict_proba(X_test)[:, 1] if hasattr(model, "predict_proba") else None

        correct = sum(p == y for p, y in zip(preds, y_test))
        accuracy = correct / len(y_test) if y_test.size else 0.0
        positive_rate = float(y_test.mean()) if y_test.size else 0.0

        result = {
            "accuracy": accuracy,
            "positive_rate": positive_rate,
            "n_test": len(y_test),
        }

        if proba is not None:
            try:
                from sklearn.metrics import roc_auc_score
                result["auc"] = float(roc_auc_score(y_test, proba))
            except Exception:
                pass

        return result
    except Exception as exc:
        logger.error("Evaluation failed: %s", exc)
        return {}
