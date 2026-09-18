"""Sample answers for when no model key is set.

They are built from the same case data the agent would receive, so the console
can be demonstrated end to end before a key exists. Every response the console
shows from here is labeled as a sample.
"""

from __future__ import annotations

from typing import Any

from .domain import APPLICATIONS, CANDIDATES, REQUISITIONS


def _case(scenario_id: str):
    application = APPLICATIONS[scenario_id]
    return application, REQUISITIONS[application.requisition_id], CANDIDATES[application.candidate_id]


def _screening(scenario_id: str) -> dict[str, Any]:
    _, requisition, candidate = _case(scenario_id)

    met = [skill for skill in requisition.must_have if skill.lower() in {s.lower() for s in candidate.skills}]
    missing = [skill for skill in requisition.must_have if skill not in met]
    arabic_ok = (not requisition.arabic_required) or ("Arabic" in candidate.languages)

    if len(met) >= len(requisition.must_have) - 1 and arabic_ok:
        recommendation = "shortlist"
        next_step = "Invite to a technical interview and confirm the panel."
    elif not arabic_ok:
        recommendation = "reject"
        next_step = "Tell the candidate the language requirement was not met, and suggest roles without it."
    else:
        recommendation = "hold"
        next_step = "Ask the candidate for evidence of the missing requirements before deciding."

    return {
        "recommendation": recommendation,
        "fit_summary": (
            f"{candidate.name} has {candidate.years_experience} years as {candidate.current_role.lower()}. "
            f"The application meets {len(met)} of {len(requisition.must_have)} requirements for {requisition.title}."
        ),
        "evidence": [f"Holds {skill}" for skill in met] + [f"Languages: {', '.join(candidate.languages)}"],
        "gaps": [f"No evidence of {skill}" for skill in missing] or ["None found in the application"],
        "policy_notes": (
            ["HR-31: roles facing residents require professional Arabic"] if requisition.arabic_required else []
        )
        + ["HR-47: this recommendation records the evidence it relies on"],
        "next_step": next_step,
    }


def _job_description(scenario_id: str) -> dict[str, Any]:
    _, requisition, _ = _case(scenario_id)
    return {
        "title": requisition.title,
        "purpose": f"You will work in {requisition.entity}, building and running the services residents rely on.",
        "responsibilities": [
            "Deliver work in a small team with a named product owner",
            "Use the Commons packages rather than rebuilding shared plumbing",
            "Leave documentation another team can follow",
        ],
        "requirements": requisition.must_have,
        "arabic_note": ("Professional Arabic is required for this role." if requisition.arabic_required else None),
    }


def _interview_plan(scenario_id: str) -> dict[str, Any]:
    _, requisition, candidate = _case(scenario_id)
    return {
        "focus_areas": [f"Depth in {skill}" for skill in requisition.must_have[:3]],
        "questions": [
            f"Walk us through a system you built using {requisition.must_have[0]}. What did you own?",
            "Tell us about a time a pipeline failed in production. How did you find the cause?",
            "How do you decide between reusing a shared package and writing your own?",
        ],
        "evidence_to_probe": [candidate.notes] if candidate.notes else [],
        "panel_advice": (
            "Two interviewers, forty minutes, one scenario question each. Score against the requirements only."
        ),
    }


def sample_output(schema_key: str | None, scenario_id: str) -> dict[str, Any]:
    builders = {
        "screening_result_v1": _screening,
        "job_description_v1": _job_description,
        "interview_plan_v1": _interview_plan,
    }
    builder = builders.get(schema_key or "")
    if builder is None:
        return {"note": "This agent has no sample response. Set a model key to run it for real."}
    return builder(scenario_id)
