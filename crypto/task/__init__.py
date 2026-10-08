# 定时任务模块
# 将 crypto/task 目录加入 sys.path，使子模块的裸导入在 Web 模式下也能正确解析
import sys, os as _os
_task_dir = _os.path.dirname(_os.path.abspath(__file__))
if _task_dir not in sys.path:
    sys.path.insert(0, _task_dir)
