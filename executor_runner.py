"""
Repository executor: clones a paper's GitHub repo, sets up its Python
environment, and runs a lightweight smoke test — all locally (no Docker).

Used by multi_agent_pipeline.py's Executor agent. Can also be invoked
standalone.

State machine:
  CLONE   -> git clone the repo into the workspace
  INSPECT -> list files, read README, summarize structure
  INSTALL -> create venv, install deps (requirements.txt / pyproject.toml)
  RUN     -> multi-turn interactive run using README guidance

Safety note: this runs LLM-generated shell commands directly on the host
machine. The workspace is pinned to whatever path the caller passes — by
default the multi-agent pipeline pins this to <output_dir>/executor_workspace/
to keep activity contained. There is no sandboxing.

Importable: when imported as a module, only the helper functions and
`run_repository_smoke_test` are exposed. Side-effect-free at import time.
"""

import os
import sys
import shutil
import argparse
from pathlib import Path
from datetime import datetime


# ----------------------------------------------------------------------------
# State prompts — adapted from the user's executor.py
# ----------------------------------------------------------------------------

STATE_PROMPTS = {
    "CLONE": """Task: Clone this repository: {repo_url}
Rules: Clone into the current directory. Output ONLY the git clone command in a ```sh block.""",

    "INSPECT": """Task: Inspect the repository structure.
Rules: You are in the repo root. List files and read the README.md to understand the entry points.""",

    "INSTALL": """Task: Setup Python environment.
Rules: Create a venv and install dependencies. Skip standard libraries (os, sys, etc.) in any scans.
Use 'venv/bin/python -m pip install'.""",

    "RUN": """Task: Execute the research code using the simulated data already staged for you.

DATA AVAILABILITY:
{data_context}

EXECUTION RULES:
1. Read README.md ONLY to identify the entry-point script and its expected data-path argument.
2. Run that script using 'venv/bin/python', and POINT IT AT THE STAGED DATA PATH ABOVE if data is staged.
   - Look for CLI flags like --data, --data-dir, --dataset, --input, --root, or a config file
     where the data path is set. Adapt your command to use the staged path.
   - Do NOT download datasets from the internet.
   - Do NOT use the repo's default data path if a staged path is available.
3. Use the smallest subset (smallest --epochs, --batch-size, --num-samples, etc.) the README mentions
   so the run completes quickly.
4. If you cannot figure out how to point the code at the staged data, print a clear message
   explaining what flag or config the repo expects, and stop. Do not fall back to defaults.
5. If the repo has no data at all (no staged data, no defaults), say so and stop.""",
}


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def _make_user_proxy(executor):
    """Build a UserProxyAgent configured to run shell commands via the given executor.

    Imported lazily inside the function call so the module is import-safe.
    """
    from autogen import UserProxyAgent
    return UserProxyAgent(
        name="executor",
        human_input_mode="NEVER",
        code_execution_config={"executor": executor},
        system_message="""You are a shell expert running on a local machine.
- Use bash/zsh syntax.
- NEVER use 'source' or 'activate'.
- Call python binaries directly via 'venv/bin/python'.
- Use relative paths only. NEVER hardcode /Users/ paths.
- If a command fails, analyze the error and try a fix.""",
    )


def _detect_repo_dir(workspace: Path) -> str | None:
    """After CLONE, find which subdirectory was just created."""
    if not workspace.exists():
        return None
    candidates = [
        d for d in workspace.iterdir()
        if d.is_dir() and not d.name.startswith(".") and d.name not in ("venv", "__pycache__")
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0].name


# ----------------------------------------------------------------------------
# Main entry point
# ----------------------------------------------------------------------------

