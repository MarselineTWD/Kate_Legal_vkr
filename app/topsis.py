from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from math import sqrt
from pathlib import Path

from .models import Role, User


@dataclass
class RankedLawyer:
    user: User
    score: float


TOPSIS_SETTINGS_PATH = Path("app/data/topsis_settings.json")
TOPSIS_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)

DEFAULT_TOPSIS_SETTINGS = {
    "criteria": [
        {
            "id": "spec",
            "label": "Соответствие специализации",
            "description": "Насколько профиль юриста совпадает с категорией дела.",
            "weight": 0.30,
            "enabled": True,
            "mode": "benefit",
        },
        {
            "id": "load",
            "label": "Текущая загрузка",
            "description": "Чем ниже текущая загрузка, тем выше итоговая оценка.",
            "weight": 0.20,
            "enabled": True,
            "mode": "cost",
        },
        {
            "id": "exp",
            "label": "Опыт по схожим делам",
            "description": "Количество и качество опыта по сопоставимым кейсам.",
            "weight": 0.20,
            "enabled": True,
            "mode": "benefit",
        },
        {
            "id": "avg_days",
            "label": "Средний срок выполнения",
            "description": "Юристы с более коротким средним циклом получают преимущество.",
            "weight": 0.15,
            "enabled": True,
            "mode": "cost",
        },
        {
            "id": "deadline",
            "label": "Соблюдение сроков",
            "description": "Показывает стабильность соблюдения сроков выполнения по предыдущей работе.",
            "weight": 0.15,
            "enabled": True,
            "mode": "benefit",
        },
    ]
}


def get_default_topsis_settings() -> dict:
    return deepcopy(DEFAULT_TOPSIS_SETTINGS)


def load_topsis_settings() -> dict:
    settings = get_default_topsis_settings()
    raw_settings: dict = {}
    if TOPSIS_SETTINGS_PATH.exists():
        try:
            raw_settings = json.loads(TOPSIS_SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw_settings = {}

    saved_by_id = {
        str(item.get("id", "")).strip(): item
        for item in raw_settings.get("criteria", [])
        if str(item.get("id", "")).strip()
    }

    for item in settings["criteria"]:
        saved = saved_by_id.get(item["id"], {})
        item["enabled"] = bool(saved.get("enabled", item["enabled"]))
        try:
            item["weight"] = max(float(saved.get("weight", item["weight"])), 0.0)
        except (TypeError, ValueError):
            item["weight"] = float(item["weight"])

    active_criteria = [item for item in settings["criteria"] if item["enabled"]]
    if not active_criteria:
        settings = get_default_topsis_settings()
        active_criteria = [item for item in settings["criteria"] if item["enabled"]]

    total_weight = sum(float(item["weight"]) for item in active_criteria) or 1.0
    for item in settings["criteria"]:
        item["normalized_weight"] = (
            round(float(item["weight"]) / total_weight, 4) if item["enabled"] else 0.0
        )

    settings["updated_at"] = raw_settings.get("updated_at", "")
    return settings


def save_topsis_settings(criteria: list[dict]) -> dict:
    defaults = get_default_topsis_settings()
    saved_by_id = {
        str(item.get("id", "")).strip(): item
        for item in criteria
        if str(item.get("id", "")).strip()
    }

    payload = {"criteria": [], "updated_at": datetime.utcnow().isoformat()}
    for item in defaults["criteria"]:
        saved = saved_by_id.get(item["id"], {})
        try:
            weight = max(float(saved.get("weight", item["weight"])), 0.0)
        except (TypeError, ValueError):
            weight = float(item["weight"])
        payload["criteria"].append(
            {
                "id": item["id"],
                "enabled": bool(saved.get("enabled", item["enabled"])),
                "weight": round(weight, 4),
            }
        )

    TOPSIS_SETTINGS_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return load_topsis_settings()


def topsis_rank(case_category: str, lawyers: list[User]) -> list[RankedLawyer]:
    if not lawyers:
        return []

    topsis_settings = load_topsis_settings()
    criteria = [item for item in topsis_settings["criteria"] if item["enabled"]]
    if not criteria:
        criteria = [item for item in get_default_topsis_settings()["criteria"] if item["enabled"]]

    weights = {item["id"]: float(item["normalized_weight"]) for item in criteria}
    benefit = {item["id"]: item.get("mode") == "benefit" for item in criteria}

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

    denom = {key: sqrt(sum(row[key] ** 2 for row in matrix)) or 1.0 for key in weights}

    weighted = []
    for row in matrix:
        ranked_row = {"user": row["user"]}
        for key, weight in weights.items():
            ranked_row[key] = (row[key] / denom[key]) * weight
        weighted.append(ranked_row)

    ideal_best = {}
    ideal_worst = {}
    for key in weights:
        values = [row[key] for row in weighted]
        if benefit[key]:
            ideal_best[key] = max(values)
            ideal_worst[key] = min(values)
        else:
            ideal_best[key] = min(values)
            ideal_worst[key] = max(values)

    ranked = []
    for row in weighted:
        d_best = sqrt(sum((row[key] - ideal_best[key]) ** 2 for key in weights))
        d_worst = sqrt(sum((row[key] - ideal_worst[key]) ** 2 for key in weights))
        score = d_worst / (d_best + d_worst) if (d_best + d_worst) else 0.0
        ranked.append(RankedLawyer(user=row["user"], score=round(score, 4)))

    ranked.sort(key=lambda item: item.score, reverse=True)
    return ranked
