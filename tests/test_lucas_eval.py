import pytest

from chessgrandmaster import lucas_eval as le


@pytest.mark.parametrize(
    (
        "best_cp",
        "played_cp",
        "expected_loss",
        "expected_category",
        "expected_nag",
    ),
    [
        (0, 0, 0.0, le.NO_RATING, 0),
        (
            0,
            -50,
            3.76158650617667,
            le.INACCURACY,
            6,
        ),
        (
            -62,
            -241,
            12.753156745917728,
            le.MISTAKE,
            2,
        ),
        (
            -50,
            -306,
            17.80852467770592,
            le.BLUNDER,
            4,
        ),
    ],
)
def test_cp_classification_matches_lucas_curve(
    best_cp,
    played_cp,
    expected_loss,
    expected_category,
    expected_nag,
):
    loss, category, nag = le.classify(
        best_cp=best_cp,
        played_cp=played_cp,
    )

    assert loss == pytest.approx(
        expected_loss,
        abs=1e-12,
    )
    assert category == expected_category
    assert nag == expected_nag


@pytest.mark.parametrize(
    (
        "best_mate",
        "played_mate",
        "expected_loss",
        "expected_category",
        "expected_nag",
    ),
    [
        (
            10,
            7,
            le.TH_INACCURACY,
            le.INACCURACY,
            6,
        ),
        (
            20,
            8,
            le.TH_MISTAKE,
            le.MISTAKE,
            2,
        ),
        (
            25,
            5,
            le.TH_BLUNDER,
            le.BLUNDER,
            4,
        ),
    ],
)
def test_mate_thresholds_map_to_lucas_categories(
    best_mate,
    played_mate,
    expected_loss,
    expected_category,
    expected_nag,
):
    loss, category, nag = le.classify(
        best_mate=best_mate,
        played_mate=played_mate,
    )

    assert loss == expected_loss
    assert category == expected_category
    assert nag == expected_nag
