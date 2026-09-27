#!/usr/bin/env python3
"""本地启动：python3 run.py  →  http://127.0.0.1:8000

环境变量：
  PORT                端口（默认 8000）
  FRIDGE_DATA         数据目录（默认 ./data，含 SQLite 与标签/照片文件）
  FRIDGE_TICK         后台任务间隔秒数（默认 5）
  FRIDGE_APPEAL_SECONDS  申诉期秒数（默认 172800 = 48h）
"""
import os

import uvicorn

if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    port = int(os.environ.get("PORT", "8000"))
    print(f"写字楼公共冰箱格位与过期清理系统  →  http://127.0.0.1:{port}")
    print("数据目录：./data（SQLite + 标签/清理照片）")
    uvicorn.run("fridge.app:app", host="127.0.0.1", port=port, reload=False)
