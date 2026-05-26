#!/usr/bin/env python3
"""
run.py — Paper Pipeline Orchestrator (Claude Code CLI 版)

設計
----
* 每個 Agent 都用 `claude -p` 子程序呼叫 —— 每次 spawn 都是**全新 session**，
  彼此之間沒有對話記憶；唯一的「共同記憶」是 projects/<name>/ 底下的檔案。
* Orchestrator 本身不呼叫 LLM、不做網路請求 —— 只負責流程控制與檔案搬移。
  搜尋、下載、PDF 讀取、檔案寫入全部由 Claude Code 內建工具完成。

Usage
-----
    python run.py <project_name>
    python run.py <project_name> --max-papers 3      # 測試用
    python run.py <project_name> --skip-finder       # 重用既有 _inbox
    python run.py <project_name> --resume-from 5     # 從 Step 5 開始
"""

import argparse
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Windows 中文系統的 cp950 console 編碼會炸 emoji，強制 stdout/stderr 用 UTF-8
# 另外加 line_buffering，避免 stdout 被導向檔案時 block buffering 導致看不到即時進度
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)

# ============================================================
# Config
# ============================================================
ROOT = Path(__file__).parent
AGENTS_DIR = ROOT / "agents"
PROJECTS_DIR = ROOT / "projects"

MODEL = "claude-sonnet-4-6"
MAX_PAPERS = 10
MAX_REVISIONS = 2          # 加上 v1 共 3 版（v1, v2, v3 強制收件）


# ============================================================
# Timing tracker —— 紀錄每個 step 花多久，最後印 breakdown
# ============================================================
class TimingTracker:
    def __init__(self):
        self.steps = []      # list of (name, duration_sec)
        self.start = time.time()

    def step(self, name: str):
        return _StepTimer(self, name)

    def _record(self, name: str, duration: float):
        self.steps.append((name, duration))
        total = time.time() - self.start
        print(f"   ⏱  step: {duration/60:5.1f}m  |  pipeline total: {total/60:5.1f}m")

    def report(self):
        if not self.steps:
            return
        total = sum(d for _, d in self.steps)
        print(f"\n{'─' * 60}")
        print(f"  ⏱  Time breakdown")
        print(f"{'─' * 60}")
        for name, dur in self.steps:
            pct = dur / total * 100 if total > 0 else 0
            bar_len = min(int(pct / 2.5), 40)
            bar = "█" * bar_len + "░" * (40 - bar_len)
            print(f"  {name:30s}  {dur/60:5.1f}m  {pct:4.1f}%  {bar}")
        print(f"  {'─' * 56}")
        print(f"  {'TOTAL':30s}  {total/60:5.1f}m")


class _StepTimer:
    def __init__(self, tracker: TimingTracker, name: str):
        self.tracker = tracker
        self.name = name
        self.result = None

    def __enter__(self):
        self.t0 = time.time()
        return self

    def __exit__(self, *exc):
        self.tracker._record(self.name, time.time() - self.t0)


# ============================================================
# THE single entry point to Claude Code
# 每次呼叫 = 全新子程序 = 全新 session = 完全沒有歷史
# ============================================================
def call_agent(project_path: Path, agent_name: str, user_prompt: str) -> int:
    agent_md = AGENTS_DIR / agent_name.replace("-", "_") / "agent.md"
    if not agent_md.exists():
        sys.exit(f"❌ 找不到 agent 定義：{agent_md}")

    cmd = [
        "claude",
        "-p", user_prompt,
        "--append-system-prompt-file", str(agent_md),
        "--model", MODEL,
        "--dangerously-skip-permissions",
        "--output-format", "text",
    ]

    print(f"\n   🤖 [{agent_name}] spawning fresh Claude Code session")
    print(f"      cwd = {project_path}")
    t0 = time.time()

    # 不 capture，讓 Claude Code 的進度直接 stream 到使用者終端機
    result = subprocess.run(cmd, cwd=str(project_path), check=False)

    elapsed = time.time() - t0
    if result.returncode != 0:
        sys.exit(f"\n❌ [{agent_name}] 異常退出 (rc={result.returncode})")
    print(f"   ✅ [{agent_name}] done in {elapsed:.1f}s")
    return result.returncode


