#!/usr/bin/env python3
"""
Multi-agent reproducibility assessment for scientific papers.

Six agents collaborate in an autogen GroupChat with autonomous speaker
selection to assess whether a paper's code/data are reproducible — and
optionally generate a simulated dataset if no real data exists:

  - Extractor : finds and classifies URLs in the paper
  - Planner   : triages which repos to scan deeply under an API budget
  - Analyst   : inspects GitHub repos for runtime landmines
  - Critic    : challenges Analyst findings, demands evidence
  - Judge     : synthesizes a final reproducibility score + narrative
  - Simulator : generates a simulated dataset (CONDITIONAL — only fires if
                no data repo was found OR user passed --simulate)

The agents share state through a Blackboard object. Tools mutate the
blackboard and return short confirmations; agents read summaries of the
blackboard injected into their system messages each turn.

Outputs:
  - <paper>.report.md         (final report)
  - <paper>.transcript.md     (full agent conversation, for demos)
  - <paper>.report.json       (structured findings)
  - <paper>.multiagent.log    (run log)
  - simulated_data_output/    (if Simulator ran)

Usage:
    # Standard run — Simulator triggers only if no data repo found
    python multi_agent_pipeline.py paper.pdf --output-dir results/

    # Force simulation even if data repos exist (human request)
    python multi_agent_pipeline.py paper.pdf --output-dir results/ --simulate

    # Never simulate
    python multi_agent_pipeline.py paper.pdf --output-dir results/ --no-simulate

    # Demo-safe (always produces output if GroupChat loops)
    python multi_agent_pipeline.py paper.pdf --output-dir results/ --fallback-on-loop
"""

import os
import re
import sys
import json
import argparse
import logging
from pathlib import Path
from datetime import datetime
from typing_extensions import Annotated

from autogen import (
    ConversableAgent,
    UserProxyAgent,
    GroupChat,
    GroupChatManager,
    LLMConfig,
)

# Import helpers from the (now-importable) sibling scripts
sys.path.insert(0, str(Path(__file__).parent))
from readpaper_extracturls import (
    read_paper_local,
    find_urls as regex_find_urls,
    clean_url,
    classify_urls,
    enrich_code,
    enrich_data,
    repair_github_url,
)
from analyze_repos import (
    parse_github_url,
    analyze_github_repo,
)
from data_simulator import trigger_data_simulation
from executor_runner import run_repository_smoke_test

# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

parser = argparse.ArgumentParser(
    description="Multi-agent reproducibility assessment of a paper."
)
parser.add_argument("paper_path", type=Path, help="Path to the PDF")
parser.add_argument("--model", default="gpt-5-nano", help="OpenAI model")
parser.add_argument("--output-dir", type=Path, default=Path("."),
                    help="Output directory")
parser.add_argument("--max-rounds", type=int, default=30,
                    help="Hard cap on GroupChat rounds (safety)")
parser.add_argument("--max-chars", type=int, default=200_000,
                    help="Max PDF chars to read")
parser.add_argument("--max-py-files", type=int, default=15,
                    help="Max Python files to scan per repo")

# Simulator gating
sim_group = parser.add_mutually_exclusive_group()
sim_group.add_argument("--simulate", action="store_true",
                       help="Force the Simulator agent to run even if a data repo exists")
sim_group.add_argument("--no-simulate", action="store_true",
                       help="Never run the Simulator agent (skip even if no data repo)")

# Executor gating (clone + locally execute a repo)
exec_group = parser.add_mutually_exclusive_group()
exec_group.add_argument("--execute-repo", action="store_true",
                        help="Force the Executor agent to clone & smoke-test the top-priority repo locally")
exec_group.add_argument("--no-execute-repo", action="store_true",
                        help="Never run the Executor agent")
parser.add_argument("--executor-workspace", type=Path, default=None,
                    help="Workspace dir for the Executor (default: <output-dir>/executor_workspace)")

parser.add_argument("--fallback-on-loop", action="store_true",
                    help="Run deterministic completion if GroupChat doesn't terminate cleanly")
args = parser.parse_args()

if not args.paper_path.is_file():
    parser.error(f"Paper not found: {args.paper_path}")

args.output_dir.mkdir(parents=True, exist_ok=True)
stem = args.paper_path.stem
md_path = args.output_dir / f"{stem}.report.md"
json_path = args.output_dir / f"{stem}.report.json"
transcript_path = args.output_dir / f"{stem}.transcript.md"
log_path = args.output_dir / f"{stem}.multiagent.log"

# Default the Executor workspace to a subdir of output-dir if the user didn't override
if args.executor_workspace is None:
    args.executor_workspace = args.output_dir / "executor_workspace"

# ----------------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------------

logger = logging.getLogger("multiagent")
logger.setLevel(logging.INFO)
logger.handlers.clear()

console = logging.StreamHandler(sys.stdout)
console.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(console)

file_handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
file_handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s",
                                            datefmt="%H:%M:%S"))
logger.addHandler(file_handler)
log = logger.info

log(f"Run started: {datetime.now().isoformat(timespec='seconds')}")
log(f"Paper: {args.paper_path}")
log(f"Model: {args.model} · max_rounds={args.max_rounds}")
if args.simulate:
    log("Simulator: FORCED ON (human request)")
elif args.no_simulate:
    log("Simulator: DISABLED")
else:
    log("Simulator: conditional (will fire if no data repo found)")

if args.execute_repo:
    log(f"Executor: FORCED ON (--execute-repo) · workspace: {args.executor_workspace}")
elif args.no_execute_repo:
    log("Executor: DISABLED")
else:
    log("Executor: disabled by default (pass --execute-repo to enable)")

# ----------------------------------------------------------------------------
# Shared Blackboard
# ----------------------------------------------------------------------------

