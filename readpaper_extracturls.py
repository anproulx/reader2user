#!/usr/bin/env python3
"""
Extract data and code links from a scientific paper.

Pipeline:
  1. Read PDF locally (text + clickable link annotations)
  2. Find URLs with regex, clean PDF artifacts, repair GitHub mishaps
  3. Classify by hostname (deterministic)
  4. For known code hosts, hit their API for metadata (no LLM).
     Bare GitHub org/user pages (no repo) are skipped — only repos the
     paper explicitly cites get reported.
  5. For ambiguous URLs only, use LLM to classify from surrounding context
  6. Verify all final URLs resolve

Outputs:
  - <paper>.report.md    Human-readable Markdown report
  - <paper>.report.json  Machine-readable JSON
  - <paper>.log          Full run log (everything printed to console)

Usage:
    python readpaper_extracturls.py mypaper.pdf
    python readpaper_extracturls.py mypaper.pdf --output-dir results/

Importable: when imported as a module, only helper functions are exposed.
The CLI pipeline only runs when the script is executed directly.
"""

import os
import re
import sys
import json
import argparse
import logging
import requests
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse
from typing_extensions import Annotated
from pypdf import PdfReader

# ----------------------------------------------------------------------------
# Module-level constants — importable
# ----------------------------------------------------------------------------

logger = logging.getLogger("readpaper")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    _console = logging.StreamHandler(sys.stdout)
    _console.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(_console)
log = logger.info

