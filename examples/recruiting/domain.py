"""Stand-in recruiting data.

In a real product these come from the recruiting service and its database. The
point of the example is the boundary: the business side loads what it already
knows the agent needs, and the agent is asked only for judgment.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Requisition:
    id: str
    title: str
    entity: str
    grade: str
    must_have: list[str]
    nice_to_have: list[str]
    arabic_required: bool


@dataclass
class Candidate:
    id: str
    name: str
    years_experience: int
    current_role: str
    skills: list[str]
    languages: list[str]
    education: str
    notes: str = ""


@dataclass
class Application:
    id: str
    requisition_id: str
    candidate_id: str
    status: str
    submitted_at: str
    screening_score: int | None = None
    history: list[dict] = field(default_factory=list)


REQUISITIONS: dict[str, Requisition] = {
    "REQ-4471": Requisition(
        id="REQ-4471",
        title="Senior Data Engineer",
        entity="Department of Government Enablement",
        grade="G12",
        must_have=["Python", "SQL", "data pipelines", "cloud platform"],
        nice_to_have=["Arabic", "government experience", "Kubernetes"],
        arabic_required=False,
    ),
    "REQ-4488": Requisition(
        id="REQ-4488",
        title="Customer Happiness Officer",
        entity="Abu Dhabi Government Services",
        grade="G9",
        must_have=["customer service", "Arabic", "case handling"],
        nice_to_have=["CRM systems", "English"],
        arabic_required=True,
    ),
}

CANDIDATES: dict[str, Candidate] = {
    "CAN-9001": Candidate(
        id="CAN-9001",
        name="Noura A.",
        years_experience=7,
        current_role="Data Engineer, national utility",
        skills=["Python", "SQL", "Airflow", "Azure", "dbt"],
        languages=["Arabic", "English"],
        education="BSc Computer Science, Khalifa University",
        notes="Led the migration of a reporting platform to the sovereign cloud.",
    ),
    "CAN-9002": Candidate(
        id="CAN-9002",
        name="Rahul M.",
        years_experience=3,
        current_role="Analytics Engineer, retail group",
        skills=["SQL", "Power BI", "Python"],
        languages=["English", "Hindi"],
        education="BEng Information Systems",
        notes="Strong reporting background, little pipeline engineering.",
    ),
    "CAN-9003": Candidate(
        id="CAN-9003",
        name="Fatima S.",
        years_experience=5,
        current_role="Service Officer, municipality",
        skills=["customer service", "case handling", "CRM"],
        languages=["Arabic", "English"],
        education="BA Public Administration",
        notes="Handles escalations in Arabic and English.",
    ),
}

APPLICATIONS: dict[str, Application] = {
    "APP-7781": Application(
        id="APP-7781",
        requisition_id="REQ-4471",
        candidate_id="CAN-9001",
        status="in_screening",
        submitted_at="2026-09-02",
        history=[{"event": "applied", "at": "2026-09-02"}, {"event": "shortlisted_by_system", "at": "2026-09-03"}],
    ),
    "APP-7782": Application(
        id="APP-7782",
        requisition_id="REQ-4471",
        candidate_id="CAN-9002",
        status="in_screening",
        submitted_at="2026-09-04",
        history=[{"event": "applied", "at": "2026-09-04"}],
    ),
    "APP-7790": Application(
        id="APP-7790",
        requisition_id="REQ-4488",
        candidate_id="CAN-9003",
        status="in_screening",
        submitted_at="2026-09-06",
        history=[{"event": "applied", "at": "2026-09-06"}],
    ),
}

#: Hiring policy the agent may search. Short on purpose.
POLICY_CLAUSES = [
    {
        "code": "HR-12",
        "title": "Two years in grade before promotion",
        "text": "An internal candidate must have served two years in their current grade before moving up a grade.",
    },
    {
        "code": "HR-31",
        "title": "Arabic for resident-facing roles",
        "text": (
            "Roles that deal with residents directly require professional Arabic. "
            "It cannot be waived by the panel."
        ),
    },
    {
        "code": "HR-47",
        "title": "Evidence for every screening decision",
        "text": (
            "A screening decision records the evidence it relies on. A score alone is not a decision."
        ),
    },
    {
        "code": "HR-58",
        "title": "Equal treatment",
        "text": (
            "Screening considers skills, experience and qualifications. "
            "Nationality, gender, age and family name are never factors."
        ),
    },
]


def application_view(application_id: str) -> dict:
    """What the recruiting service loads before it calls an agent."""
    application = APPLICATIONS[application_id]
    requisition = REQUISITIONS[application.requisition_id]
    candidate = CANDIDATES[application.candidate_id]
    return {
        "application": {k: v for k, v in asdict(application).items() if k != "history"},
        "requisition": asdict(requisition),
        "candidate": asdict(candidate),
    }


def scenarios() -> list[dict]:
    """The demo's preloaded cases, shown in a dropdown."""
    return [
        {
            "id": "APP-7781",
            "label": "Strong fit: Noura A. for Senior Data Engineer",
            "request": "Screen this application against the requisition and explain the evidence.",
        },
        {
            "id": "APP-7782",
            "label": "Weak fit: Rahul M. for Senior Data Engineer",
            "request": "Screen this application. Be clear about what is missing.",
        },
        {
            "id": "APP-7790",
            "label": "Arabic required: Fatima S. for Customer Happiness Officer",
            "request": "Screen this application and check the language requirement.",
        },
    ]