class Blackboard:
    """Mutable shared state. Tools mutate this; agents see summaries."""

    def __init__(self, paper_path: Path):
        self.paper_path = paper_path
        self.paper_text: str = ""
        self.annotation_urls: set[str] = set()
        self.buckets: dict[str, list[str]] = {
            "code": [], "data": [], "ambiguous": [], "skip": []
        }
        self.code_enriched: list[dict] = []
        self.data_enriched: list[dict] = []
        self.repo_analyses: list[dict] = []
        self.repo_priorities: dict[str, str] = {}
        self.critic_challenges: list[dict] = []
        self.verdict: dict | None = None
        self.simulation_result: dict | None = None
        self.executor_result: dict | None = None
        self.audit_trail: list[str] = []

    def log_event(self, agent: str, event: str):
        ts = datetime.now().strftime("%H:%M:%S")
        msg = f"[{ts}] {agent}: {event}"
        self.audit_trail.append(msg)
        log(f"  {msg}")

    def has_data_repo(self) -> bool:
        """True if a real data repository was found by the Extractor."""
        return len(self.data_enriched) > 0

    def should_simulate(self) -> bool:
        """Decide whether the Simulator should run.

        Rules:
          - --no-simulate: never
          - --simulate: always (human override)
          - default: only if no data repo was found
        """
        if args.no_simulate:
            return False
        if args.simulate:
            return True
        return not self.has_data_repo()

    def should_execute_repo(self) -> bool:
        """Decide whether the Executor should clone+run the top-priority repo.

        Rules:
          - --no-execute-repo: never
          - --execute-repo: always (human override) — but only if a code repo exists
          - default: never (Executor is opt-in due to cost & safety)
        """
        if args.no_execute_repo:
            return False
        if not self.buckets["code"]:
            return False  # nothing to run
        if args.execute_repo:
            return True
        return False

    def top_priority_repo(self) -> str | None:
        """Pick the single highest-priority repo for the Executor.

        Strategy: among code URLs, prefer those with priority=high (Planner-set),
        then those with a clean/low severity from the Analyst (most likely to run),
        then most stars.
        """
        if not self.buckets["code"]:
            return None
        sev_rank = {"clean": 0, "low": 1, "unknown": 2, "medium": 3, "high": 4}
        prio_rank = {"high": 0, "medium": 1, "skip": 9, None: 2}
        sev_by_url = {a["url"]: a.get("severity", "unknown")
                      for a in self.repo_analyses}
        stars_by_url = {item["url"]: (item.get("stars") or 0)
                        for item in self.code_enriched}

        def key(url):
            return (
                prio_rank.get(self.repo_priorities.get(url), 2),
                sev_rank.get(sev_by_url.get(url, "unknown"), 2),
                -stars_by_url.get(url, 0),
            )
        candidates = [u for u in self.buckets["code"]
                      if self.repo_priorities.get(u) != "skip"]
        if not candidates:
            return None
        return sorted(candidates, key=key)[0]


bb = Blackboard(args.paper_path)

# ----------------------------------------------------------------------------
# Tool functions — each mutates the blackboard, returns a short string
# ----------------------------------------------------------------------------

def tool_read_paper() -> str:
    """Read the paper PDF and extract text + URL annotations."""
    text, ann_urls = read_paper_local(str(args.paper_path), args.max_chars)
    bb.paper_text = text
    bb.annotation_urls = ann_urls
    bb.log_event("Extractor", f"read paper ({len(text):,} chars, "
                              f"{len(ann_urls)} annotations)")
    return f"Read {len(text):,} chars and {len(ann_urls)} link annotations."


def tool_extract_and_classify() -> str:
    """Find URLs in the loaded paper text and classify them by host + context."""
    if not bb.paper_text:
        return "ERROR: paper not loaded yet. Call read_paper first."
    text_urls = regex_find_urls(bb.paper_text)
    ann_urls = {clean_url(u) for u in bb.annotation_urls if clean_url(u)}
    all_urls = {repair_github_url(u) for u in (text_urls | ann_urls)}
    bb.buckets = classify_urls(bb.paper_text, all_urls)

    # Drop bare github.com/<owner> URLs from the code bucket — they're org
    # or user pages, not repos. enrich_code() already filters these out of
    # bb.code_enriched, but downstream agents read bb.buckets["code"], so
    # we filter the bucket itself here.
    org_page_re = re.compile(r"^https?://github\.com/[^/?#]+/?$")
    bare_org_urls = [u for u in bb.buckets["code"] if org_page_re.match(u)]
    if bare_org_urls:
        bb.buckets["code"] = [u for u in bb.buckets["code"]
                              if not org_page_re.match(u)]
        bb.buckets["skip"].extend(bare_org_urls)
        bb.log_event("Extractor",
                     f"dropped {len(bare_org_urls)} bare org/user page(s) from code bucket: "
                     f"{bare_org_urls}")

    bb.code_enriched = enrich_code(bb.buckets["code"])
    bb.data_enriched = enrich_data(bb.buckets["data"])
    bb.log_event("Extractor",
                 f"classified {len(all_urls)} URLs into "
                 f"code={len(bb.buckets['code'])}, "
                 f"data={len(bb.buckets['data'])}, "
                 f"ambiguous={len(bb.buckets['ambiguous'])}")
    return (f"Classified {len(all_urls)} URLs. "
            f"Code: {len(bb.buckets['code'])}, "
            f"Data: {len(bb.buckets['data'])}, "
            f"Ambiguous: {len(bb.buckets['ambiguous'])}. "
            f"Data repo present: {bb.has_data_repo()}")


def tool_reclassify_url(
    url: Annotated[str, "URL to reclassify"],
    new_category: Annotated[str, "One of: code, data, ambiguous, skip"],
    reason: Annotated[str, "Why the reclassification is justified"],
) -> str:
    """Move a URL between classification buckets."""
    if new_category not in bb.buckets:
        return f"ERROR: unknown category '{new_category}'"
    for bucket in bb.buckets.values():
        if url in bucket:
            bucket.remove(url)
    bb.buckets[new_category].append(url)
    bb.log_event("Extractor", f"moved {url} -> {new_category} ({reason})")
    return f"Moved {url} to {new_category}."


