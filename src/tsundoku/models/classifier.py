import numpy as np
import pandas as pd
from cytoolz import frequencies, keyfilter
from scipy.sparse import csr_matrix, hstack
from sklearn.base import BaseEstimator
from sklearn.calibration import CalibratedClassifierCV
from sklearn.metrics import classification_report
from sklearn.model_selection import KFold, StratifiedKFold, train_test_split
from sklearn.utils.class_weight import compute_sample_weight
from xgboost.sklearn import XGBClassifier


class PartiallyLabeledXGB(object):
    def __init__(self, xgb_params=None, seed=42):
        self.xgb = XGBClassifier(**xgb_params)
        self.calibrated_clf = None

    def fit(self, X, y, eval_set=None, eval_fraction=0.1, early_stopping_rounds=10):
        F = X

        labeled_idx = np.arange(len(y))[pd.notnull(y)]
        print("# rows with label", len(labeled_idx))

        F_labeled = F[labeled_idx]
        y_labeled = y[labeled_idx]

        fit_params = {}

        if eval_set is not None:
            fit_params["eval_set"] = eval_set
            fit_params["early_stopping_rounds"] = early_stopping_rounds
            fit_params["sample_weight"] = compute_sample_weight("balanced", y_labeled)
            self.xgb.fit(F_labeled, y_labeled, **fit_params)
        elif eval_fraction > 0:
            F_train, F_val, y_train, y_val = train_test_split(
                F_labeled, y_labeled, test_size=eval_fraction, stratify=y_labeled
            )
            print("# validation rows", len(y_val))
            fit_params["eval_set"] = [[F_val, y_val]]
            fit_params["early_stopping_rounds"] = early_stopping_rounds
            fit_params["sample_weight"] = compute_sample_weight("balanced", y_train)
            self.xgb.fit(F_train, y_train, **fit_params)
        else:
            fit_params["sample_weight"] = compute_sample_weight("balanced", y_labeled)
            self.xgb.fit(F_labeled, y_labeled, **fit_params)

        self.classes_ = self.xgb.classes_

    def fit_calibrate(
        self,
        X,
        y,
        val_fraction=0.1,
        eval_set=None,
        eval_fraction=0.1,
        early_stopping_rounds=10,
    ):
        labeled_idx = np.arange(len(y))[pd.notnull(y)]
        y_labeled = y[labeled_idx]
        X_labeled = X[labeled_idx]

        X_train, X_val, y_train, y_val = train_test_split(
            X_labeled, y_labeled, test_size=val_fraction
        )
        print("# calibration rows", len(y_val))
        print(frequencies(y_val))
        self.fit(
            X_train,
            y_train,
            eval_set=eval_set,
            eval_fraction=eval_fraction,
            early_stopping_rounds=early_stopping_rounds,
        )

        self.calibrated_clf = CalibratedClassifierCV(
            self.xgb, cv="prefit", method="isotonic"
        )
        self.calibrated_clf.fit(X_val, y_val)

    def predict(self, X):
        F = X

        if self.calibrated_clf is not None:
            return self.calibrated_clf.predict(F)

        return self.xgb.predict(F)

    def predict_proba(self, X):
        F = X
        if self.calibrated_clf is not None:
            return self.calibrated_clf.predict_proba(F)
        return self.xgb.predict_proba(F)

    def classify_and_report(self, y_test, X_test, output_dict=False):
        labeled_idx = np.arange(len(y_test))[pd.notnull(y_test)]
        # print('# rows with label', len(labeled_idx))
        y_labeled = y_test[labeled_idx]
        X_labeled = X_test[labeled_idx]
        return classification_report(
            y_labeled, self.predict(X_labeled), output_dict=output_dict
        )

    def classify_and_label(self, X, min_probability, non_labeled_value=None):
        results = pd.DataFrame(self.predict_proba(X), columns=self.xgb._le.classes_)
        results["max_probability"] = results.max(axis=1)
        results["label"] = results[self.xgb._le.classes_].idxmax(axis=1)
        results["source"] = "xgb"

        results["label"][results.max_probability < min_probability] = non_labeled_value

        return results


def cross_validate(
    xgb_parameters,
    X,
    y,
    eval_fraction=0.1,
    early_stopping_rounds=10,
    stratify_on=None,
    n_splits=5,
    random_state=42,
    null_name="__null__",
):
    if stratify_on is None:
        stratify_on = np.copy(y)
        stratify_on[pd.isnull(stratify_on)] = null_name

    if len(stratify_on) != len(y):
        raise ValueError("stratify and y have different lengths")

    skf = StratifiedKFold(n_splits=n_splits)

    outputs = []

    for idx_train, idx_test in skf.split(np.arange(X.shape[0]), y=stratify_on):
        y_train = y[idx_train]
        y_test = y[idx_test]

        X_train = X[idx_train]
        X_test = X[idx_test]

        clf = PartiallyLabeledXGB(xgb_params=xgb_parameters)
        clf.fit(
            X_train,
            y_train,
            eval_fraction=eval_fraction,
            early_stopping_rounds=early_stopping_rounds,
        )

        report = clf.classify_and_report(y_test, X_test, output_dict=True)
        print(clf.classify_and_report(y_test, X_test, output_dict=False))
        outputs.append(report)

    return outputs