#!/usr/bin/env python3
"""
Analyze code/data repositories surfaced by readpaper_extracturls.py.

Pipeline (metadata-only — no git clone, no local execution):
  1. Read the first agent's <paper>.report.json
  2. For each code repo (and Zenodo data record):
       - Fetch repo metadata + recursive file tree via host API
       - Fetch manifests as raw files
       - Fetch a capped set of likely-entry-point .py files
       - Run four landmine scans on the fetched content
  3. Use the LLM only to summarize findings into a short prose narrative
  4. Append a "Repository analysis" section to the existing
     <paper>.report.md, and merge findings into <paper>.report.json

Importable: when imported as a module, only helper functions and constants
are exposed. The CLI pipeline only runs when the script is executed directly.

Usage:
    python analyze_repos.py results/mypaper.report.json
"""

import os
import re
import sys
import json
import base64
import argparse
import logging
import requests
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse

# ----------------------------------------------------------------------------
# Module-level logger and constants — importable
# ----------------------------------------------------------------------------

logger = logging.getLogger("analyst")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    _console = logging.StreamHandler(sys.stdout)
    _console.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(_console)
log = logger.info

GH_HEADERS = {"Accept": "application/vnd.github+json"}
if os.environ.get("GITHUB_TOKEN"):
    GH_HEADERS["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"

WEIGHT_FILE_PATTERNS = (
    r"['\"]([^'\"]+\.(?:pth|pt|ckpt|safetensors|h5|bin|pkl|joblib|onnx|tflite|pb))['\"]",
    r"['\"]([^'\"]+/(?:weights|checkpoints?|models?)/[^'\"]+)['\"]",
)
DATA_FILE_PATTERNS = (
    r"['\"]([^'\"]+\.(?:csv|tsv|jsonl|parquet|npz|npy|hdf5|mat))['\"]",
    r"['\"]([^'\"]+/(?:data|datasets?)/[^'\"]+\.[a-zA-Z0-9]{1,6})['\"]",
)

DEPRECATED_DEPS = {
    "sklearn":         "use 'scikit-learn' — 'sklearn' on PyPI is a deprecated stub",
    "tensorflow-gpu":  "merged into 'tensorflow' since 2.0; pinning this fails on modern installs",
    "torch-scatter":   "must match exact torch+CUDA build; very brittle across environments",
    "torch-sparse":    "must match exact torch+CUDA build; very brittle across environments",
    "torch-geometric": "older 1.x versions incompatible with torch >= 2.0",
    "pytorch":         "the PyPI package is named 'torch', not 'pytorch'",
    "opencv":          "use 'opencv-python' or 'opencv-python-headless'",
}

LANDMINE_PATTERNS = {
    "hardcoded_unix_path":   r"['\"](/home/[^'\"]+|/Users/[^'\"]+)['\"]",
    "hardcoded_windows_path": r"['\"]([A-Z]:\\\\[^'\"]+)['\"]",
    "hardcoded_cuda_device": r"(?:\.to\(|device\s*=\s*)['\"]cuda(?::\d+)?['\"]",
    "hardcoded_cuda_call":   r"\.cuda\(\s*\d*\s*\)",
    "aws_access_key":        r"AKIA[0-9A-Z]{16}",
    "generic_secret":        r"(?i)(?:api[_-]?key|secret|token|password)\s*=\s*['\"][A-Za-z0-9_\-]{16,}['\"]",
    "wandb_entity":          r"wandb\.init\([^)]*entity\s*=\s*['\"]([^'\"]+)['\"]",
}

MANIFEST_FILES = (
    "requirements.txt", "requirements-dev.txt", "requirements_dev.txt",
    "pyproject.toml", "setup.py", "setup.cfg", "Pipfile", "Pipfile.lock",
    "environment.yml", "environment.yaml", "conda.yml",
    "README.md", "README.rst", "README.txt", "README",
    ".python-version", "runtime.txt",
)

ENTRY_POINT_HINTS = (
    "train.py", "main.py", "run.py", "infer.py", "inference.py", "predict.py",
    "evaluate.py", "eval.py", "demo.py", "app.py", "test.py",
)

URL_PATTERN = re.compile(r"https?://[^\s\)\]\>,'\"`]+")

# ----------------------------------------------------------------------------
# GitHub API helpers
# ----------------------------------------------------------------------------

def parse_github_url(url: str) -> tuple[str, str] | None:
    m = re.match(r"https?://github\.com/([^/]+)/([^/?#]+)", url)
    if not m:
        return None
    owner = m.group(1)
    repo = m.group(2).removesuffix(".git").rstrip("/")
    return owner, repo


def gh_get(path: str, **kwargs) -> requests.Response | None:
    try:
        r = requests.get(f"https://api.github.com{path}",
                         headers=GH_HEADERS, timeout=15, **kwargs)
        if r.status_code == 403 and "rate limit" in r.text.lower():
            log("      [error] GitHub API rate limit hit — "
                "set GITHUB_TOKEN to raise the limit to 5000/hr")
        return r
    except Exception as e:
        log(f"      [warn] GitHub request failed: {e}")
        return None


def fetch_repo_metadata(owner: str, repo: str) -> dict | None:
    r = gh_get(f"/repos/{owner}/{repo}")
    if not r or r.status_code != 200:
        return None
    d = r.json()
    return {
        "default_branch": d.get("default_branch", "main"),
        "description": d.get("description"),
        "language": d.get("language"),
        "size_kb": d.get("size"),
        "stars": d.get("stargazers_count"),
        "updated": d.get("updated_at"),
        "archived": d.get("archived", False),
        "license": (d.get("license") or {}).get("spdx_id"),
        "topics": d.get("topics", []),
    }


def fetch_repo_tree(owner: str, repo: str, branch: str) -> list[dict]:
    r = gh_get(f"/repos/{owner}/{repo}/git/trees/{branch}",
               params={"recursive": "1"})
    if not r or r.status_code != 200:
        return []
    data = r.json()
    if data.get("truncated"):
        log(f"      [warn] tree truncated for {owner}/{repo}")
    return data.get("tree", [])


def fetch_file(owner: str, repo: str, path: str, branch: str) -> str | None:
    r = gh_get(f"/repos/{owner}/{repo}/contents/{path}",
               params={"ref": branch})
    if not r or r.status_code != 200:
        return None
    data = r.json()
    if data.get("encoding") != "base64" or "content" not in data:
        return None
    try:
        raw = base64.b64decode(data["content"])
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return None


# ----------------------------------------------------------------------------
# Landmine scanners
# ----------------------------------------------------------------------------

def scan_missing_files(scanned_files: dict[str, str],
                       tree_paths: set[str]) -> list[dict]:
    findings = []
    patterns = [(p, "weight") for p in WEIGHT_FILE_PATTERNS] + \
               [(p, "data") for p in DATA_FILE_PATTERNS]

    referenced: dict[str, dict] = {}
    for path, content in scanned_files.items():
        for pattern, kind in patterns:
            for m in re.finditer(pattern, content):
                ref = m.group(1)
                if ref.startswith(("http://", "https://")):
                    continue
                normalized = ref.lstrip("./").lstrip("/")
                entry = referenced.setdefault(ref, {
                    "kind": kind, "normalized": normalized, "sources": []
                })
                if path not in entry["sources"]:
                    entry["sources"].append(path)

    for ref, info in referenced.items():
        norm = info["normalized"]
        basename = norm.rsplit("/", 1)[-1]
        has_match = any(
            tp == norm or tp.endswith("/" + norm) or tp.endswith("/" + basename)
            for tp in tree_paths
        )
        if not has_match:
            findings.append({
                "reference": ref,
                "kind": info["kind"],
                "found_in": info["sources"][:3],
            })
    return findings


def scan_dependencies(manifests: dict[str, str]) -> list[dict]:
    findings = []
    declared_pyver = None
    pyver_sources = []

    def add(severity, kind, detail, source):
        findings.append({"severity": severity, "kind": kind,
                         "detail": detail, "source": source})

    for name, content in manifests.items():
        if not name.startswith("requirements") or not name.endswith(".txt"):
            continue
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("-"):
                continue
            spec = re.split(r"[;\s]", line, maxsplit=1)[0]
            m = re.match(r"^([A-Za-z0-9_.\-]+)\s*([<>=!~].*)?$", spec)
            if not m:
                continue
            pkg = m.group(1).lower()
            version_spec = (m.group(2) or "").strip()

            if pkg in DEPRECATED_DEPS:
                add("high", "deprecated_dependency",
                    f"{pkg}: {DEPRECATED_DEPS[pkg]}", name)
            if not version_spec:
                add("medium", "unpinned_dependency",
                    f"{pkg} has no version constraint", name)

    py = manifests.get("pyproject.toml", "")
    if py:
        m = re.search(r"requires-python\s*=\s*['\"]([^'\"]+)['\"]", py)
        if m:
            declared_pyver = m.group(1)
            pyver_sources.append("pyproject.toml")
        for dep in re.findall(r"['\"]([A-Za-z][A-Za-z0-9_.\-]*)['\"]", py):
            if dep.lower() in DEPRECATED_DEPS:
                add("high", "deprecated_dependency",
                    f"{dep}: {DEPRECATED_DEPS[dep.lower()]}", "pyproject.toml")

    for name in ("environment.yml", "environment.yaml", "conda.yml"):
        env = manifests.get(name, "")
        if not env:
            continue
        m = re.search(r"python\s*[=:]\s*['\"]?([\d.]+)", env)
        if m:
            declared_pyver = m.group(1)
            pyver_sources.append(name)
        for line in env.splitlines():
            ls = line.strip().lstrip("-").strip()
            pkg = re.split(r"[=<>]", ls, maxsplit=1)[0].strip().lower()
            if pkg in DEPRECATED_DEPS:
                add("high", "deprecated_dependency",
                    f"{pkg}: {DEPRECATED_DEPS[pkg]}", name)

    for name in ("runtime.txt", ".python-version"):
        c = manifests.get(name, "").strip()
        if c:
            m = re.search(r"(\d+\.\d+(?:\.\d+)?)", c)
            if m:
                v = m.group(1)
                if declared_pyver and v not in declared_pyver and declared_pyver not in v:
                    add("medium", "python_version_mismatch",
                        f"{name} says {v} but {pyver_sources[0]} says {declared_pyver}",
                        name)
                declared_pyver = declared_pyver or v
                pyver_sources.append(name)

    if declared_pyver and re.search(r"\b(2\.[0-9]|3\.[0-7])\b", declared_pyver):
        add("high", "eol_python_version",
            f"declared Python {declared_pyver} is past upstream end-of-life",
            ", ".join(pyver_sources))

    return findings


def scan_landmines(scanned_files: dict[str, str]) -> list[dict]:
    findings = []
    for path, content in scanned_files.items():
        for label, pattern in LANDMINE_PATTERNS.items():
            for m in re.finditer(pattern, content):
                snippet = m.group(0)
                if len(snippet) > 120:
                    snippet = snippet[:117] + "..."
                line_no = content[:m.start()].count("\n") + 1
                findings.append({
                    "kind": label,
                    "file": path,
                    "line": line_no,
                    "snippet": snippet,
                })
    return findings


def scan_urls_in_repo(scanned_files: dict[str, str]) -> list[dict]:
    urls: set[str] = set()
    for path, content in scanned_files.items():
        for m in URL_PATTERN.finditer(content):
            u = m.group(0).rstrip(".,;:!?)]'\"`")
            urls.add(u)

    findings = []
    for url in sorted(urls):
        host = urlparse(url).netloc.lower()
        if host.endswith(("shields.io", "badge.fury.io", "travis-ci.org",
                          "circleci.com", "readthedocs.org",
                          "opensource.org", "www.gnu.org", "creativecommons.org")):
            continue
        try:
            r = requests.head(url, allow_redirects=True, timeout=8)
            ok = 200 <= r.status_code < 400
            findings.append({"url": url, "status": r.status_code, "ok": ok})
        except Exception as e:
            findings.append({"url": url, "status": None, "error": str(e), "ok": False})
    return findings


def pick_py_files(tree: list[dict], cap: int) -> list[str]:
    py = [t["path"] for t in tree
          if t.get("type") == "blob" and t["path"].endswith(".py")]
    def rank(p: str) -> tuple[int, int, str]:
        basename = p.rsplit("/", 1)[-1].lower()
        is_entry = 0 if basename in ENTRY_POINT_HINTS else 1
        depth = p.count("/")
        return (is_entry, depth, p)
    py.sort(key=rank)
    return py[:cap]


def severity_for_repo(a: dict) -> str:
    if a.get("error"):
        return "unknown"
    score = (
        len(a["missing_files"])
        + sum(2 for d in a["dependency_issues"] if d["severity"] == "high")
        + sum(1 for d in a["dependency_issues"] if d["severity"] == "medium")
        + len([m for m in a["landmines"] if m["kind"] != "hardcoded_cuda_call"])
        + sum(1 for u in a["url_checks"] if not u["ok"])
    )
    if score >= 8:
        return "high"
    if score >= 3:
        return "medium"
    if score >= 1:
        return "low"
    return "clean"


def analyze_github_repo(url: str, max_py_files: int = 20,
                        max_tree_entries: int = 5000) -> dict:
    """Run all four scans against a single GitHub repo. API-only, no clone."""
    parsed = parse_github_url(url)
    if not parsed:
        return {"url": url, "error": "not a GitHub repo URL"}
    owner, repo = parsed
    log(f"\n  -> {owner}/{repo}")

    meta = fetch_repo_metadata(owner, repo)
    if not meta:
        return {"url": url, "error": "could not fetch repo metadata"}
    branch = meta["default_branch"]
    log(f"     branch: {branch}, lang: {meta.get('language')}, "
        f"stars: {meta.get('stars')}, archived: {meta.get('archived')}")

    tree = fetch_repo_tree(owner, repo, branch)
    tree_paths = {t["path"] for t in tree if t.get("type") == "blob"}
    log(f"     tree: {len(tree_paths)} files")

    manifests: dict[str, str] = {}
    for name in MANIFEST_FILES:
        if name in tree_paths:
            content = fetch_file(owner, repo, name, branch)
            if content is not None:
                manifests[name] = content
    log(f"     manifests fetched: {sorted(manifests.keys())}")

    scanned_files: dict[str, str] = dict(manifests)
    if len(tree_paths) <= max_tree_entries:
        py_targets = pick_py_files(tree, max_py_files)
        log(f"     scanning {len(py_targets)} .py files (cap={max_py_files})")
        for p in py_targets:
            content = fetch_file(owner, repo, p, branch)
            if content is not None:
                scanned_files[p] = content
    else:
        log(f"     [skip] tree has {len(tree_paths)} entries; skipping .py scan")

    missing = scan_missing_files(scanned_files, tree_paths)
    deps = scan_dependencies(manifests)
    mines = scan_landmines(scanned_files)
    urls = scan_urls_in_repo(scanned_files)

    log(f"     findings: {len(missing)} missing-file refs, "
        f"{len(deps)} dependency issues, "
        f"{len(mines)} hardcoded-env issues, "
        f"{sum(1 for u in urls if not u['ok'])}/{len(urls)} URLs broken")

    analysis = {
        "url": url, "owner": owner, "repo": repo, "metadata": meta,
        "tree_size": len(tree_paths),
        "scanned_files": sorted(scanned_files.keys()),
        "missing_files": missing, "dependency_issues": deps,
        "landmines": mines, "url_checks": urls,
    }
    analysis["severity"] = severity_for_repo(analysis)
    return analysis


def _fmt_repo(a: dict) -> list[str]:
    SEV_BADGE = {"high": "🔴 high", "medium": "🟡 medium",
                 "low": "🟢 low", "clean": "✅ clean", "unknown": "⚪ unknown"}
    out = []
    if a.get("error"):
        out.append(f"### [{a['url']}]({a['url']})")
        out.append(f"_Could not analyze: {a['error']}_\n")
        return out

    header = f"### [{a['owner']}/{a['repo']}]({a['url']}) — {SEV_BADGE[a['severity']]}"
    out.append(header)
    meta = a["metadata"]
    bits = []
    if meta.get("language"): bits.append(f"`{meta['language']}`")
    if meta.get("stars") is not None: bits.append(f"★ {meta['stars']}")
    if meta.get("updated"): bits.append(f"updated {meta['updated'][:10]}")
    if meta.get("license"): bits.append(meta["license"])
    if meta.get("archived"): bits.append("**archived**")
    if bits:
        out.append(" · ".join(bits))
    out.append("")

    if a["missing_files"]:
        out.append("**Missing referenced files:**")
        for f in a["missing_files"][:10]:
            srcs = ", ".join(f"`{s}`" for s in f["found_in"])
            out.append(f"- `{f['reference']}` ({f['kind']}) — referenced in {srcs}")
        if len(a["missing_files"]) > 10:
            out.append(f"- _…and {len(a['missing_files']) - 10} more_")
        out.append("")

    if a["dependency_issues"]:
        out.append("**Dependency issues:**")
        for d in a["dependency_issues"][:15]:
            out.append(f"- _{d['severity']}_ · `{d['source']}` · {d['detail']}")
        if len(a["dependency_issues"]) > 15:
            out.append(f"- _…and {len(a['dependency_issues']) - 15} more_")
        out.append("")

    if a["landmines"]:
        out.append("**Environment assumptions / hardcoded values:**")
        by_kind: dict[str, list[dict]] = {}
        for m in a["landmines"]:
            by_kind.setdefault(m["kind"], []).append(m)
        for kind, ms in by_kind.items():
            sample = ms[0]
            out.append(f"- `{kind}` ({len(ms)}×) — e.g. `{sample['file']}:{sample['line']}` "
                       f"`{sample['snippet']}`")
        out.append("")

    broken = [u for u in a["url_checks"] if not u["ok"]]
    if broken:
        out.append("**Broken URLs in README/code:**")
        for u in broken[:10]:
            status = u.get("status") or "ERR"
            out.append(f"- [{u['url']}]({u['url']}) — {status}")
        if len(broken) > 10:
            out.append(f"- _…and {len(broken) - 10} more_")
        out.append("")

    if a["severity"] == "clean":
        out.append("_No landmines detected in scanned files._\n")
    return out


# ----------------------------------------------------------------------------
# CLI / main — only runs when script is executed directly
# ----------------------------------------------------------------------------

def main():
    from autogen import ConversableAgent, UserProxyAgent, LLMConfig

    parser = argparse.ArgumentParser(
        description="Analyze repos referenced by a scientific paper for runtime landmines."
    )
    parser.add_argument("report_json", help="Path to <paper>.report.json from the first agent")
    parser.add_argument("--model", default="gpt-5-nano", help="OpenAI model to use")
    parser.add_argument("--max-py-files", type=int, default=20,
                        help="Max .py files to fetch per repo for landmine scanning")
    parser.add_argument("--max-tree-entries", type=int, default=5000,
                        help="Skip per-file scans on repos with more tree entries than this")
    args = parser.parse_args()

    report_json_path = Path(args.report_json)
    if not report_json_path.is_file():
        parser.error(f"Report JSON not found: {report_json_path}")

    stem = report_json_path.name.removesuffix(".report.json")
    out_dir = report_json_path.parent
    md_path = out_dir / f"{stem}.report.md"
    log_path = out_dir / f"{stem}.analyst.log"

    if not md_path.is_file():
        parser.error(f"Expected sibling markdown report not found: {md_path}")

    file_handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s",
                                                datefmt="%H:%M:%S"))
    logger.addHandler(file_handler)

    log(f"Run started: {datetime.now().isoformat(timespec='seconds')}")
    log(f"Input report: {report_json_path}")
    log(f"Model: {args.model}")

    if not os.environ.get("GITHUB_TOKEN"):
        log("      [warn] GITHUB_TOKEN not set; API limited to 60 requests/hour")

    llm_config = LLMConfig(
        {"api_type": "openai", "model": args.model,
         "api_key": os.environ["OPENAI_API_KEY"]}
    )

    log(f"\n[1/3] Loading {report_json_path}...")
    first_report = json.loads(report_json_path.read_text(encoding="utf-8"))
    code_items = first_report.get("code_urls", [])
    data_items = first_report.get("data_urls", [])
    log(f"      {len(code_items)} code URLs, {len(data_items)} data URLs from first agent")

    github_targets = [item["url"] for item in code_items
                      if parse_github_url(item["url"])]
    non_github_code = [item["url"] for item in code_items
                       if not parse_github_url(item["url"])]
    log(f"      {len(github_targets)} GitHub repos to analyze "
        f"({len(non_github_code)} non-GitHub code URLs skipped)")

    log(f"\n[2/3] Analyzing {len(github_targets)} repos via GitHub API...")
    analyses = [analyze_github_repo(url, args.max_py_files,
                                    args.max_tree_entries)
                for url in github_targets]

    log("\n[3/3] Generating prose summary via LLM...")

    digest_lines = []
    for a in analyses:
        if a.get("error"):
            digest_lines.append(f"REPO {a['url']}: ERROR — {a['error']}")
            continue
        digest_lines.append(f"REPO {a['owner']}/{a['repo']} (severity={a['severity']}):")
        if a["metadata"].get("archived"):
            digest_lines.append("  - repo is ARCHIVED")
        if a["missing_files"]:
            digest_lines.append(f"  - {len(a['missing_files'])} referenced file(s) "
                                f"not present in tree")
        for d in a["dependency_issues"][:5]:
            digest_lines.append(f"  - [{d['severity']}] {d['kind']}: {d['detail']}")
        landmine_kinds = sorted({m['kind'] for m in a["landmines"]})
        if landmine_kinds:
            digest_lines.append(f"  - environment assumptions: {', '.join(landmine_kinds)}")
        broken = [u for u in a["url_checks"] if not u["ok"]]
        if broken:
            digest_lines.append(f"  - {len(broken)} broken URL(s) in README/code")

    digest = "\n".join(digest_lines) if digest_lines else "(no repos analyzed)"

    summarizer = ConversableAgent(
        name="summarizer",
        system_message=(
            "You write a short, sober reproducibility assessment for a scientific "
            "paper's code/data repositories. Given a digest of static-analysis "
            "findings, produce 2-4 short paragraphs in plain Markdown (no headers) "
            "covering: (1) overall reproducibility outlook, (2) the most serious "
            "landmines a reader would hit when trying to run this code, (3) any "
            "repos that look healthy. Be concrete — name files and dependencies. "
            "Do not invent findings not in the digest. End with 'DONE' on its own line."
        ),
        llm_config=llm_config,
    )

    runner = UserProxyAgent(
        name="runner",
        human_input_mode="NEVER",
        max_consecutive_auto_reply=1,
        is_termination_msg=lambda x: (x.get("content") or "").rstrip().endswith("DONE"),
        code_execution_config=False,
    )

    chat = runner.initiate_chat(
        summarizer,
        message=f"Findings digest:\n\n{digest}",
        max_turns=2,
    )
    narrative = (chat.summary or "").replace("DONE", "").strip()

    first_report["repo_analysis"] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "model": args.model,
        "repos": analyses,
        "narrative": narrative,
    }
    report_json_path.write_text(json.dumps(first_report, indent=2), encoding="utf-8")

    md_section = [
        "\n---\n",
        "# Repository analysis\n",
        f"_Generated: {first_report['repo_analysis']['generated_at']} · "
        f"Model: {args.model}_\n",
    ]

    if non_github_code:
        md_section.append(f"_{len(non_github_code)} non-GitHub code URL(s) "
                          f"skipped (analyzer is GitHub-only)._\n")

    md_section.append("## Reproducibility outlook\n")
    md_section.append(narrative if narrative else "_No narrative generated._")
    md_section.append("")

    md_section.append("## Repos analyzed\n")
    sev_order = {"high": 0, "medium": 1, "low": 2, "clean": 3, "unknown": 4}
    for a in sorted(analyses, key=lambda x: sev_order.get(x.get("severity"), 5)):
        md_section.extend(_fmt_repo(a))

    with md_path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(md_section))

    log("\n" + "=" * 60)
    log("Reports updated:")
    log(f"  - {md_path}   (appended)")
    log(f"  - {report_json_path} (repo_analysis key added)")
    log(f"  - {log_path}     (full run log)")
    log("=" * 60)


if __name__ == "__main__":
    main()
