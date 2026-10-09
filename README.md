# Ayos

**A local AI technician for your PC. It finds out why your internet or computer is not working, shows you the proof, and fixes it with your approval. It runs on your own computer, so it works when the internet does not.**

"Ayos" is Filipino for *fixed* and for *all good*.

Built for the AppBuildersPH Hackathon 2026 (theme: Local AI).

## The problem

When the internet stops working, the tools that could help are on the internet. A cloud AI assistant cannot answer, a search engine cannot be reached, and the family member who "knows computers" is not home. Most of the causes are small settings a technician fixes in a minute: a wrong DNS server, a proxy left on, an adapter switched off, a line in the hosts file.

A repair assistant has to be local, because the fault it repairs is the connection itself.

## What Ayos does

1. You say what is wrong in your own words, in English or Taglish.
2. A language model running on your PC decides which check to run, reads the result, and decides again. Each check is shown as it happens.
3. The model names a cause. **Plain code then checks that the results really show that cause.** If they do not, the conclusion is refused and the model has to look again.
4. Ayos shows the cause, the evidence, and the exact change it wants to make.
5. Nothing changes until you press **Fix it**. After the fix, the checks run again to prove it worked. Every fix can be undone.
6. Ayos remembers what this PC looks like when it works, and what went wrong before. Next time it compares against that first.

```
  you ──"my internet is not working"──▶  local model (Ollama, on this PC)
                                              │ picks ONE check from a fixed menu
                                              ▼
                                   checks (read-only Python + PowerShell)
                                              │ result, in plain words
                                              ▼
                                   local model reads it, picks again ... names a cause
                                              │
                                              ▼
                              safety check (plain code, not the model):
                              "do the check results really show this cause?"
                                   no ──▶ refused, look again
                                   yes
                                              ▼
                              you approve ──▶ one named, reversible fix ──▶ checks run again
                                              │
                                              ▼
                              memory on this PC: what normal looks like, what broke before
```

## What runs locally

