"""递归扫描：文件枚举、SHA-256、来源区域、文件类型分类与 Excel 元数据解析。

只读保证：本模块对源目录只执行 stat 与读文件操作，不写、不删、不改名。
单个文件的所有异常都被隔离在该文件内，记录为解析失败，不影响整体扫描。
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import excel_metadata as em
from .models import (
    ErrorType,
    FileInfo,
    FileType,
    ParseStatus,
    ProductCandidate,
    SourceZone,
)

GZ_DIR_NAME = "gz"
_HASH_CHUNK = 1024 * 1024  # 1 MiB


@dataclass
class ScanOptions:
    """扫描行为配置。"""

    parse_xls: bool = True  # 是否解析 .xls 内容（估值日期、核心字段）
    parse_xlsx: bool = True  # .xlsx 始终解析以区分估值表与交易记录
    workers: int = 1


@dataclass
class ScanResult:
    files: list[FileInfo] = field(default_factory=list)
    root_name: str = ""

    def sorted(self) -> ScanResult:
        self.files.sort(key=lambda f: f.rel_path)
        return self


def iter_rel_files(root: Path) -> list[str]:
    """递归枚举 root 下所有文件的相对 POSIX 路径，按路径排序（确定性）。"""
    rels: list[str] = []
    for current, dir_names, file_names in os.walk(
        root, topdown=True, followlinks=False
    ):
        current_path = Path(current)
        dir_names[:] = [
            name for name in dir_names if not _is_link_like(current_path / name)
        ]
        for name in file_names:
            path = current_path / name
            if not _is_link_like(path) and path.is_file():
                rels.append(path.relative_to(root).as_posix())
    rels.sort()
    return rels


def _is_link_like(path: Path) -> bool:
    """识别符号链接与 Windows 连接点，避免遍历或读取其目标。"""
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction is not None and is_junction())


def _path_error(root: Path, rel: str, message: str) -> FileInfo:
    rel_parts = rel.split("/")
    file_name = rel_parts[-1]
    return FileInfo(
        rel_path=rel,
        file_name=file_name,
        ext=Path(file_name).suffix.lower(),
        size_bytes=-1,
        mtime="",
        zone=zone_of(rel_parts),
        parse_status=ParseStatus.FAILED,
        error_type=ErrorType.READ_ERROR,
        error_message=redact_error(message, root),
    )


def _resolve_inside_root(root: Path, rel: str) -> Path | None:
    path = root / rel
    if _is_link_like(path):
        return None
    resolved_root = root.resolve()
    resolved_path = path.resolve(strict=False)
    if not resolved_path.is_relative_to(resolved_root):
        return None
    return resolved_path


def zone_of(rel_parts: list[str]) -> SourceZone:
    """来源区域：首层目录为 gz → gz；形如“XX估值表” → primary；其余 → other。"""
    if not rel_parts:
        return SourceZone.OTHER
    top = rel_parts[0]
    if top == GZ_DIR_NAME:
        return SourceZone.GZ
    if top.endswith(em.PRODUCT_DIR_SUFFIX):
        return SourceZone.PRIMARY
    return SourceZone.OTHER


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(_HASH_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def redact_error(text: str, root: Path) -> str:
    """隐藏错误信息中的源根绝对路径，避免其进入任何报告。"""
    variants: set[str] = set()
    for value in (str(root), str(root.resolve())):
        variants.add(value)
        variants.add(value.replace("\\", "/"))
    for value in sorted(variants, key=len, reverse=True):
        text = text.replace(value, "<root>")
    return text


def initial_file_type(rel_parts: list[str], file_name: str, ext: str) -> FileType:
    """扩展名/文件名层面的初步分类（内容解析后可能修正）。"""
    if file_name.endswith(".bak_cum_nav") or ext.startswith(".bak"):
        return FileType.BACKUP_SIDECAR
    if ext == ".xls":
        return FileType.VALUATION_XLS  # 待内容确认
    if ext in (".xlsx", ".xlsm"):
        return FileType.UNKNOWN  # 待内容区分为估值表或交易记录
    return FileType.UNKNOWN


def looks_like_transaction(
    rel_parts: list[str], file_name: str, sheet_names: list[str]
) -> bool:
    """交易记录工作簿识别：路径/文件名/工作表名含“交易记录”线索。"""
    for part in rel_parts:
        if any(hint in part for hint in em.TRANSACTION_HINTS):
            return True
    if any(hint in file_name for hint in em.TRANSACTION_HINTS):
        return True
    return any(any(hint in s for hint in em.TRANSACTION_HINTS) for s in sheet_names)


def scan_file(
    root: Path, rel: str, options: ScanOptions, catalog: em.ProductCatalog
) -> FileInfo:
    """扫描单个文件：stat → 哈希 → （可选）Excel 内容解析。异常全部就地记录。"""
    rel_parts = rel.split("/")
    file_name = rel_parts[-1]
    ext = Path(file_name).suffix.lower()

    try:
        path = _resolve_inside_root(root, rel)
    except (OSError, RuntimeError) as e:
        return _path_error(root, rel, f"path validation failed: {e}")
    if path is None:
        return _path_error(root, rel, "path is a link or escapes scan root")

    try:
        st = path.stat()
        mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(
            timespec="seconds"
        )
        info = FileInfo(
            rel_path=rel,
            file_name=file_name,
            ext=ext,
            size_bytes=st.st_size,
            mtime=mtime,
            zone=zone_of(rel_parts),
            file_type=initial_file_type(rel_parts, file_name, ext),
        )
    except OSError as e:
        # 连 stat 都失败：生成仅含路径信息的记录，不再尝试读取
        return FileInfo(
            rel_path=rel,
            file_name=file_name,
            ext=ext,
            size_bytes=-1,
            mtime="",
            zone=zone_of(rel_parts),
            parse_status=ParseStatus.FAILED,
            error_type=ErrorType.READ_ERROR,
            error_message=redact_error(f"stat failed: {e}", root),
        )

    try:
        info.sha256 = sha256_of(path)
    except OSError as e:
        info.parse_status = ParseStatus.FAILED
        info.error_type = ErrorType.READ_ERROR
        info.error_message = redact_error(f"hash failed: {e}", root)
        return info

    _parse_excel_metadata(info, path, root, rel_parts, options, catalog)
    return info


def _parse_excel_metadata(
    info: FileInfo,
    path: Path,
    root: Path,
    rel_parts: list[str],
    options: ScanOptions,
    catalog: em.ProductCatalog,
) -> None:
    """读取 Excel 内容并回填估值表判定、日期、产品候选等字段。"""
    ext = info.ext
    is_xls = ext == ".xls"
    is_xlsx = ext in (".xlsx", ".xlsm")
    if not (is_xls or is_xlsx):
        info.parse_status = ParseStatus.NOT_APPLICABLE
        return
    if is_xls and not options.parse_xls:
        info.parse_status = ParseStatus.NOT_APPLICABLE
        info.error_message = "skipped: --no-parse-xls"
        return

    try:
        grids = em.load_grids(path)
        facts = em.analyze_grids(grids)
    except ImportError as e:
        info.parse_status = ParseStatus.FAILED
        info.error_type = ErrorType.MISSING_DEPENDENCY
        info.error_message = redact_error(f"missing dependency: {e}", root)
        return
    except Exception as e:  # noqa: BLE001 - 单文件损坏、加密或格式异常必须隔离并继续扫描
        info.parse_status = ParseStatus.FAILED
        info.error_type = ErrorType.READ_ERROR
        info.error_message = redact_error(f"{type(e).__name__}: {e}", root)
        return

    info.sheet_names = facts.sheet_names
    info.is_valuation = facts.is_valuation
    if facts.facts is not None:
        info.sheet_name = facts.chosen_sheet
        info.header_row = facts.facts.header_row
        info.valuation_date = facts.facts.valuation_date
        info.text_number_count = facts.facts.text_number_count
        info.text_number_samples = facts.facts.text_number_samples

    # 行列数：取被选中的工作表网格
    chosen_grid = next((g for g in grids if g.name == facts.chosen_sheet), None)
    if chosen_grid is not None:
        info.row_count = chosen_grid.row_count
        info.col_count = chosen_grid.col_count

    # —— 文件类型修正 ——
    if is_xls:
        info.file_type = (
            FileType.VALUATION_XLS if info.is_valuation else FileType.UNKNOWN
        )
    else:  # xlsx
        if info.is_valuation:
            info.file_type = FileType.VALUATION_XLSX
        elif looks_like_transaction(rel_parts, info.file_name, info.sheet_names):
            info.file_type = FileType.TRANSACTION_XLSX
        else:
            info.file_type = FileType.UNKNOWN

    # —— 候选产品收集与冲突判定 ——
    stem = Path(info.file_name).stem
    candidates: list[ProductCandidate] = []
    for part in rel_parts[:-1]:
        for value in catalog.candidates_from_dir_name(part):
            candidates.append(ProductCandidate(value=value, source="path", detail=part))
    if info.sheet_name:
        for value in catalog.candidates_from_text(info.sheet_name):
            candidates.append(
                ProductCandidate(
                    value=value, source="sheet_name", detail=info.sheet_name
                )
            )
    for value in catalog.candidates_from_text(stem):
        candidates.append(
            ProductCandidate(value=value, source="file_name", detail=stem)
        )
    title_text = facts.title_text()
    for value in catalog.candidates_from_text(title_text):
        candidates.append(
            ProductCandidate(value=value, source="title_text", detail=title_text[:80])
        )

    info.product_candidates = candidates
    resolved, conflict = catalog.resolve([c.value for c in candidates])
    info.product = resolved
    info.identity_conflict = conflict

    info.parse_status = ParseStatus.OK


def scan(
    root: Path,
    options: ScanOptions | None = None,
    catalog: em.ProductCatalog | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> ScanResult:
    """扫描 root 下全部文件并返回按相对路径排序的结果。"""
    options = options or ScanOptions()
    catalog = catalog or em.ProductCatalog()
    rels = iter_rel_files(root)
    done = 0
    progress_lock = threading.Lock()

    def _one(rel: str) -> FileInfo:
        nonlocal done
        info = scan_file(root, rel, options, catalog)
        with progress_lock:
            done += 1
            if progress is not None:
                progress(done, len(rels))
        return info

    if options.workers > 1:
        with ThreadPoolExecutor(max_workers=options.workers) as pool:
            files = list(pool.map(_one, rels))
    else:
        files = [_one(rel) for rel in rels]

    result = ScanResult(files=files, root_name=root.name)
    return result.sorted()
