from enum import StrEnum


class Role(StrEnum):
    PM = "pm"
    TRIAGE = "triage"
    ARCHITECT = "architect"
    SENIOR = "senior"
    JUNIOR = "junior"
    REVIEWER = "reviewer"
    QA = "qa"
    LEAD = "lead"
    LIBRARIAN = "librarian"
    HUMAN = "human"