# ============================================================
# Step 1: Validate
# ============================================================
def step_validate(project_path: Path):
    print(f"\n[Step 1] Validating {project_path.name}")
    req = project_path / "Requirement.md"
    if not req.exists():
        sys.exit(f"❌ {req} not found")
    content = req.read_text(encoding="utf-8")
    if content.count("<!--") > 5:
        print("   ⚠️  Requirement.md 看起來還是樣板（大量 <!-- 註解未刪）")
        if input("   仍要繼續？(y/N) ").strip().lower() != "y":
            sys.exit(1)

    # 若已有上次跑的 final/，先 archive 起來避免污染
    final_path = project_path / "final" / "summary_final.md"
    if final_path.exists():
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive = project_path / "final" / f"archive_{ts}"
        archive.mkdir(parents=True, exist_ok=True)
        shutil.move(str(final_path), str(archive / "summary_final.md"))
        # 順便把舊 summaries/ 一起搬
        sum_dir = project_path / "summaries"
        for old in list(sum_dir.glob("v*.md")):
            shutil.move(str(old), str(archive / old.name))
        print(f"   📦 舊結果已備份到 {archive.relative_to(project_path)}/")

    print("   ✅ Requirement.md OK")


# ============================================================
# Step 2: Paper Finder
# ============================================================
def step_paper_finder(project_path: Path):
    print(f"\n[Step 2] Paper Finder")
    prompt = f"""執行你 system prompt 中的完整 workflow。

## 環境
- 當前 cwd 是 project 資料夾
- `Requirement.md` 在當前目錄
- 輸出寫到 `papers/_inbox/` 與 `papers/_rejected/`
- Python 套件 `arxiv`、`semanticscholar`、`requests` 已預裝（用 Bash `python -c "..."` 或寫成 script 都可以）
- Scrapling 不一定有裝，若 `import scrapling` 失敗就略過 Google Scholar
- 上限 **{MAX_PAPERS} 篇** paper 進 `_inbox/`

## 任務
1. 讀 Requirement.md 抽 keywords / scope / exclude
2. 三來源並行搜尋 → 去重 → 依 Rubric 評分 → 取前 {MAX_PAPERS} 篇
3. 下載 PDF 到 `papers/_inbox/paper_001.pdf`（zero-pad 三位數）
4. 寫 `papers/_inbox/metadata.json` 與 `papers/_rejected/rejected.json`

完成後簡短回覆「DONE: <passed> papers in _inbox/」。
"""
    call_agent(project_path, "paper-finder", prompt)

    # 驗證
    inbox = project_path / "papers" / "_inbox"
    pdfs = list(inbox.glob("*.pdf"))
    if not pdfs:
        sys.exit("❌ Paper Finder 跑完但 _inbox/ 沒有 PDF")
    if not (inbox / "metadata.json").exists():
        sys.exit("❌ Paper Finder 沒寫 metadata.json")
    print(f"   ✅ {len(pdfs)} PDFs in _inbox/")


# ============================================================
# Step 3: mv _inbox → _reading
# ============================================================
def step_move_to_reading(project_path: Path):
    print(f"\n[Step 3] Moving _inbox → _reading")
    inbox = project_path / "papers" / "_inbox"
    reading = project_path / "papers" / "_reading"
    reading.mkdir(parents=True, exist_ok=True)
    moved = 0
    for pdf in inbox.glob("*.pdf"):
        shutil.move(str(pdf), str(reading / pdf.name))
        moved += 1
    print(f"   ✅ moved {moved} PDFs")


# ============================================================
# Step 4: Grad Student Phase 1 (一篇一個新 session)
# ============================================================
def step_grad_student_phase1(project_path: Path):
    print(f"\n[Step 4] Grad Student Phase 1 (per paper, fresh session each)")
    reading = project_path / "papers" / "_reading"
    pdfs = sorted(reading.glob("*.pdf"))
    if not pdfs:
        sys.exit("❌ _reading/ 沒有 PDF 可讀")

    for i, pdf in enumerate(pdfs, 1):
        paper_id = pdf.stem
        kp_file = project_path / "key_points" / f"{paper_id}.md"
        if kp_file.exists():
            print(f"\n   ⏭  [{i}/{len(pdfs)}] {paper_id} 已存在 key_points，跳過")
            continue

        print(f"\n   📖 [{i}/{len(pdfs)}] {paper_id}")
        prompt = f"""**Phase 1**：執行你 system prompt 中的 Phase 1 workflow。

## 任務
1. Read `Requirement.md`（記住 research questions）
2. Read `papers/_reading/{paper_id}.pdf`（按 IMRD 順序：Abstract → Intro → Discussion → Results → Methods → References）
3. 對照 Requirement 重新給 relevance_score 與 quality_score
4. **Write `key_points/{paper_id}.md`** —— 完整符合你 system prompt 的 Phase 1 schema（含 frontmatter）
5. 用 Bash `mv papers/_reading/{paper_id}.pdf papers/_done/` 把 PDF 移走

完成後簡短回覆「DONE: key_points/{paper_id}.md」。
"""
        call_agent(project_path, "grad-student", prompt)

        # 驗證
        if not kp_file.exists():
            sys.exit(f"❌ Grad Student 沒寫 {kp_file}")
        if pdf.exists():
            print(f"   ⚠️  PDF 沒被搬走，orchestrator 補搬")
            (project_path / "papers" / "_done").mkdir(parents=True, exist_ok=True)
            shutil.move(str(pdf), str(project_path / "papers" / "_done" / pdf.name))