def tool_set_priority(
    url: Annotated[str, "Repo URL to prioritize"],
    priority: Annotated[str, "One of: high, medium, skip"],
    reason: Annotated[str, "Why this priority is appropriate"],
) -> str:
    """Planner sets analysis priority for a repo."""
    if priority not in ("high", "medium", "skip"):
        return f"ERROR: invalid priority '{priority}'"
    bb.repo_priorities[url] = priority
    bb.log_event("Planner", f"prioritized {url} -> {priority} ({reason})")
    return f"Set {url} to {priority}."


def tool_analyze_repo(
    url: Annotated[str, "GitHub repo URL to analyze"],
) -> str:
    """Run static analysis on a single GitHub repo via the GitHub API."""
    if any(a["url"] == url for a in bb.repo_analyses):
        return f"SKIPPED: {url} already analyzed."

    # Record failures in the blackboard so the state machine sees the URL
    # as "handled" and moves on, rather than routing back to the Analyst.
    if not parse_github_url(url):
        bb.repo_analyses.append({
            "url": url,
            "error": "not a GitHub repo URL (likely an org/user page)",
            "severity": "unknown",
        })
        bb.log_event("Analyst", f"FAILED on {url}: not a valid repo URL "
                                f"(recorded so workflow can continue)")
        return (f"Could not analyze {url}: not a GitHub repo URL "
                "(likely an org/user page). Recorded as unanalyzed; moving on.")

    if bb.repo_priorities.get(url) == "skip":
        bb.repo_analyses.append({
            "url": url,
            "error": "deprioritized by Planner",
            "severity": "unknown",
        })
        return f"SKIPPED: {url} was deprioritized by the Planner."

    analysis = analyze_github_repo(url, args.max_py_files)
    bb.repo_analyses.append(analysis)

    if analysis.get("error"):
        bb.log_event("Analyst", f"FAILED on {url}: {analysis['error']}")
        return f"Could not analyze {url}: {analysis['error']}"

    bb.log_event("Analyst", f"analyzed {analysis['owner']}/{analysis['repo']} "
                            f"-> severity={analysis['severity']}")
    return (f"Analyzed {analysis['owner']}/{analysis['repo']}: "
            f"severity={analysis['severity']}. "
            f"Missing files: {len(analysis['missing_files'])}. "
            f"Dependency issues: {len(analysis['dependency_issues'])}. "
            f"Hardcoded landmines: {len(analysis['landmines'])}. "
            f"Broken URLs: {sum(1 for u in analysis['url_checks'] if not u['ok'])}.")


def tool_inspect_finding(
    repo_url: Annotated[str, "Repo URL the finding belongs to"],
    finding_kind: Annotated[str, "One of: missing_file, dependency, landmine, url"],
    index: Annotated[int, "Which finding (0-indexed) to inspect"],
) -> str:
    """Look up a specific finding's details."""
    analysis = next((a for a in bb.repo_analyses if a["url"] == repo_url), None)
    if not analysis:
        return f"ERROR: no analysis for {repo_url}"
    key_map = {
        "missing_file": "missing_files",
        "dependency": "dependency_issues",
        "landmine": "landmines",
        "url": "url_checks",
    }
    key = key_map.get(finding_kind)
    if not key or key not in analysis:
        return f"ERROR: unknown finding kind '{finding_kind}'"
    findings = analysis[key]
    if index < 0 or index >= len(findings):
        return f"ERROR: index {index} out of range (have {len(findings)})"
    return f"{finding_kind} #{index} in {repo_url}: {json.dumps(findings[index])}"


def tool_challenge_finding(
    repo_url: Annotated[str, "Repo URL"],
    finding_kind: Annotated[str, "One of: missing_file, dependency, landmine, url"],
    index: Annotated[int, "Finding index"],
    verdict: Annotated[str, "One of: true_positive, false_positive, needs_recheck"],
    reasoning: Annotated[str, "Why this verdict is appropriate"],
) -> str:
    """Critic records a verdict on a specific finding."""
    challenge = {
        "repo_url": repo_url, "finding_kind": finding_kind,
        "index": index, "verdict": verdict, "reasoning": reasoning,
    }
    bb.critic_challenges.append(challenge)
    bb.log_event("Critic", f"{verdict} on {finding_kind}#{index} of {repo_url}")

    if verdict == "false_positive":
        analysis = next((a for a in bb.repo_analyses if a["url"] == repo_url), None)
        if analysis:
            key_map = {
                "missing_file": "missing_files",
                "dependency": "dependency_issues",
                "landmine": "landmines", "url": "url_checks",
            }
            key = key_map.get(finding_kind)
            if key and 0 <= index < len(analysis[key]):
                removed = analysis[key].pop(index)
                bb.log_event("Critic", f"removed false positive: {removed}")
    return f"Recorded {verdict} for {finding_kind}#{index} of {repo_url}."


