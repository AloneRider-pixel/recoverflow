from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import TypedDict

from langgraph.graph import END, START, StateGraph


class ActionState(TypedDict, total=False):
    overdue: int
    balance: Decimal
    max_balance: Decimal
    promise_broken: bool
    dispute: bool
    payment_claimed: bool
    promised_date: date | None
    last_contact_at: datetime | None
    due_date: date
    score: int
    priority: str
    action_type: str
    recommended_action: str
    reason: str
    next_action_at: datetime


def _score(state: ActionState) -> ActionState:
    max_balance = state.get("max_balance") or Decimal("1")
    balance = state.get("balance") or Decimal("0")
    score = Decimal("10")
    score += Decimal(str(min(max(state.get("overdue", 0), 0), 60))) * Decimal("0.7")
    score += (balance / max_balance) * Decimal("30")

    if state.get("promise_broken"):
        score += Decimal("24")
    elif state.get("dispute"):
        score += Decimal("18")
    elif state.get("payment_claimed"):
        score += Decimal("12")

    state["score"] = int(min(score, Decimal("100")))
    return state


def _recommend(state: ActionState) -> ActionState:
    today = date.today()
    overdue = state.get("overdue", 0)
    due_date = state["due_date"]

    if state.get("promise_broken"):
        priority, action_type, action = "Critical", "CALL", "Broken promise — follow up today"
        reason = "The promised payment date has passed."
    elif state.get("dispute"):
        priority, action_type, action = "High", "RESOLVE_DISPUTE", "Resolve dispute before chasing payment"
        reason = "The account has an unresolved collection exception."
    elif state.get("payment_claimed"):
        priority, action_type, action = "High", "VERIFY_PAYMENT", "Verify the customer's payment claim"
        reason = "The customer says payment was made; verify it before sending another chase."
    elif overdue >= 30:
        priority, action_type, action = "Critical", "CALL", "Call + payment link"
        reason = "The invoice is more than 30 days overdue."
    elif overdue >= 8:
        priority, action_type, action = "High", "WHATSAPP", "WhatsApp + payment link"
        reason = "The invoice is overdue and needs an active collection step."
    elif overdue >= 1:
        priority, action_type, action = "Medium", "WHATSAPP", "Send overdue reminder"
        reason = "The invoice is past due."
    elif due_date == today:
        priority, action_type, action = "Medium", "WHATSAPP", "Due today — request confirmation"
        reason = "The invoice is due today."
    elif (due_date - today).days <= 3:
        priority, action_type, action = "Low", "WHATSAPP", "Pre-due reminder"
        reason = "The invoice is approaching its due date."
    else:
        priority, action_type, action = "Low", "MONITOR", "Monitor"
        reason = "No immediate collection intervention is required."

    state.update(
        priority=priority,
        action_type=action_type,
        recommended_action=action,
        reason=reason,
    )
    return state


def _schedule(state: ActionState) -> ActionState:
    now = datetime.utcnow()
    today = date.today()
    action_type = state.get("action_type", "MONITOR")
    promise_date = state.get("promised_date")
    last_contact = state.get("last_contact_at")

    if action_type == "MONITOR":
        state["next_action_at"] = datetime.combine(state["due_date"], time(hour=10))
        return state

    if promise_date and not state.get("promise_broken"):
        target = promise_date
        state["next_action_at"] = datetime.combine(target, time(hour=10))
        return state

    if (
        last_contact
        and last_contact.date() == today
        and action_type not in {"CALL", "VERIFY_PAYMENT", "RESOLVE_DISPUTE"}
    ):
        state["next_action_at"] = datetime.combine(today + timedelta(days=1), time(hour=10))
        return state

    state["next_action_at"] = now
    return state


_graph = StateGraph(ActionState)
_graph.add_node("score", _score)
_graph.add_node("recommend", _recommend)
_graph.add_node("schedule", _schedule)
_graph.add_edge(START, "score")
_graph.add_edge("score", "recommend")
_graph.add_edge("recommend", "schedule")
_graph.add_edge("schedule", END)
_collection_graph = _graph.compile()


def recommend_next_action(
    *,
    overdue: int,
    balance: Decimal,
    max_balance: Decimal,
    promise_broken: bool,
    dispute: bool,
    payment_claimed: bool,
    promised_date: date | None,
    last_contact_at: datetime | None,
    due_date: date,
) -> dict:
    result = _collection_graph.invoke(
        {
            "overdue": overdue,
            "balance": balance,
            "max_balance": max_balance,
            "promise_broken": promise_broken,
            "dispute": dispute,
            "payment_claimed": payment_claimed,
            "promised_date": promised_date,
            "last_contact_at": last_contact_at,
            "due_date": due_date,
        }
    )
    return {
        "score": result["score"],
        "priority": result["priority"],
        "action_type": result["action_type"],
        "recommended_action": result["recommended_action"],
        "reason": result["reason"],
        "next_action_at": result["next_action_at"],
    }
