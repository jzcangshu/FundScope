"""数据模型：文件清单、文件类型、来源区域、解析状态与错误类型。

设计约束：
- ``FileInfo`` 只保存相对路径，不保存绝对路径，避免报告泄露服务器真实路径。
- 所有枚举继承 ``str``，序列化后直接是人类可读的稳定字符串。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date
from typing import Any


class FileType(str, enum.Enum):
    """文件类型分类。"""

    VALUATION_XLS = "valuation_xls"
    VALUATION_XLSX = "valuation_xlsx"
    TRANSACTION_XLSX = "transaction_xlsx"
    BACKUP_SIDECAR = "backup_sidecar"
    UNKNOWN = "unknown"


class SourceZone(str, enum.Enum):
    """来源区域：产品主目录为第一来源，gz 为第二来源。"""

    PRIMARY = "primary"
    GZ = "gz"
    OTHER = "other"


class ParseStatus(str, enum.Enum):
    OK = "ok"
    NOT_APPLICABLE = "not_applicable"
    FAILED = "failed"


class ErrorType(str, enum.Enum):
    NONE = "none"
    READ_ERROR = "read_error"
    PARSE_ERROR = "parse_error"
    MISSING_DEPENDENCY = "missing_dependency"


@dataclass
class ProductCandidate:
    """一个候选产品识别结果及其来源。"""

    value: str
    source: str  # path / file_name / sheet_name / title_text
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"value": self.value, "source": self.source, "detail": self.detail}


@dataclass
class FileInfo:
    """单个文件的全部盘点信息。报告中以 ``rel_path``（相对 --root 的 POSIX 风格路径）为主标识。"""

    rel_path: str
    file_name: str
    ext: str
    size_bytes: int
    mtime: str  # ISO-8601 UTC 时间，精确到秒
    zone: SourceZone
    sha256: str | None = None
    product: str | None = None
    product_candidates: list[ProductCandidate] = field(default_factory=list)
    identity_conflict: bool = False
    file_type: FileType = FileType.UNKNOWN
    is_valuation: bool = False
    valuation_date: date | None = None
    sheet_names: list[str] = field(default_factory=list)
    sheet_name: str | None = None
    header_row: int | None = None  # 1-based
    row_count: int | None = None
    col_count: int | None = None
    parse_status: ParseStatus = ParseStatus.NOT_APPLICABLE
    error_type: ErrorType = ErrorType.NONE
    error_message: str = ""
    text_number_count: int = 0
    text_number_samples: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """确定性字典视图，键序固定，供 JSON 与 CSV 输出共用。"""
        return {
            "rel_path": self.rel_path,
            "file_name": self.file_name,
            "ext": self.ext,
            "size_bytes": self.size_bytes,
            "mtime": self.mtime,
            "zone": self.zone.value,
            "sha256": self.sha256,
            "product": self.product,
            "product_candidates": [c.as_dict() for c in self.product_candidates],
            "identity_conflict": self.identity_conflict,
            "file_type": self.file_type.value,
            "is_valuation": self.is_valuation,
            "valuation_date": self.valuation_date.isoformat()
            if self.valuation_date
            else None,
            "sheet_names": list(self.sheet_names),
            "sheet_name": self.sheet_name,
            "header_row": self.header_row,
            "row_count": self.row_count,
            "col_count": self.col_count,
            "parse_status": self.parse_status.value,
            "error_type": self.error_type.value,
            "error_message": self.error_message,
            "text_number_count": self.text_number_count,
            "text_number_samples": list(self.text_number_samples),
        }

    @staticmethod
    def csv_columns() -> list[str]:
        """CSV 固定列顺序（product_candidates 等列表字段拍平为分号分隔文本）。"""
        return [
            "rel_path",
            "file_name",
            "ext",
            "size_bytes",
            "mtime",
            "zone",
            "sha256",
            "product",
            "product_candidates",
            "identity_conflict",
            "file_type",
            "is_valuation",
            "valuation_date",
            "sheet_names",
            "sheet_name",
            "header_row",
            "row_count",
            "col_count",
            "parse_status",
            "error_type",
            "error_message",
            "text_number_count",
            "text_number_samples",
        ]

    def as_csv_row(self) -> dict[str, str]:
        d = self.as_dict()
        d["product_candidates"] = ";".join(
            f"{c['source']}:{c['value']}" for c in d["product_candidates"]
        )
        d["sheet_names"] = ";".join(d["sheet_names"])
        d["text_number_samples"] = ";".join(d["text_number_samples"])
        for key in (
            "sha256",
            "product",
            "valuation_date",
            "sheet_name",
            "header_row",
            "row_count",
            "col_count",
            "error_message",
        ):
            d[key] = "" if d[key] is None else str(d[key])
        d["identity_conflict"] = "true" if d["identity_conflict"] else "false"
        d["is_valuation"] = "true" if d["is_valuation"] else "false"
        return d
