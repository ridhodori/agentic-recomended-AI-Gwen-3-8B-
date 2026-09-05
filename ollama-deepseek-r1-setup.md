# Running DeepSeek-R1 Locally with Ollama + Agentic AI Setup

Documentation for setting up local LLM inference and agentic AI on this machine.

## System Specs (confirmed)

| Component | Spec |
|---|---|
| GPU | NVIDIA GeForce RTX 5060 |
| VRAM | 8 GB (8151 MiB) |
| Driver | 610.88 |
| RAM | 32 GB |
| OS | Windows 11 Pro |

Confirmed via `nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv`.

Target model: `deepseek-r1:8b` (Qwen-based 8B distill, ~4.9 GB at default Q4 quantization) — fits fully in 8 GB VRAM with room for context.

---

## Step 1: Install Ollama

```bash
winget install Ollama.Ollama
```

Alternative: download the installer manually from [ollama.com/download](https://ollama.com/download).

Ollama runs as a background service (system tray icon) and exposes an API at `http://localhost:11434`.

Verify install (reopen terminal first):

```bash
ollama --version
```

## Step 2: Pull DeepSeek-R1 8B

```bash
ollama pull deepseek-r1:8b
```

Note: this is the 8B distill, not the full 671B R1 — the full model isn't runnable on consumer hardware.

## Step 3: Run it

```bash
ollama run deepseek-r1:8b
```

Interactive chat in the terminal. Type `/bye` to exit.

R1 is a **reasoning** model — it emits a `<think>...</think>` block before the actual answer, so responses are slower/longer than a plain chat model. This is expected behavior.

## Step 4: Verify GPU usage

While a prompt is running, in a second terminal:

```bash
nvidia-smi
```

Look for an `ollama.exe` / `ollama_llama_server` process holding GPU memory. Or:

```bash
ollama ps
```

Should show `100% GPU` since the model is smaller than available VRAM.

---

## Step 5: Agentic AI Options

Two paths to get tool-use / multi-step agent behavior on top of the local Ollama server.

### Option A — No-code (AnythingLLM)

[AnythingLLM](https://anythingllm.com) desktop app for Windows. Connects directly to local Ollama. Built-in "Agent Skills" (web search, web browsing, file summarization, code execution) enabled via slash commands in chat. No Python needed.

Fastest way to get an agent running.

### Option B — Build it yourself (Python)

Ollama exposes an OpenAI-compatible API, so any agent framework can target `localhost:11434`.

```bash
pip install ollama
```

Then use the `ollama` Python package directly, or point LangChain / CrewAI's `ChatOllama` at the local server.

**Caveat:** DeepSeek-R1-distill models have weaker native function/tool-calling support than models like Qwen2.5 or Llama3.1. For reliable tool-calling agents, consider pairing:
- `deepseek-r1:8b` for reasoning/planning steps
- A tool-calling capable model (e.g. `ollama pull qwen2.5:7b`) for the "acting" steps

Both fit in 8 GB VRAM individually (not simultaneously without offloading).

---

## Status / Next Steps

- [x] Ollama installed
- [x] `deepseek-r1:8b` pulled
- [ ] GPU usage confirmed via `nvidia-smi` / `ollama ps`
- [x] Agent path chosen: AnythingLLM tried, found unsuitable (see below)
- [ ] Agent framework set up and tested

---

## Why AnythingLLM wasn't enough

AnythingLLM is a chat/RAG app, not a coding agent. Its "agent skills" are a fixed menu
(web search, scrape a URL, run a sandboxed snippet) invoked by slash command — it can't
read/edit project files, run your code and react to real errors, or drive an actual
browser session. That's why it plateaus at chit-chat regardless of model.

## Model history / pivot (2026-09-03)

Started with `deepseek-r1:8b` (reasoning/chat) alongside a separate coder model
for agent work. Both were deleted and consolidated into a single model:
**`qwen3:8b`**. Rationale: R1-distill is a pure reasoning model with weak
tool-call reliability; Qwen3 is a hybrid reasoning model (also emits
`<think>...</think>` blocks, so expect similar verbosity/slower responses)
but was built with agentic tool-calling in mind, making it a better single
model for both plain chat and agent work rather than juggling two.

Current local models:

```
qwen3-agent:latest    5.2 GB   16k context — used by Cline / Open Interpreter
qwen3:8b              5.2 GB   base, default ~2k context — plain chat only
```

## Path 1 (recommended first): Cline — VS Code coding agent

VS Code 1.135.0 already installed on this machine.

1. Install the **Cline** extension in VS Code (publisher: saoudrizwan).
2. Cline panel → gear icon → API Provider: `Ollama`, Base URL: `http://localhost:11434`,
   Model: `qwen3:8b`.
3. **Set the context window** — Ollama defaults to 2048 tokens, which breaks Cline's
   large prompts almost immediately. Set Cline's "Context Window" field to `16384`
   (or `32768` if okay with slower CPU/RAM offload).
4. Give it a real bug in plain language. It reads the file, proposes an edit (you
   approve/reject), runs the code in the integrated terminal, reads the actual error,
   and adjusts — this is the real debug loop AnythingLLM couldn't do.
5. Browser testing is built in — when a task involves a running web app, Cline can
   launch a real Chromium session, click around, and read console output/screenshots
   to inform its next edit. No extra setup.

## Path 2 (later): browser-use — general web browsing agent

Separate use case from debugging: a general agent that navigates arbitrary websites
(research, form-filling, etc.), not tied to your own project.

```bash
pip install browser-use
playwright install chromium
```

Point it at Ollama via its `ChatOllama` integration instead of a cloud API key.

**Reliability note:** this is the weaker of the two paths on an 8B local model —
multi-step web navigation gives the model many more chances to pick a wrong action
than "edit this file, run this command" does. Expect flakiness at 8B. With 32GB
system RAM there's headroom to try a 14B model with partial CPU offload (slower,
more reliable) if 7-8B proves too unreliable for this specifically.

## qwen3:8b — setup walkthrough (completed)

1. Confirm pull: `ollama list` → should show `qwen3:8b`.
2. Sanity test: `ollama run qwen3:8b`, confirm response, check `ollama ps`
   shows `100% GPU`.
3. **Bake in a larger context window** (fixes Ollama's default 2048-token context,
   which is too small for agent use) via a custom Modelfile:

   ```
   FROM qwen3:8b
   PARAMETER num_ctx 16384
   ```

   ```bash
   ollama create qwen3-agent -f Modelfile
   ```

   This produces a new local tag `qwen3-agent` with 16k context as the
   default — works from any tool, not just Cline's per-session setting.
4. Cline config: Settings → API Provider `Ollama`, Base URL `http://localhost:11434`,
   Model `qwen3-agent`.
5. Test with a real (or deliberately broken) script — confirm the read → edit
   (approve/reject) → run → read error → iterate loop works end to end.
6. If `ollama ps` shows partial CPU offload during a real task (context pushed
   past 8GB VRAM), drop `num_ctx` to `8192` in the Modelfile and rebuild.

## Known issue

`ollama` CLI not recognized in a freshly opened terminal right after the winget
install — PATH likely needs a new login session to pick up the change. The
background Ollama service (tray icon) appears to still be reachable at
`localhost:11434` regardless (AnythingLLM was using it). Re-check with
`ollama --version` after a reboot/re-login if the CLI is still needed.
