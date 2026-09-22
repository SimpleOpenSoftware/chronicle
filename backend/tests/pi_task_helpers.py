"""Fake only the external Pi process; retain task tools, validation and artifacts."""

from dataclasses import replace
from pathlib import Path

from backend.services.memory.agent.pi_agent import _PiEventResult, _PiRuntimeConfig
from backend.services.timeline import pi_tasks


def review_result(verdict, reason):
    """Construct an explicit per-item reviewer response for the supplied candidate."""

    def respond(handler):
        import json

        candidate = json.loads(handler.materials["task.json"])["candidate"]
        result = {
            "reason": reason,
            "checks": [
                {
                    "target": target,
                    "index": index,
                    "action": "keep",
                    "reason": (
                        "Supported by inspected evidence"
                        if target == "claim"
                        else "The answer changes the proposed memory"
                    ),
                }
                for target, rows in [
                    ("claim", candidate["claims"]),
                    ("question", candidate["questions"]),
                ]
                for index in range(len(rows))
            ],
            "account_issues": [reason] if verdict == "revise" else [],
        }
        handler.dispatch("finish_task", {"result": result})

    return respond


def install_pi(monkeypatch, tmp_path, actions):
    config = _PiRuntimeConfig(
        binary="pi",
        provider="test",
        model="test",
        base_url="http://example.invalid",
        api_key="test",
        thinking="off",
        max_tokens=4096,
        context_window=32768,
        timeout_seconds=30,
        reasoning=False,
        temperature=0,
    )
    monkeypatch.setattr(pi_tasks, "_resolve_pi_config", lambda _: config)
    monkeypatch.setattr(pi_tasks, "settings", lambda: {"max_attempts": 3})
    monkeypatch.setattr(pi_tasks, "runtime_version", lambda _: "0.85.1-test")
    monkeypatch.setenv("INFERENCE_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    calls = []

    async def invoke(root, **kwargs):
        calls.append(kwargs)
        handler = kwargs["tool_handler"]
        action = actions.pop(0)
        if callable(action):
            returned = action(handler)
            if isinstance(returned, _PiEventResult):
                return returned, None
        else:
            handler.dispatch("finish_task", {"result": action})
        return _PiEventResult(stdout="retained-pi-events", returncode=0), None

    monkeypatch.setattr(pi_tasks, "_invoke_pi", invoke)
    return calls
