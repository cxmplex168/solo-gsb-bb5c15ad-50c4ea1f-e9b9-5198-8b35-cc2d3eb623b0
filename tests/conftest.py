"""测试运行前把案件数据库指到临时目录,避免在仓库根目录生成 data/。"""
from __future__ import annotations

import os
import tempfile

os.environ.setdefault(
    "CASE_DB_PATH",
    os.path.join(tempfile.mkdtemp(prefix="reconcile-test-"), "cases.db"),
)
