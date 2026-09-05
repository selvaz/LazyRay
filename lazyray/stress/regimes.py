"""Percentile regimes plus an auxiliary, full-sample-smoothed HMM."""
from __future__ import annotations

import numpy as np
import pandas as pd


def hysteresis_labels(probabilities) -> list[str]:
    """Enter stress at .60 and remain there until probability reaches .40."""
    label = "calm"
    labels = []
    for p in probabilities:
        if p >= .6:
            label = "stress"
        elif p <= .4:
            label = "calm"
        labels.append(label)
    return labels


def percentile_labels(index: pd.Series, min_history: int = 756) -> list[str]:
    """No-look-ahead expanding-percentile labels with three-tier hysteresis."""
    label, labels, seen = "calm", [], []
    for value in index:
        if pd.isna(value):
            labels.append(None)
            continue
        seen.append(float(value))
        if len(seen) < min_history:
            label = "calm"
        else:
            q85, q75, q60, q50 = np.quantile(seen, [.85, .75, .60, .50])
            if label == "stress":
                if value < q75:
                    label = "elevated" if value >= q60 else "calm"
            elif label == "elevated":
                if value >= q85:
                    label = "stress"
                elif value < q50:
                    label = "calm"
            elif value >= q85:
                label = "stress"
            elif value >= q60:
                label = "elevated"
            else:
                label = "calm"
        labels.append(label)
    return labels


def _fit_hmm(valid: pd.Series):
    from hmmlearn.hmm import GaussianHMM

    for components in (3, 2):
        model = GaussianHMM(n_components=components, covariance_type="diag", n_iter=75, random_state=42)
        try:
            model.fit(valid.to_numpy().reshape(-1, 1))
            if model.monitor_.converged:
                return model
        except (ValueError, FloatingPointError):
            pass
    return None


def fit_regimes(index: pd.Series, min_history: int = 756) -> pd.DataFrame:
    """Return causal percentile regimes and an auxiliary smoothed HMM regime.

    The HMM is fit on the full sample, so its posterior and ``regime_hmm`` are
    deliberately relabelled at each refit and are not point-in-time labels.
    """
    out = pd.DataFrame(index=index.index, columns=["p_stress", "regime", "regime_hmm"], dtype=object)
    valid = index.dropna()
    out.loc[valid.index, "regime"] = percentile_labels(valid, min_history)
    if len(valid) < 20 or valid.nunique() < 2:
        out.loc[valid.index, "p_stress"] = 0.0
        out.loc[valid.index, "regime_hmm"] = "calm"
        return out
    model = _fit_hmm(valid)
    if model is None:
        out.loc[valid.index, "p_stress"] = 0.0
        out.loc[valid.index, "regime_hmm"] = "calm"
        return out
    # State ids are arbitrary. Mapping them by fitted mean makes the reported
    # stress state stable even when hmmlearn assigns the two ids differently.
    state_by_mean = np.argsort(model.means_.ravel())
    stress_state = int(state_by_mean[-1])
    probabilities = model.predict_proba(valid.to_numpy().reshape(-1, 1))[:, stress_state]
    out.loc[valid.index, "p_stress"] = probabilities
    out.loc[valid.index, "regime_hmm"] = hysteresis_labels(probabilities)
    return out
