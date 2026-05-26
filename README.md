# Paper Pipeline

An automated literature review pipeline that uses [Claude Code CLI](https://docs.claude.com/en/docs/agents-and-tools/claude-code/overview) as the agent runtime. Four specialized agents — Paper Finder, Grad Student, TA, Professor — collaborate to find papers, extract key points, write teaching-friendly analyses, and produce a peer-reviewed literature review for any given research question.

## Why this exists

Most LLM-based literature review tools either dump synthesized prose with no source traceability or read papers one at a time without cross-paper context. This pipeline separates concerns into distinct agents, each with a single responsibility, communicating only through files in the project folder. Every state transition is observable (you can read the intermediate files), every step is restartable, and the revision loop between Grad Student and Professor mirrors how real academic peer review works.

The architecture is documented in `讀Paper的PipeLine.drawio` (open in [draw.io](https://app.diagrams.net/)).

## Architecture

```
[User] → Requirement.md
    ↓
[Paper Finder Agent] → searches arXiv + Semantic Scholar (+ Google Scholar via Scrapling)
    ↓                   scores candidates, downloads top N PDFs
papers/_inbox/
    ↓ (orchestrator moves to _reading)
[Grad Student Agent] (Phase 1, per paper, fresh session each)
    ↓                   reads PDF using IMRD order, extracts structured key points
key_points/paper_*.md
    ↓
[TA Agent] (per paper, fresh session each)
    ↓                   rewrites IMRD key points into textbook-style teaching notes
paper分析/paper_*_<Method>.md
    ↓
[Grad Student Agent] (Phase 2)
    ↓                   compiles all key points into a synthesized summary
summaries/v1.md
    ↓
[Professor Agent]
    ↓                   reviews against rubric; ≥75 → approve, else → revise
final/summary_final.md       OR    summaries/v1_review.md → back to Phase 2 (v2, v3)
```

Each agent invocation is a **fresh Claude Code subprocess** (no session history). Inter-agent state lives only in files. The orchestrator (`run.py`) handles flow control, file moves, and revision loop logic.

## Setup

Requires Python 3.10+, [Claude Code CLI](https://docs.claude.com/en/docs/agents-and-tools/claude-code/overview) installed and authenticated, and an Anthropic subscription (the agents run through Claude Code, no API key needed in your environment).

```bash
git clone https://github.com/TyrannosaurusR/Paper-pipeline.git
cd paper-pipeline
python -m venv .venv
source .venv/bin/activate          # macOS / Linux
# .venv\Scripts\Activate.ps1       # Windows PowerShell
pip install -r requirements.txt
```

## Usage

1. Copy the template to start a new research topic:
   ```bash
   cp -r projects/_template projects/my-topic
   ```

2. Edit `projects/my-topic/Requirement.md` and fill in the 8 sections (research questions, keywords, scope, exclusions, acceptance criteria).

3. Run the pipeline:
   ```bash
   # Smoke test (2 papers, ~30 minutes)
   python run.py my-topic --max-papers 2

   # Full run (10 papers, ~2 hours)
   python run.py my-topic
   ```

4. Read the output: `projects/my-topic/final/summary_final.md` is the approved review; `projects/my-topic/paper分析/*.md` are per-paper teaching notes.

### Restart after interruption

```bash
python run.py my-topic --skip-finder       # skip paper search, reuse existing _inbox/
python run.py my-topic --resume-from 5     # restart from step 5 (TA stage)
```

Step numbers: 1=validate, 2=paper finder, 3=move to reading, 4=Grad Student Phase 1, 5=TA, 6=Grad Student Phase 2, 7=Professor.

## Customization

- **Agent behavior**: edit the relevant `agents/<name>/agent.md`. Each is a Markdown file with YAML frontmatter (name, model, tools) and a structured body (role, mission, rules, workflow, output format).
- **Model**: change `MODEL = "claude-sonnet-4-6"` at the top of `run.py`.
- **Revision limit**: change `MAX_REVISIONS = 2` (max professor rejections before force-approve).
- **Paper count**: change `MAX_PAPERS = 10` (default cap for Paper Finder).

## Output format

A completed project folder contains:

```
projects/my-topic/
├── Requirement.md                user-written research spec
├── papers/_done/*.pdf            downloaded papers
├── papers/_inbox/metadata.json   what was found, scores
├── papers/_rejected/rejected.json   what was filtered out
├── key_points/paper_*.md         IMRD-format extracts (per paper)
├── paper分析/paper_*.md          teaching-style analyses (per paper)
├── summaries/v1.md, v1_review.md, v2.md, ...   synthesis + revision history
└── final/summary_final.md        approved literature review
```

The `paper分析/` folder is in Chinese (zh-TW) by default, since the TA agent's prompt specifies a Chinese-language teaching style. To switch languages, edit `agents/ta/agent.md`.

## Costs

Running on a Claude subscription (Pro / Team), a single 10-paper pipeline run takes approximately 2 hours of wall-clock time and counts as several hundred to a few thousand messages against your usage quota. There is no API cost in addition to the subscription.

## Limitations

- **Single-agent literature reviews only.** No multi-perspective synthesis or contrarian review.
- **PDF extraction depends on Claude's vision capability**; complex figures and tables may be misread.
- **Paper Finder uses ad-hoc Python scripts** that the agent generates per run, not a fixed retrieval system. Results vary.
- **The Professor's rubric is hardcoded in `agents/professor/agent.md`**; modify it if your evaluation criteria differ.
- **PDFs in `papers/_done/` are not committed** in this public repo. If you fork and want to track research data, modify `.gitignore`.

## License

MIT. See `LICENSE`.

## Contributing

This is a personal project shared for reuse. Issues and PRs welcome but response time is not guaranteed.
