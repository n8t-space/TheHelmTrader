# TradeBot — Windows 11 Project Context

> **Purpose of this document:** Seed a fresh Claude project on Windows 11 with everything learned from the macOS + Parallels build of TradeBot. Drop this into the new project as project knowledge so Claude starts informed instead of starting from zero.

---

## 1. Working Agreement (read this first)

This is non-negotiable context for how I want to work:

- **I build it. You guide and teach.** You are the architect; I am the engineer doing the work. Hand me explanations, not finished systems.
- **Local-first, fully offline.** No cloud APIs, no internet dependencies in the runtime path. Internet is only acceptable for one-time installs (Python, Ollama, model pulls).
- **Documentation is a deliverable, not an afterthought.** Every major change updates the relevant doc in the same turn the code changes.
- **Pragmatic over perfect.** The right tool for the job. Native/simple before complex/clever. Working beats elegant.
- **Be precise about scope.** When you give me code, be explicit: full file replacement, partial patch, or new file. I will catch inconsistencies and push back. Save us both time by being clear up front.
- **Less-code tools are welcome** where they earn their place (Task Scheduler, AutoHotkey, Power Automate, etc.) — don't reach for a Python library when Windows already ships the answer.

---

## 2. Project Goal

Build a **local, offline, AI-powered trading signal automation system** that:

1. Captures screenshots of a trading application window
2. Sends those screenshots to a **local LLM** (Ollama / Mistral 7B) for analysis
3. Extracts and stores structured trade signals
4. Surfaces signals through a local dashboard
5. Runs on a schedule, unattended, on a single Windows 11 machine

No SaaS. No cloud. No telemetry. Everything on the box.

---

## 3. Current State (carried over from macOS build)

### 3.1 What was built on macOS

Five core Python modules — these are the architectural primitives, the names will carry over:

| Module | Responsibility |
|---|---|
| `screenshot_capturer.py` | Captures a frame of the trading app |
| `local_llm_analyzer.py` | Sends image + prompt to local Ollama, parses response |
| `signal_storage.py` | Persists structured signals (timestamp, signal type, confidence, raw text) |
| `dashboard.py` | Local web UI to review signals |
| `trading_bot.py` | Orchestrator — schedules captures, calls analyzer, writes storage |

Plus a startup launcher (`_Run_Bot.sh` on macOS — **this becomes a `.ps1` or `.bat` file on Windows**, see §7).

### 3.2 Tech stack carrying over

- **Python** with a virtual environment (named `trading_env`)
- **Ollama** running **Mistral 7B** locally
- **PIL / Pillow** for image work
- **`requests`** for local Ollama HTTP API calls
- **`schedule`** library for recurring task management
- **VS Code** as primary editor with project-level `.vscode/` config

### 3.3 Resolved issues (don't re-fight these battles)

- **Python externally-managed environment errors** → solved by always using a venv. On Windows this is `python -m venv trading_env` then `.\trading_env\Scripts\activate`. Never `pip install` outside the venv.
- **`pip` invocation** → on macOS we aliased `pip` to `python3 -m pip`. On Windows, inside an activated venv, plain `pip` works correctly. No alias needed.

### 3.4 The macOS pain point that **goes away** on Windows 11

On macOS, the trading app was inside a **Parallels Windows 11 VM**. This meant:
- `pygetwindow` couldn't see VM windows
- AppleScript automation against Parallels timed out
- We fell back to **full-screen capture with `ImageGrab.grab()`** as a working compromise

**On Windows 11 native, this entire problem disappears.** The trading app is a normal Win32 (or UWP) window and can be addressed directly. See §7.2 for what to use instead.

### 3.5 Open items left over

- Multi-terminal launch via `osascript` was unresolved on macOS — **irrelevant on Windows.** Use Windows Terminal tabs/panes or just launch each component as a Scheduled Task. Don't port the `osascript` approach.
- VS Code workspace config (`.vscode/settings.json`, `.vscode/launch.json`) and project meta-files (`README.md`, `ARCHITECTURE.md`, `SETUP.md`, `USAGE.md`, `requirements.txt`, `.gitignore`) — re-create these fresh on Windows. Don't try to port macOS paths.

---