def tool_get_state() -> str:
    """Return a compact snapshot of the blackboard. Any agent can call this
    to see what URLs have been classified, which repos are prioritized,
    what analyses have been done, and so on. Always call this first to
    orient yourself before deciding what to do."""
    lines = ["=== BLACKBOARD STATE ==="]
    lines.append(f"Paper text loaded: {len(bb.paper_text):,} chars")
    lines.append(f"")
    lines.append(f"CODE URLs ({len(bb.buckets['code'])}):")
    for u in bb.buckets["code"]:
        prio = bb.repo_priorities.get(u, "unset")
        analyzed = any(a["url"] == u for a in bb.repo_analyses)
        lines.append(f"  - {u}  [priority={prio}, analyzed={analyzed}]")
    lines.append(f"")
    lines.append(f"DATA URLs ({len(bb.buckets['data'])}):")
    for u in bb.buckets["data"]:
        lines.append(f"  - {u}")
    lines.append(f"")
    lines.append(f"AMBIGUOUS URLs: {len(bb.buckets['ambiguous'])}")
    lines.append(f"")
    if bb.repo_analyses:
        lines.append(f"REPO ANALYSES:")
        for a in bb.repo_analyses:
            if a.get("error"):
                lines.append(f"  - {a['url']}: ERROR ({a['error']})")
            else:
                lines.append(f"  - {a['owner']}/{a['repo']}: "
                             f"severity={a.get('severity')}, "
                             f"missing={len(a.get('missing_files', []))}, "
                             f"deps={len(a.get('dependency_issues', []))}, "
                             f"landmines={len(a.get('landmines', []))}, "
                             f"broken_urls={sum(1 for u in a.get('url_checks', []) if not u['ok'])}")
    else:
        lines.append("REPO ANALYSES: none yet")
    lines.append(f"")
    lines.append(f"CRITIC CHALLENGES: {len(bb.critic_challenges)}")
    lines.append(f"SIMULATION: "
                 f"{bb.simulation_result.get('status') if bb.simulation_result else 'not run'}")
    lines.append(f"EXECUTOR: "
                 f"{bb.executor_result.get('status') if bb.executor_result else 'not run'}")
    lines.append(f"VERDICT: {'set' if bb.verdict else 'not set'}")
    return "\n".join(lines)


def tool_check_simulation_needed() -> str:
    """Judge calls this to check whether the Simulator should run.

    Combines: presence/absence of data repo, --simulate flag, --no-simulate flag.
    """
    decision = bb.should_simulate()
    reason_parts = []
    if args.simulate:
        reason_parts.append("--simulate flag is set (human request)")
    if args.no_simulate:
        reason_parts.append("--no-simulate flag is set")
    if not args.simulate and not args.no_simulate:
        if bb.has_data_repo():
            reason_parts.append(f"data repo found ({len(bb.data_enriched)} entries)")
        else:
            reason_parts.append("no data repo found")
    reason = "; ".join(reason_parts)
    bb.log_event("Judge", f"simulation decision: {decision} ({reason})")
    return f"Should simulate: {decision}. Reason: {reason}."


def tool_run_simulation(
    github_url: Annotated[str, "Optional GitHub repo URL to guide simulation (pass '' if none)"] = "",
) -> str:
    """Run the Simulator agent. Refuses if already run or if simulation is not needed."""
    if bb.simulation_result is not None:
        return (f"REFUSED: simulation already ran "
                f"(status={bb.simulation_result.get('status')}). "
                "Do not run it again. Yield to the Judge.")
    if not bb.should_simulate():
        return ("REFUSED: simulation is not needed. A data repo exists and "
                "--simulate was not passed. Use --simulate to override.")
    log("\n" + "-" * 60)
    log("[Simulator] Invoking data_simulator.trigger_data_simulation...")
    log("-" * 60)
    bb.log_event("Simulator", "starting data generation sub-conversation")

    target_url = github_url if github_url else None
    # If no URL was passed but the Extractor found code repos, use the best one
    if not target_url and bb.code_enriched:
        target_url = bb.code_enriched[0].get("url")
        bb.log_event("Simulator", f"using code URL {target_url} as guidance")

    try:
        # Capture CWD at the moment of the call — trigger_data_simulation
        # writes to ./simulated_data_output/ relative to whatever CWD is now.
        sim_output_dir = (Path.cwd() / "simulated_data_output").resolve()
        trigger_data_simulation(pdf_path=str(args.paper_path), github_url=target_url)

        # Verify the simulator actually wrote something. The LLM sometimes
        # says "TERMINATE" without producing files, so an empty dir means a
        # silent failure even if no exception was raised.
        files_written = []
        if sim_output_dir.is_dir():
            files_written = [str(p.relative_to(sim_output_dir))
                             for p in sim_output_dir.rglob("*") if p.is_file()]

        if not files_written:
            bb.simulation_result = {
                "status": "failed",
                "reason": ("no files written to simulated_data_output/ — "
                           "the simulator agent may have produced code that "
                           "didn't execute, or terminated early"),
                "output_dir": str(sim_output_dir),
                "finished_at": datetime.now().isoformat(timespec="seconds"),
            }
            bb.log_event("Simulator", "FAILED: no files produced")
            return ("Simulation produced no output files. "
                    "Check simulated_data_output/ — it is empty.")

        bb.simulation_result = {
            "status": "completed",
            "github_url": target_url,
            "output_dir": str(sim_output_dir),  # ABSOLUTE path, captured at write time
            "file_count": len(files_written),
            "sample_files": files_written[:5],
            "finished_at": datetime.now().isoformat(timespec="seconds"),
        }
        bb.log_event("Simulator", "data generation completed")
        return ("Simulation complete. Mock dataset written to "
                "./simulated_data_output/ — check that directory for output files.")
    except Exception as e:
        bb.simulation_result = {"status": "failed", "error": str(e)}
        bb.log_event("Simulator", f"FAILED: {e}")
        return f"Simulation failed: {e}"


def tool_check_executor_needed() -> str:
    """Judge / Executor calls this to check whether the Executor should run.

    The Executor is opt-in (--execute-repo). It only fires if:
      - the user passed --execute-repo,
      - at least one code repo exists,
      - --no-execute-repo was not passed.
    """
    decision = bb.should_execute_repo()
    reasons = []
    if args.no_execute_repo:
        reasons.append("--no-execute-repo flag set")
    elif args.execute_repo:
        if not bb.buckets["code"]:
            reasons.append("--execute-repo set but no code repos available")
        else:
            reasons.append(f"--execute-repo set and {len(bb.buckets['code'])} code repo(s) available")
    else:
        reasons.append("Executor is opt-in; pass --execute-repo to enable")
    bb.log_event("Judge", f"executor decision: {decision} ({'; '.join(reasons)})")
    return f"Should execute repo: {decision}. Reason: {'; '.join(reasons)}."


