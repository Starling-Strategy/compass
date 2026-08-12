"""Database adapters admitted into the fresh Compass API.

Read-only repos: catalog, policy_answers, scenario cases, frontend_contracts.
B-spine read repos: criteria, scenarios_v2.
B-spine write repo: verdicts.
"""

from .catalog import ReadOnlyCatalogRepository
from .criteria import ReadOnlyCriteriaRepository
from .frontend_contracts import PostgresFrontendContractRepository
from .policy_answers import ReadOnlyPolicyAnswerRepository
from .scenarios import ReadOnlyScenarioCaseRepository, ReadOnlyScenarioRepository
from .scenarios_v2 import ReadOnlyScenariosV2Repository
from .verdicts import VerdictsRepository

__all__ = [
    "PostgresFrontendContractRepository",
    "ReadOnlyCatalogRepository",
    "ReadOnlyCriteriaRepository",
    "ReadOnlyPolicyAnswerRepository",
    "ReadOnlyScenarioCaseRepository",
    "ReadOnlyScenarioRepository",
    "ReadOnlyScenariosV2Repository",
    "VerdictsRepository",
]
