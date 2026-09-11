
import math

# Lucas Chess R6 defaults
LIMIT_SCORE = 2000
CURVE_DEGREE = 30

DIFMATE_INACCURACY = 3
DIFMATE_MISTAKE = 12
DIFMATE_BLUNDER = 20
MATE_HUMAN = 15

TH_INACCURACY = 3.3
TH_MISTAKE = 7.5
TH_BLUNDER = 15.5


NO_RATING = "NO_RATING"
INACCURACY = "INACCURACY"
MISTAKE = "MISTAKE"
BLUNDER = "BLUNDER"


def centipawns_abs(cp=0, mate=0):
    """
    Equivalent to Lucas EngineResponse.centipawns_abs().
    """
    if mate:
        if mate < 0:
            return -30000 - (mate + 1) * 10
        else:
            return 30000 - (mate - 1) * 10

    return cp


def lv(cp):
    """
    Lucas nonlinear evaluation -> winning-value scale.
    """
    is_negative = cp < 0

    if is_negative:
        cp = -cp

    def base(xcp):
        return (
            200.0 /
            (1.0 + math.exp(-CURVE_DEGREE * xcp / 10000.0))
        ) - 100.0

    xr = min(
        max(
            base(cp) * 50.0 / base(LIMIT_SCORE),
            0.0
        ),
        50.0
    )

    return 50.0 + (-xr if is_negative else xr)


def lv_dif(cp_best, cp_other):
    return lv(cp_best) - lv(cp_other)


def evaluate_dif(
    best_cp=0,
    best_mate=0,
    played_cp=0,
    played_mate=0,
):
    """
    Port of Lucas Chess R6 AnalysisEval.evaluate_dif().
    """

    # CP vs CP
    if best_mate == 0 and played_mate == 0:
        return lv_dif(best_cp, played_cp)

    # Best is mate, played move loses the forced mate
    elif played_mate == 0:

        if best_mate > MATE_HUMAN:
            xadd = TH_INACCURACY

        else:
            dif_mate = MATE_HUMAN - best_mate

            if dif_mate >= DIFMATE_BLUNDER:
                xadd = TH_BLUNDER

            elif dif_mate >= DIFMATE_MISTAKE:
                xadd = TH_MISTAKE

            elif dif_mate >= DIFMATE_INACCURACY:
                xadd = TH_INACCURACY

            else:
                xadd = 0

        return (
            lv_dif(LIMIT_SCORE, played_cp)
            + xadd
        )

    # Best is normal CP, played move allows mate against player
    elif best_mate == 0 and played_mate < 0:

        best_abs = centipawns_abs(
            cp=best_cp,
            mate=best_mate
        )

        played_abs = centipawns_abs(
            cp=played_cp,
            mate=played_mate
        )

        return max(
            lv_dif(best_abs, played_abs),
            TH_MISTAKE,
        )

    # Mate vs mate
    else:
        dif_mate = abs(
            best_mate - played_mate
        )

        if dif_mate >= DIFMATE_BLUNDER:
            return TH_BLUNDER

        if dif_mate >= DIFMATE_MISTAKE:
            return TH_MISTAKE

        if dif_mate >= DIFMATE_INACCURACY:
            return TH_INACCURACY

        return 0


def classify(
    best_cp=0,
    best_mate=0,
    played_cp=0,
    played_mate=0,
):
    loss = evaluate_dif(
        best_cp=best_cp,
        best_mate=best_mate,
        played_cp=played_cp,
        played_mate=played_mate,
    )

    if loss >= TH_BLUNDER:
        return loss, BLUNDER, 4

    if loss >= TH_MISTAKE:
        return loss, MISTAKE, 2

    if loss >= TH_INACCURACY:
        return loss, INACCURACY, 6

    return loss, NO_RATING, 0
