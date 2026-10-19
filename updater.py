import os
import sys
import subprocess

REPO_URL = os.getenv("GIT_REPO_URL", "https://github.com/TanguyCze/Discord-Bot.git")
BRANCH = os.getenv("GIT_BRANCH", "main")


def run_cmd(cmd: list[str]) -> int:
    print(f"▶️ Running: {' '.join(cmd)}", flush=True)
    return subprocess.call(cmd)


def main() -> None:
    # Ensure HOME is writable in Pterodactyl Docker containers so git/pip don't fail
    if os.name != "nt":
        os.environ["HOME"] = "/tmp"
        os.environ["XDG_CACHE_HOME"] = "/tmp/.cache"
        run_cmd(["git", "config", "--global", "--add", "safe.directory", os.getcwd()])

    print(f"🔄 Updating bot from {REPO_URL} ({BRANCH})...", flush=True)

    if os.path.isdir(".git"):
        run_cmd(["git", "remote", "set-url", "origin", REPO_URL])
        run_cmd(["git", "fetch", "origin", BRANCH])
        run_cmd(["git", "reset", "--hard", f"origin/{BRANCH}"])
    else:
        run_cmd(["git", "init"])
        run_cmd(["git", "remote", "add", "origin", REPO_URL])
        run_cmd(["git", "fetch", "origin", BRANCH])
        run_cmd(["git", "checkout", "-f", "-B", BRANCH, f"origin/{BRANCH}"])

    print("📦 Updating Python packages from requirements.txt...", flush=True)
    run_cmd([sys.executable, "-m", "pip", "install", "-U", "--no-cache-dir", "-r", "requirements.txt"])

    print("🚀 Starting discord_bot.py...", flush=True)
    os.execvp(sys.executable, [sys.executable, "discord_bot.py"])


if __name__ == "__main__":
    main()
