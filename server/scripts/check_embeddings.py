"""诊断 RAG 索引里的坏块：零范数 / nan·inf / 维度不符的嵌入向量。

这些坏块会让 vector_search 的余弦计算触发 divide-by-zero/overflow/invalid 告警
（检索层已容错、不会崩，但坏块本身检索不到内容）。本脚本只读不改，逐个规则书 / 模组
汇总坏块数并列出问题 chunk，便于判断是否需要在页面上「重建索引」。

用法：cd server && .venv/bin/python scripts/check_embeddings.py
      cd server && .venv/bin/python scripts/check_embeddings.py --verbose   # 逐条列出坏块
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ai.embedding import DEFAULT_EMBED_DIM  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import Module, ModuleChunk, RuleChunk, Rulebook  # noqa: E402


def _classify(blob: bytes, expected_dim: int) -> str | None:
    """返回坏块类型（None=正常）。"""
    vec = np.frombuffer(blob, dtype=np.float32)
    if vec.size == 0:
        return "空向量"
    if expected_dim and vec.size != expected_dim:
        return f"维度不符（{vec.size}≠{expected_dim}）"
    if not np.isfinite(vec).all():
        return "含 nan/inf"
    if float(np.linalg.norm(vec)) == 0.0:
        return "零范数"
    return None


def _scan(db, chunk_model, owner_model, owner_fk: str, title_attr: str, verbose: bool) -> int:
    """扫描一类 chunk（规则书 / 模组），按归属分组汇总坏块，返回坏块总数。"""
    total_bad = 0
    owners = db.query(owner_model).all()
    for owner in owners:
        chunks = db.query(chunk_model).filter(
            getattr(chunk_model, owner_fk) == owner.id
        ).all()
        bad: list[tuple[str, str]] = []
        for c in chunks:
            kind = _classify(c.embedding or b"", DEFAULT_EMBED_DIM)
            if kind:
                bad.append((c.id, kind))
        title = getattr(owner, title_attr, owner.id)
        status = f"，坏块 {len(bad)}/{len(chunks)}" if bad else f"，{len(chunks)} 块全部正常"
        flag = "  [需重建]" if bad else ""
        print(f"  · {title}（{owner.id[:8]}）{status}{flag}")
        if bad and verbose:
            for cid, kind in bad[:20]:
                print(f"      - {cid[:8]}: {kind}")
            if len(bad) > 20:
                print(f"      …… 另有 {len(bad) - 20} 条")
        total_bad += len(bad)
    if not owners:
        print("  （无）")
    return total_bad


def main() -> None:
    parser = argparse.ArgumentParser(description="诊断 RAG 索引坏块")
    parser.add_argument("--verbose", action="store_true", help="逐条列出坏块 chunk")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        print(f"嵌入维度基准：{DEFAULT_EMBED_DIM}\n")
        print("规则书索引：")
        bad_rules = _scan(db, RuleChunk, Rulebook, "rulebook_id", "title", args.verbose)
        print("\n模组原文索引：")
        bad_modules = _scan(db, ModuleChunk, Module, "module_id", "title", args.verbose)

        total = bad_rules + bad_modules
        print("\n" + "=" * 50)
        if total == 0:
            print("全部索引健康，无坏块。")
        else:
            print(f"共发现 {total} 个坏块（规则书 {bad_rules} + 模组 {bad_modules}）。")
            print("处理：在对应页面点「重建索引」重新生成即可；vector_search 已容错，"
                  "坏块只是检索不到内容，不会导致崩溃。")
    finally:
        db.close()


if __name__ == "__main__":
    main()