def tool_execute_repo() -> str:
    """Clone the top-priority repo locally and smoke-test it.

    Uses simulated_data_output/ as the data source if it exists.
    """
    if bb.executor_result is not None:
        return (f"REFUSED: Executor already ran "
                f"(status={bb.executor_result.get('status')}). "
                "Do not run it again.")
    if not bb.should_execute_repo():
        return ("REFUSED: Executor is not enabled. Pass --execute-repo to enable, "
                "or check that at least one code repo was found.")

    target_url = bb.top_priority_repo()
    if not target_url:
        bb.executor_result = {"status": "skipped",
                              "reason": "no eligible repo (all were skip-priority)"}
        bb.log_event("Executor", "no eligible repo to run")
        return "No eligible repo to run."

    log("\n" + "-" * 60)
    log(f"[Executor] Cloning + smoke-testing {target_url}")
    log("-" * 60)
    bb.log_event("Executor", f"starting local smoke test of {target_url}")

    # Pull the simulated data path from the blackboard (captured at
    # simulation time as an absolute path). Don't recompute — the CWD may
    # have changed and "simulated_data_output" relative resolution is fragile.
    sim_dir = None
    if bb.simulation_result and bb.simulation_result.get("status") == "completed":
        recorded = bb.simulation_result.get("output_dir")
        if recorded and Path(recorded).is_dir():
            sim_dir = Path(recorded)
            file_count = bb.simulation_result.get("file_count", "?")
            bb.log_event("Executor",
                         f"will use simulated data: {sim_dir} ({file_count} files)")
        else:
            bb.log_event("Executor",
                         f"simulation reported completed but path missing: {recorded}")

    try:
        result = run_repository_smoke_test(
            repo_url=target_url,
            workspace_dir=args.executor_workspace.resolve(),
            simulated_data_dir=sim_dir,
            model=args.model,
        )
        bb.executor_result = result
        bb.log_event("Executor", f"finished: status={result['status']}")
        state_summary = ", ".join(
            f"{k}={'ok' if v.get('ok') else 'fail'}"
            for k, v in result.get("states", {}).items()
        )
        return (f"Executor {result['status']}. States: {state_summary}. "
                f"Workspace: {result['workspace']}.")
    except Exception as e:
        bb.executor_result = {"status": "failed", "error": str(e), "repo_url": target_url}
        bb.log_event("Executor", f"FAILED: {e}")
        return f"Executor failed: {e}"


def tool_set_verdict(
    score: Annotated[int, "Reproducibility score 0-10"],
    summary: Annotated[str, "2-4 paragraph narrative summarizing the assessment"],
) -> str:
    """Judge sets the final reproducibility verdict.

    Refuses if (a) verdict already set, (b) the Extractor hasn't classified URLs,
    or (c) simulation was requested but hasn't happened yet.
    """
    if bb.verdict is not None:
        return (f"REFUSED: verdict already set to {bb.verdict['score']}/10. "
                "A verdict can only be set once.")
    if not bb.code_enriched and not bb.data_enriched and not bb.buckets["ambiguous"]:
        return "REFUSED: Extractor has not classified URLs yet. Wait for the workflow."
    if bb.should_simulate() and bb.simulation_result is None:
        return ("REFUSED: simulation is required (--simulate or no data repo) "
                "but has not run yet. Route to the Simulator first.")
    if bb.should_execute_repo() and bb.executor_result is None:
        return ("REFUSED: --execute-repo was passed but the Executor has not run yet. "
                "Route to the Executor first.")
    if not 0 <= score <= 10:
        return f"ERROR: score must be 0-10, got {score}"
    bb.verdict = {"score": score, "summary": summary,
                  "set_at": datetime.now().isoformat(timespec="seconds")}
    bb.log_event("Judge", f"final verdict: {score}/10")
    return f"Verdict set: {score}/10. Now reply with 'TERMINATE' on its own line."


# ----------------------------------------------------------------------------
# Agent system messages
# ----------------------------------------------------------------------------

EXTRACTOR_MSG = """You are the Extractor. Find and classify all URLs in a scientific paper.

Workflow:
1. Call `read_paper` to load the PDF text.
2. Call `extract_and_classify` to find URLs and bucket them.
3. If another agent reports a URL is misclassified, call `reclassify_url`.

Be brief — your tools do the heavy lifting. Do NOT say TERMINATE."""

PLANNER_MSG = """You are the Planner. Decide which code repos the Analyst should scan deeply.

Workflow:
1. FIRST: call `get_state` to see the list of code URLs you need to prioritize.
2. THEN: for EACH code URL in the state, call `set_priority(url, priority, reason)` exactly once.
   - priority must be one of: "high", "medium", "skip"
   - Pass the EXACT URL string from the state — do not paraphrase or shorten it.
3. Bias toward "high". Use "skip" only for clearly irrelevant repos (org pages, unrelated forks).
4. After every code URL has a priority, stop. Do NOT say TERMINATE."""

ANALYST_MSG = """You are the Analyst. Run static analysis on each prioritized code repo.

Workflow:
1. FIRST: call `get_state` to see which repos have priorities and which are already analyzed.
2. THEN: for EACH code URL with priority high or medium that is not yet analyzed,
   call `analyze_repo(url)` with the EXACT URL string from the state.
3. After every prioritized repo has been analyzed, stop. Do NOT say TERMINATE.
4. If the Critic later challenges a finding, call `inspect_finding` to re-examine it."""

CRITIC_MSG = """You are the Critic. Challenge the Analyst's findings to filter false positives.

Workflow:
1. FIRST: call `get_state` to see which repos were analyzed and their severity.
2. For each repo with severity medium or high, call `inspect_finding(repo_url, finding_kind, index)`
   on at least 1-2 of its findings. Valid finding_kind values: "missing_file", "dependency", "landmine", "url".
3. For each inspected finding, call `challenge_finding` with verdict:
   - "true_positive" (real issue, keep it)
   - "false_positive" (remove it)
   - "needs_recheck" (Analyst should look again)
4. Common false positives: paths generated at runtime, deps pinned in a different manifest,
   hardcoded paths in comments or docstrings.
5. After challenging, stop. Do NOT say TERMINATE."""

