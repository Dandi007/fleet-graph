#!/usr/local/bin/python
"""Git 只从本次 secret 文件读取 token，不写入 remote URL。"""

import os
import sys
from pathlib import Path

if "username" in sys.argv[1].lower():
    print("x-access-token")
else:
    print(Path(os.environ["GH_TOKEN_FILE"]).read_text().strip())