# ============================================================
# Step 5: TA (per paper, 把 key_points 改寫成 textbook 風格教學文)
# ============================================================
def step_ta(project_path: Path):
    print(f"\n[Step 5] TA (per paper, fresh session each)")
    kp_dir = project_path / "key_points"
    pa_dir = project_path / "paper分析"
    pa_dir.mkdir(parents=True, exist_ok=True)

    kp_files = sorted(kp_dir.glob("paper_*.md"))
    if not kp_files:
        print(f"   ⚠️  key_points/ 是空的，跳過 TA step")
        return

    for i, kp in enumerate(kp_files, 1):
        paper_id = kp.stem  # e.g., "paper_001"

        # Check if already analyzed (any file matching paper_XXX_*.md)
        existing = list(pa_dir.glob(f"{paper_id}_*.md"))
        if existing:
            print(f"\n   ⏭  [{i}/{len(kp_files)}] {paper_id} 已有教學文 ({existing[0].name})，跳過")
            continue

        print(f"\n   📖 [{i}/{len(kp_files)}] {paper_id}")
        prompt = f"""**TA task**：讀 `key_points/{paper_id}.md` 並依你 system prompt 的 Output Template 寫教學文。

## 任務
1. Read `key_points/{paper_id}.md`
2. 從 frontmatter + 1-Sentence Summary 找出此 paper 的核心技術名稱（例：PAIRED、SFL、NAVIX）
3. Write `paper分析/{paper_id}_<MethodName>.md`，**完整遵守 Critical Rules**（無 emoji、無比喻、無 performative 段落、直接定義開頭）
4. 若 key_points 標記 relevance=0/10（誤收錄），不要產出教學文，回覆 SKIPPED

完成後簡短回覆 `DONE: paper分析/{paper_id}_<MethodName>.md` 或 `SKIPPED: {paper_id}`。
"""
        call_agent(project_path, "ta", prompt)

        # Verify output
        new_files = list(pa_dir.glob(f"{paper_id}_*.md"))
        if not new_files:
            print(f"      ⚠️  {paper_id} 沒產生教學文（可能 SKIPPED，或失敗）")
        else:
            print(f"      ✅ wrote {new_files[0].name}")


# ============================================================
# Step 6: Grad Student Phase 2
# ============================================================
def step_grad_student_phase2(project_path: Path, version: int):
    print(f"\n[Step 6] Grad Student Phase 2 → summaries/v{version}.md")

    if version > 1:
        prev_review_note = (
            f"3. Read `summaries/v{version-1}_review.md`"
            f" —— 上一輪教授退件意見，**必須**逐條處理（[Must Fix] / [Should Fix]）\n"
            f"4. Write `summaries/v{version}.md`，**§6 Response to Previous Review**"
            f" 一節需明確回應每條 review"
        )
    else:
        prev_review_note = f"3. Write `summaries/v{version}.md`"

    prompt = f"""**Phase 2**：執行你 system prompt 中的 Phase 2 workflow，產出 `summaries/v{version}.md`。

## 任務
1. Read `Requirement.md`
2. Read 所有 `key_points/*.md`（一篇一個檔）
{prev_review_note}

依你 system prompt 的 Phase 2 schema 完整輸出（含 frontmatter）。

完成後簡短回覆「DONE: summaries/v{version}.md」。
"""
    call_agent(project_path, "grad-student", prompt)

    out = project_path / "summaries" / f"v{version}.md"
    if not out.exists():
        sys.exit(f"❌ Grad Student Phase 2 沒寫 {out}")


