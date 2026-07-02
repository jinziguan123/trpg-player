"""KP 生成质量评估回路（与 pytest 分离：跑真模型、花钱、手动触发）。

用法见 evals/README.md：
- python -m evals.snapshot <session_id> --turn <seq>   从真实会话导出 fixture
- python -m evals.run --suite kp_core                  重放 fixture 并打分
- python -m evals.compare <old.json> <new.json>        对比两次 scorecard
"""
