"""Version-5 experiment contracts and SQLite persistence.

The existing :mod:`adcs.live` package remains the schema-v4 execution-pacing
runtime while this package supplies the explicitly versioned UC-01/UC-02
foundation. Nothing in this package fabricates model responses or holdings.
"""

from .catalog import (
    PROFILE_CATALOG_VERSION,
    V5_INSTITUTION_PROFILES,
    build_confirmatory_preregistration,
    profile_catalog_readiness,
)
from .models import ExperimentPreregistration, InstitutionDecisionV5

__all__ = [
    "ExperimentPreregistration",
    "InstitutionDecisionV5",
    "PROFILE_CATALOG_VERSION",
    "V5_INSTITUTION_PROFILES",
    "build_confirmatory_preregistration",
    "profile_catalog_readiness",
]
