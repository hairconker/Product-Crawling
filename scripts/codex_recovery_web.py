from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ROOT_DIR = Path(__file__).resolve().parents[1]
RESTORE_SCRIPT = ROOT_DIR / "scripts" / "restore_codex_sessions.py"
EXPORT_SCRIPT = ROOT_DIR / "scripts" / "export_codex_sessions.py"
DASHBOARD_HTML = ROOT_DIR / "codex_recovery_dashboard.html"
SAFE_OUTPUT_NAME = re.compile(r"^[A-Za-z0-9_.\-\u4e00-\u9fff]+$")


@dataclass(slots=True)
class CommandResult:
    name: str
    command: list[str]
    returncode: int
    stdout: str
    stderr: str


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a local Codex session recovery dashboard.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host. Default: 127.0.0.1.")
    parser.add_argument("--port", type=int, default=8765, help="Bind port. Default: 8765.")
    return parser.parse_args(argv)


def run_tool(name: str, args: list[str], timeout: int = 120) -> CommandResult:
    command = [sys.executable, *args]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    completed = subprocess.run(
        command,
        cwd=ROOT_DIR,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    return CommandResult(
        name=name,
        command=command,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def result_to_json(result: CommandResult) -> dict[str, Any]:
    return {
        "name": result.name,
        "command": command_display(result.command),
        "returncode": result.returncode,
        "ok": result.returncode == 0,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def command_display(command: list[str]) -> str:
    parts: list[str] = []
    for item in command:
        if re.search(r"\s", item):
            parts.append(f'"{item}"')
        else:
            parts.append(item)
    return " ".join(parts)


def scan_state() -> dict[str, Any]:
    report = run_tool("report", [str(RESTORE_SCRIPT), "--report"], timeout=60)
    repair = run_tool("repair-api-visible-copies", [str(RESTORE_SCRIPT), "--repair-api-visible-copies"], timeout=90)
    api_copy = run_tool("api-visible-copy", [str(RESTORE_SCRIPT), "--api-visible-copy"], timeout=90)
    normalize = run_tool("normalize-cwd", [str(RESTORE_SCRIPT), "--normalize-cwd"], timeout=60)
    projects = run_tool("list-projects", [str(EXPORT_SCRIPT), "--list-only"], timeout=90)
    return {
        "report": result_to_json(report),
        "repair": result_to_json(repair),
        "apiCopy": result_to_json(api_copy),
        "normalize": result_to_json(normalize),
        "projectsRaw": result_to_json(projects),
        "parsed": {
            "providers": parse_provider_counts(report.stdout),
            "projects": parse_project_rows(report.stdout),
            "summary": parse_report_summary(report.stdout),
            "repair": parse_first_count(repair.stdout),
            "apiCopy": parse_first_count(api_copy.stdout),
            "normalize": parse_first_count(normalize.stdout),
            "projectList": parse_project_list(projects.stdout),
        },
    }


def parse_provider_counts(text: str) -> dict[str, int]:
    providers: dict[str, int] = {}
    in_block = False
    for line in text.splitlines():
        if line.strip() == "Provider counts:":
            in_block = True
            continue
        if in_block and not line.strip():
            break
        if in_block:
            match = re.match(r"\s*(.+?):\s*(\d+)\s*$", line)
            if match:
                providers[match.group(1)] = int(match.group(2))
    return providers


def parse_project_rows(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    pattern = re.compile(
        r"^\s*(?P<display>.+?)\s+\|\s+(?P<project>.+?)\s+\|\s+(?P<origin>configured|discovered)\s+\|\s+custom=(?P<custom>\d+)\s+"
        r"\(plain=(?P<plain>\d+),\s+extended=(?P<extended>\d+)\)\s+\|\s+"
        r"openai=(?P<openai>\d+)\s+\|\s+user_like_custom=(?P<user_like>\d+)\s*$",
    )
    for line in text.splitlines():
        match = pattern.match(line)
        if not match:
            continue
        key = (match.group("project"), match.group("origin"))
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "project": match.group("project"),
                "displayName": match.group("display"),
                "origin": match.group("origin"),
                "custom": int(match.group("custom")),
                "plain": int(match.group("plain")),
                "extended": int(match.group("extended")),
                "openai": int(match.group("openai")),
                "userLikeCustom": int(match.group("user_like")),
            },
        )
    return rows


def parse_report_summary(text: str) -> dict[str, int]:
    summary: dict[str, int] = {}
    for line in text.splitlines():
        match = re.match(r"custom extended cwd rows:\s*(\d+)\s*$", line)
        if match:
            summary["customExtendedCwdRows"] = int(match.group(1))
            continue
        match = re.match(r"marked api-visible copies:\s*(\d+)\s*$", line)
        if match:
            summary["markedApiVisibleCopies"] = int(match.group(1))
    return summary


def parse_first_count(text: str) -> int | None:
    match = re.search(r":\s*(\d+)\s+", text)
    if match:
        return int(match.group(1))
    return None


def parse_project_list(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    pattern = re.compile(r"^\s*(?P<count>\d+)\s+(?P<project>.+?)\s*$")
    for line in text.splitlines():
        match = pattern.match(line)
        if match:
            rows.append({"count": int(match.group("count")), "project": match.group("project")})
    return rows


def run_action(payload: dict[str, Any]) -> dict[str, Any]:
    action = str(payload.get("action", ""))
    selected_projects = selected_project_args(payload)
    if action == "report":
        return {"result": result_to_json(run_tool("report", [str(RESTORE_SCRIPT), "--report"], timeout=60))}
    if action == "repairDry":
        return {
            "result": result_to_json(
                run_tool(
                    "repair-api-visible-copies",
                    [str(RESTORE_SCRIPT), "--repair-api-visible-copies", *selected_projects],
                    timeout=90,
                ),
            ),
        }
    if action == "repairApply":
        return {
            "result": result_to_json(
                run_tool(
                    "repair-api-visible-copies-apply",
                    [str(RESTORE_SCRIPT), "--repair-api-visible-copies", "--apply", *selected_projects],
                    timeout=180,
                ),
            ),
        }
    if action == "backupOnly":
        return {
            "result": result_to_json(
                run_tool("backup-only", [str(RESTORE_SCRIPT), "--backup-only"], timeout=60),
            ),
        }
    if action == "apiCopyDry":
        return {"result": result_to_json(run_tool("api-visible-copy", [str(RESTORE_SCRIPT), "--api-visible-copy"]))}
    if action == "apiCopyApply":
        return {
            "result": result_to_json(
                run_tool("api-visible-copy-apply", [str(RESTORE_SCRIPT), "--api-visible-copy", "--apply"], timeout=180),
            ),
        }
    if action == "normalizeDry":
        return {"result": result_to_json(run_tool("normalize-cwd", [str(RESTORE_SCRIPT), "--normalize-cwd"], timeout=60))}
    if action == "normalizeApply":
        return {
            "result": result_to_json(
                run_tool("normalize-cwd-apply", [str(RESTORE_SCRIPT), "--normalize-cwd", "--apply"], timeout=180),
            ),
        }
    if action == "exportList":
        return {"result": result_to_json(run_tool("list-projects", [str(EXPORT_SCRIPT), "--list-only"], timeout=90))}
    if action == "exportAll":
        output = safe_output_name(str(payload.get("output") or "codex-session-export-web"))
        return {
            "result": result_to_json(
                run_tool(
                    "export-all",
                    [str(EXPORT_SCRIPT), "--output", output, "--copy-raw"],
                    timeout=300,
                ),
            ),
        }
    if action == "exportProject":
        project_filter = str(payload.get("projectFilter") or "").strip()
        if not project_filter:
            raise ValueError("projectFilter is required")
        output = safe_output_name(str(payload.get("output") or "codex-session-export-project"))
        return {
            "result": result_to_json(
                run_tool(
                    "export-project",
                    [str(EXPORT_SCRIPT), "--project-filter", project_filter, "--output", output, "--copy-raw"],
                    timeout=300,
                ),
            ),
        }
    if action == "scan":
        return scan_state()
    raise ValueError(f"Unsupported action: {action}")


def selected_project_args(payload: dict[str, Any]) -> list[str]:
    values = payload.get("projects")
    if not isinstance(values, list):
        return []
    args: list[str] = []
    for value in values:
        if isinstance(value, str) and value.strip():
            args.extend(["--project-exact", value.strip()])
    return args


def safe_output_name(value: str) -> str:
    cleaned = value.strip().strip("\\/")
    if not cleaned or not SAFE_OUTPUT_NAME.match(cleaned) or ".." in cleaned:
        raise ValueError("Output name may only contain letters, numbers, dot, dash, underscore, or Chinese characters.")
    return cleaned


class RecoveryHandler(BaseHTTPRequestHandler):
    server_version = "CodexRecoveryDashboard/1.0"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_file(DASHBOARD_HTML, "text/html; charset=utf-8")
            return
        if parsed.path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return
        if parsed.path == "/api/scan":
            self.send_json(scan_state())
            return
        if parsed.path == "/health":
            self.send_json({"ok": True})
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/action":
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        try:
            payload = self.read_json_body()
            self.send_json(run_action(payload))
        except (ValueError, subprocess.TimeoutExpired) as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object")
        return value

    def send_file(self, path: Path, content_type: str) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write(f"[codex-recovery-web] {self.address_string()} - {format % args}\n")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if not RESTORE_SCRIPT.exists() or not EXPORT_SCRIPT.exists():
        sys.stderr.write("Missing recovery scripts. Run this from the repository root checkout.\n")
        return 2
    server = ThreadingHTTPServer((args.host, args.port), RecoveryHandler)
    url = f"http://{args.host}:{args.port}/"
    sys.stdout.write(f"Codex recovery dashboard: {url}\n")
    sys.stdout.write("Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stdout.write("\nStopping server.\n")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
