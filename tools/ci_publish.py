"""Used by the GitHub Actions workflows only: publish result files so they can be read without opening the logs.

Each file is printed as a workflow notice and, when a token is available, committed to a results branch.
"""
import glob
import os
import subprocess
import sys


def esc(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main() -> int:
    label = sys.argv[1]
    files = [f for pat in sys.argv[2:] for f in sorted(glob.glob(pat))]
    for f in files:
        with open(f, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        for i in range(0, min(len(text), 240000), 60000):
            print(f"::notice title={label} {os.path.basename(f)} part {i // 60000 + 1}::{esc(text[i:i + 60000])}")
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not (token and repo and files):
        return 0
    out = os.path.abspath("ci_branch")
    os.makedirs(out, exist_ok=True)
    for f in files:
        with open(f, "rb") as src, open(os.path.join(out, os.path.basename(f)), "wb") as dst:
            dst.write(src.read())
    sha = os.environ.get("GITHUB_SHA", "")[:7]
    cmds = [
        ["git", "init", "-q", "-b", "results"],
        ["git", "config", "user.name", "ayos-ci"],
        ["git", "config", "user.email", "ayos-ci@users.noreply.github.com"],
        ["git", "add", "-A"],
        ["git", "commit", "-q", "-m", f"CI results: {label} at {sha}"],
        ["git", "push", "-q", "-f", f"https://x-access-token:{token}@github.com/{repo}", f"HEAD:refs/heads/ci-results-{label}"],
    ]
    for c in cmds:
        r = subprocess.run(c, cwd=out, capture_output=True, text=True)
        if r.returncode != 0:
            print("could not publish to a branch:", (r.stderr or r.stdout).replace(token, "***")[:300])
            return 0
    print(f"published to branch ci-results-{label}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
