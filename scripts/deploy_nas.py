#!/usr/bin/env python3
"""Deploy the Xianyu/Taobao crawler to a Linux NAS over SSH/SFTP.

The script intentionally does not accept a literal password argument. Use an
environment variable or the interactive prompt so the password is not written to
shell history. Playwright login state files are optional and, when uploaded, are
chmod 600 on the NAS.
"""

from __future__ import annotations

import argparse
import getpass
import os
import posixpath
import shlex
import stat
import sys
import warnings
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

warnings.filterwarnings("ignore", message=r".*Blowfish has been deprecated.*")
warnings.filterwarnings("ignore", category=DeprecationWarning, module=r"paramiko\..*")

try:
    import paramiko
except ImportError:
    print("Missing paramiko. Run: python -m pip install paramiko", file=sys.stderr)
    raise


ROOT = Path(__file__).resolve().parent.parent

INCLUDE_FILES = [
    Path("run_cpu_crawl_pw.py"),
    Path("run_cpu_crawl.py"),
    Path("requirements-nas.txt"),
    Path("keywords.example.txt"),
    Path("README.md"),
    Path("AGENTS.md"),
    Path("viewer.html"),
    Path("docs/nas_deploy.md"),
]
INCLUDE_DIRS = [
    Path("scripts"),
    Path("config"),
    Path("core"),
    Path("spiders"),
]
STATE_FILES = [
    Path("state/xianyu_state.json"),
    Path("state/taobao_state.json"),
]
EXCLUDE_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".venv",
    "venv",
    "logs",
    "state",
    "data",
    "vendor",
}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".db", ".sqlite", ".tmp"}
EXCLUDE_FILES = {
    Path("config/config.yaml"),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deploy the Xianyu/Taobao crawler to a Linux NAS")
    parser.add_argument("--host", default="192.168.31.217", help="NAS IP")
    parser.add_argument("--port", type=int, default=22, help="SSH port")
    parser.add_argument("--user", default="root", help="SSH user")
    parser.add_argument(
        "--remote-dir",
        default="/root/autoPCBulid",
        help="Deployment directory on the NAS",
    )
    parser.add_argument(
        "--password-env",
        default="NAS_PASSWORD",
        help="Environment variable used for the SSH password; prompts when absent",
    )
    parser.add_argument(
        "--include-state",
        action="store_true",
        help="Upload state/xianyu_state.json and state/taobao_state.json",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Create a venv, install requirements-nas.txt, and install Chromium on the NAS",
    )
    parser.add_argument(
        "--install-deps",
        action="store_true",
        help="Also run playwright install-deps chromium; this may change system packages",
    )
    parser.add_argument(
        "--run-sample",
        action="store_true",
        help="Run one small real crawl after deployment; off by default to avoid account risk",
    )
    parser.add_argument(
        "--schedule",
        action="store_true",
        help="Install a daily cron job on the NAS",
    )
    parser.add_argument(
        "--schedule-time",
        default="03:10",
        help="Daily cron time in HH:MM, NAS local time",
    )
    parser.add_argument(
        "--schedule-keywords",
        default="cpu",
        help="Comma-separated keywords for the scheduled crawl",
    )
    parser.add_argument(
        "--schedule-platforms",
        default="xianyu",
        help="Comma-separated platforms for the scheduled crawl; use xianyu,taobao only after Taobao is verified",
    )
    parser.add_argument(
        "--schedule-pages",
        type=int,
        default=1,
        help="Pages per keyword for the scheduled crawl",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only list files; do not connect to the NAS",
    )
    parser.add_argument("--sample-keywords", default="cpu", help="Sample crawl keywords")
    parser.add_argument("--sample-pages", type=int, default=1, help="Sample crawl pages")
    return parser.parse_args()


def should_skip(path: Path) -> bool:
    if path in EXCLUDE_FILES:
        return True
    if any(part in EXCLUDE_DIRS for part in path.parts):
        return True
    return path.suffix in EXCLUDE_SUFFIXES


def iter_deploy_files(include_state: bool) -> list[Path]:
    files: list[Path] = []
    for rel in INCLUDE_FILES:
        full = ROOT / rel
        if full.is_file():
            files.append(rel)
    for rel_dir in INCLUDE_DIRS:
        full_dir = ROOT / rel_dir
        if not full_dir.is_dir():
            continue
        for full in full_dir.rglob("*"):
            if not full.is_file():
                continue
            rel = full.relative_to(ROOT)
            if not should_skip(rel):
                files.append(rel)
    if include_state:
        for rel in STATE_FILES:
            if (ROOT / rel).is_file():
                files.append(rel)
            else:
                print(f"[warn] state file missing; skipped: {rel}")
    return sorted(set(files), key=lambda p: p.as_posix())


def sftp_mkdirs(sftp: paramiko.SFTPClient, path: str) -> None:
    parts: list[str] = []
    cur = path
    while cur not in ("", "/"):
        parts.append(cur)
        cur = posixpath.dirname(cur)
    for item in reversed(parts):
        try:
            sftp.stat(item)
        except OSError:
            sftp.mkdir(item)


def chmod_for(rel: Path) -> int:
    if rel.parts and rel.parts[0] == "state":
        return stat.S_IRUSR | stat.S_IWUSR
    if rel.suffix == ".sh":
        return stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IROTH
    return stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH


def upload_files(
    client: paramiko.SSHClient,
    files: list[Path],
    remote_dir: str,
) -> None:
    with client.open_sftp() as sftp:
        sftp_mkdirs(sftp, remote_dir)
        for rel in files:
            local = ROOT / rel
            remote = posixpath.join(remote_dir, rel.as_posix())
            sftp_mkdirs(sftp, posixpath.dirname(remote))
            sftp.put(str(local), remote)
            sftp.chmod(remote, chmod_for(rel))
            print(f"[upload] {rel.as_posix()} -> {remote}")


def run_remote(client: paramiko.SSHClient, command: str) -> int:
    print(f"[remote] {command}")
    stdin, stdout, stderr = client.exec_command(command, get_pty=True)
    stdin.close()
    for line in iter(stdout.readline, ""):
        print(f"[nas] {line}", end="")
    err = stderr.read().decode("utf-8", errors="replace")
    if err.strip():
        print(err, file=sys.stderr)
    code = stdout.channel.recv_exit_status()
    if code != 0:
        raise RuntimeError(f"NAS command failed with exit code {code}: {command}")
    return code


def install_command(remote_dir: str, install_deps: bool) -> str:
    qdir = shlex.quote(remote_dir)
    deps_line = "$PY -m playwright install-deps chromium" if install_deps else "true"
    return (
        "set -e; "
        f"cd {qdir}; "
        "if command -v python3 >/dev/null 2>&1; then PY=python3; "
        "elif command -v python >/dev/null 2>&1; then PY=python; "
        "else echo 'python/python3 was not found on the NAS' >&2; exit 1; fi; "
        "$PY -m venv .venv 2>/tmp/autoPCBulid_venv.err || true; "
        "if [ -x .venv/bin/python ]; then PY=.venv/bin/python; fi; "
        "export PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple; "
        "export PIP_DEFAULT_TIMEOUT=120; "
        "export PIP_RETRIES=5; "
        "$PY -m pip install --progress-bar off --upgrade pip || true; "
        "$PY -m pip install --progress-bar off -r requirements-nas.txt; "
        f"{deps_line}; "
        "$PY -m playwright install chromium; "
        "$PY -m py_compile run_cpu_crawl_pw.py scripts/login_helper.py"
    )


def smoke_command(remote_dir: str) -> str:
    qdir = shlex.quote(remote_dir)
    return (
        "set -e; "
        f"cd {qdir}; "
        "PY=.venv/bin/python; [ -x \"$PY\" ] || PY=python3; "
        "$PY -m py_compile run_cpu_crawl_pw.py scripts/login_helper.py"
    )


def run_sample_command(remote_dir: str, keywords: str, pages: int) -> str:
    qdir = shlex.quote(remote_dir)
    qkw = shlex.quote(keywords)
    return (
        "set -e; "
        f"cd {qdir}; "
        "PY=.venv/bin/python; [ -x \"$PY\" ] || PY=python3; "
        "$PY run_cpu_crawl_pw.py --only xianyu --only taobao "
        f"--keywords {qkw} --pages {int(pages)}"
    )


def parse_schedule_time(value: str) -> tuple[int, int]:
    try:
        hour_text, minute_text = value.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--schedule-time must be HH:MM") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise argparse.ArgumentTypeError("--schedule-time must be HH:MM")
    return hour, minute


def schedule_command(
    remote_dir: str,
    platforms: str,
    keywords: str,
    pages: int,
    schedule_time: str,
) -> str:
    hour, minute = parse_schedule_time(schedule_time)
    qdir = shlex.quote(remote_dir)
    qplatforms = shlex.quote(platforms)
    qkw = shlex.quote(keywords)
    marker = "# autoPCBulid crawler"
    job = (
        f"{minute} {hour} * * * cd {qdir} && "
        f"PLATFORMS={qplatforms} KEYWORDS={qkw} PAGES={int(pages)} "
        "scripts/nas_run_crawl.sh >> logs/nas_cron.log 2>&1"
    )
    qmarker = shlex.quote(marker)
    qjob = shlex.quote(job)
    return (
        "set -e; "
        "if ! command -v crontab >/dev/null 2>&1; then "
        "echo 'crontab was not found on the NAS' >&2; exit 1; "
        "fi; "
        f"cd {qdir}; mkdir -p logs; "
        f"(crontab -l 2>/dev/null | grep -vF {qmarker} | "
        "grep -vF 'scripts/nas_run_crawl.sh' || true; "
        f"printf '%s\\n' {qmarker} {qjob}) | crontab -; "
        "crontab -l | tail -n 10"
    )


def main() -> int:
    args = parse_args()
    files = iter_deploy_files(include_state=args.include_state)
    if not files:
        print("No deployable files found.", file=sys.stderr)
        return 2
    if args.dry_run:
        print(f"[dry-run] Upload {len(files)} files to {args.user}@{args.host}:{args.remote_dir}")
        for rel in files:
            print(f"  {rel.as_posix()}")
        return 0

    password = os.environ.get(args.password_env)
    if password is None:
        password = getpass.getpass(f"{args.user}@{args.host} SSH password: ")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            args.host,
            port=args.port,
            username=args.user,
            password=password or None,
            look_for_keys=True,
            allow_agent=True,
            timeout=15,
        )
        upload_files(client, files, args.remote_dir)
        if args.install:
            run_remote(client, install_command(args.remote_dir, args.install_deps))
        else:
            run_remote(client, smoke_command(args.remote_dir))
        if args.run_sample:
            run_remote(
                client,
                run_sample_command(args.remote_dir, args.sample_keywords, args.sample_pages),
            )
        if args.schedule:
            run_remote(
                client,
                schedule_command(
                    args.remote_dir,
                    args.schedule_platforms,
                    args.schedule_keywords,
                    args.schedule_pages,
                    args.schedule_time,
                ),
            )
    finally:
        client.close()

    print("[done] NAS deployment completed")
    print(f"[done] Remote directory: {args.remote_dir}")
    print(f"[done] Brief log: {args.remote_dir}/logs/crawl_pw_brief_YYYY-MM-DD.log")
    print(f"[done] Detailed log: {args.remote_dir}/logs/crawl_pw_YYYY-MM-DD.log")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