JUDGE_MSG = """You are the Judge. Produce the final reproducibility verdict.

Workflow:
1. FIRST: call `get_state` to see the complete picture.
2. Call `check_simulation_needed`. If it returns "Should simulate: True" AND simulation has not run,
   yield to the Simulator by stating "Simulator, please run the simulation." Do NOT call set_verdict yet.
3. After simulation (or if not needed), call `check_executor_needed`. If it returns "Should execute repo: True"
   AND the Executor has not run yet, yield to the Executor by stating "Executor, please run the repo."
   Do NOT call set_verdict yet.
4. Once simulation and Executor are done (or not needed), call `set_verdict(score, summary)`:
   - score 0-10: 9-10 fully reproducible, 6-8 minor friction, 3-5 significant gaps, 0-2 nothing reproducible
   - summary: 2-4 short paragraphs of FINAL ASSESSMENT, not deliberation.
     If the Executor ran, factor its success/failure into the score (a repo that won't even import is worse).
5. AFTER `set_verdict` returns successfully, your VERY NEXT message must be exactly:
   TERMINATE
   (just that word, on its own line, nothing else)"""

SIMULATOR_MSG = """You are the Simulator. Generate a simulated dataset.

Workflow:
1. Call `get_state` to see if a code URL is available to guide simulation.
2. Call `run_simulation(github_url)` once. Pass the first code URL from the state
   if there is one, otherwise pass an empty string "".
3. Report the outcome briefly and stop. Do NOT say TERMINATE — the Judge ends the chat."""

EXECUTOR_MSG = """You are the Executor. Clone the top-priority code repo locally and smoke-test it against the simulated dataset (or with no data if no simulation was run).

Workflow:
1. Call `get_state` to confirm a top-priority repo exists.
2. Call `execute_repo()` exactly once — it takes no arguments. The tool picks the
   highest-priority repo from the Planner's prioritization and runs a state
   machine LOCALLY: CLONE -> INSPECT -> INSTALL -> RUN.
3. Report the outcome briefly (which states passed, which failed) and stop.
   Do NOT say TERMINATE — the Judge ends the chat.

Note: this agent runs shell commands directly on the host. It is opt-in
(only fires when --execute-repo is passed). If it refuses, just yield back
to the Judge."""

# ----------------------------------------------------------------------------
# Build agents
# ----------------------------------------------------------------------------

llm_config = LLMConfig(
    {"api_type": "openai", "model": args.model,
     "api_key": os.environ["OPENAI_API_KEY"]}
)


def make_agent(name: str, system_msg: str, tools: list) -> ConversableAgent:
    agent = ConversableAgent(
        name=name,
        system_message=system_msg,
        llm_config=llm_config,
        human_input_mode="NEVER",
    )
    for tool_fn, tool_name, tool_desc in tools:
        agent.register_for_llm(name=tool_name, description=tool_desc)(tool_fn)
    return agent


extractor = make_agent("Extractor", EXTRACTOR_MSG, [
    (tool_read_paper, "read_paper",
     "Read the paper PDF and extract text and link annotations."),
    (tool_extract_and_classify, "extract_and_classify",
     "Find URLs in the paper text and bucket them by host+context."),
    (tool_reclassify_url, "reclassify_url",
     "Move a URL between classification buckets."),
])

planner = make_agent("Planner", PLANNER_MSG, [
    (tool_get_state, "get_state",
     "Read the current blackboard state to see URLs, priorities, and analyses."),
    (tool_set_priority, "set_priority",
     "Set analysis priority (high/medium/skip) for a code repo."),
])

analyst = make_agent("Analyst", ANALYST_MSG, [
    (tool_get_state, "get_state",
     "Read the current blackboard state."),
    (tool_analyze_repo, "analyze_repo",
     "Run static analysis (metadata, manifests, landmines) on a GitHub repo."),
    (tool_inspect_finding, "inspect_finding",
     "Look up the details of a specific finding."),
])

critic = make_agent("Critic", CRITIC_MSG, [
    (tool_get_state, "get_state",
     "Read the current blackboard state."),
    (tool_inspect_finding, "inspect_finding",
     "Look up the details of a specific finding to evaluate it."),
    (tool_challenge_finding, "challenge_finding",
     "Record a verdict on a finding."),
])

judge = make_agent("Judge", JUDGE_MSG, [
    (tool_get_state, "get_state",
     "Read the current blackboard state."),
    (tool_check_simulation_needed, "check_simulation_needed",
     "Check whether the Simulator should run based on data-repo presence and flags."),
    (tool_check_executor_needed, "check_executor_needed",
     "Check whether the Executor should clone+run the top-priority repo."),
    (tool_set_verdict, "set_verdict",
     "Set the final reproducibility verdict (score 0-10 + narrative)."),
])

simulator = make_agent("Simulator", SIMULATOR_MSG, [
    (tool_get_state, "get_state",
     "Read the current blackboard state."),
    (tool_run_simulation, "run_simulation",
     "Generate a simulated dataset via data_simulator.trigger_data_simulation."),
])

executor_agent = make_agent("Executor", EXECUTOR_MSG, [
    (tool_get_state, "get_state",
     "Read the current blackboard state."),
    (tool_execute_repo, "execute_repo",
     "Clone the top-priority repo and smoke-test it locally."),
])

def _is_termination(m):
    """Strict termination check: only fire when TERMINATE is the last
    non-empty line of a message. Prevents the kickoff or any instruction
    that mentions the word from prematurely ending the chat."""
    content = (m.get("content") or "").rstrip()
    if not content:
        return False
    last_line = content.split("\n")[-1].strip()
    return last_line == "TERMINATE"


# Tool-runner proxy runs all tool calls. Named "ToolRunner" (not "Executor")
# because the Executor name is taken by the repo-execution agent above.
tool_runner = UserProxyAgent(
    name="ToolRunner",
    human_input_mode="NEVER",
    code_execution_config=False,
    is_termination_msg=_is_termination,
    max_consecutive_auto_reply=args.max_rounds,
)

