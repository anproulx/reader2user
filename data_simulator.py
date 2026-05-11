"""
Data simulator: generates a mock dataset from a paper PDF and optional GitHub repo.

When called directly (CLI), uses an autogen DataSimulatorAgent that writes and
executes a Python script producing mock data files in ./simulated_data_output/.

When imported, only helper functions and the trigger_data_simulation entry point
are exposed — agents are NOT instantiated at import time, so importing this
module has no side effects (no API key required, no agent creation).

Usage:
    python data_simulator.py paper.pdf
    python data_simulator.py paper.pdf https://github.com/owner/repo
"""

import os
import requests
from typing import Annotated
from pypdf import PdfReader


# ==========================================
# 1. HELPER FUNCTIONS (importable, no side effects)
# ==========================================

def extract_pdf_text(pdf_path: str) -> str:
    """Read a local PDF and extract text (first 15 pages to keep context small)."""
    try:
        reader = PdfReader(pdf_path)
        text = ""
        for page in reader.pages[:15]:
            text += page.extract_text() + "\n"
        return text
    except Exception as e:
        return f"Error reading PDF: {e}"


def get_github_tree(repo_url: str) -> str:
    """Fetch the file structure of a GitHub repo so the agent knows what exists."""
    try:
        parts = repo_url.rstrip('/').split('/')
        owner, repo = parts[-2], parts[-1]

        repo_info_url = f"https://api.github.com/repos/{owner}/{repo}"
        repo_res = requests.get(repo_info_url)
        if repo_res.status_code == 403 and "rate limit" in repo_res.text.lower():
            return "Error: GitHub API rate limit exceeded."

        branch = repo_res.json().get("default_branch", "main") if repo_res.status_code == 200 else "main"

        api_url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}?recursive=1"
        response = requests.get(api_url)

        if response.status_code == 200:
            tree = response.json().get("tree", [])
            files = [
                item["path"] for item in tree
                if item["type"] == "blob"
                and item["path"].endswith(('.py', '.R', '.csv', '.md', '.txt', '.json', '.yml'))
            ]
            if len(files) > 100:
                files = files[:100] + ["... (list truncated, too many files)"]
            return f"Repository File Tree (Branch: {branch}):\n" + "\n".join(files)
        return f"Could not fetch repository tree. Status: {response.status_code}"
    except Exception as e:
        return f"Error accessing GitHub: {e}"


def read_github_file(
    repo_url: Annotated[str, "The base URL of the GitHub repository"],
    file_path: Annotated[str, "The specific file path from the repository tree to read"],
) -> str:
    """Read the exact content of a specific file in the provided GitHub repository."""
    try:
        parts = repo_url.rstrip('/').split('/')
        owner, repo = parts[-2], parts[-1]
        repo_res = requests.get(f"https://api.github.com/repos/{owner}/{repo}")
        branch = repo_res.json().get("default_branch", "main") if repo_res.status_code == 200 else "main"
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{file_path}"
        response = requests.get(raw_url)
        if response.status_code == 200:
            return response.text[:5000]
        return f"File '{file_path}' not found. Status: {response.status_code}"
    except Exception as e:
        return f"Error reading file: {e}"


# ==========================================
# 2. AGENT SYSTEM PROMPT (constant, no side effects)
# ==========================================

