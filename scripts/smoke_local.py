"""Exercise real native sessions and ai-memory in the isolated test project."""

import json
from pathlib import Path
import uuid

import anyio

from takopi.model import CompletedEvent, StartedEvent
from takopi.runners.claude import build_runner as build_claude
from takopi.runners.codex import build_runner as build_codex
from takopi.utils.paths import reset_run_base_dir, set_run_base_dir


async def main() -> None:
    directory = Path.home() / ".local/share/takopi-test-project"
    state = directory / "validation"
    state.mkdir(mode=0o700, exist_ok=True)
    marker = set_run_base_dir(directory)
    results = []
    try:
        for engine, builder, options in (
            ("codex", build_codex, {"mode": "exec", "extra_args": []}),
            ("claude", build_claude, {"permission_mode": "auto"}),
        ):
            config = {"ai_memory": True, **options}
            secret = "TAKOPI-" + uuid.uuid4().hex[:12]
            resume = None
            for turn, prompt in enumerate(
                (
                    f"Teste de integração. Memorize {secret} nesta conversa e responda "
                    "apenas com esse código. Não use ferramentas nem altere arquivos.",
                    "Qual foi o código que pedi para memorizar? Responda apenas com "
                    "o código. Não use ferramentas nem altere arquivos.",
                ),
                1,
            ):
                # Rebuild on each turn to also verify persistence across restarts.
                runner = builder(config, state / "takopi.toml")
                with anyio.fail_after(240):
                    events = [event async for event in runner.run(prompt, resume)]
                started = next(e for e in events if isinstance(e, StartedEvent))
                completed = next(e for e in events if isinstance(e, CompletedEvent))
                assert completed.ok, completed.error
                assert secret in completed.answer, completed.answer
                assert resume is None or resume == started.resume
                resume = started.resume
                result = {
                    "engine": engine,
                    "turn": turn,
                    "session": resume.value,
                    "answer": completed.answer,
                    "ok": completed.ok,
                }
                results.append(result)
                (state / "results.json").write_text(json.dumps(results, indent=2))
                print(json.dumps(result), flush=True)
    finally:
        reset_run_base_dir(marker)


if __name__ == "__main__":
    anyio.run(main)