for tool_fn, tool_name in [
    (tool_read_paper, "read_paper"),
    (tool_extract_and_classify, "extract_and_classify"),
    (tool_reclassify_url, "reclassify_url"),
    (tool_get_state, "get_state"),
    (tool_set_priority, "set_priority"),
    (tool_analyze_repo, "analyze_repo"),
    (tool_inspect_finding, "inspect_finding"),
    (tool_challenge_finding, "challenge_finding"),
    (tool_check_simulation_needed, "check_simulation_needed"),
    (tool_run_simulation, "run_simulation"),
    (tool_check_executor_needed, "check_executor_needed"),
    (tool_execute_repo, "execute_repo"),
    (tool_set_verdict, "set_verdict"),
]:
    tool_runner.register_for_execution(name=tool_name)(tool_fn)

# ----------------------------------------------------------------------------
# GroupChat with deterministic speaker selection (state machine)
# ----------------------------------------------------------------------------

def select_next_speaker(last_speaker, groupchat):
    """State machine that routes based on blackboard state, not LLM judgment.

    Workflow stages, checked in order:
      1. Paper not read         -> Extractor (the URL-extracting agent)
      2. URLs not classified    -> Extractor
      3. Code repos exist & not all prioritized -> Planner
      4. Prioritized repos not all analyzed     -> Analyst
      5. Analyst findings exist & Critic hasn't challenged -> Critic
      6. Simulation needed & not run            -> Simulator
      7. Repo execution needed & not run        -> Executor (the repo-running agent)
      8. Verdict not set                        -> Judge
      9. Verdict set                            -> return None (end)

    After any agent emits a tool call, route to ToolRunner next to run it.
    """
    agents_by_name = {a.name: a for a in groupchat.agents}

    # If the last speaker emitted a tool call, run it via the ToolRunner
    last_msg = groupchat.messages[-1] if groupchat.messages else None
    if last_msg and last_msg.get("tool_calls"):
        return agents_by_name["ToolRunner"]

    # State checks (in order)
    if not bb.paper_text:
        return agents_by_name["Extractor"]

    urls_classified = bool(bb.buckets["code"] or bb.buckets["data"]
                           or bb.buckets["ambiguous"] or bb.buckets["skip"])
    if not urls_classified:
        return agents_by_name["Extractor"]

    code_urls = [u for u in bb.buckets["code"]]
    if code_urls and len(bb.repo_priorities) < len(code_urls):
        return agents_by_name["Planner"]

    repos_to_analyze = [u for u in code_urls
                        if bb.repo_priorities.get(u, "high") != "skip"]
    analyzed = {a["url"] for a in bb.repo_analyses}
    if any(u not in analyzed for u in repos_to_analyze):
        return agents_by_name["Analyst"]

    # After analysis, Critic gets one pass if there are findings worth challenging
    has_findings = any(
        a.get("severity") in ("medium", "high")
        and (a.get("missing_files") or a.get("dependency_issues")
             or a.get("landmines"))
        for a in bb.repo_analyses
    )
    if has_findings and not bb.critic_challenges:
        return agents_by_name["Critic"]

    # Simulation if needed and not yet run
    if bb.should_simulate() and bb.simulation_result is None:
        return agents_by_name["Simulator"]

    # Executor if enabled (--execute-repo) and not yet run
    if bb.should_execute_repo() and bb.executor_result is None:
        return agents_by_name["Executor"]

    # Verdict last
    if bb.verdict is None:
        return agents_by_name["Judge"]

    # Everything done — let the chat terminate
    return None


group_chat = GroupChat(
    agents=[extractor, planner, analyst, critic, judge, simulator, executor_agent, tool_runner],
    messages=[],
    max_round=args.max_rounds,
    speaker_selection_method=select_next_speaker,
    allow_repeat_speaker=True,
)

manager = GroupChatManager(
    groupchat=group_chat,
    llm_config=llm_config,
    is_termination_msg=_is_termination,
)

# ----------------------------------------------------------------------------
# Run the GroupChat with a safety net
# ----------------------------------------------------------------------------

KICKOFF = f"""Assess the reproducibility of the paper at `{args.paper_path}`.

Workflow:
1. Extractor: read the paper and classify all URLs.
2. Planner: prioritize each code repo for analysis.
3. Analyst: run static analysis on each prioritized repo.
4. Critic: challenge the Analyst's findings.
5. Judge: call check_simulation_needed. If simulation is needed, the Simulator runs first; then Judge sets the verdict and says TERMINATE.

Coordinate among yourselves. Each agent should do its part and yield to the next."""

log("\n" + "=" * 60)
log("Starting multi-agent GroupChat...")
log("=" * 60)

groupchat_ok = False
try:
    tool_runner.initiate_chat(manager, message=KICKOFF, clear_history=True)
    groupchat_ok = bb.verdict is not None
    if not groupchat_ok:
        log("\n[warn] GroupChat ended without Judge setting a verdict.")
except Exception as e:
    log(f"\n[error] GroupChat crashed: {e}")

# Safety net
if not groupchat_ok and args.fallback_on_loop:
    log("\n[fallback] Running deterministic completion...")
    if not bb.paper_text:
        tool_read_paper()
    if not bb.buckets["code"] and not bb.buckets["data"]:
        tool_extract_and_classify()
    for url in bb.buckets["code"]:
        if not any(a["url"] == url for a in bb.repo_analyses):
            tool_analyze_repo(url)
    if bb.should_simulate() and not bb.simulation_result:
        tool_run_simulation("")
    if bb.should_execute_repo() and not bb.executor_result:
        tool_execute_repo()
    if not bb.verdict:
        sev_weights = {"clean": 0, "low": 1, "medium": 3, "high": 6, "unknown": 2}
        total = sum(sev_weights.get(a.get("severity", "unknown"), 2)
                    for a in bb.repo_analyses)
        score = max(0, 10 - total)
        tool_set_verdict(
            score=score,
            summary=("Deterministic fallback verdict — the multi-agent "
                     "GroupChat did not reach clean termination. Score "
                     "computed from severity-weighted findings."),
        )

