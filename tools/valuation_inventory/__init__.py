"""历史估值文件只读盘点工具（valuation_inventory）。

对指定目录做递归扫描、哈希、估值表识别、产品/日期解析与主目录/gz 去重比较，
输出 JSON / CSV / Markdown 盘点报告。工具只读源目录，绝不修改、移动或删除源文件。
"""

from .models import ErrorType, FileInfo, FileType, ParseStatus, SourceZone

__version__ = "1.0.0"

__all__ = [
    "ErrorType",
    "FileInfo",
    "FileType",
    "ParseStatus",
    "SourceZone",
    "__version__",
]
