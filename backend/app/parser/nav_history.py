"""Template-tolerant parser for product NAV history workbooks.

A NAV history file is one product observed over many dates, unlike a valuation
workbook which is one product on one date.  Every provider ships a different
column layout, so a file is matched to one of a few table adapters by its
header row; a new provider only needs a new entry in ``NAV_TABLES``.

Share-class rows (for example ``xxx基金A`` / ``xxx基金B`` alongside the fund
row ``xxx基金``) are recognized because their product name extends the fund
name by a suffix, and are attached to the fund-level entry of that date.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from pathlib import Path

from .excel_reader import WorksheetData, read_workbook
from .interface import ParsedShareClass
from .normalizers import decimal, normalize_label, parse_date, ratio, text


class NavHistoryParseError(ValueError):
    """Raised when a workbook cannot be interpreted as a NAV history table."""


@dataclass(frozen=True, slots=True)
class NavHistoryEntry:
    """One product on one date, normalized from any provider layout."""

    product_name: str
    valuation_date: date
    product_code: str | None = None
    unit_nav: Decimal | None = None
    cumulative_unit_nav: Decimal | None = None
    net_asset_value: Decimal | None = None
    total_assets: Decimal | None = None
    total_liabilities: Decimal | None = None
    shares_outstanding: Decimal | None = None
    daily_return: Decimal | None = None
    share_classes: tuple[ParsedShareClass, ...] = ()
    source_row: int = 0


@dataclass(frozen=True, slots=True)
class NavHistoryTable:
    """Column labels of one provider layout, in priority order.

    ``date`` must exist; every other mapping is optional and a miss simply
    leaves the corresponding field unset.  A layout matches a header when all
    of its labels appear there, so unknown extra columns never disqualify it.
    When ``date_from_title`` is set the date column is absent and the
    valuation date is read from the announcement title above the header.
    """

    date: tuple[str, ...]
    product_name: tuple[str, ...] = ()
    product_code: tuple[str, ...] = ()
    unit_nav: tuple[str, ...] = ()
    cumulative_nav: tuple[str, ...] = ()
    net_asset_value: tuple[str, ...] = ()
    total_assets: tuple[str, ...] = ()
    total_liabilities: tuple[str, ...] = ()
    shares_outstanding: tuple[str, ...] = ()
    daily_return: tuple[str, ...] = ()
    share_name: tuple[str, ...] = ()
    share_code: tuple[str, ...] = ()
    share_net_assets: tuple[str, ...] = ()
    date_from_title: bool = False
    name: str = "nav-history"


# The known provider layouts.  ``share_*`` columns exist only in layouts that
# are known to mix share-class rows into the fund-level list.
NAV_TABLES: tuple[NavHistoryTable, ...] = (
    # 托管户净值列表（云川一号/云川二号/明曜三号）：
    # 日期 基金名称 基金代码 资产净值(元) 资产份额(份) 资产总值(元) 单位净值 累计单位净值 净值波动率(%)
    NavHistoryTable(
        date=("日期",),
        product_name=("基金名称",),
        product_code=("基金代码",),
        net_asset_value=("资产净值(元)", "资产净值"),
        shares_outstanding=("资产份额(份)", "资产份额"),
        total_assets=("资产总值(元)", "资产总值"),
        unit_nav=("单位净值",),
        cumulative_nav=("累计单位净值",),
        daily_return=("净值波动率(%)", "净值波动率"),
        share_name=("基金名称",),
        share_code=("基金代码",),
        share_net_assets=("资产净值(元)", "资产净值"),
        name="custodian-nav-list",
    ),
    # 券商 TA 净值序列（启航二号）：
    # 产品名称 产品编码 净值日期 单位净值 累计净值 资产净值 资产总值 资产份额
    NavHistoryTable(
        date=("净值日期",),
        product_name=("产品名称",),
        product_code=("产品编码",),
        unit_nav=("单位净值",),
        cumulative_nav=("累计净值",),
        net_asset_value=("资产净值",),
        total_assets=("资产总值",),
        shares_outstanding=("资产份额",),
        name="ta-nav-series",
    ),
    # 券商托管下载（青岩稳健/紫微/磐石二号，文件名带代码与日期区间）：
    # 产品代码 净值日增长率 … 净值日期 单位净值(元/份) 累计单位净值(元/份) 资产净值(元)
    NavHistoryTable(
        date=("净值日期",),
        product_code=("产品代码",),
        unit_nav=("单位净值(元/份)", "单位净值"),
        cumulative_nav=("累计单位净值(元/份)", "累计单位净值"),
        net_asset_value=("资产净值(元)", "资产净值"),
        daily_return=("净值日增长率",),
        name="broker-nav-data",
    ),
    # 内部维护净值表（星辰）：
    # 产品名称 产品代码 净值日期 单位净值 累计净值 每日净值增长率 累计净值增长率 基金资产份额 基金资产净值 总资产合计 总负债合计
    NavHistoryTable(
        date=("净值日期",),
        product_name=("产品名称",),
        product_code=("产品代码",),
        unit_nav=("单位净值",),
        cumulative_nav=("累计净值",),
        net_asset_value=("基金资产净值",),
        total_assets=("总资产合计",),
        total_liabilities=("总负债合计",),
        shares_outstanding=("基金资产份额",),
        daily_return=("每日净值增长率",),
        name="internal-nav-table",
    ),
    # TA 对账导出（慧达二号）：
    # 日期 产品代码 协会备案代码 产品名称 资产净值 基金总净值 资产份额 单位净值 累计单位净值
    NavHistoryTable(
        date=("日期",),
        product_name=("产品名称",),
        product_code=("产品代码",),
        net_asset_value=("资产净值",),
        shares_outstanding=("资产份额",),
        unit_nav=("单位净值",),
        cumulative_nav=("累计单位净值",),
        name="ta-nav-export",
    ),
    # TA 确认净值表（慧达一号）：
    # 确认状态 产品名称 协会备案编码 TA代码 净值日期 单位净值 累计单位净值 资产净值 资产份额 总资产 总负债
    NavHistoryTable(
        date=("净值日期",),
        product_name=("产品名称",),
        product_code=("协会备案编码",),
        unit_nav=("单位净值",),
        cumulative_nav=("累计单位净值",),
        net_asset_value=("资产净值",),
        total_assets=("总资产",),
        total_liabilities=("总负债",),
        shares_outstanding=("资产份额",),
        share_name=("产品名称",),
        share_code=("TA代码",),
        share_net_assets=("资产净值",),
        name="ta-confirmed-nav",
    ),
    # 托管部净值浏览表（云川三号，表头前有标题行）：
    # 账套名称 产品代码 日期 资产净值 资产总值 资产份额 单位净值 累计净值 净值是否确认
    NavHistoryTable(
        date=("日期",),
        product_name=("账套名称",),
        product_code=("产品代码",),
        net_asset_value=("资产净值",),
        total_assets=("资产总值",),
        shares_outstanding=("资产份额",),
        unit_nav=("单位净值",),
        cumulative_nav=("累计净值",),
        name="custodian-browse-table",
    ),
    # 示例证券托管对账单（明曜一号，份额行带"类"字后缀）：
    # 基金代码 产品名称 业务日期 分级基金代码 单位净值 累计单位净值 托管人 成立日期 清盘日期 状态 资产净值
    NavHistoryTable(
        date=("业务日期",),
        product_name=("产品名称",),
        product_code=("基金代码",),
        unit_nav=("单位净值",),
        cumulative_nav=("累计单位净值",),
        net_asset_value=("资产净值",),
        share_name=("产品名称",),
        share_code=("分级基金代码",),
        share_net_assets=("资产净值",),
        name="custodian-recon-list",
    ),
    # 托管净值列表合并版（云川一号/云川二号/明曜三号的多日合并文件）：
    # 日期 基金名称 基金代码 资产净值（元） 资产份额（份） 单位净值 累计单位净值 资产总值（元）
    NavHistoryTable(
        date=("日期",),
        product_name=("基金名称",),
        product_code=("基金代码",),
        net_asset_value=("资产净值",),
        shares_outstanding=("资产份额",),
        unit_nav=("单位净值",),
        cumulative_nav=("累计单位净值",),
        total_assets=("资产总值",),
        name="custodian-nav-list-compact",
    ),
    # 基金每日净值表（启航二号）：
    # 日期 资产代码 资产名称 资产份额净值(元) 资产份额累计净值(元) 总份额 资产总值(元)
    NavHistoryTable(
        date=("日期",),
        product_name=("资产名称",),
        product_code=("资产代码",),
        unit_nav=("资产份额净值",),
        cumulative_nav=("资产份额累计净值",),
        shares_outstanding=("总份额",),
        total_assets=("资产总值",),
        name="fund-daily-nav-table",
    ),
    # 托管净值公告（青岩稳健/紫微/磐石二号的每日与历史净值导出）：
    # 净值日期 产品代码 产品名称 单位净值 累计单位净值 产品总份额 产品资产净值 产品总资产
    NavHistoryTable(
        date=("净值日期",),
        product_name=("产品名称",),
        product_code=("产品代码",),
        unit_nav=("单位净值",),
        cumulative_nav=("累计单位净值",),
        shares_outstanding=("产品总份额",),
        net_asset_value=("产品资产净值",),
        total_assets=("产品总资产",),
        name="broker-nav-announcement",
    ),
    # TA 确认导出（慧达一号，表头带中英双语括号注释）：
    # 日期 产品名称 单位净值 累计单位净值 资产份额 资产净值 总资产 产品代码 协会备案编码
    NavHistoryTable(
        date=("日期",),
        product_name=("产品名称",),
        product_code=("协会备案编码",),
        unit_nav=("单位净值",),
        cumulative_nav=("累计单位净值",),
        net_asset_value=("资产净值",),
        total_assets=("总资产",),
        shares_outstanding=("资产份额",),
        name="ta-confirmed-export",
    ),
    # 外包净值复核表（梦溪一号/天衡上将/慧达二号的补发邮件）：
    # 估值日期 协会备案代码 产品代码 产品全称 单位净值 累计单位净值 资产净值 资产份额 净值状态
    NavHistoryTable(
        date=("估值日期",),
        product_name=("产品全称",),
        product_code=("协会备案代码", "产品代码"),
        unit_nav=("单位净值",),
        cumulative_nav=("累计单位净值",),
        net_asset_value=("资产净值",),
        shares_outstanding=("资产份额",),
        name="outsourced-nav-review",
    ),
    # 外包净值复核表（天衡上将缺资产净值/资产份额列）：
    NavHistoryTable(
        date=("估值日期",),
        product_name=("产品全称",),
        product_code=("协会备案代码", "产品代码"),
        unit_nav=("单位净值",),
        cumulative_nav=("累计单位净值",),
        name="outsourced-nav-review-compact",
    ),
    # 长江证券资产净值公告（云川三号，两行表头，日期在标题行）：
    # 账套名称 基金代码 / 单位净值 累计单位净值 资产份额 资产净值 资产总值
    NavHistoryTable(
        date=("日期",),
        product_name=("账套名称",),
        product_code=("基金代码",),
        unit_nav=("单位净值",),
        cumulative_nav=("累计单位净值",),
        net_asset_value=("资产净值",),
        total_assets=("资产总值",),
        shares_outstanding=("资产份额",),
        date_from_title=True,
        name="custodian-announcement",
    ),
)

# Trailing annotations are stripped before header comparison, so
# "资产净值(元)" == "资产净值" == "资产净值（Net Asset Value）".
_LABEL_SUFFIX = re.compile(r"(?:[(（][^)）]*[)）]|%)$")


def _header_key(value: object) -> str:
    label = normalize_label(value)
    return _LABEL_SUFFIX.sub("", label)


def _table_labels(table: NavHistoryTable) -> set[str]:
    fields = (
        () if table.date_from_title else table.date,
        table.product_name,
        table.product_code,
        table.unit_nav,
        table.cumulative_nav,
        table.net_asset_value,
        table.total_assets,
        table.total_liabilities,
        table.shares_outstanding,
        table.daily_return,
        table.share_name,
        table.share_code,
        table.share_net_assets,
    )
    return {_header_key(label) for field in fields for label in field}


def _match_table(header: tuple[object, ...]) -> NavHistoryTable | None:
    """Return the most specific layout fully covered by this header row."""

    keys = {_header_key(value) for value in header if text(value)}
    if not keys:
        return None
    best: NavHistoryTable | None = None
    best_size = 0
    for table in NAV_TABLES:
        labels = _table_labels(table)
        if not labels or not labels <= keys:
            continue
        if len(labels) > best_size:
            best = table
            best_size = len(labels)
    return best


def _column_map(header: tuple[object, ...], table: NavHistoryTable) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for field in (
        "date",
        "product_name",
        "product_code",
        "unit_nav",
        "cumulative_nav",
        "net_asset_value",
        "total_assets",
        "total_liabilities",
        "shares_outstanding",
        "daily_return",
        "share_name",
        "share_code",
        "share_net_assets",
    ):
        labels = {_header_key(label) for label in getattr(table, field)}
        for index, value in enumerate(header):
            if _header_key(value) in labels:
                mapping[field] = index
                break
    return mapping


def parse_nav_history(path: Path) -> tuple[NavHistoryEntry, ...]:
    """Parse one NAV history workbook into per-date fund-level entries."""

    worksheets = read_workbook(path)
    for worksheet in worksheets:
        entries = _parse_worksheet(worksheet)
        if entries:
            return entries
        entries = _parse_key_value_worksheet(worksheet)
        if entries:
            return entries
    raise NavHistoryParseError("nav_history_header_not_found")


def _parse_worksheet(worksheet: WorksheetData) -> tuple[NavHistoryEntry, ...]:
    for header_index, row in enumerate(worksheet.rows):
        if _match_table(row) is None:
            continue
        entries = _parse_table_rows(
            worksheet,
            row,
            header_index + 1,
            title_rows=worksheet.rows[:header_index],
        )
        if entries:
            return entries
    # A two-row header block — identity labels on top, the data labels of the
    # NAV block underneath — only becomes visible once both rows are merged.
    for index in range(len(worksheet.rows) - 1):
        composite = _composite_header(worksheet.rows[index], worksheet.rows[index + 1])
        if _match_table(composite) is None:
            continue
        entries = _parse_table_rows(
            worksheet,
            composite,
            index + 2,
            title_rows=worksheet.rows[:index],
        )
        if entries:
            return entries
    return ()


def _parse_table_rows(
    worksheet: WorksheetData,
    header: tuple[object, ...],
    data_start: int,
    *,
    title_rows: tuple[tuple[object, ...], ...],
) -> tuple[NavHistoryEntry, ...]:
    table = _match_table(header)
    assert table is not None  # guarded by the caller
    columns = _column_map(header, table)
    title_date: date | None = None
    if "date" not in columns:
        if not table.date_from_title:
            return ()
        title_date = parse_date(
            " ".join(text(value) for row in title_rows for value in row if text(value))
        )
        if title_date is None:
            return ()

    rows_by_date: dict[date, list[tuple[tuple[object, ...], int]]] = {}
    for row_index, row in enumerate(worksheet.rows[data_start:], data_start + 1):
        row_date = (
            parse_date(_cell(row, columns["date"])) if "date" in columns else title_date
        )
        if row_date is None:
            continue
        # With the date in the title every trailing row would be dated too, so
        # rows without any NAV value (免责声明 footers and the like) are dropped.
        if "date" not in columns and not _row_has_nav_data(row, columns):
            continue
        rows_by_date.setdefault(row_date, []).append((row, row_index))

    entries: list[NavHistoryEntry] = []
    for row_date in sorted(rows_by_date):
        entry = _build_date_entry(row_date, rows_by_date[row_date], columns)
        if entry is not None:
            entries.append(entry)
    return tuple(entries)


def _composite_header(
    top: tuple[object, ...], bottom: tuple[object, ...]
) -> tuple[object, ...]:
    """Merge an adjacent row pair into one candidate header.

    The bottom row carries the data-column labels; the top row labels only the
    identity columns the bottom row leaves blank (账套名称/基金代码).  An
    announcement template repeats a NAV label under such an identity column;
    that cell is discarded because the label recurs later in the bottom row,
    while a genuine sub-label is unique there.
    """

    header: list[object] = []
    for column in range(max(len(top), len(bottom))):
        top_cell = top[column] if column < len(top) else None
        bottom_cell = bottom[column] if column < len(bottom) else None
        bottom_text = text(bottom_cell)
        if not bottom_text:
            header.append(top_cell if text(top_cell) else None)
        elif text(top_cell) and bottom_text in [
            text(value) for value in bottom[column + 1 :]
        ]:
            header.append(top_cell)
        else:
            header.append(bottom_cell)
    return tuple(header)


def _row_has_nav_data(row: tuple[object, ...], columns: dict[str, int]) -> bool:
    return any(
        decimal(_cell(row, columns.get(field))) is not None
        for field in (
            "unit_nav",
            "net_asset_value",
            "total_assets",
            "shares_outstanding",
        )
    )


def _first_column(columns: dict[str, int], *fields: str) -> int | None:
    for field in fields:
        if (column := columns.get(field)) is not None:
            return column
    return None


def _build_date_entry(
    row_date: date,
    rows: list[tuple[tuple[object, ...], int]],
    columns: dict[str, int],
) -> NavHistoryEntry | None:
    name_column = _first_column(columns, "share_name", "product_name")

    def name_of(row: tuple[object, ...]) -> str:
        return text(_cell(row, name_column)) if name_column is not None else ""

    names = {name_of(row) for row, _ in rows} - {""}
    # All rows of one date belong to one product, so the fund-level row is the
    # one whose name no other row extends with a suffix (基金 / 基金A / 基金B).
    fund_names = {
        name
        for name in names
        if not any(other != name and name.startswith(other) for other in names)
    }
    fund_rows = [
        (row, index) for row, index in rows if name_of(row) in fund_names or not names
    ]
    class_rows = [
        (row, index) for row, index in rows if names and name_of(row) not in fund_names
    ]
    if not fund_rows and not class_rows:
        return None

    primary_rows = fund_rows or rows
    entry = _entry_from_rows(row_date, primary_rows, columns)
    classes = tuple(
        share_class
        for share_class in (
            _share_class_from_row(row, columns) for row, _ in class_rows
        )
        if share_class is not None
    )
    if not fund_rows:
        # The fund-level row is absent; the classes are the whole truth.
        values = [item.net_assets for item in classes if item.net_assets is not None]
        if values:
            entry = replace(
                entry, net_asset_value=sum(values, Decimal(0)), share_classes=classes
            )
    elif classes:
        entry = replace(entry, share_classes=classes)
    return entry


def _entry_from_rows(
    row_date: date,
    rows: list[tuple[tuple[object, ...], int]],
    columns: dict[str, int],
) -> NavHistoryEntry:
    row, row_index = rows[0]
    name_column = _first_column(columns, "product_name", "share_name")
    names = [
        text(_cell(item, name_column))
        for item, _ in rows
        if name_column is not None and text(_cell(item, name_column))
    ]
    return NavHistoryEntry(
        product_name=min(names, key=len) if names else "",
        valuation_date=row_date,
        product_code=text(_cell(row, columns.get("product_code"))) or None,
        unit_nav=decimal(_cell(row, columns.get("unit_nav"))),
        cumulative_unit_nav=decimal(_cell(row, columns.get("cumulative_nav"))),
        net_asset_value=decimal(_cell(row, columns.get("net_asset_value"))),
        total_assets=decimal(_cell(row, columns.get("total_assets"))),
        total_liabilities=decimal(_cell(row, columns.get("total_liabilities"))),
        shares_outstanding=decimal(_cell(row, columns.get("shares_outstanding"))),
        daily_return=ratio(_cell(row, columns.get("daily_return"))),
        source_row=row_index,
    )


def _share_class_from_row(
    row: tuple[object, ...], columns: dict[str, int]
) -> ParsedShareClass | None:
    name_column = columns.get("share_name")
    if name_column is None:
        return None
    share_name = text(_cell(row, name_column))
    if not share_name:
        return None
    return ParsedShareClass(
        share_code=text(_cell(row, columns.get("share_code"))) or share_name,
        share_name=share_name,
        net_assets=decimal(_cell(row, columns.get("share_net_assets"))),
        paid_in_capital=None,
        unit_nav=decimal(_cell(row, columns.get("unit_nav"))),
        cumulative_unit_nav=decimal(_cell(row, columns.get("cumulative_nav"))),
        previous_unit_nav=None,
        daily_return=ratio(_cell(row, columns.get("daily_return"))),
    )


def _cell(row: tuple[object, ...], column: int | None) -> object:
    return row[column] if column is not None and len(row) > column else None


# Key-value announcements carry one product on one date per sheet: every row
# starts with a label and the values follow in the next columns.  Extra value
# columns beyond the first are share classes of the same product.
_KEY_VALUE_LABELS: dict[str, str] = {
    "基金代码": "product_code",
    "产品代码": "product_code",
    "协会备案代码": "product_code",
    "协会备案编码": "product_code",
    "基金名称": "product_name",
    "产品名称": "product_name",
    "估值日期": "valuation_date",
    "净值日期": "valuation_date",
    "资产净值": "net_asset_value",
    "基金资产净值": "net_asset_value",
    "产品资产净值": "net_asset_value",
    "资产总值": "total_assets",
    "总层面资产合计": "total_assets",
    "产品总资产": "total_assets",
    "资产份额": "shares_outstanding",
    "基金资产份额": "shares_outstanding",
    "产品总份额": "shares_outstanding",
    "单位净值": "unit_nav",
    "基金份额净值": "unit_nav",
    "产品单位净值": "unit_nav",
    "累计单位净值": "cumulative_unit_nav",
    "基金份额累计净值": "cumulative_unit_nav",
    "产品累计单位净值": "cumulative_unit_nav",
}

# Fund catalogues key the fund by its 协会备案 code, so a filing code wins
# over a TA/product code when a sheet carries both.
_FILING_CODE_LABELS = frozenset({"协会备案代码", "协会备案编码"})

# The announcement date sits in the title block when no label row carries it.
_TITLE_ROWS = 8

# The label column is not always the first one — the 示例证券 公告 indents it
# into column B — so each row is scanned for its first known label.
_LABEL_COLUMNS = 4


def _parse_key_value_worksheet(
    worksheet: WorksheetData,
) -> tuple[NavHistoryEntry, ...]:
    labelled: list[tuple[int, str, str, tuple[object, ...], int]] = []
    for row_index, row in enumerate(worksheet.rows):
        for column in range(min(_LABEL_COLUMNS, len(row))):
            label = _header_key(row[column])
            field = _KEY_VALUE_LABELS.get(label)
            if field is None:
                continue
            value_column = next(
                (index for index in range(column + 1, len(row)) if text(row[index])),
                None,
            )
            if value_column is not None:
                labelled.append((row_index, label, field, row, value_column))
            break
    if not labelled:
        return ()
    # The announcements indent the value block behind a blank column, so the
    # block starts where the earliest of them does, not right after the label.
    value_column = min(item[4] for item in labelled)

    values_by_field: dict[str, tuple[object, ...]] = {}
    source_row = 0
    for row_index, label, field, row, _ in labelled:
        values = tuple(row[value_column:])
        if field == "product_code" and label in _FILING_CODE_LABELS:
            values_by_field[field] = values
        else:
            values_by_field.setdefault(field, values)
        if field == "product_name":
            source_row = row_index + 1

    names = values_by_field.get("product_name", ())
    if not names or not text(names[0]):
        return ()
    date_values = values_by_field.get("valuation_date", ())
    row_date = parse_date(date_values[0]) if date_values else None
    if row_date is None:
        row_date = parse_date(
            " ".join(
                text(value)
                for row in worksheet.rows[:_TITLE_ROWS]
                for value in row
                if text(value)
            )
        )
    if row_date is None:
        return ()

    codes = values_by_field.get("product_code", ())
    share_classes: list[ParsedShareClass] = []
    for index in range(1, len(names)):
        share_name = text(names[index])
        if not share_name:
            continue
        share_classes.append(
            ParsedShareClass(
                share_code=_key_value_text(codes, index) or share_name,
                share_name=share_name,
                net_assets=_key_value_decimal(
                    values_by_field, "net_asset_value", index
                ),
                paid_in_capital=None,
                unit_nav=_key_value_decimal(values_by_field, "unit_nav", index),
                cumulative_unit_nav=_key_value_decimal(
                    values_by_field, "cumulative_unit_nav", index
                ),
                previous_unit_nav=None,
                daily_return=None,
            )
        )
    return (
        NavHistoryEntry(
            product_name=text(names[0]),
            valuation_date=row_date,
            product_code=_key_value_text(codes),
            unit_nav=_key_value_decimal(values_by_field, "unit_nav"),
            cumulative_unit_nav=_key_value_decimal(
                values_by_field, "cumulative_unit_nav"
            ),
            net_asset_value=_key_value_decimal(values_by_field, "net_asset_value"),
            total_assets=_key_value_decimal(values_by_field, "total_assets"),
            total_liabilities=None,
            shares_outstanding=_key_value_decimal(
                values_by_field, "shares_outstanding"
            ),
            daily_return=None,
            share_classes=tuple(share_classes),
            source_row=source_row,
        ),
    )


def _key_value_text(values: tuple[object, ...], index: int = 0) -> str | None:
    return text(values[index]) or None if len(values) > index else None


def _key_value_decimal(
    values_by_field: dict[str, tuple[object, ...]],
    field: str,
    index: int = 0,
) -> Decimal | None:
    values = values_by_field.get(field, ())
    return decimal(values[index]) if len(values) > index else None


__all__ = [
    "NavHistoryEntry",
    "NavHistoryParseError",
    "NavHistoryTable",
    "parse_nav_history",
]
