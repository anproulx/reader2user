# PaperRoute

This repository provides an automated, multi-agent framework powered by AG2 to bridge the gap between AI research papers and code reproducibility. It orchestrates a specialized pipeline of autonomous agents to discover codebases, analyze environment requirements, adapt to custom datasets, and execute experiments with human oversight.

The workflow is structured as follows:
1. Discovery & Audit
- Researcher Agent: Extracts GitHub repository links and official implementation details from PDF/text inputs.
- Analyst Agent: Scans the codebase to identify file structures, hidden dependencies, and potential runtime "landmines" (missing weights, deprecated APIs).

2. Strategic Gateway (HITL)
- Human-Admin: Reviews the audit report. The system pauses for the human to approve the run and decide between a Sample Run (using provided repo data) or a Custom Run (using the user's specific dataset).

3. Alignment & Execution
- Adapter Agent: (Triggered for custom data) Bridges the gap by mapping new data schemas to the repository's expected input format and resolving pathing conflicts.
- Runner Agent: Operates in a secure container to git clone, parse imports to reconstruct missing environments (beyond requirements.txt), and execute scripts. It captures all stderr logs for debugging.

4. Verification
- Reproducibility Agent: Performs a final statistical comparison between the local execution results and the metrics claimed in the original paper, flagging significant variances.

### Prerequisites
- Python 3.10+
- AG2 (AutoGen)
- Docker (Recommended for the Runner Agent)
- OpenAI API Key (or supported LLM provider)

### Clone the repository
git clone https://github.com/your-username/ag2-paper-repro.git
cd ag2-paper-repro

### Installation
''git clone https://github.com/your-username/PaperRoute.git
cd PaperRoute
pip install -r requirements.txt''

### Usage 
python main.py --paper "https://arxiv.org/pdf/xxxx.xxxx.pdf"

### Outputs
-Environment Log: A record of all dependencies installed.
- Execution Trace: Full logs of the code run.
- Reproducibility Report: A comparison table of Original Paper Results vs. Your Results.
