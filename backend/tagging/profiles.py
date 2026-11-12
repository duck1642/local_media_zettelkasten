"""Apply output rules after model decoding, preserving the source predictions."""

from collections import defaultdict

from tagging.contracts import Prediction


def score_key(tag: dict) -> float:
    score = tag.get("score")
    return float(score) if score is not None else -1.0


def limit_predictions(predictions: list[Prediction], limit: int) -> list[Prediction]:
    """Her kaynak kategoride en yüksek skorlu tahminleri belirtilen sayıyla sınırla."""
    categories = defaultdict(list)
    for prediction in predictions:
        categories[prediction.category].append(prediction)
    selected = []
    for values in categories.values():
        values.sort(key=lambda value: value.score if value.score is not None else -1, reverse=True)
        selected.extend(values[:limit])
    return selected


def apply_profile(
    predictions: list[Prediction], profile: dict, max_tags: int
) -> dict[str, list[dict]]:
    """Kategori ve etiket adlarını eşle; aynı hedef etikette en iyi skoru tut.

    Hedefi None olan kategori gösterilmez. Kaynak ad ve kategori, görünen
    etiket yeniden adlandırılsa bile sonuçta korunur.
    """
    category_map = profile.get("category_map") or {}
    label_map = profile.get("label_map") or {}
    grouped = defaultdict(dict)
    for prediction in predictions:
        category = category_map.get(prediction.category, prediction.category)
        if category is None:
            continue
        label = (label_map.get(prediction.category) or {}).get(prediction.name)
        name = label if label is not None else prediction.name
        tag = {
            "name": name,
            "display_name": label or prediction.display_name or name.replace("_", " "),
            "source_name": prediction.name,
            "source_category": prediction.category,
        }
        if prediction.score is not None:
            tag["score"] = prediction.score
        previous = grouped[category].get(name)
        if previous is None or score_key(tag) > score_key(previous):
            grouped[category][name] = tag
    return {
        category: sorted(values.values(), key=score_key, reverse=True)[
            : 1 if category == "rating" else max_tags
        ]
        for category, values in grouped.items()
    }


def legacy_fields(groups: dict[str, list[dict]]) -> tuple[dict | None, list[dict], list[dict]]:
    """Yeni gruplardan eski rating, karakter ve genel etiket alanlarını çıkar."""
    ratings = groups.get("rating") or []
    rating = dict(ratings[0]) if ratings else None
    if rating:
        rating["label"] = rating["display_name"]
    return rating, groups.get("character", []), groups.get("general", [])
