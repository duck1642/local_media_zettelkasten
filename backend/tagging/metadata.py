"""Additional categories share the existing metadata index without flattening."""

LEGACY_CATEGORIES = {"general", "character", "rating"}


def extra_groups(value) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    groups = {}
    for category, tags in value.items():
        if (
            not isinstance(category, str)
            or not category
            or category in LEGACY_CATEGORIES
            or not isinstance(tags, list)
        ):
            continue
        names = []
        for tag in tags:
            name = (
                tag
                if isinstance(tag, str)
                else (tag.get("display_name") or tag.get("name") or "")
                if isinstance(tag, dict)
                else ""
            )
            if str(name).strip():
                names.append(str(name).strip())
        groups[category] = list(dict.fromkeys(names))
    return groups
