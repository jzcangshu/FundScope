"""Import all models so metadata contains the complete initial schema."""

from .analytics import (
    AnalysisRun,
    CompanyMetricDaily,
    FundMetricDaily,
    RiskEvent,
    RiskRule,
)
from .benchmark import BenchmarkDaily, BenchmarkSyncState
from .catalog import (
    Fund,
    FundAlias,
    ParserRuleSet,
    ShareClass,
    SubjectMapping,
)
from .imports import (
    BackgroundJob,
    ImportBatch,
    ImportBatchFile,
    SourceFile,
    SourceMessage,
)
from .security import AuditLog, SystemState, User, UserFundAccess, UserSession
from .valuation import (
    AccountSubjectDaily,
    FieldProvenance,
    FundDailySnapshot,
    PositionDaily,
    ShareClassDailySnapshot,
    ValidationResult,
    ValuationVersion,
)

__all__ = [
    "AccountSubjectDaily",
    "AnalysisRun",
    "AuditLog",
    "BackgroundJob",
    "BenchmarkDaily",
    "BenchmarkSyncState",
    "CompanyMetricDaily",
    "FieldProvenance",
    "Fund",
    "FundAlias",
    "FundDailySnapshot",
    "FundMetricDaily",
    "ImportBatch",
    "ImportBatchFile",
    "ParserRuleSet",
    "PositionDaily",
    "RiskEvent",
    "RiskRule",
    "ShareClass",
    "ShareClassDailySnapshot",
    "SourceFile",
    "SourceMessage",
    "SubjectMapping",
    "SystemState",
    "User",
    "UserFundAccess",
    "UserSession",
    "ValidationResult",
    "ValuationVersion",
]