GH_HEADERS = {"Accept": "application/vnd.github+json"}
if os.environ.get("GITHUB_TOKEN"):
    GH_HEADERS["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"

KNOWN_HOSTS = {
    "github.com": "code",
    "gitlab.com": "code",
    "bitbucket.org": "code",
    "codeberg.org": "code",
    "sourceforge.net": "code",
    "huggingface.co": "ambiguous",
    "zenodo.org": "ambiguous",
    "osf.io": "ambiguous",
    "figshare.com": "data",
    "datadryad.org": "data",
    "dataverse.harvard.edu": "data",
    "kaggle.com": "data",
    "data.mendeley.com": "data",
    "physionet.org": "data",
    "ncbi.nlm.nih.gov": "data",
    "ebi.ac.uk": "data",
    "ensembl.org": "data",
    "uniprot.org": "data",
    "pdb.org": "data",
    "rcsb.org": "data",
    "arxiv.org": "skip",
    "biorxiv.org": "skip",
    "medrxiv.org": "skip",
    "doi.org": "skip",
    "scholar.google.com": "skip",
}

CODE_HINTS = (
    "code", "implementation", "source", "repository", "repo",
    "github", "gitlab", "script", "software",
)
DATA_HINTS = (
    "data", "dataset", "deposited", "available at", "accession",
    "supplementary", "benchmark", "corpus",
)
DATA_PATH_HINTS = (
    "data", "dataset", "datasets", "download", "get_data",
    "downloads", "files", "records",
)
CODE_PATH_HINTS = (
    "code", "src", "source", "repo", "repository",
)

# ----------------------------------------------------------------------------
# Helpers — all at module level, all importable
# ----------------------------------------------------------------------------

def read_paper_local(path: str, max_chars: int = 200_000) -> tuple[str, set[str]]:
    """Extract text and URL annotations from a PDF, with PyMuPDF fallback."""
    text_parts = []
    annotation_urls: set[str] = set()
    failed_pages = 0

    try:
        reader = PdfReader(path)
        for i, page in enumerate(reader.pages):
            try:
                text_parts.append(page.extract_text() or "")
            except Exception:
                failed_pages += 1
                text_parts.append("")

            if "/Annots" in page:
                try:
                    for annot in page["/Annots"]:
                        obj = annot.get_object()
                        if obj.get("/Subtype") == "/Link" and "/A" in obj:
                            uri = obj["/A"].get_object().get("/URI")
                            if uri:
                                annotation_urls.add(str(uri))
                except Exception:
                    pass
    except Exception as e:
        log(f"      [warn] pypdf failed to open the PDF: {e}")

    extracted_chars = sum(len(p) for p in text_parts)

    if extracted_chars < 1000:
        log(f"      [info] pypdf gave only {extracted_chars} chars "
            f"({failed_pages} pages failed); falling back to PyMuPDF...")
        try:
            import fitz
            doc = fitz.open(path)
            text_parts = [page.get_text() for page in doc]
            for page in doc:
                for link in page.get_links():
                    if link.get("uri"):
                        annotation_urls.add(link["uri"])
            doc.close()
            log(f"      [info] PyMuPDF recovered "
                f"{sum(len(p) for p in text_parts):,} chars")
        except ImportError:
            log("      [warn] PyMuPDF not installed; "
                "run `pip install pymupdf` for fallback")
        except Exception as e:
            log(f"      [warn] PyMuPDF also failed: {e}")
    elif failed_pages:
        log(f"      [warn] {failed_pages} page(s) failed text extraction")

    text = "\n".join(text_parts)
    return text[:max_chars], annotation_urls


def clean_url(url: str) -> str:
    """Strip sentence punctuation and other PDF-extraction artifacts."""
    url = url.rstrip(".,;:!?")
    last_slash = url.rfind("/")
    tail = url[last_slash + 1:] if last_slash != -1 else url
    m = re.search(r"\.([A-Z][a-zA-Z]+)$", tail)
    if m:
        url = url[: last_slash + 1 + m.start()] if last_slash != -1 else url[: m.start()]

    for open_c, close_c in [("(", ")"), ("[", "]"), ("{", "}")]:
        if url.endswith(close_c) and url.count(open_c) < url.count(close_c):
            url = url[:-1]

    return url


def find_urls(text: str) -> set[str]:
    """Find URLs and DOIs in text, repairing common PDF extraction artifacts."""
    cleaned = re.sub(r"(https?://\S*?)[\s\u00ad\u200b]+(\S)", r"\1\2", text)
    cleaned = re.sub(r"-\n", "", cleaned)

    url_pattern = r'https?://[^\s\)\]\>,]+'
    doi_pattern = r'\b10\.\d{4,9}/[-._;()/:A-Z0-9]+'

    raw_urls = set(re.findall(url_pattern, cleaned, re.IGNORECASE))
    raw_urls.update(
        f"https://doi.org/{d}" for d in re.findall(doi_pattern, cleaned, re.IGNORECASE)
    )

    return {clean_url(u) for u in raw_urls if clean_url(u)}


def repair_github_url(url: str) -> str:
    """For github.com URLs whose path has a suspicious dot, probe variants."""
    if "github.com" not in url:
        return url
    parsed = urlparse(url)
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if not parts:
        return url
    first = parts[0]
    m = re.search(r"\.[A-Z][a-z]", first)
    if not m:
        return url
    truncated_first = first[: m.start()]
    if not truncated_first:
        return url
    candidate = f"https://github.com/{truncated_first}"
    try:
        r = requests.head(candidate, allow_redirects=True, timeout=5)
        if r.status_code < 400:
            log(f"      [repair] {url} -> {candidate}")
            return candidate
    except Exception:
        pass
    return url


def url_context(text: str, url: str, window: int = 200) -> str:
    """Return ~window chars around url, tolerating PDF line breaks."""
    idx = text.find(url)
    if idx == -1:
        parsed = urlparse(url)
        path_parts = parsed.path.split("/")
        first_seg = path_parts[1] if len(path_parts) > 1 else ""
        needle = f"{parsed.netloc}/{first_seg}" if first_seg else parsed.netloc
        idx = text.find(needle)
        if idx == -1:
            idx = text.find(parsed.netloc)
            if idx == -1:
                return ""
    return text[max(0, idx - window): idx + len(url) + window]


def score_context(context: str) -> tuple[int, int]:
    lower = context.lower()
    code_score = sum(1 for h in CODE_HINTS if h in lower)
    data_score = sum(1 for h in DATA_HINTS if h in lower)
    return code_score, data_score


def classify_urls(text: str, urls: set[str]) -> dict:
    buckets = {"code": [], "data": [], "ambiguous": [], "skip": []}
    for url in urls:
        parsed = urlparse(url)
        host = parsed.netloc.lower().removeprefix("www.")
        path = parsed.path.lower()
        category = KNOWN_HOSTS.get(host)

        if category in ("code", "data", "skip"):
            buckets[category].append(url)
            continue

        path_segments = [s for s in path.split("/") if s]
        path_says_data = any(h in seg for seg in path_segments for h in DATA_PATH_HINTS)
        path_says_code = any(h in seg for seg in path_segments for h in CODE_PATH_HINTS)

        if path_says_data and not path_says_code:
            buckets["data"].append(url)
            continue
        if path_says_code and not path_says_data:
            buckets["code"].append(url)
            continue

        if host.endswith(".github.io"):
            buckets["ambiguous"].append(url)
            continue

        ctx = url_context(text, url)
        code_score, data_score = score_context(ctx)

        if code_score > data_score and code_score >= 2:
            buckets["code"].append(url)
        elif data_score > code_score and data_score >= 2:
            buckets["data"].append(url)
        else:
            buckets["ambiguous"].append(url)
    return buckets


def github_info(url: str) -> dict | None:
    """Fetch metadata for a github.com/owner/repo URL."""
    m = re.match(r"https?://github\.com/([^/]+)/([^/?#]+)", url)
    if not m:
        return None
    owner, repo = m.group(1), m.group(2).removesuffix(".git")
    try:
        r = requests.get(
            f"https://api.github.com/repos/{owner}/{repo}",
            timeout=10, headers=GH_HEADERS,
        )
        if r.status_code == 200:
            d = r.json()
            return {
                "url": url,
                "description": d.get("description"),
                "language": d.get("language"),
                "stars": d.get("stargazers_count"),
                "updated": d.get("updated_at"),
            }
    except Exception:
        pass
    return None


def zenodo_info(url: str) -> dict | None:
    m = re.match(r"https?://zenodo\.org/record[s]?/(\d+)", url)
    if not m:
        return None
    record_id = m.group(1)
    try:
        r = requests.get(f"https://zenodo.org/api/records/{record_id}", timeout=10)
        if r.status_code == 200:
            d = r.json()
            md = d.get("metadata", {})
            return {
                "url": url,
                "title": md.get("title"),
                "resource_type": md.get("resource_type", {}).get("type"),
                "description": (md.get("description") or "")[:300],
            }
    except Exception:
        pass
    return None


def enrich_code(urls: list[str]) -> list[dict]:
    """Enrich code URLs. Drops bare github.com/<owner> pages (not a repo)."""
    enriched = []
    for url in urls:
        if re.match(r"https?://github\.com/[^/?#]+/?$", url):
            log(f"      [skip] {url} is an org/user page, not a repo")
            continue
        info = github_info(url) or zenodo_info(url) or {"url": url}
        enriched.append(info)
    return enriched


def enrich_data(urls: list[str]) -> list[dict]:
    """Enrich data URLs (Zenodo only for now; other hosts pass through)."""
    enriched = []
    for url in urls:
        info = zenodo_info(url) or {"url": url}
        enriched.append(info)
    return enriched


def check_url_local(url: str) -> dict:
    """HEAD request to confirm a URL resolves."""
    try:
        r = requests.head(url, allow_redirects=True, timeout=10)
        return {
            "url": url,
            "status": r.status_code,
            "content_type": r.headers.get("content-type", "unknown"),
            "ok": 200 <= r.status_code < 400,
        }
    except Exception as e:
        return {"url": url, "status": None, "error": str(e), "ok": False}


def _md_status(url: str, verify_by_url: dict) -> str:
    r = verify_by_url.get(url)
    if not r:
        return ""
    if r.get("ok"):
        return f" ✅ {r['status']}"
    if r.get("error"):
        return f" ❌ error"
    return f" ⚠️ {r.get('status', '?')}"


def _md_entry(item: dict, verify_by_url: dict) -> str:
    url = item["url"]
    line = f"- [{url}]({url}){_md_status(url, verify_by_url)}"
    bits = []
    if item.get("description"):
        bits.append(item["description"])
    if item.get("title"):
        bits.append(item["title"])
    if item.get("language"):
        bits.append(f"`{item['language']}`")
    if item.get("stars") is not None:
        bits.append(f"★ {item['stars']}")
    if item.get("updated"):
        bits.append(f"updated {item['updated'][:10]}")
    if bits:
        line += "\n  - " + " · ".join(str(b) for b in bits)
    return line


# ----------------------------------------------------------------------------
# CLI / main — only runs when script is executed directly
# ----------------------------------------------------------------------------

def main():
    from autogen import ConversableAgent, UserProxyAgent, LLMConfig

    parser = argparse.ArgumentParser(
        description="Extract data and code links from a scientific paper."
    )
    parser.add_argument("paper_path", help="Path to the PDF file")
    parser.add_argument("--model", default="gpt-5-nano", help="OpenAI model to use")
    parser.add_argument("--max-chars", type=int, default=200_000,
                        help="Max characters of paper text to keep")
    parser.add_argument("--output-dir", default=".",
                        help="Directory to write report files (default: cwd)")
    args = parser.parse_args()

    paper_path = Path(args.paper_path)
    if not paper_path.is_file():
        parser.error(f"File not found: {paper_path}")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = paper_path.stem
    log_path = output_dir / f"{stem}.log"
    json_path = output_dir / f"{stem}.report.json"
    md_path = output_dir / f"{stem}.report.md"

    file_handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s",
                                                datefmt="%H:%M:%S"))
    logger.addHandler(file_handler)

    log(f"Run started: {datetime.now().isoformat(timespec='seconds')}")
    log(f"Paper: {paper_path}")
    log(f"Model: {args.model}")

    llm_config = LLMConfig(
        {"api_type": "openai", "model": args.model,
         "api_key": os.environ["OPENAI_API_KEY"]}
    )

    log(f"\n[1/5] Reading {paper_path}...")
    text, annotation_urls = read_paper_local(str(paper_path), args.max_chars)
    log(f"      Extracted {len(text):,} characters, "
        f"{len(annotation_urls)} link annotations")

    log("\n[2/5] Finding URLs...")
    text_urls = find_urls(text)
    annotation_urls = {clean_url(u) for u in annotation_urls if clean_url(u)}
    all_urls = text_urls | annotation_urls

    log("      Repairing suspicious GitHub URLs...")
    repaired = {repair_github_url(u) for u in all_urls}
    n_repaired = len(all_urls) - len(repaired)
    all_urls = repaired
    log(f"      {len(text_urls)} from text, {len(annotation_urls)} from annotations, "
        f"{len(all_urls)} unique total"
        + (f" ({n_repaired} merged after repair)" if n_repaired > 0 else ""))

    log("\n[3/5] Classifying by host + context...")
    buckets = classify_urls(text, all_urls)
    for k, v in buckets.items():
        log(f"      {k:>10}: {len(v)}")

    log("\n[4/5] Enriching known code/data URLs via host APIs...")
    code_enriched = enrich_code(buckets["code"])
    data_enriched = enrich_data(buckets["data"])
    for item in code_enriched + data_enriched:
        desc = item.get("description") or item.get("title") or "(no metadata)"
        log(f"      {item['url']}\n         {desc[:120]}")

    llm_findings = ""
    if buckets["ambiguous"]:
        log(f"\n[5/5] Classifying {len(buckets['ambiguous'])} ambiguous URLs with LLM...")
        ambig_payload = []
        for url in buckets["ambiguous"]:
            ctx = url_context(text, url, window=300)
            ambig_payload.append(f"URL: {url}\nContext: {ctx}\n---")
        payload = "\n".join(ambig_payload)

        extractor = ConversableAgent(
            name="extractor",
            system_message=(
                "You classify URLs from a scientific paper as either 'code', "
                "'data', or 'neither' based on the surrounding context. "
                "For each URL provided, reply with one line: "
                "URL -> category (one-line justification). "
                "Reply 'DONE' on the final line."
            ),
            llm_config=llm_config,
        )

        runner = UserProxyAgent(
            name="runner",
            human_input_mode="NEVER",
            max_consecutive_auto_reply=2,
            is_termination_msg=lambda x: (x.get("content") or "").rstrip().endswith("TERMINATE"),
            code_execution_config=False,
        )

        extraction_result = runner.initiate_chat(
            extractor,
            message=f"Classify these URLs:\n\n{payload}",
            max_turns=2,
        )
        llm_findings = extraction_result.summary
    else:
        log("\n[5/5] No ambiguous URLs — skipping LLM classification.")

    log("\n[verify] Checking that final URLs resolve...")
    verification_results = []
    for item in code_enriched + data_enriched:
        res = check_url_local(item["url"])
        verification_results.append(res)
        log(f"  {res['url']} -> {res.get('status', 'ERROR')}")
    for url in buckets["ambiguous"]:
        res = check_url_local(url)
        verification_results.append(res)
        log(f"  {url} -> {res.get('status', 'ERROR')}")

    verify_by_url = {r["url"]: r for r in verification_results}

    report = {
        "paper": str(paper_path),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "model": args.model,
        "counts": {k: len(v) for k, v in buckets.items()},
        "code_urls": code_enriched,
        "data_urls": data_enriched,
        "ambiguous_urls": buckets["ambiguous"],
        "skipped_urls": buckets["skip"],
        "verification": verification_results,
        "llm_classification": llm_findings,
    }
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    md = []
    md.append(f"# Link extraction report\n")
    md.append(f"**Paper:** `{paper_path.name}`  ")
    md.append(f"**Generated:** {report['generated_at']}  ")
    md.append(f"**Model:** {args.model}\n")
    md.append("## Summary\n")
    md.append(f"- Code repositories: **{len(code_enriched)}**")
    md.append(f"- Data repositories: **{len(data_enriched)}**")
    md.append(f"- Ambiguous (needs review): **{len(buckets['ambiguous'])}**")
    md.append(f"- Skipped (papers/DOIs): **{len(buckets['skip'])}**\n")
    md.append("## Code repositories\n")
    if code_enriched:
        md.extend(_md_entry(i, verify_by_url) for i in code_enriched)
    else:
        md.append("_None found._")
    md.append("")
    md.append("## Data repositories\n")
    if data_enriched:
        md.extend(_md_entry(i, verify_by_url) for i in data_enriched)
    else:
        md.append("_None found._")
    md.append("")
    md.append("## Ambiguous URLs\n")
    if buckets["ambiguous"]:
        for url in buckets["ambiguous"]:
            md.append(f"- [{url}]({url}){_md_status(url, verify_by_url)}")
        if llm_findings:
            md.append("\n### LLM classification\n")
            md.append("```")
            md.append(llm_findings)
            md.append("```")
    else:
        md.append("_None._")
    md.append("")
    md.append("## Skipped (papers/DOIs)\n")
    if buckets["skip"]:
        for url in buckets["skip"]:
            md.append(f"- {url}")
    else:
        md.append("_None._")

    md_path.write_text("\n".join(md), encoding="utf-8")

    log("\n" + "=" * 60)
    log("Reports written:")
    log(f"  - {md_path}   (human-readable)")
    log(f"  - {json_path} (machine-readable)")
    log(f"  - {log_path}      (full run log)")
    log("=" * 60)


if __name__ == "__main__":
    main()
