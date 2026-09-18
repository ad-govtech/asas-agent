"""A business service that uses an agent.

The service loads what it already knows must be loaded, authorizes the caller,
then asks the agent for judgment. The agent gets two optional read tools for
the cases where the baseline context is not enough.

Run it:

    uv run python examples/customer_advisor/app.py
"""

from __future__ import annotations

import asyncio
import os

from pydantic import BaseModel

from asas_agent import RuntimeContext, build_platform, capability, output_schema

# ---------------------------------------------------------------- output


@output_schema("customer_advice_v1")
class CustomerAdvice(BaseModel):
    explanation: str
    reason_codes: list[str]
    next_actions: list[str]
    additional_information_used: list[str]


# ------------------------------------------------------------ capabilities
# A capability is a thin adapter. Authorization stays in the owning service.


@capability("policy.search", risk="read", description="Search published policies.")
def make_policy_search(context: RuntimeContext):
    from agents import function_tool

    @function_tool
    async def policy_search(query: str) -> list[dict]:
        """Find published policy clauses relevant to a question."""
        # Replace with a call to the policy service, passing context.access_token.
        return [
            {
                "code": "POLICY_17",
                "title": "Minimum residency period",
                "summary": "An applicant must have held residency for 12 months before applying.",
            }
        ]

    return policy_search


@capability("case.history", risk="read", description="Past cases for one customer.")
def make_case_history(context: RuntimeContext):
    from agents import function_tool

    @function_tool
    async def case_history(customer_id: str) -> list[dict]:
        """List this customer's earlier cases and their outcomes."""
        # Replace with a call to the case service, scoped to context.tenant_id.
        return [{"case_id": "C-2031", "outcome": "approved", "closed_at": "2026-02-11"}]

    return case_history


# --------------------------------------------------------- business service


async def explain_rejection(application_id: str) -> CustomerAdvice:
    platform = build_platform()

    # What the service always knows. The agent should not rediscover this.
    business_context = {
        "customer": {"id": "C123", "segment": "resident", "language": "en"},
        "application": {"id": application_id, "status": "rejected", "submitted_at": "2026-08-30"},
        "decision": {"reason_codes": ["POLICY_17"], "decided_at": "2026-09-05"},
    }

    try:
        result = await platform.runtime.run(
            agent_key="customer-advisor",
            environment=os.getenv("ASAS_ENVIRONMENT", "dev"),
            user_input="Explain this rejection and propose what the customer can do next.",
            business_context=business_context,
            context=RuntimeContext(
                tenant_id="T001",
                user_id="U812",
                correlation_id="REQ-09F4",
                environment=os.getenv("ASAS_ENVIRONMENT", "dev"),
            ),
        )
        return result.output
    finally:
        await platform.close()


if __name__ == "__main__":
    advice = asyncio.run(explain_rejection("A992"))
    print(advice.model_dump_json(indent=2) if hasattr(advice, "model_dump_json") else advice)
