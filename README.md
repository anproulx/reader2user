# Paper2Run

This repository provides an automated, multi-agent framework powered by AG2 to bridge the gap between AI research papers and code reproducibility. It orchestrates a specialized pipeline of autonomous agents to discover codebases, analyze environment requirements, adapt to custom datasets, and execute experiments with human oversight.

The workflow is structured into five distinct phases:
- **Discovery & Analysis**: A Researcher Agent identifies the official GitHub repository linked to a paper, while an Analyst Agent audits the file structure and predicts potential environment conflicts or missing assets.
- **Human-in-the-Loop (HITL)**: The system pauses for a human administrator to review the audit, decide whether to proceed, and specify the data source (Sample vs. Custom).
- **Data Alignment**: If custom data is provided, an Adapter Agent maps schema differences and resolves pathing gaps between the target dataset and the repository’s expected format.
- **Autonomous Execution**: The Runner Agent operates in a secure environment to clone the repo, auto-detect dependencies from script imports, build the environment, and execute the code.
- **Reproducibility Audit**: A final Expert Agent performs a statistical comparison between the generated results and the original paper's claims to verify reproducibility.

### Prerequisites
- Python 3.10+
- AG2 (AutoGen)
- Docker (Recommended for the Runner Agent)
- OpenAI API Key (or supported LLM provider)

### Clone the repository
git clone https://github.com/your-username/ag2-paper-repro.git
cd ag2-paper-repro