# ============================================================
# Step 7: Professor
# ============================================================
def step_professor(project_path: Path, version: int, is_final_round: bool) -> str:
    print(f"\n[Step 7] Professor reviewing v{version}.md (final_round={is_final_round})")

    force_note = ""
    if is_final_round:
        force_note = (
            f"\n⚠️ **這是 v{version}（已退 {version-1} 次），不可再退件。**"
            f"若未達 75 分，**必須** force_approve —— 寫 `final/summary_final.md`"
            f" 並在 frontmatter 標 `force_approved: true` 與 `needs_human_review: true`，"
            f"把未解決問題列在 `unresolved_issues:`。\n"
        )

    if version > 1:
        prev_review_note = (
            f"3. Read `summaries/v{version-1}_review.md` —— 你上輪寫的，"
            f"比對 grad-student 是否在 v{version}.md §6 處理上輪意見"
        )
    else:
        prev_review_note = ""

    prompt = f"""請審 `summaries/v{version}.md`。
{force_note}
## 任務
1. Read `Requirement.md`（特別是「通過標準 (Acceptance Criteria)」）
2. Read `summaries/v{version}.md`
{prev_review_note}

依你 system prompt 的 Rubric 對 5 個維度打分，計算總分（≥75 通過）。

## 輸出（擇一）
- **通過** → Write `final/summary_final.md`（含通過 schema 的 frontmatter）
- **退件** → Write `summaries/v{version}_review.md`（含 verdict: revise 的 frontmatter）
- **強制收件**（僅 is_final_round=true 時可用）→ Write `final/summary_final.md`
  且 frontmatter 含 `force_approved: true`

**只能寫上述其中一個檔案，不要兩個都寫。**

完成後簡短回覆「DONE: <approved|revise|force_approved>」。
"""
    call_agent(project_path, "professor", prompt)

    # 用檔案存在性判斷 verdict（比解析 stdout 可靠）
    final_path = project_path / "final" / "summary_final.md"
    review_path = project_path / "summaries" / f"v{version}_review.md"

    if final_path.exists():
        content = final_path.read_text(encoding="utf-8")
        if "force_approved: true" in content or "force_approved:true" in content:
            return "force_approved"
        return "approved"
    elif review_path.exists():
        return "revise"
    else:
        sys.exit(f"❌ Professor 沒寫任何預期檔案"
                 f"（既無 final/summary_final.md 也無 v{version}_review.md）")


# ============================================================
# Main
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="Paper Pipeline Orchestrator (Claude Code CLI)")
    parser.add_argument("project_name", help="projects/ 底下的子資料夾名稱")
    parser.add_argument("--skip-finder", action="store_true",
                        help="跳過 Paper Finder，重用既有 _inbox 內容")
    parser.add_argument("--max-papers", type=int, default=None,
                        help="覆寫 Paper Finder 上限（測試用，預設 10）")
    parser.add_argument("--resume-from", type=int, default=1, choices=[1, 2, 3, 4, 5, 6, 7],
                        help="從第幾步開始（中斷後續跑用）；5=TA, 6=Phase2, 7=Professor")
    args = parser.parse_args()

    global MAX_PAPERS
    if args.max_papers:
        MAX_PAPERS = args.max_papers

    project_path = PROJECTS_DIR / args.project_name
    if not project_path.exists():
        sys.exit(f"❌ {project_path} 不存在。\n"
                 f"   先執行：Copy-Item -Recurse projects\\_template projects\\{args.project_name}")

    # 確認 claude CLI 在 PATH
    if shutil.which("claude") is None:
        sys.exit("❌ 找不到 `claude` 指令。請確認 Claude Code CLI 已安裝且在 PATH 中。")

    print(f"\n{'=' * 60}")
    print(f"  Paper Pipeline (Claude Code CLI mode)")
    print(f"  Project: {args.project_name}")
    print(f"  Resume from: Step {args.resume_from}")
    print(f"{'=' * 60}")

    tracker = TimingTracker()

    if args.resume_from <= 1:
        with tracker.step("1. Validate"):
            step_validate(project_path)
    if args.resume_from <= 2 and not args.skip_finder:
        with tracker.step("2. Paper Finder"):
            step_paper_finder(project_path)
    if args.resume_from <= 3:
        with tracker.step("3. mv → _reading"):
            step_move_to_reading(project_path)
    if args.resume_from <= 4:
        with tracker.step("4. Phase 1 (all papers)"):
            step_grad_student_phase1(project_path)
    if args.resume_from <= 5:
        with tracker.step("5. TA (all papers)"):
            step_ta(project_path)

    # Step 6 + 7: 退件迴圈
    version = 1
    while True:
        if args.resume_from <= 6 or version > 1:
            with tracker.step(f"6. Phase 2 v{version}"):
                step_grad_student_phase2(project_path, version)
        is_final = (version >= 1 + MAX_REVISIONS)        # v3 為強制收件輪
        with tracker.step(f"7. Professor v{version}"):
            verdict = step_professor(project_path, version, is_final_round=is_final)

        if verdict in ("approved", "force_approved"):
            break
        version += 1
        if version > 1 + MAX_REVISIONS:                  # 保險（理論上 force_approve 已 break）
            break

    tracker.report()

    print(f"\n{'=' * 60}")
    print(f"  ✅ Pipeline complete!")
    print(f"  📄 Final: {project_path / 'final' / 'summary_final.md'}")
    print(f"  📊 Logs: {project_path / 'logs'}/ (Claude Code session logs)")
    print(f"{'=' * 60}\n")


if __name__ == "__main__":
    main()