# ----------------------------------------------------------------------------
# Persist outputs
# ----------------------------------------------------------------------------

log("\nWriting outputs...")

report = {
    "paper": str(args.paper_path),
    "generated_at": datetime.now().isoformat(timespec="seconds"),
    "model": args.model,
    "mode": "multi-agent",
    "counts": {k: len(v) for k, v in bb.buckets.items()},
    "code_urls": bb.code_enriched,
    "data_urls": bb.data_enriched,
    "ambiguous_urls": bb.buckets["ambiguous"],
    "skipped_urls": bb.buckets["skip"],
    "repo_analyses": bb.repo_analyses,
    "repo_priorities": bb.repo_priorities,
    "critic_challenges": bb.critic_challenges,
    "verdict": bb.verdict,
    "simulation_result": bb.simulation_result,
    "executor_result": bb.executor_result,
    "groupchat_terminated_cleanly": groupchat_ok,
}
json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

# Transcript
transcript = ["# Multi-agent transcript\n",
              f"**Paper:** `{args.paper_path.name}`  ",
              f"**Generated:** {report['generated_at']}  ",
              f"**Model:** {args.model}  ",
              f"**Terminated cleanly:** {groupchat_ok}  ",
              f"**Simulation ran:** {bb.simulation_result is not None}  ",
              f"**Executor ran:** {bb.executor_result is not None}\n",
              "## Audit trail\n"]
for line in bb.audit_trail:
    transcript.append(f"- {line}")
transcript.append("\n## Full conversation\n")
for msg in group_chat.messages:
    name = msg.get("name", "system")
    content = msg.get("content", "")
    tool_calls = msg.get("tool_calls") or []
    transcript.append(f"### {name}\n")
    if content:
        transcript.append(content)
    for tc in tool_calls:
        fn = tc.get("function", {})
        transcript.append(f"\n**Tool call:** `{fn.get('name')}`")
        if fn.get("arguments"):
            transcript.append(f"```json\n{fn['arguments']}\n```")
    transcript.append("")
transcript_path.write_text("\n".join(transcript), encoding="utf-8")

# Final markdown report
md = [f"# Reproducibility report (multi-agent)\n",
      f"**Paper:** `{args.paper_path.name}`  ",
      f"**Generated:** {report['generated_at']}  ",
      f"**Model:** {args.model}\n"]

if bb.verdict:
    md.append(f"## Verdict: {bb.verdict['score']}/10\n")
    md.append(bb.verdict["summary"])
    md.append("")
else:
    md.append("## Verdict: not set\n")
    md.append("_The Judge did not produce a final verdict._\n")

md.append("## Summary\n")
md.append(f"- Code repositories: **{len(bb.code_enriched)}**")
md.append(f"- Data repositories: **{len(bb.data_enriched)}**")
md.append(f"- Repos analyzed: **{len(bb.repo_analyses)}**")
md.append(f"- Critic challenges: **{len(bb.critic_challenges)}** "
          f"({sum(1 for c in bb.critic_challenges if c['verdict'] == 'false_positive')} "
          f"false positives removed)")
if bb.simulation_result:
    status = bb.simulation_result.get("status", "?")
    md.append(f"- Data simulation: **{status}** "
              f"(→ `simulated_data_output/`)")
else:
    md.append(f"- Data simulation: _not run_")
if bb.executor_result:
    status = bb.executor_result.get("status", "?")
    repo = bb.executor_result.get("repo_dir") or bb.executor_result.get("repo_url", "?")
    md.append(f"- Repo execution: **{status}** "
              f"(`{repo}` → `{bb.executor_result.get('workspace', '?')}`)")
    states = bb.executor_result.get("states", {})
    if states:
        state_line = ", ".join(
            f"{k}={'✅' if v.get('ok') else '❌'}"
            for k, v in states.items()
        )
        md.append(f"  - States: {state_line}")
else:
    md.append(f"- Repo execution: _not run_")
md.append("")

md.append("## Repos analyzed\n")
sev_order = {"high": 0, "medium": 1, "low": 2, "clean": 3, "unknown": 4}
SEV_BADGE = {"high": "🔴 high", "medium": "🟡 medium",
             "low": "🟢 low", "clean": "✅ clean", "unknown": "⚪ unknown"}
for a in sorted(bb.repo_analyses, key=lambda x: sev_order.get(x.get("severity"), 5)):
    if a.get("error"):
        md.append(f"### [{a['url']}]({a['url']})")
        md.append(f"_Could not analyze: {a['error']}_\n")
        continue
    md.append(f"### [{a['owner']}/{a['repo']}]({a['url']}) — "
              f"{SEV_BADGE[a['severity']]}")
    if a["missing_files"]:
        md.append(f"- {len(a['missing_files'])} missing file reference(s)")
    if a["dependency_issues"]:
        md.append(f"- {len(a['dependency_issues'])} dependency issue(s)")
    if a["landmines"]:
        md.append(f"- {len(a['landmines'])} hardcoded environment value(s)")
    broken = [u for u in a["url_checks"] if not u["ok"]]
    if broken:
        md.append(f"- {len(broken)} broken URL(s) in README/code")
    md.append("")

md.append("## Audit trail\n")
for line in bb.audit_trail:
    md.append(f"- {line}")
md.append(f"\n_Full conversation transcript: [`{transcript_path.name}`]"
          f"({transcript_path.name})_")

md_path.write_text("\n".join(md), encoding="utf-8")

log("\n" + "=" * 60)
log("Outputs written:")
log(f"  - {md_path}       (final report)")
log(f"  - {transcript_path} (demo artifact)")
log(f"  - {json_path}     (structured findings)")
log(f"  - {log_path}      (run log)")
log(f"GroupChat terminated cleanly: {groupchat_ok}")
log(f"Simulation ran: {bb.simulation_result is not None}")
log(f"Executor ran: {bb.executor_result is not None}")
log("=" * 60)