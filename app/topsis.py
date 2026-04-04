from __future__ import annotations

from dataclasses import dataclass
from math import sqrt

from .models import Role, User


@dataclass
class RankedLawyer:
    user: User
    score: float


def topsis_rank(case_category: str, lawyers: list[User]) -> list[RankedLawyer]:
    if not lawyers:
        return []

    weights = {
        "spec": 0.30,
        "load": 0.20,
        "exp": 0.20,
        "avg_days": 0.15,
        "deadline": 0.15,
    }
    benefit = {"spec": True, "load": False, "exp": True, "avg_days": False, "deadline": True}

    matrix = []
    for user in lawyers:
        if user.role != Role.LAWYER:
            continue
        spec_score = 1.0 if case_category.lower() in (user.specialization or "").lower() else 0.2
        matrix.append(
            {
                "user": user,
                "spec": spec_score,
                "load": float(user.current_load),
                "exp": float(user.similar_cases_experience),
                "avg_days": float(user.avg_task_days),
                "deadline": float(user.deadline_success_rate),
            }
        )

    if not matrix:
        return []

    denom = {
        k: sqrt(sum(row[k] ** 2 for row in matrix)) or 1.0
        for k in weights
    }

    weighted = []
    for row in matrix:
        w = {"user": row["user"]}
        for k, wv in weights.items():
            w[k] = (row[k] / denom[k]) * wv
        weighted.append(w)

    ideal_best = {}
    ideal_worst = {}
    for k in weights:
        vals = [r[k] for r in weighted]
        if benefit[k]:
            ideal_best[k] = max(vals)
            ideal_worst[k] = min(vals)
        else:
            ideal_best[k] = min(vals)
            ideal_worst[k] = max(vals)

    ranked = []
    for row in weighted:
        d_best = sqrt(sum((row[k] - ideal_best[k]) ** 2 for k in weights))
        d_worst = sqrt(sum((row[k] - ideal_worst[k]) ** 2 for k in weights))
        score = d_worst / (d_best + d_worst) if (d_best + d_worst) else 0.0
        ranked.append(RankedLawyer(user=row["user"], score=round(score, 4)))

    ranked.sort(key=lambda x: x.score, reverse=True)
    return ranked