## 4. Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Windows 11 Host                          │
│                                                             │
│  ┌──────────────┐    ┌──────────────────┐                  │
│  │ Trading App  │◄───┤ screenshot_      │                  │
│  │ (native win) │    │ capturer.py      │                  │
│  └──────────────┘    └────────┬─────────┘                  │
│                                │ PNG bytes                 │
│                                ▼                           │
│                      ┌──────────────────┐                  │
│                      │ local_llm_       │   HTTP           │
│                      │ analyzer.py      │◄──────► Ollama   │
│                      └────────┬─────────┘  localhost:11434 │
│                                │ structured signal         │
│                                ▼                           │
│                      ┌──────────────────┐                  │
│                      │ signal_          │                  │
│                      │ storage.py       │──► SQLite/JSONL  │
│                      └────────┬─────────┘                  │
│                                │                           │
│                                ▼                           │
│                      ┌──────────────────┐                  │
│                      │ dashboard.py     │──► localhost:5000│
│                      └──────────────────┘                  │
│                                                             │
│  Orchestrator: trading_bot.py (uses `schedule` lib)        │
└─────────────────────────────────────────────────────────────┘
```

Data flow per tick:
**capture → analyze → store → (dashboard reads on demand)**

Each module is independently testable. Don't tightly couple them.

---

## 5. Recommended Project Layout (Windows)

```
C:\TradeBot\
├── trading_env\              ← Python virtual environment (don't commit)
├── src\
│   ├── screenshot_capturer.py
│   ├── local_llm_analyzer.py
│   ├── signal_storage.py
│   ├── dashboard.py
│   └── trading_bot.py
├── data\
│   ├── screenshots\          ← captured frames (rotate/expire these)
│   └── signals.db            ← SQLite or JSONL
├── docs\
│   ├── README.md
│   ├── ARCHITECTURE.md
│   ├── SETUP.md
│   ├── USAGE.md
│   └── CHANGELOG.md
├── scripts\
│   ├── run_bot.ps1           ← startup launcher (replaces _Run_Bot.sh)
│   └── stop_bot.ps1
├── .vscode\
│   ├── settings.json
│   └── launch.json
├── requirements.txt
└── .gitignore
```

> Pick a path that doesn't require admin rights. `C:\TradeBot\` works if you create it under your user, or use `C:\Users\<you>\TradeBot\`. Avoid `Program Files` (admin/UAC headaches) and avoid OneDrive-synced folders (sync conflicts on the SQLite file).

---

## 6. Tech Stack — Windows 11 Specifics

| Concern | Choice | Notes |
|---|---|---|
| Python | 3.11 or 3.12 | 3.14 worked on macOS but is bleeding-edge; 3.11/3.12 has the widest wheel support on Windows. Skip 3.13/3.14 unless you have a reason. |
| Virtual env | `python -m venv trading_env` | Activate: `.\trading_env\Scripts\Activate.ps1` (PowerShell) or `trading_env\Scripts\activate.bat` (cmd) |
| LLM runtime | Ollama for Windows | Native `.exe` installer. Runs as a background service on `http://localhost:11434`. |
| Model | Mistral 7B | `ollama pull mistral` — keep the same model so prompt work transfers. |
| Screen capture | **`mss`** (preferred) or `pywin32` for window-specific | See §7.2. Don't default to `ImageGrab` here — you have better options now. |
| HTTP client | `requests` | Same as before. |
| Scheduling (in-process) | `schedule` | Same as before. |
| Scheduling (OS-level) | **Windows Task Scheduler** | Replaces macOS LaunchAgent. Use this for "start bot on login." |
| Dashboard | Flask or FastAPI, localhost only | Bind to `127.0.0.1`, not `0.0.0.0`, so it's not exposed on your network. |
| Storage | SQLite (`sqlite3` stdlib) or JSONL | SQLite if you want to query/filter; JSONL if you want simplicity and grep-ability. |
| Editor | VS Code | Same. Install the Python extension. |
| Shell | PowerShell 7 | Replaces zsh. `.ps1` scripts replace `.sh` scripts. |

---

## 7. Migration Notes — What Actually Changes

### 7.1 Paths

Every macOS path needs translation:

| macOS | Windows 11 |
|---|---|
| `/Volumes/AssistantSSD/TradeBot` | `C:\TradeBot\` (or wherever you put it) |
| `~/.zshrc` | PowerShell `$PROFILE` |
| `/usr/bin/python3` | `python` (inside activated venv) |
| `chmod +x script.sh` | n/a — Windows uses file extensions, not exec bits |

In Python, **always use `pathlib.Path`** and let it handle separators. Never hardcode `\\` or `/`.

### 7.2 Screen capture — the big upgrade

This is where Windows-native is dramatically better than macOS+Parallels was. Three options, in order of preference:

1. **`mss`** — fastest, works for full-screen and arbitrary regions. Drop-in for what `ImageGrab.grab()` did, but ~5–10× faster. Good default.
2. **`pygetwindow` + `mss`** — find the trading app window by title, get its bounding box, capture just that region. This is what you wanted on macOS and couldn't get. **You can have it now.**
3. **`pywin32`** (the `win32gui` module) — lowest-level access, can capture occluded/background windows in some cases. Reserve for if (1) and (2) don't satisfy.

> Don't keep using full-screen `ImageGrab.grab()` out of habit. The reason for that fallback is gone. Move to window-specific capture early — it makes the LLM's job easier (less irrelevant pixels) and your prompts smaller.

### 7.3 Startup launcher

`_Run_Bot.sh` becomes `scripts\run_bot.ps1`:

```powershell
# scripts\run_bot.ps1 — example shape, you'll write the real one
Set-Location "C:\TradeBot"
.\trading_env\Scripts\Activate.ps1
python .\src\trading_bot.py
```

For "run on login," **don't** try to be clever — register it as a Windows Task Scheduler task with trigger "At log on of <your user>." The macOS LaunchAgent equivalent. Built-in, no extra software.

### 7.4 PowerShell execution policy

First time you run a `.ps1` script, Windows will block it. Fix once, system-wide for your user:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

Document this in `SETUP.md`. It's the kind of thing that bites you 6 months later when you forget.

### 7.5 Firewall / loopback

The dashboard on `127.0.0.1:5000` and Ollama on `127.0.0.1:11434` are loopback — Windows Defender Firewall does **not** block loopback by default, so you should be fine. If something seems blocked, check that you're binding to `127.0.0.1` and not `0.0.0.0`.

---

## 8. Roadmap — Model Quality Improvements

The four-stage progression carries over unchanged. Tackle them **in order**; don't skip ahead:

1. **Prompt optimization** — biggest ROI for least effort. Iterate the system prompt against a small labeled set of past screenshots until you hit a plateau.
2. **Few-shot learning** — include 3–5 example (screenshot → correct signal) pairs in the prompt. Plateau again.
3. **RAG (retrieval-augmented generation)** — when the prompt gets too long, move examples to a local vector store and retrieve the most relevant ones per call. Local embedding model via Ollama keeps it offline.
4. **Fine-tuning** — only after (1)–(3) are exhausted. Fine-tune Mistral (LoRA) on your accumulated labeled data. Highest cost, biggest commitment, save for last.

Target arc: ~3 months from baseline to high reliability. Track accuracy in `signals.db` so you can measure each stage's lift instead of guessing.

---

## 9. Documentation Standards

### 9.1 Color palette (project-wide)

All visual documentation, dashboards, diagrams, and rendered docs use a **red / black / white** palette:

| Role | Color | Hex |
|---|---|---|
| Primary text, structure | Black | `#000000` |
| Background, surface | White | `#FFFFFF` |
| Accent — signals, alerts, critical actions | Red | `#D32F2F` (or pure `#FF0000` for max contrast) |

Keep it disciplined. Two neutrals + one accent. No drift into greens, blues, etc., even when "it would look nice." The constraint is the point.

### 9.2 Required docs and when to update them

| Doc | Contents | Update trigger |
|---|---|---|
| `README.md` | One-paragraph what-it-is, quick start, links to other docs | Major feature adds |
| `ARCHITECTURE.md` | Module responsibilities, data flow, key design decisions | Any architectural change |
| `SETUP.md` | Step-by-step Windows install (Python, venv, Ollama, model pull, first run) | Any install/config change |
| `USAGE.md` | How to run, how to read signals, how to stop, troubleshooting | Any operational change |
| `CHANGELOG.md` | Dated entries: what changed, why | **Every** change |

Rule of thumb: if a future me opens this in 6 months and can't figure out what to do, the docs failed.

---

## 10. How to Use Claude in the New Project

When you start the Windows 11 project, lead with this document as project knowledge. Then on the first real task, set the tone explicitly:

> "Acting as my AI architect — I'll build, you guide. Walk me through [first task] step by step. Be explicit about whether code is a full file or a patch. Update [relevant doc] in the same response."

Patterns that have worked:
- **One concept per turn.** Don't let conversations cover three unrelated changes — they get inconsistent.
- **Ask "why" liberally.** If a recommendation feels arbitrary, push for the reason. Real architects can defend their calls.
- **Reject over-engineering.** If the answer reaches for a library when Task Scheduler / AutoHotkey / a 10-line script would do, push back.
- **Ship the docs in the same turn as the code.** Otherwise they drift.

---

## 11. First Concrete Tasks (suggested order)

When you spin up the new project, here's a sane sequence:

1. Install Python 3.11/3.12, create `C:\TradeBot\`, create venv, set PowerShell execution policy.
2. Install Ollama for Windows, `ollama pull mistral`, verify with a curl/Invoke-RestMethod against `localhost:11434`.
3. Write `screenshot_capturer.py` using `mss` + `pygetwindow` to capture the trading app window by title. This is the part that *couldn't* work before — get it working first.
4. Write `local_llm_analyzer.py` — port the existing prompt, send a test screenshot, parse response.
5. Write `signal_storage.py` (start with SQLite — `sqlite3` is stdlib, no dep).
6. Write `trading_bot.py` orchestrator using `schedule`.
7. Write `dashboard.py` (Flask, bind `127.0.0.1`).
8. Write `scripts\run_bot.ps1` and register as Task Scheduler task.
9. Write all five docs in §9.2 as you go — not at the end.

Don't skip ahead. The order matters because each step de-risks the next one.

---

## 12. Things to Explicitly Not Do

- Don't reintroduce full-screen capture as the default. Window-specific is achievable now.
- Don't use Python 3.14 on Windows just because it worked on macOS. Use 3.11 or 3.12.
- Don't bind the dashboard to `0.0.0.0`. Loopback only.
- Don't put the project in OneDrive / iCloud / Dropbox folders. SQLite + cloud sync = corruption.
- Don't add cloud dependencies "just for one thing." The whole point is offline.
- Don't write code without updating docs in the same turn.
- Don't let Claude hand you a finished system. You're building this to understand it.

---

*End of context document. Drop this into the new Windows 11 project's knowledge base before the first conversation.*