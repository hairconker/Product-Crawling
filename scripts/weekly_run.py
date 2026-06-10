"""阶段 13：每周调度总入口。

顺序执行：
  1. 阶段 9  dict_build（docyx → dict/ + skus/）         —— 硬依赖，失败即中止
  2. 阶段 10 dict_jd_build（京东分类页国行品牌补充）      —— 软依赖，失败继续
  3. 阶段 11 dict_merge（三源合并 → _merged/）           —— 硬依赖
  4. 阶段 12 run_cpu_crawl_pw.py --from-dict             —— SKU 级重试，失败不中止

用法：
  python scripts/weekly_run.py                              # 当前 ISO 周，全流程
  python scripts/weekly_run.py --only dict                  # 只跑 dict（9+10+11）
  python scripts/weekly_run.py --only prices                # 只跑 price 采集
  python scripts/weekly_run.py --week 2026-W17 --categories gpu,cpu,ram,ssd --headed
  python scripts/weekly_run.py --skip-jd                    # 跳过阶段 10（默认环境不在 Windows 时建议）

默认热门品类（阶段 12 只跑这些减少耗时）：gpu, cpu, ram, storage_ssd, mb
冷门（storage_hdd/psu/case/cooler/nic_*）按需手动跑。
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
_PY = sys.executable or "python3"


DEFAULT_HOT_CATEGORIES = "gpu,cpu,ram,storage_ssd,mb"


def current_iso_week() -> str:
    y, w, _ = date.today().isocalendar()
    return f"{y}-W{w:02d}"


@dataclass
class StepResult:
    name: str
    returncode: int
    elapsed_sec: float
    cmd: list[str]
    stdout_tail: str = ""
    stderr_tail: str = ""


@dataclass
class RunSummary:
    week: str
    started_at: str
    ended_at: str = ""
    steps: list[StepResult] = field(default_factory=list)
    overall_success: bool = False


def _run_step(
    name: str,
    cmd: list[str],
    log_path: Path,
    *,
    allow_fail: bool = False,
) -> StepResult:
    """跑单步。stdout+stderr 落盘，返回截断摘要。allow_fail=True 时失败不抛。"""
    t0 = time.monotonic()
    logging.info(f"[weekly] ▶ {name}: {' '.join(cmd)}")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(log_path, "w", encoding="utf-8") as fp:
            proc = subprocess.run(
                cmd, stdout=fp, stderr=subprocess.STDOUT,
                cwd=str(_ROOT), check=False, text=True,
            )
    except FileNotFoundError as e:
        elapsed = time.monotonic() - t0
        logging.error(f"[weekly] ✗ {name}: 可执行文件不存在: {e}")
        return StepResult(name=name, returncode=127, elapsed_sec=elapsed,
                          cmd=cmd, stderr_tail=str(e))

    elapsed = time.monotonic() - t0
    tail = ""
    if log_path.exists():
        text = log_path.read_text(encoding="utf-8", errors="replace")
        tail = "\n".join(text.splitlines()[-20:])
    ok = proc.returncode == 0
    icon = "✓" if ok else "✗"
    logging.info(
        f"[weekly] {icon} {name} (rc={proc.returncode}, {elapsed:.1f}s) → {log_path}"
    )
    if not ok and not allow_fail:
        return StepResult(name=name, returncode=proc.returncode,
                          elapsed_sec=elapsed, cmd=cmd, stdout_tail=tail)
    return StepResult(name=name, returncode=proc.returncode,
                      elapsed_sec=elapsed, cmd=cmd, stdout_tail=tail)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="阶段 13：每周字典+价格总调度")
    ap.add_argument("--week", default=None, help="ISO 周号，默认当前周")
    ap.add_argument(
        "--only", choices=("dict", "prices", "all"), default="all",
        help="子阶段选择：dict=9+10+11 / prices=12 / all=全部",
    )
    ap.add_argument("--skip-jd", action="store_true",
                    help="跳过阶段 10（非 Windows 环境建议）")
    ap.add_argument("--categories", default=DEFAULT_HOT_CATEGORIES,
                    help=f"阶段 12 的品类过滤，默认 {DEFAULT_HOT_CATEGORIES}")
    ap.add_argument("--brand", default=None,
                    help="阶段 12 品牌过滤（逗号分隔）")
    ap.add_argument("--chip", default=None,
                    help="阶段 12 芯片过滤（逗号分隔子串）")
    ap.add_argument("--pages", type=int, default=3,
                    help="阶段 12 每平台页数，默认 3")
    ap.add_argument("--headed", action="store_true",
                    help="阶段 12 Playwright 显示窗口")
    ap.add_argument("--resume", action="store_true",
                    help="阶段 12 断点续跑")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印将跑的命令，不真跑")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s - %(message)s",
    )

    week = args.week or current_iso_week()
    summary = RunSummary(week=week, started_at=datetime.now().isoformat(timespec="seconds"))
    log_dir = _ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    weekly_log_path = log_dir / f"weekly_{week}.log"
    weekly_summary_path = log_dir / f"weekly_{week}.json"
    # 附加 FileHandler 让聚合日志真落盘（Codex 审 S2 修）
    fh = logging.FileHandler(weekly_log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s - %(message)s"))
    logging.getLogger().addHandler(fh)

    # 组装要跑的步骤
    steps_to_run: list[tuple[str, list[str], bool]] = []  # (name, cmd, allow_fail)
    run_dict = args.only in ("dict", "all")
    run_prices = args.only in ("prices", "all")

    if run_dict:
        steps_to_run.append((
            "dict_build",
            [_PY, "scripts/dict_build.py", "--week", week],
            False,  # 硬依赖
        ))
        if not args.skip_jd:
            steps_to_run.append((
                "dict_jd_build",
                [_PY, "scripts/dict_jd_build.py", "--week", week],
                True,   # 软依赖，失败继续
            ))
        steps_to_run.append((
            "dict_merge",
            [_PY, "scripts/dict_merge.py", "--week", week],
            False,
        ))

    if run_prices:
        price_cmd = [
            _PY, "run_cpu_crawl_pw.py",
            "--from-dict", week,
            "--pages", str(args.pages),
            "--categories", args.categories,
        ]
        if args.brand:
            price_cmd.extend(["--brand", args.brand])
        if args.chip:
            price_cmd.extend(["--chip", args.chip])
        if args.headed:
            price_cmd.append("--headed")
        if args.resume:
            price_cmd.append("--resume")
        steps_to_run.append(("price_crawl", price_cmd, True))

    # 执行
    if args.dry_run:
        print(f"[dry-run] week={week}  steps={len(steps_to_run)}")
        for name, cmd, allow_fail in steps_to_run:
            flag = "(allow_fail)" if allow_fail else "(hard)"
            print(f"  {name:18s} {flag}  {' '.join(cmd)}")
        return 0

    for name, cmd, allow_fail in steps_to_run:
        step_log = log_dir / f"weekly_{week}_{name}.log"
        result = _run_step(name, cmd, step_log, allow_fail=allow_fail)
        summary.steps.append(result)
        if result.returncode != 0 and not allow_fail:
            logging.error(f"[weekly] 硬依赖步骤 {name} 失败，中止后续")
            break

    summary.ended_at = datetime.now().isoformat(timespec="seconds")
    summary.overall_success = all(
        s.returncode == 0 for s in summary.steps
        if s.name not in ("dict_jd_build", "price_crawl")  # 软依赖不影响总成功
    )

    # 写摘要 JSON
    tmp = weekly_summary_path.with_suffix(weekly_summary_path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump({
            "week": summary.week,
            "started_at": summary.started_at,
            "ended_at": summary.ended_at,
            "overall_success": summary.overall_success,
            "steps": [
                {
                    "name": s.name,
                    "returncode": s.returncode,
                    "elapsed_sec": round(s.elapsed_sec, 2),
                    "cmd": s.cmd,
                }
                for s in summary.steps
            ],
        }, fp, ensure_ascii=False, indent=2)
    tmp.replace(weekly_summary_path)

    print()
    print(f"[weekly] week={week} overall={'OK' if summary.overall_success else 'FAILED'}")
    for s in summary.steps:
        print(f"  {s.name:18s} rc={s.returncode} {s.elapsed_sec:6.1f}s")
    print(f"[weekly] 总摘要：{weekly_summary_path}")
    return 0 if summary.overall_success else 1


if __name__ == "__main__":
    raise SystemExit(main())
