"""Copy Claude histories into Linux without replacing local files or settings.

Use a snapshot as --source for a live WSL installation. Windows drive paths are
translated only in structural cwd/projectPath metadata, never inside messages.
The manifest records missing projects, duplicate files, and conflicts. Sources
are never modified. Run without --apply first to review the proposed changes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil


def linux_path(value: str) -> str:
    path = PureWindowsPath(value)
    if re.fullmatch(r"[A-Za-z]:", path.drive) and path.is_absolute():
        return str(Path("/mnt") / path.drive[0].lower() / Path(*path.parts[1:]))
    return value


def converted(data: bytes, cwd_map: dict[str, str] | None = None) -> bytes:
    lines = []
    for line in data.splitlines(keepends=True):
        try:
            record = json.loads(line)
        except ValueError:
            lines.append(line)
            continue
        changed = False
        if isinstance(record, dict):
            for key in ("cwd", "projectPath"):
                value = record.get(key)
                if isinstance(value, str):
                    mapped = linux_path(value)
                    mapped = (cwd_map or {}).get(mapped, mapped)
                    if value != mapped:
                        record[key] = mapped
                        changed = True
        lines.append(
            (json.dumps(record, ensure_ascii=False) + "\n").encode()
            if changed
            else line
        )
    return b"".join(lines)


def session_cwd(file: Path) -> str | None:
    with file.open("rb") as stream:
        for line in stream:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and isinstance(record.get("cwd"), str):
                return linux_path(record["cwd"])
    return None


def migrate(
    source: Path,
    target: Path,
    *,
    apply: bool = False,
    cwd_map: dict[str, str] | None = None,
) -> list[dict]:
    records = []
    for folder in sorted((source / "projects").iterdir()):
        if not folder.is_dir() or folder.is_symlink():
            continue
        session_dirs = {p.stem: session_cwd(p) for p in sorted(folder.glob("*.jsonl"))}
        folder_cwd = next((cwd for cwd in session_dirs.values() if cwd), None)
        if folder_cwd is None:
            records.append({"source": str(folder), "status": "unknown_project"})
            continue
        for file in sorted(folder.rglob("*")):
            if not file.is_file() or file.is_symlink():
                continue
            # Do not introduce an index containing paths into the old store.
            if file.name == "sessions-index.json":
                continue
            first = file.relative_to(folder).parts[0]
            cwd = session_dirs.get(Path(first).stem) or folder_cwd
            cwd = (cwd_map or {}).get(cwd, cwd)
            encoded = re.sub(r"[^A-Za-z0-9]", "-", cwd)
            if len(encoded.encode()) > 255:
                records.append({"source": str(file), "status": "project_name_too_long"})
                continue
            destination = target / "projects" / encoded / file.relative_to(folder)
            raw = file.read_bytes()
            data = converted(raw, cwd_map) if file.suffix == ".jsonl" else raw
            status = "copy"
            if destination.exists():
                status = "identical" if destination.read_bytes() == data else "conflict"
            item = {
                "source": str(file),
                "target": str(destination),
                "status": status,
                "cwd": cwd,
                "project_exists": Path(cwd).is_dir(),
                "source_sha256": hashlib.sha256(raw).hexdigest(),
                "target_sha256": hashlib.sha256(data).hexdigest(),
            }
            if apply and status == "copy":
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with destination.open("xb") as stream:
                    stream.write(data)
                shutil.copystat(file, destination)
                destination.chmod(0o600)
            records.append(item)
    for name in ("file-history", "paste-cache"):
        for file in sorted((source / name).rglob("*")):
            if not file.is_file() or file.is_symlink():
                continue
            destination = target / name / file.relative_to(source / name)
            status = "copy"
            if destination.exists():
                status = (
                    "identical"
                    if file.read_bytes() == destination.read_bytes()
                    else "conflict"
                )
            if apply and status == "copy":
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with destination.open("xb") as stream:
                    stream.write(file.read_bytes())
                shutil.copystat(file, destination)
                destination.chmod(0o600)
            records.append(
                {"source": str(file), "target": str(destination), "status": status}
            )
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", type=Path, required=True, help="Source .claude directory"
    )
    parser.add_argument("--target", type=Path, default=Path.home() / ".claude")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--cwd-map",
        type=Path,
        help="JSON mapping of verified Linux source paths to copied projects",
    )
    args = parser.parse_args()
    if args.source.resolve() == args.target.resolve():
        parser.error("Source and target must differ")
    os.umask(0o077)
    cwd_map = json.loads(args.cwd_map.read_text()) if args.cwd_map else None
    records = migrate(args.source, args.target, apply=args.apply, cwd_map=cwd_map)
    args.manifest.write_text(
        json.dumps({"applied": args.apply, "files": records}, indent=2)
    )
    from collections import Counter

    print(json.dumps(dict(Counter(row["status"] for row in records))))


if __name__ == "__main__":
    main()