Everything. The language model runs on this computer through [Ollama](https://ollama.com). The checks, the safety check, the fixes and the memory are Python on this computer. Your question, your settings and the diagnosis never leave it.

The only network traffic Ayos makes is the diagnosis itself: it tries to reach a Microsoft test page, public DNS servers and your router, because testing the connection is the job. No cloud AI or other online service is used at any point.

## Why the model cannot break your PC

- The model never writes commands. Each turn it returns one small JSON object that picks from a fixed menu of 11 read-only checks or names one of 14 known causes. The menu is enforced by the model server's structured output, and checked again in code.
- A cause is only accepted if the check results support it (`ayos/rules.py`, `supports()`). A model that guesses is refused.
- Fix details (which adapter, which hosts line) come from the check results, never from the model's text.
- Only 9 fixes exist (`ayos/fixes.py`). Each one states exactly what it changes, needs your approval, and has an undo.
- The web page talks to a server on `127.0.0.1` only, and every action needs a secret token created at start-up, so a website open in another tab cannot drive Ayos.
- If no model is running, or the model returns something unusable, built-in rules take over that step. The interface always shows who made each decision.

## Run it

You need Windows 10 or 11, Python 3.10 or newer, and Ollama.

1. Install [Ollama](https://ollama.com), then in a terminal: `ollama pull gemma3:4b`
2. Double-click **`start_ayos.bat`**. It asks for administrator rights (needed to change network settings) and opens `http://127.0.0.1:8020`.
3. Type what is wrong, or use the **Practice bench** on the right to break one setting on purpose, then ask Ayos to find it.

Other ways to start:

| File | What it does |
|---|---|
| `start_ayos.bat` | The real thing, on this PC. |
| `start_ayos_simulated.bat` | A pretend PC for rehearsing. Changes nothing on this computer. No administrator rights needed. |
| `selftest.bat` | Runs every read-only check once and times the model. Changes nothing. |
| `restore_network.bat` | Emergency reset. Undoes the practice faults without Python or Ayos. |

Options: `python -m ayos --model gemma3:1b` picks a model. `python -m ayos --api openai --url http://127.0.0.1:1234` uses LM Studio or any OpenAI-compatible local server.

No Python packages need to be installed. Ayos uses only the Python standard library.

## The practice bench

Real faults are hard to produce on demand, so Ayos can create four safe ones on the real PC. Each is a single setting, and each is undone by **Put everything back** or by `restore_network.bat`.

| Fault | What you would notice | What Ayos should find | The fix |
|---|---|---|---|
| Wrong DNS server | Wi-Fi shows connected, no website opens | DNS set by hand to a server that does not answer | Set DNS back to automatic |
| Adapter turned off | No connection at all | The network adapter is turned off | Turn it back on |
| Fake proxy | The browser reaches nothing | A proxy is on, pages load without it | Turn the proxy off |
| Block example.com | Only that one site fails | A hosts-file line blocks the site | Remove that line |

The simulated PC adds eight more that cannot be staged safely on a real machine, such as a dead router, a provider outage, a full drive and low memory.

## Measured results

<!-- RESULTS -->
Results are being measured. See the section below after the first full run.
<!-- /RESULTS -->

To measure a model on your own computer:

```
python -m ayos.evalsim                    best installed model
python -m ayos.evalsim --model gemma3:1b  a specific model
python -m ayos.evalsim --no-memory        without the "what changed" memory
python -m ayos.evalsim --rules            built-in rules only, for comparison
```

It reports two scores. **Model alone** is how often the model's own first conclusion was the right cause. **With safety check** is how often the final diagnosis was right. The gap between the two is what the safety check is for.

## How it is tested

- `python -m unittest discover -s tests` runs more than 60 tests on any computer: every fault on the simulated PC, the safety check against wrong model conclusions, approval and undo, memory, the web server's token and host checks, and the Windows layer against a stand-in PowerShell.
- `tests/windows_live_test.py` runs on real Windows as administrator: it breaks a real setting, lets Ayos find and fix it, and checks the internet is back.
- `tests/ui_test.py` drives the whole interface in a browser.
- GitHub Actions runs the unit tests and the live test on a real Windows machine, and runs real models on a CPU-only machine, on every push. See `.github/workflows`.

## What is in the box

| Path | What it is |
|---|---|
| `ayos/brain.py` | The model client (Ollama or OpenAI-compatible), the prompt, the JSON menu, and the rule-based fallback. |
| `ayos/agent.py` | One repair session: recall, investigate, conclude, safety check, approval, fix, verify, remember. |
| `ayos/tools.py` | The 11 read-only checks. Each returns a plain-words summary for the model and structured data for the safety check. |
| `ayos/rules.py` | The 14 causes, and the code that decides whether evidence supports a cause. |
| `ayos/fixes.py` | The 9 fixes with undo, the 4 practice faults, and "put everything back". |
| `ayos/memory.py` | What normal looks like on this PC, and the history of past problems. A JSON file in `data/`. |
| `ayos/system.py` | Everything that touches the computer: the real Windows layer and the simulated PC. |
| `ayos/server.py`, `web/index.html` | The local web server and the interface. |
| `ayos/evalsim.py` | Measures a model on simulated faults. |

## Limits, stated plainly

- Windows only. The agent logic runs anywhere on the simulated PC, but the real checks and fixes use Windows commands.
- It diagnoses the 14 causes in `ayos/rules.py`. For anything else it says it could not find a single cause and lists what it checked. It does not guess.
- It cannot fix a dead router or a provider outage. It tells you that is what it is, so you stop changing settings on a PC that is fine.
- Model speed depends on the computer. On a laptop with no graphics card a small model takes several seconds per decision.
- "Learning" here means memory: a saved picture of healthy settings and a history of past problems on this PC. No model is trained or fine-tuned.
- If your browser's secure DNS is set to a specific provider, the browser may keep working when Windows DNS is broken. Ayos checks the Windows setting.

## Disclosures

**Models.** Any open chat model that Ollama can run. Developed and measured with Google's Gemma 3 (`gemma3:4b`, `gemma3:1b`), plus `qwen2.5:3b` and `llama3.2:3b` for comparison. The models are used as published. No model was trained or fine-tuned for this project.

**Frameworks and tools.** Python standard library only for Ayos itself. Ollama serves the model. Windows PowerShell networking cmdlets are used for checks and fixes. Playwright is used in one optional interface test. GitHub Actions runs the tests.

**External APIs.** None. No cloud AI, no online service.

**AI coding tools.** Code in this repository was written with Claude (Anthropic) as an AI coding assistant, directed and tested by the team. Commits carry a `Co-Authored-By: Claude` line.

**Existing code.** None. Everything in this repository was written during the hackathon.

**What we built on top of the open model.** The model is one part. The agent loop, the fixed menu of checks, the safety check that verifies the model's conclusion, the reversible fixes, the memory, the Windows layer, the simulated PC, the evaluation tool and the interface were all built during the hackathon.

## Team

<!-- TEAM -->
Add team member names here.
<!-- /TEAM -->
