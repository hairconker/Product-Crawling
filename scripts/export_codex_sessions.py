from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_SOURCE_DIRS = ("sessions", "sessions.bak", "archived_sessions.bak")
VISIBLE_ROLES = {"user", "assistant"}
SKIP_TEXT_PREFIXES = (
    "<permissions instructions>",
    "<app-context>",
    "<environment_context>",
    "<collaboration_mode>",
    "<skills_instructions>",
    "# AGENTS.md instructions",
    "The following is the Codex agent history",
    "We need continue from summary",
)


@dataclass(slots=True)
class ChatMessage:
    timestamp: str
    role: str
    text: str
    phase: str | None = None


@dataclass(slots=True)
class SessionRecord:
    session_id: str
    title: str
    cwd: str
    created_at: str
    updated_at: str
    source_path: Path
    source_group: str
    model_provider: str
    cli_version: str
    messages: list[ChatMessage] = field(default_factory=list)
    line_count: int = 0
    parse_errors: int = 0


def default_codex_home() -> Path:
    env_home = os.environ.get("CODEX_HOME")
    if env_home:
        return Path(env_home).expanduser()
    return Path.home() / ".codex"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows


def load_index_titles(codex_home: Path) -> dict[str, tuple[str, str]]:
    titles: dict[str, tuple[str, str]] = {}
    for name in ("session_index.jsonl", "session_index.jsonl.bak"):
        path = codex_home / name
        if not path.exists():
            continue
        for row in read_jsonl(path):
            session_id = as_str(row.get("id"))
            title = as_str(row.get("thread_name"))
            updated_at = as_str(row.get("updated_at"))
            if session_id and title:
                titles[session_id] = (title, updated_at)
    return titles


def iter_session_files(codex_home: Path, sources: list[Path], include_backups: bool) -> list[Path]:
    found: list[Path] = []
    if sources:
        candidates = sources
    else:
        names = DEFAULT_SOURCE_DIRS if include_backups else ("sessions",)
        candidates = [codex_home / name for name in names]

    for source in candidates:
        path = source.expanduser()
        if path.is_file() and path.suffix.lower() == ".jsonl":
            found.append(path)
        elif path.is_dir():
            found.extend(path.rglob("*.jsonl"))
    return sorted(set(found), key=lambda item: str(item).lower())


def parse_session(path: Path, titles: dict[str, tuple[str, str]], codex_home: Path) -> SessionRecord | None:
    messages: list[ChatMessage] = []
    seen_visible: set[tuple[str, str]] = set()
    session_id = ""
    title = ""
    cwd = ""
    created_at = ""
    updated_at = ""
    model_provider = ""
    cli_version = ""
    line_count = 0
    parse_errors = 0

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            raw = line.strip()
            if not raw:
                continue
            line_count += 1
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                parse_errors += 1
                continue
            if not isinstance(row, dict):
                continue

            timestamp = as_str(row.get("timestamp"))
            row_type = as_str(row.get("type"))
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue

            if row_type == "session_meta":
                session_id = session_id or as_str(payload.get("id"))
                created_at = created_at or as_str(payload.get("timestamp")) or timestamp
                cwd = cwd or as_str(payload.get("cwd"))
                model_provider = model_provider or as_str(payload.get("model_provider"))
                cli_version = cli_version or as_str(payload.get("cli_version"))
                continue

            if row_type == "turn_context":
                cwd = cwd or as_str(payload.get("cwd"))
                continue

            if row_type == "event_msg":
                payload_type = as_str(payload.get("type"))
                if payload_type == "user_message":
                    text = as_str(payload.get("message")).strip()
                    add_visible_message(messages, seen_visible, timestamp, "user", text, None)
                elif payload_type == "agent_message":
                    text = as_str(payload.get("message")).strip()
                    phase = as_str(payload.get("phase")) or None
                    add_visible_message(messages, seen_visible, timestamp, "assistant", text, phase)
                elif payload_type == "task_complete":
                    text = as_str(payload.get("last_agent_message")).strip()
                    add_visible_message(messages, seen_visible, timestamp, "assistant", text, "final")
                continue

            if row_type == "response_item":
                item_type = as_str(payload.get("type"))
                if item_type == "message":
                    role = as_str(payload.get("role"))
                    if role in VISIBLE_ROLES:
                        text = extract_content_text(payload.get("content")).strip()
                        if not should_skip_message(text):
                            add_visible_message(messages, seen_visible, timestamp, role, text, None)

    if not session_id:
        session_id = session_id_from_filename(path)
    if not session_id:
        return None

    indexed = titles.get(session_id)
    if indexed:
        if not is_internal_title(indexed[0]):
            title = indexed[0]
        updated_at = indexed[1]
    if not title:
        title = infer_title(messages, session_id)
    if not updated_at:
        updated_at = messages[-1].timestamp if messages else created_at
    if not cwd:
        cwd = "unknown-workspace"
    if not created_at:
        created_at = datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")

    return SessionRecord(
        session_id=session_id,
        title=title,
        cwd=cwd,
        created_at=created_at,
        updated_at=updated_at,
        source_path=path,
        source_group=source_group(path, codex_home),
        model_provider=model_provider,
        cli_version=cli_version,
        messages=messages,
        line_count=line_count,
        parse_errors=parse_errors,
    )