def run_repository_smoke_test(
    repo_url: str,
    workspace_dir: Path,
    simulated_data_dir: Path | None = None,
    model: str = "gpt-5-nano",
    timeout_per_state: int = 300,
    run_turns: int = 5,
) -> dict:
    """Clone a repo, install its deps, and smoke-test it — locally, no Docker.

    If `simulated_data_dir` is provided, its contents are copied into
    `<workspace_dir>/data/` before the state machine runs. The RUN prompt
    is told about this concrete path so the LLM can point the code at it.

    Args:
        repo_url: GitHub URL of the repo to test.
        workspace_dir: directory the executor works inside. Cloned repo,
            venv, and staged data all live here. Created if missing.
        simulated_data_dir: optional path to simulated data. If present,
            its contents are copied into <workspace_dir>/data/.
        model: OpenAI model name (used by the LLM to generate shell commands).
        timeout_per_state: per-state timeout in seconds.
        run_turns: max conversation turns for the interactive RUN state.

    Returns:
        A dict with status, per-state results, staged-data info, and the
        repo directory name (if cloned successfully).
    """
    # Lazy autogen imports — module import stays side-effect-free
    from autogen import AssistantAgent
    from autogen.coding import LocalCommandLineCodeExecutor
    from autogen.coding.base import CodeBlock
    from autogen.code_utils import extract_code

    workspace_dir = workspace_dir.resolve()
    workspace_dir.mkdir(parents=True, exist_ok=True)

    # Stage simulated data into the workspace at a KNOWN absolute path so
    # the RUN state can point the code at it without ambiguity. We use
    # <workspace>/data/ — it lives alongside the cloned repo (which will
    # be at <workspace>/<repo_name>/), so a script can reach it via
    # ../data/ from the repo root or /absolute/path/data/.
    staged_data_path: Path | None = None
    staged_file_count = 0
    if simulated_data_dir and Path(simulated_data_dir).is_dir():
        staged_data_path = workspace_dir / "data"
        # Re-stage every run so updated sim data overrides old. shutil.copytree
        # fails if target exists, so clear first.
        if staged_data_path.exists():
            shutil.rmtree(staged_data_path)
        shutil.copytree(simulated_data_dir, staged_data_path)
        staged_file_count = sum(1 for _ in staged_data_path.rglob("*") if _.is_file())
        print(f"[executor] staged {staged_file_count} simulated data file(s) "
              f"from {simulated_data_dir} -> {staged_data_path}")

    # Build the data context block injected into the RUN prompt. Make it
    # concrete (absolute paths, file counts, sample names) so the LLM can't
    # plausibly claim it didn't know where the data was.
    if staged_data_path:
        sample = sorted(p.name for p in staged_data_path.rglob("*") if p.is_file())[:5]
        data_context = (
            f"Simulated data IS available and has been staged for you.\n"
            f"  - Absolute path:  {staged_data_path}\n"
            f"  - Relative path from the cloned repo root:  ../data\n"
            f"  - File count:     {staged_file_count}\n"
            f"  - Sample files:   {', '.join(sample) if sample else '(none)'}\n"
            f"You MUST point the code at this path. Do not use the repo's default dataset."
        )
    else:
        data_context = (
            "NO simulated data was provided.\n"
            "If the repo's code requires data, identify which dataset(s) it needs "
            "and stop — do not attempt to download anything."
        )

    print(f"[executor] workspace: {workspace_dir}")
    print(f"[executor] repo: {repo_url}")
    print(f"[executor] data_context preview:\n{data_context}")

    # 1. Build the local executor + assistant
    code_executor = LocalCommandLineCodeExecutor(
        work_dir=str(workspace_dir),
        timeout=timeout_per_state,
    )

    assistant = AssistantAgent(
        name="assistant",
        llm_config={
            "api_type": "openai",
            "model": model,
            "api_key": os.environ["OPENAI_API_KEY"],
        },
        # Block code execution on the assistant side — only the explicit
        # local executor below should ever run commands
        code_execution_config=False,
    )
    user_proxy = _make_user_proxy(code_executor)

    # 2. Run the state machine
    state_results: dict[str, dict] = {}
    repo_dir: str | None = None
    states = ("CLONE", "INSPECT", "INSTALL", "RUN")

    for state in states:
        print(f"\n[executor] === STATE: {state} ===")

        prompt = STATE_PROMPTS[state].format(
            repo_url=repo_url,
            data_context=data_context,
        )

        if state == "RUN":
            # Multi-turn interactive run — let the assistant read the README,
            # decide what to do, run commands, and iterate.
            try:
                chat = user_proxy.initiate_chat(
                    assistant,
                    message=prompt,
                    max_turns=run_turns,
                    clear_history=True,
                )
                # Capture some signal from the chat
                last_msg = ""
                for m in reversed(chat.chat_history if hasattr(chat, "chat_history") else []):
                    if m.get("content"):
                        last_msg = m["content"][:1000]
                        break
                state_results[state] = {
                    "ok": True,
                    "summary": "interactive run completed",
                    "last_message": last_msg,
                }
            except Exception as e:
                state_results[state] = {
                    "ok": False,
                    "summary": f"interactive run failed: {e}",
                }
            continue

        # Single-shot states: ask LLM for code, extract block, execute
        try:
            reply = assistant.generate_reply(
                messages=[{"role": "user", "content": f"Current State: {state}\n{prompt}"}]
            )
        except Exception as e:
            state_results[state] = {
                "ok": False, "exit_code": -1,
                "output": f"LLM call failed: {e}",
            }
            print(f"[executor] LLM call failed: {e}")
            if state == "CLONE":
                print("[executor] CLONE LLM failed; aborting")
                break
            continue

        code_blocks = extract_code(reply or "")
        if not code_blocks or code_blocks == [("unknown", reply or "")]:
            state_results[state] = {
                "ok": False, "exit_code": -1,
                "output": "no code block in LLM reply",
                "reply": (reply or "")[:500],
            }
            print(f"[executor] no code block from LLM for {state}")
            if state == "CLONE":
                print("[executor] CLONE produced no code; aborting state machine")
                break
            continue

        # Execute every extracted block, capture the last result
        last_result = None
        for lang, code in code_blocks:
            clean_code = code.replace("TERMINATE", "")
            print(f"[executor] running [{lang}]:\n{clean_code[:400]}")
            try:
                result = code_executor.execute_code_blocks(
                    [CodeBlock(code=clean_code, language=lang)]
                )
                last_result = {
                    "exit_code": result.exit_code,
                    "output": (result.output or "")[:4000],
                    "ok": result.exit_code == 0,
                }
            except Exception as e:
                last_result = {
                    "exit_code": -1, "ok": False,
                    "output": f"executor error: {e}",
                }
            print(f"[executor] exit={last_result['exit_code']}, "
                  f"output[:200]={last_result['output'][:200]!r}")

        state_results[state] = last_result

        # After CLONE: detect what directory was just created and switch
        # the executor's working directory to it for subsequent states.
        if state == "CLONE" and last_result and last_result["ok"]:
            detected = _detect_repo_dir(workspace_dir)
            if detected:
                repo_dir = detected
                new_workdir = workspace_dir / detected
                print(f"[executor] repo detected: {detected} -> using as workdir")
                code_executor = LocalCommandLineCodeExecutor(
                    work_dir=str(new_workdir),
                    timeout=timeout_per_state,
                )
                user_proxy = _make_user_proxy(code_executor)
            else:
                print("[executor] CLONE succeeded but no repo dir detected")

        # Hard-stop if CLONE failed
        if state == "CLONE" and not (last_result and last_result["ok"]):
            print("[executor] CLONE failed; aborting state machine")
            break

    # 3. Wrap up
    overall_ok = all(
        (r or {}).get("ok") is True for r in state_results.values()
    )
    return {
        "status": "completed" if overall_ok else "partial",
        "repo_url": repo_url,
        "repo_dir": repo_dir,
        "workspace": str(workspace_dir),
        "staged_data_path": str(staged_data_path) if staged_data_path else None,
        "staged_file_count": staged_file_count,
        "states": state_results,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
    }


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Clone, install, and smoke-test a research repo locally (no Docker)."
    )
    parser.add_argument("repo_url", help="GitHub repo URL to test")
    parser.add_argument("--workspace", type=Path, default=Path("executor_workspace"),
                        help="Local directory used as the executor's workspace")
    parser.add_argument("--simulated-data-dir", type=Path,
                        help="Path to simulated data (passed to the RUN prompt)")
    parser.add_argument("--model", default="gpt-5-nano", help="OpenAI model")
    parser.add_argument("--timeout-per-state", type=int, default=300,
                        help="Per-state timeout in seconds")
    parser.add_argument("--run-turns", type=int, default=5,
                        help="Max conversation turns for the interactive RUN state")
    args = parser.parse_args()

    result = run_repository_smoke_test(
        repo_url=args.repo_url,
        workspace_dir=args.workspace,
        simulated_data_dir=args.simulated_data_dir,
        model=args.model,
        timeout_per_state=args.timeout_per_state,
        run_turns=args.run_turns,
    )
    import json
    print("\n" + "=" * 60)
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
