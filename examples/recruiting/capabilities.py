"""The recruiting service's tools, as thin adapters.

Each one is registered under a name that agent definitions can list. The
adapter reads the caller's context, so one recruiter's request never uses
another's identity. Authorization stays in the recruiting service.
"""

from __future__ import annotations

from asas_agent import RuntimeContext, capability

from .domain import APPLICATIONS, POLICY_CLAUSES, REQUISITIONS


@capability("policy.search", risk="read", description="Search hiring policy clauses.")
def make_policy_search(context: RuntimeContext):
    from agents import function_tool

    @function_tool
    async def policy_search(query: str) -> list[dict]:
        """Find hiring policy clauses relevant to a question."""
        words = {w for w in query.lower().split() if len(w) > 3}
        hits = [c for c in POLICY_CLAUSES if words & set(f"{c['title']} {c['text']}".lower().split())]
        return hits or POLICY_CLAUSES[:2]

    return policy_search


@capability("candidate.history", risk="read", description="Earlier applications from one candidate.")
def make_candidate_history(context: RuntimeContext):
    from agents import function_tool

    @function_tool
    async def candidate_history(candidate_id: str) -> list[dict]:
        """List this candidate's earlier applications and what happened to them."""
        return [
            {
                "application_id": application.id,
                "requisition": REQUISITIONS[application.requisition_id].title,
                "status": application.status,
                "submitted_at": application.submitted_at,
            }
            for application in APPLICATIONS.values()
            if application.candidate_id == candidate_id
        ]

    return candidate_history


@capability("requisition.lookup", risk="read", description="Read a requisition.")
def make_requisition_lookup(context: RuntimeContext):
    from agents import function_tool

    @function_tool
    async def requisition_lookup(requisition_id: str) -> dict:
        """Read the requirements of an open requisition."""
        requisition = REQUISITIONS.get(requisition_id)
        if requisition is None:
            return {"error": f"No requisition {requisition_id}"}
        return {
            "id": requisition.id,
            "title": requisition.title,
            "grade": requisition.grade,
            "must_have": requisition.must_have,
            "nice_to_have": requisition.nice_to_have,
            "arabic_required": requisition.arabic_required,
        }

    return requisition_lookup


@capability("interview.schedule", risk="action", description="Book an interview slot. Changes data.")
def make_interview_schedule(context: RuntimeContext):
    from agents import function_tool

    @function_tool
    async def interview_schedule(application_id: str, slot: str) -> dict:
        """Book an interview. The recruiting service still checks the panel and the caller's rights."""
        return {
            "application_id": application_id,
            "slot": slot,
            "status": "proposed",
            "note": "The recruiting service confirms the panel before this becomes a booking.",
            "requested_by": context.user_id,
        }

    return interview_schedule