SIMULATOR_SYSTEM_PROMPT = """
You are an Expert Data Scientist and Reproducibility Engineer.
Your task is to generate a realistic simulated dataset based on a provided paper and an optional GitHub repository.

CRITICAL INSTRUCTIONS FOR CODE GENERATION:
1. NO STUBS/PLACEHOLDERS: You must write the actual file creation code (e.g., using `df.to_csv()` or `json.dump()`). Do not leave comments saying "generate data here".
2. FOLDER HIERARCHY: You MUST create nested folders for each subject (e.g., `./dataset/sub-01/`) using `os.makedirs(..., exist_ok=True)`.
3. MOCK FILES: Inside EVERY subject's folder, you must generate and save the relevant mock data files.
4. RELATIVE PATHS: Always save data to the current directory (use paths starting with `./`).
5. MARKDOWN FORMAT: You MUST wrap your code in a standard Markdown code block (starting with ```python and ending with ```).

STEP 1: ANALYSIS
Read the provided paper text and repo tree to identify the variables and folder structure.

STEP 2: WRITE THE SCRIPT
Write the Python script fulfilling all the critical instructions above.
Add `print("SUCCESS: Full dataset generated.")` at the very end of your script.
CRITICAL: DO NOT write the word TERMINATE in this message. You must wait for the CodeExecutor to run the code.

STEP 3: TERMINATION
Once you receive a message from the CodeExecutor saying "exitcode: 0", your job is DONE.
ONLY THEN should you reply with the exact word TERMINATE and nothing else.
"""


# ==========================================
# 3. THE TRIGGER FUNCTION — instantiates agents lazily
# ==========================================

def trigger_data_simulation(pdf_path: str, github_url: str = None):
    """Generate a simulated dataset for a paper.

    Agents are instantiated INSIDE this function so importing this module has
    no side effects (no OPENAI_API_KEY required at import time).
    """
    # Lazy imports — only paid when the function actually runs
    import autogen
    from autogen.coding import LocalCommandLineCodeExecutor

    llm_config = {
        "config_list": [{"model": "gpt-5-nano",
                         "api_key": os.environ["OPENAI_API_KEY"]}],
    }

    print(f"Reading local paper: {pdf_path}...")
    paper_text = extract_pdf_text(pdf_path)

    github_context = "No GitHub repository provided."
    if github_url:
        print(f"Fetching GitHub repo tree from {github_url}...")
        github_context = get_github_tree(github_url)

    os.makedirs("simulated_data_output", exist_ok=True)

    data_simulator_agent = autogen.AssistantAgent(
        name="DataSimulatorAgent",
        system_message=SIMULATOR_SYSTEM_PROMPT,
        llm_config=llm_config,
    )

    local_executor = LocalCommandLineCodeExecutor(
        timeout=300,
        work_dir="simulated_data_output",
    )

    code_executor_agent = autogen.UserProxyAgent(
        name="CodeExecutor",
        human_input_mode="NEVER",
        max_consecutive_auto_reply=10,
        is_termination_msg=lambda x: (x.get("content") or "").rstrip().endswith("TERMINATE"),
        code_execution_config={"executor": local_executor},
        system_message=(
            "You execute the Python code provided by the DataSimulatorAgent. "
            "If the execution succeeds (exitcode: 0), append this to your output: "
            "'The code ran successfully. You MUST now reply with TERMINATE.'"
        ),
    )

    autogen.agentchat.register_function(
        read_github_file,
        caller=data_simulator_agent,
        executor=code_executor_agent,
        name="read_github_file",
        description="Read the exact content of a specific file in the GitHub repo.",
    )

    prompt = f"""
The original data for this paper is unavailable. Please generate a simulated dataset.

PAPER CONTEXT:
{paper_text}

GITHUB REPOSITORY: {github_url}
{github_context}

If the repository has files that look like they contain the data schema or modeling code, use your `read_github_file` tool to inspect them before writing your simulation code.
"""

    print("Starting Simulation...\n")
    code_executor_agent.initiate_chat(
        data_simulator_agent,
        message=prompt,
    )


# ==========================================
# 4. CLI ENTRY POINT
# ==========================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate a simulated dataset from a paper, optionally guided by a GitHub repo."
    )
    parser.add_argument("pdf_path", help="Path to the PDF file")
    parser.add_argument(
        "github_url",
        nargs="?",
        default=None,
        help="Optional GitHub repository URL",
    )
    args = parser.parse_args()

    trigger_data_simulation(pdf_path=args.pdf_path, github_url=args.github_url)
