"""Typed results the recruiting agents return.

Each one is registered under a name that agent definitions refer to.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from asas_agent import output_schema


@output_schema("screening_result_v1")
class ScreeningResult(BaseModel):
    recommendation: str = Field(description="shortlist, hold or reject")
    fit_summary: str = Field(description="Two or three sentences a recruiter can read out.")
    evidence: list[str] = Field(
        default_factory=list, description="Facts from the application behind the recommendation."
    )
    gaps: list[str] = Field(default_factory=list, description="What the candidate does not show.")
    policy_notes: list[str] = Field(default_factory=list, description="Policy clauses that apply.")
    next_step: str = Field(description="What the recruiter should do next.")


@output_schema("job_description_v1")
class JobDescription(BaseModel):
    title: str
    purpose: str
    responsibilities: list[str]
    requirements: list[str]
    arabic_note: str | None = Field(default=None, description="Language requirement, when the role faces residents.")


@output_schema("interview_plan_v1")
class InterviewPlan(BaseModel):
    focus_areas: list[str] = Field(description="What this interview needs to establish.")
    questions: list[str]
    evidence_to_probe: list[str] = Field(default_factory=list, description="Claims from the application worth testing.")
    panel_advice: str
