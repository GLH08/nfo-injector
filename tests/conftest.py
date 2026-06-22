import sys
from pathlib import Path

# 让 `import backend.xxx` 在任意工作目录下都可用
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
