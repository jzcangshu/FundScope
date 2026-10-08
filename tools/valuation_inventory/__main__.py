"""``python -m tools.valuation_inventory`` 入口。

说明：规格允许的文件清单未列出本文件，但 ``python -m 包名`` 语法必须有
``__main__.py`` 才能执行；本文件仅做一行转发，不包含其他逻辑。
"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
