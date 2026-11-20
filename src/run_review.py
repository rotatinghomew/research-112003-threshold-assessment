"""审议流程运行器。

隔离模式（-I）下解释器不会自动把 cwd/PYTHONPATH 加入 sys.path，
因此这里显式只注入脚本自身所在目录；数据目录仅以参数形式传入，
解释器不会从数据目录加载任何模块。

在项目根目录运行：
    python3 -I src/run_review.py --data data --outdir reports
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from review.loader import load  # noqa: E402
from review.evaluator import evaluate_all  # noqa: E402
from review.report import render_json, render_markdown  # noqa: E402


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--outdir", default="reports")
    args = ap.parse_args()

    wh = load(args.data)
    verdicts = evaluate_all(wh)

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "review.md").write_text(render_markdown(verdicts), encoding="utf-8")
    (outdir / "review.json").write_text(render_json(verdicts), encoding="utf-8")

    for v in verdicts:
        print(f"{v.plan.plan}: {v.final_decision} — {v.reason_summary}")
    print(f"\n报告已写入 {outdir / 'review.md'} 与 {outdir / 'review.json'}")


if __name__ == "__main__":
    main()
