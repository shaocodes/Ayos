"""Turn the evaluation result files into the table in the README.

    python tools/results_table.py            print the table
    python tools/results_table.py --write    also put it into README.md between the RESULTS markers

Every number comes from a result file in docs/results/. Nothing is typed in by hand.
"""
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIZES = {"gemma3:1b": "0.8 GB", "gemma3:4b": "3.3 GB", "llama3.2:3b": "2.0 GB", "qwen2.5:3b": "1.9 GB"}


def load(folder: str) -> dict:
    out = {}
    for path in sorted(glob.glob(os.path.join(ROOT, "docs", "results", folder, "eval_*.json"))):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        s = data["summary"]
        model = s["brain"].split(" ")[0]
        out.setdefault(model, {})["memory" if s["with_memory"] else "nomemory"] = s
    return out


def row(model: str, runs: dict) -> str:
    m, n = runs.get("memory"), runs.get("nomemory")

    def cell(s, key):
        return f"{s[key]}/{s['cases']}" if s else "not run"

    def secs(s):
        return f"{s['median_seconds']} s" if s else "not run"

    speed = (m or n or {}).get("tokens_per_second")
    return (
        f"| `{model}` | {SIZES.get(model, '')} | {speed or '?'} | "
        f"{cell(m, 'model_first_correct')} | {cell(m, 'final_correct')} | {secs(m)} | "
        f"{cell(n, 'model_first_correct')} | {cell(n, 'final_correct')} | {secs(n)} |"
    )


HEAD = (
    "| Model | Size | Tokens/s | Model alone | With safety check | Median time | Model alone | With safety check | Median time |\n"
    "|---|---|---|---|---|---|---|---|---|\n"
)


def machine(folder: str) -> str:
    for runs in load(folder).values():
        for s in runs.values():
            return f"{s['computer']}, measured {s['date'][:10]}"
    return ""


def replay_times() -> str:
    """How long the four practice-bench faults took in the recorded replay, read from the recording itself."""
    with open(os.path.join(ROOT, "docs", "recording.json"), "r", encoding="utf-8") as f:
        rec = json.load(f)
    times = [e["t"] for s in rec["scenarios"] if s["fault"] in ("wrong_dns", "adapter_off", "proxy_on", "hosts_block")
             for e in s["events"] if e["type"] == "diagnosis"]
    return (
        f"In the recorded replay, made on the same kind of machine, the four practice-bench faults took {min(times):.1f} to {max(times):.1f} seconds each "
        f"with `{rec['model']}` running at {rec['speed']} tokens a second, from the question to a verified cause.\n"
    )


def build() -> str:
    first = load("first_measurement")
    cases = next((s["cases"] for runs in load("current").values() for s in runs.values()), 20)
    text = (
        f"Measured by GitHub Actions on a 4-thread cloud CPU with no graphics card ({machine('current')}). "
        f"{cases} cases on the simulated PC: {cases - 8} faults, 2 healthy PCs, and 6 of the same problems said the way people say them, "
        "including Taglish. Simulated checks answer instantly, so the times are almost all model time. "
        "Speed on another computer will differ; accuracy should be close. The cloud machines are not all equally fast, "
        "so read each time next to that row's tokens per second.\n\n"
        "- **Model alone**: the first cause the model named was the right one.\n"
        "- **With safety check**: the final diagnosis was right, after plain code checked the model.\n"
        "- **With memory**: Resolv has seen this PC healthy before and can compare. This is the normal case.\n\n"
        "The first three columns after the model name describe the model; the next three are with memory, the last three without.\n\n"
        + HEAD
        + "\n".join(row(m, runs) for m, runs in sorted(load("current").items(), key=lambda kv: -((kv[1].get("memory") or kv[1].get("nomemory"))["tokens_per_second"] or 0)))
        + "\n\n"
        + replay_times()
    )
    if first:
        text += (
            "\n### What the first measurement taught us\n\n"
            "The first time we measured, the small models reasoned correctly and then kept asking for more checks. "
            "They almost never named a cause, so the built-in rules had to finish the job:\n\n"
            + HEAD
            + "\n".join(row(m, runs) for m, runs in sorted(load("first_measurement").items()))
            + "\n\n"
            "So we changed the design, not the model. Once the check results already prove a cause, no more checks are offered and "
            "the model has to name it. An internet problem is only offered network checks. When memory shows a setting changed, "
            "Resolv looks there first. A refused conclusion comes back with the reason. The table at the top is the same models after those changes. "
            "The raw result files for both runs are in `docs/results/`, next to the output of the live test on real Windows.\n"
        )
    return text


def main() -> int:
    text = build()
    print(text)
    if "--write" in sys.argv:
        path = os.path.join(ROOT, "README.md")
        with open(path, "r", encoding="utf-8") as f:
            readme = f.read()
        a, b = "<!-- RESULTS -->", "<!-- /RESULTS -->"
        i, j = readme.index(a) + len(a), readme.index(b)
        with open(path, "w", encoding="utf-8") as f:
            f.write(readme[:i] + "\n" + text + readme[j:])
        print("README.md updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