def add_visible_message(
    messages: list[ChatMessage],
    seen_visible: set[tuple[str, str]],
    timestamp: str,
    role: str,
    text: str,
    phase: str | None,
) -> None:
    if not text or should_skip_message(text):
        return
    key = (role, normalize_seen_text(text))
    if key in seen_visible:
        return
    seen_visible.add(key)
    messages.append(ChatMessage(timestamp=timestamp, role=role, text=text, phase=phase))


def extract_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        for key in ("text", "input_text", "output_text"):
            value = item.get(key)
            if isinstance(value, str) and value:
                parts.append(value)
                break
    return "\n\n".join(parts)


def should_skip_message(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    return any(stripped.startswith(prefix) for prefix in SKIP_TEXT_PREFIXES)


def is_internal_title(title: str) -> bool:
    stripped = title.strip()
    if not stripped:
        return True
    return any(stripped.startswith(prefix) for prefix in SKIP_TEXT_PREFIXES)


def normalize_seen_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()[:4000]


def session_id_from_filename(path: Path) -> str:
    match = re.search(r"(019[a-z0-9-]{20,})", path.stem)
    return match.group(1) if match else ""


def infer_title(messages: list[ChatMessage], session_id: str) -> str:
    for message in messages:
        if message.role == "user":
            first_line = re.sub(r"\s+", " ", message.text).strip()
            if first_line:
                return first_line[:60]
    return f"session-{session_id[:8]}"


def as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def source_group(path: Path, codex_home: Path) -> str:
    try:
        rel = path.relative_to(codex_home)
    except ValueError:
        return "custom"
    parts = rel.parts
    return parts[0] if parts else "custom"


def dedupe_sessions(records: list[SessionRecord]) -> list[SessionRecord]:
    best: dict[str, SessionRecord] = {}
    for record in records:
        current = best.get(record.session_id)
        if current is None or session_score(record) > session_score(current):
            best[record.session_id] = record
    return sorted(best.values(), key=lambda item: sortable_time(item.updated_at), reverse=True)


def session_score(record: SessionRecord) -> tuple[int, int, float]:
    source_weight = {"sessions": 3, "sessions.bak": 2, "archived_sessions.bak": 1}.get(record.source_group, 0)
    return (source_weight, len(record.messages), record.source_path.stat().st_mtime)


def sortable_time(value: str) -> str:
    return value or ""


def workspace_slug(cwd: str) -> str:
    cleaned = cwd.strip() or "unknown-workspace"
    cleaned = cleaned.replace("\\", "_").replace("/", "_").replace(":", "")
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "_", cleaned)
    cleaned = re.sub(r"\s+", "_", cleaned).strip("._- ")
    digest = hashlib.sha1(cwd.encode("utf-8", errors="replace")).hexdigest()[:8]
    return f"{cleaned[:70] or 'unknown-workspace'}_{digest}"


def session_filename(record: SessionRecord) -> str:
    date = compact_date(record.updated_at or record.created_at)
    title = re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+", "_", record.title).strip("._-")
    title = title[:80] or "untitled"
    return f"{date}_{title}_{record.session_id[:8]}.md"


def compact_date(value: str) -> str:
    if not value:
        return "unknown-date"
    return value[:10].replace("-", "")


def write_outputs(records: list[SessionRecord], output_dir: Path, copy_raw: bool) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    projects_dir = output_dir / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)

    by_workspace: dict[str, list[SessionRecord]] = {}
    for record in records:
        by_workspace.setdefault(record.cwd, []).append(record)

    project_index_entries: list[tuple[str, str, int, str]] = []
    for cwd, workspace_records in sorted(by_workspace.items(), key=lambda item: item[0].lower()):
        slug = workspace_slug(cwd)
        project_dir = projects_dir / slug
        project_dir.mkdir(parents=True, exist_ok=True)
        write_project_index(project_dir, cwd, workspace_records)
        for record in workspace_records:
            target = project_dir / session_filename(record)
            target.write_text(render_session_markdown(record), encoding="utf-8")
            if copy_raw:
                raw_dir = project_dir / "raw"
                raw_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(record.source_path, raw_dir / f"{record.session_id}.jsonl")
        newest = workspace_records[0].updated_at
        project_index_entries.append((slug, cwd, len(workspace_records), newest))

    write_root_index(output_dir, records, project_index_entries)
    write_machine_index(output_dir, records)


