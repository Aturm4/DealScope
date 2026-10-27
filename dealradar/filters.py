"""Explainable, deterministic decisions. Unknown required fields fail closed."""

from __future__ import annotations

from .models import Decision, Offer, SearchRule
from .parser import normalize


def evaluate(offer: Offer, rule: SearchRule) -> Decision:
    evidence = {
        "capacity_gb": offer.capacity_gb, "interface": offer.interface,
        "price": str(offer.price) if offer.price is not None else None,
        "currency": offer.currency, "max_price": str(rule.max_price),
        "availability": offer.availability,
    }
    rejected: list[str] = []
    unknown: list[str] = []
    value = normalize(offer.search_text)
    if not rule.enabled:
        return Decision("rejected", ("rule_disabled",), evidence)
    if offer.availability == "sold":
        return Decision("rejected", ("sold",), evidence)
    if "buy_request" in offer.issues:
        return Decision("rejected", ("buy_request",), evidence)
    if any(normalize(word) in value for word in rule.exclusions):
        rejected.append("excluded_keyword")
    if rule.keywords:
        hits = [normalize(word) in value for word in rule.keywords]
        if not (all(hits) if rule.keyword_mode == "all" else any(hits)):
            rejected.append("keywords_not_matched")
    for attribute, expected in (("drive_type", rule.drive_type), ("interface", rule.interface)):
        actual = getattr(offer, attribute)
        if expected is not None:
            if actual is None:
                unknown.append(f"unknown_{attribute}")
            elif actual != expected:
                rejected.append(f"wrong_{attribute}")
    for attribute, allowed in (
        ("capacity_gb", rule.capacities_gb), ("brand", rule.brands),
        ("model", rule.models), ("condition", rule.conditions),
    ):
        if allowed:
            actual = getattr(offer, attribute)
            if actual is None:
                unknown.append(f"unknown_{attribute}")
            elif attribute in ("brand", "model"):
                if normalize(actual) not in {normalize(str(item)) for item in allowed}:
                    rejected.append(f"wrong_{attribute}")
            elif actual not in allowed:
                rejected.append(f"wrong_{attribute}")
    if offer.currency is None:
        unknown.append("unknown_currency")
    elif offer.currency != rule.currency:
        unknown.append("currency_not_comparable")
    if offer.price is None:
        unknown.append("unknown_price")
    elif offer.currency == rule.currency and offer.price > rule.max_price:
        rejected.append("price_above_limit")
    if offer.price_kind != "full":
        unknown.append("not_unambiguous_full_price")
    unknown.extend(offer.issues)
    if offer.availability == "ambiguous":
        unknown.append("ambiguous_availability")
    if rejected:
        return Decision("rejected", tuple(dict.fromkeys(rejected)), evidence)
    if unknown:
        return Decision("needs_review", tuple(dict.fromkeys(unknown)), evidence)
    return Decision("matched", ("all_required_fields_match", "price_within_limit"), evidence)
