"""Launch native CLIs through ai-memory without replacing their configuration."""

from __future__ import annotations

import os
import shutil
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .config import ConfigError
from .model import ResumeToken, StartedEvent


@dataclass(frozen=True)
class AiMemoryConfig:
    executable: str
    state_path: Path

    @classmethod
    def from_config(cls, config: dict, config_path: Path) -> AiMemoryConfig | None:
        enabled = config.get("ai_memory", False)
        if not isinstance(enabled, bool):
            raise ConfigError("ai_memory must be true or false")
        if not enabled:
            return None
        executable = shutil.which("ai-memory")
        if executable is None:
            raise ConfigError("ai-memory is required on PATH for managed sessions")
        return cls(executable, config_path.with_name("ai_memory_sessions.sqlite3"))

    def connect(self) -> sqlite3.Connection:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.state_path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        connection = sqlite3.connect(self.state_path, timeout=5)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS sessions ("
            "engine TEXT NOT NULL, native_id TEXT NOT NULL, cwd TEXT NOT NULL, "
            "config_home TEXT NOT NULL, workstream TEXT NOT NULL, "
            "PRIMARY KEY(engine, native_id, cwd, config_home))"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS adoptions (engine TEXT, native_id TEXT, cwd TEXT, "
            "config_home TEXT, PRIMARY KEY(engine,native_id,cwd,config_home))"
        )
        return connection

    def allow_import(self, engine: str, native_id: str, cwd: Path) -> None:
        home_key = "CODEX_HOME" if engine == "codex" else "CLAUDE_CONFIG_DIR"
        config_home = str(
            Path(os.environ.get(home_key) or Path.home() / f".{engine}").resolve()
        )
        with closing(self.connect()) as connection, connection:
            connection.execute(
                "INSERT OR IGNORE INTO adoptions VALUES (?,?,?,?)",
                (engine, native_id, str(cwd.resolve()), config_home),
            )

    def prepare(
        self,
        engine: str,
        native_command: list[str],
        resume: ResumeToken | None,
        cwd: Path | None,
        env: dict[str, str] | None,
    ) -> ManagedLaunch:
        if engine not in {"codex", "claude"}:
            raise ConfigError(f"ai-memory adapter is not implemented for {engine}")
        directory = str((cwd or Path.cwd()).resolve())
        environment = os.environ if env is None else env
        home_key = "CODEX_HOME" if engine == "codex" else "CLAUDE_CONFIG_DIR"
        default_home = Path(environment.get("HOME", str(Path.home()))) / f".{engine}"
        config_home = str(Path(environment.get(home_key) or default_home).resolve())
        workstream = None
        if resume is not None:
            with closing(self.connect()) as connection:
                row = connection.execute(
                    "SELECT workstream FROM sessions WHERE engine=? AND native_id=? "
                    "AND cwd=? AND config_home=?",
                    (engine, resume.value, directory, config_home),
                ).fetchone()
            if row is None:
                with closing(self.connect()) as connection:
                    imported = connection.execute(
                        "SELECT 1 FROM adoptions WHERE engine=? AND native_id=? AND cwd=? AND config_home=?",
                        (engine, resume.value, directory, config_home),
                    ).fetchone()
                if imported is None:
                    raise ConfigError(
                        "This session has no ai-memory binding for this project/configuration. "
                        "Return to its original project or create a new session with /new."
                    )
            else:
                workstream = row[0]
            with closing(self.connect()) as connection:
                imported = connection.execute(
                    "SELECT 1 FROM adoptions WHERE engine=? AND native_id=? AND cwd=? AND config_home=?",
                    (engine, resume.value, directory, config_home),
                ).fetchone()
            if imported:
                from .native_sessions import native_directory_busy

                if native_directory_busy(Path(directory)):
                    raise ConfigError(
                        "Há um CLI ativo neste diretório. Encerre-o no terminal antes de retomar pelo Telegram."
                    )
        selector = "--workstream" if workstream else "--new"
        workstream = workstream or f"tg-{uuid.uuid4().hex}"
        command = [
            self.executable,
            "run",
            "--require-server",
            "--no-autowire",
            "--executable",
            native_command[0],
            selector,
            workstream,
            engine,
            *native_command[1:],
        ]
        return ManagedLaunch(self, engine, directory, config_home, workstream, command)


@dataclass(frozen=True)
class ManagedLaunch:
    config: AiMemoryConfig
    engine: str
    cwd: str
    config_home: str
    workstream: str
    command: list[str]

    def bind(self, event: StartedEvent) -> None:
        if event.engine != self.engine:
            raise RuntimeError("ai-memory session engine mismatch")
        if (
            self.engine == "claude"
            and (event.meta or {}).get("permissionMode") != "auto"
        ):
            raise RuntimeError("Claude did not start in auto mode; execution stopped")
        with closing(self.config.connect()) as connection, connection:
            existing = connection.execute(
                "SELECT workstream FROM sessions WHERE engine=? AND native_id=? "
                "AND cwd=? AND config_home=?",
                (self.engine, event.resume.value, self.cwd, self.config_home),
            ).fetchone()
            if existing is not None and existing[0] != self.workstream:
                raise RuntimeError(
                    "Refusing to replace another workstream's session binding"
                )
            connection.execute(
                "INSERT OR IGNORE INTO sessions VALUES (?, ?, ?, ?, ?)",
                (
                    self.engine,
                    event.resume.value,
                    self.cwd,
                    self.config_home,
                    self.workstream,
                ),
            )