def write_root_index(
    output_dir: Path,
    records: list[SessionRecord],
    project_entries: list[tuple[str, str, int, str]],
) -> None:
    lines = [
        "# Codex Session Export",
        "",
        f"- Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"- Sessions: {len(records)}",
        f"- Projects: {len(project_entries)}",
        "",
        "## Projects",
        "",
        "| Project | Sessions | Newest |",
        "|---|---:|---|",
    ]
    for slug, cwd, count, newest in sorted(project_entries, key=lambda item: item[3], reverse=True):
        lines.append(f"| [{escape_md(cwd)}](projects/{slug}/index.md) | {count} | {escape_md(newest)} |")
    lines.append("")
    (output_dir / "index.md").write_text("\n".join(lines), encoding="utf-8")


def write_project_index(project_dir: Path, cwd: str, records: list[SessionRecord]) -> None:
    lines = [
        f"# {cwd}",
        "",
        f"- Sessions: {len(records)}",
        "",
        "| Updated | Title | Source | Messages |",
        "|---|---|---|---:|",
    ]
    for record in records:
        filename = session_filename(record)
        lines.append(
            f"| {escape_md(record.updated_at)} | [{escape_md(record.title)}]({filename}) | "
            f"{escape_md(record.source_group)} | {len(record.messages)} |"
        )
    lines.append("")
    (project_dir / "index.md").write_text("\n".join(lines), encoding="utf-8")


def write_machine_index(output_dir: Path, records: list[SessionRecord]) -> None:
    data = [
        {
            "id": record.session_id,
            "title": record.title,
            "cwd": record.cwd,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "source_path": str(record.source_path),
            "source_group": record.source_group,
            "message_count": len(record.messages),
            "line_count": record.line_count,
            "parse_errors": record.parse_errors,
        }
        for record in records
    ]
    (output_dir / "index.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def render_session_markdown(record: SessionRecord) -> str:
    lines = [
        f"# {record.title}",
        "",
        f"- Session ID: `{record.session_id}`",
        f"- Project: `{record.cwd}`",
        f"- Created: {record.created_at}",
        f"- Updated: {record.updated_at}",
        f"- Source: `{record.source_path}`",
        f"- Model provider: `{record.model_provider or 'unknown'}`",
        f"- CLI version: `{record.cli_version or 'unknown'}`",
        "",
        "## Conversation",
        "",
    ]
    for message in record.messages:
        role = "User" if message.role == "user" else "Assistant"
        phase = f" ({message.phase})" if message.phase else ""
        lines.extend(
            [
                f"### {role}{phase} - {message.timestamp or 'unknown time'}",
                "",
                message.text.rstrip(),
                "",
            ]
        )
    return "\n".join(lines)


def escape_md(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export Codex Desktop JSONL sessions to project-grouped Markdown.",
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=default_codex_home(),
        help="Codex home directory. Defaults to CODEX_HOME or ~/.codex.",
    )
    parser.add_argument(
        "--source",
        type=Path,
        action="append",
        default=[],
        help="Extra source file or directory. Can be repeated.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("codex-session-export"),
        help="Output directory for Markdown archive.",
    )
    parser.add_argument(
        "--no-backups",
        action="store_true",
        help="Only scan sessions/, not sessions.bak or archived_sessions.bak.",
    )
    parser.add_argument(
        "--copy-raw",
        action="store_true",
        help="Copy original JSONL files next to exported Markdown.",
    )
    parser.add_argument(
        "--project-filter",
        default="",
        help="Only export sessions whose cwd contains this text.",
    )
    parser.add_argument(
        "--title-filter",
        default="",
        help="Only export sessions whose title contains this text.",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="Print detected projects and counts without writing files.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    codex_home = args.codex_home.expanduser()
    titles = load_index_titles(codex_home)
    files = iter_session_files(codex_home, args.source, include_backups=not args.no_backups)
    records = [record for path in files if (record := parse_session(path, titles, codex_home))]
    records = dedupe_sessions(records)

    if args.project_filter:
        needle = args.project_filter.casefold()
        records = [record for record in records if needle in record.cwd.casefold()]
    if args.title_filter:
        needle = args.title_filter.casefold()
        records = [record for record in records if needle in record.title.casefold()]

    if args.list_only:
        write_project_summary(records)
        return 0

    write_outputs(records, args.output, copy_raw=args.copy_raw)
    sys.stdout.write(f"Exported {len(records)} sessions to {args.output.resolve()}\n")
    return 0


def write_project_summary(records: list[SessionRecord]) -> None:
    by_workspace: dict[str, int] = {}
    for record in records:
        by_workspace[record.cwd] = by_workspace.get(record.cwd, 0) + 1
    for cwd, count in sorted(by_workspace.items(), key=lambda item: item[0].lower()):
        sys.stdout.write(f"{count:4d}  {cwd}\n")


if __name__ == "__main__":
    raise SystemExit(main())
