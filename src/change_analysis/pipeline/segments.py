"""Build the slice/segment layer (СХЕМА_ВИТРИНЫ §2) — "у кого изменилось".

One row per application.  The field names live here (not in the kernel) because
the mapping from a СПР response to the segment row is СПР-specific.

Ambiguous points (PLAN.md §10.2) are resolved by explicit, documented rules:

1. application decision  = ``decisionResult.decision``
2. ``PRODUCTS``          = union of ``product_trials[].product_id`` over *both*
   slices, sorted, ``;``-joined (the delivered checks split on ``";"``)
3. ``TOP_OFFER``         = the offer with the smallest ``rank``; ties broken by
   ``(product_id, type)``; empty when the slice has no offers
4. reference product     = TOP_OFFER_BEFORE, else TOP_OFFER_AFTER, else the
   lexicographically smallest product_id of the union
5. ``PD_MODEL`` / ``CUTOFF`` / ``REJECT_REASON`` are read for the *same*
   reference product on both sides — a missing product is never silently
   replaced by another one
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .core import serialize


def _trials(doc: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    trials = (doc.get("decisionResult") or {}).get("product_trials") or []
    return [t for t in trials if isinstance(t, Mapping)]


def _offers(doc: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    offers = (doc.get("decisionResult") or {}).get("available_offers") or []
    return [o for o in offers if isinstance(o, Mapping)]


def _sort_key(product_id: Any) -> tuple[int, str]:
    """Product ids are numeric in this dataset but must not be assumed numeric."""
    if isinstance(product_id, (int, float)) and not isinstance(product_id, bool):
        return (0, f"{product_id:020.6f}")
    return (1, str(product_id))


def _top_offer(doc: Mapping[str, Any]) -> str:
    offers = _offers(doc)
    if not offers:
        return ""
    best = min(
        offers,
        key=lambda o: (
            o.get("rank") if isinstance(o.get("rank"), (int, float)) else 10**9,
            _sort_key(o.get("product_id")),
            str(o.get("type") or ""),
        ),
    )
    return str(best.get("product_id"))


def _decision(doc: Mapping[str, Any]) -> str:
    return str((doc.get("decisionResult") or {}).get("decision") or "")


def _score_okb(doc: Mapping[str, Any]) -> str:
    value = (doc.get("characteristics") or {}).get("score_okb")
    return "" if value is None else serialize(value)[0]


def _trial_for(doc: Mapping[str, Any], product_id: str) -> Mapping[str, Any] | None:
    if not product_id:
        return None
    for trial in _trials(doc):
        if str(trial.get("product_id")) == product_id:
            return trial
    return None


def _trial_details(doc: Mapping[str, Any], product_id: str) -> dict[str, str]:
    trial = _trial_for(doc, product_id)
    if trial is None:
        return {"PD_MODEL": "", "CUTOFF": "", "REJECT_REASON": "",
                "PARTNER_TYPE": "", "ROUTING_METHOD": "", "PRODUCT_DECISION": ""}
    pd = trial.get("scoring") or {}
    pd = pd.get("pd") or {}
    cutoff = trial.get("cutOff") or {}
    return {
        "PD_MODEL": str(pd.get("modelName") or ""),
        "CUTOFF": str(cutoff.get("name") or ""),
        "REJECT_REASON": str(trial.get("rejectReason") or ""),
        "PARTNER_TYPE": str(trial.get("partner_type") or ""),
        "ROUTING_METHOD": str(trial.get("routing_method") or ""),
        "PRODUCT_DECISION": str(trial.get("decision") or ""),
    }


def build_segment(app_id: str, before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    products: set[str] = set()
    for doc in (before, after):
        for trial in _trials(doc):
            pid = trial.get("product_id")
            if pid is not None:
                products.add(str(pid))
    products_sorted = sorted(products, key=_sort_key)

    top_b = _top_offer(before)
    top_a = _top_offer(after)
    reference = top_b or top_a or (products_sorted[0] if products_sorted else "")

    detail_b = _trial_details(before, reference)
    detail_a = _trial_details(after, reference)

    decision_b = _decision(before)
    decision_a = _decision(after)

    return {
        "APP_ID": app_id,
        "DECISION_BEFORE": decision_b,
        "DECISION_AFTER": decision_a,
        "DECISION_CHANGED": 0 if decision_b == decision_a else 1,
        "PRODUCTS": ";".join(products_sorted),
        "TOP_OFFER_BEFORE": top_b,
        "TOP_OFFER_AFTER": top_a,
        "PARTNER_TYPE": detail_a["PARTNER_TYPE"] or detail_b["PARTNER_TYPE"],
        "ROUTING_METHOD": detail_a["ROUTING_METHOD"] or detail_b["ROUTING_METHOD"],
        "PD_MODEL_BEFORE": detail_b["PD_MODEL"],
        "PD_MODEL_AFTER": detail_a["PD_MODEL"],
        "CUTOFF_BEFORE": detail_b["CUTOFF"],
        "CUTOFF_AFTER": detail_a["CUTOFF"],
        "REJECT_REASON_BEFORE": detail_b["REJECT_REASON"],
        "REJECT_REASON_AFTER": detail_a["REJECT_REASON"],
        "SCORE_OKB_BEFORE": _score_okb(before),
        "SCORE_OKB_AFTER": _score_okb(after),
        "REFERENCE_PRODUCT_ID": reference,
        "PRODUCT_DECISION_BEFORE": detail_b["PRODUCT_DECISION"],
        "PRODUCT_DECISION_AFTER": detail_a["PRODUCT_DECISION"],
    }


def build_product_segments(
    app_id: str, before: Mapping[str, Any], after: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Detailed per-(application, slice, product) rows.

    The singular compatibility layer above has to collapse several products into
    one value; this table keeps the full picture, so the UI can filter "only
    product X" without lying about which product a scalar came from.
    """
    offers: dict[tuple[str, str], Mapping[str, Any]] = {}
    for doc, source in ((before, "BEFORE"), (after, "AFTER")):
        for offer in _offers(doc):
            offers[(source, str(offer.get("product_id")))] = offer

    out: list[dict[str, Any]] = []
    for doc, source in ((before, "BEFORE"), (after, "AFTER")):
        for trial in _trials(doc):
            product_id = str(trial.get("product_id"))
            scoring = trial.get("scoring") or {}
            pd_block = scoring.get("pd") or {}
            cutoff = trial.get("cutOff") or {}
            offer = offers.get((source, product_id)) or {}
            out.append(
                {
                    "app_id": app_id,
                    "source": source,
                    "product_id": product_id,
                    "product_name": str(trial.get("product_name") or ""),
                    "partner_type": str(trial.get("partner_type") or ""),
                    "routing_method": str(trial.get("routing_method") or ""),
                    "pd_model": str(pd_block.get("modelName") or ""),
                    "cutoff": str(cutoff.get("name") or ""),
                    "cutoff_value": "" if cutoff.get("value") is None
                    else serialize(cutoff.get("value"))[0],
                    "decision": str(trial.get("decision") or ""),
                    "reject_reason": str(trial.get("rejectReason") or ""),
                    "offer_rank": "" if offer.get("rank") is None
                    else serialize(offer.get("rank"))[0],
                    "offer_type": str(offer.get("type") or ""),
                }
            )
    return out
