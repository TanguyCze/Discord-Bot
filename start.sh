#!/bin/bash
set -e

REPO_URL="${GIT_REPO_URL:-https://github.com/TanguyCze/Discord-Bot.git}"
BRANCH="${GIT_BRANCH:-main}"
APP_DIR="$(pwd)"

echo "🔄 Checking for updates from $REPO_URL ($BRANCH)..."

export HOME="/tmp"
git config --global --add safe.directory "$APP_DIR" 2>/dev/null || true

if [ -w "$APP_DIR" ]; then
    if [ -d ".git" ]; then
        git fetch origin "$BRANCH"
        git reset --hard "origin/$BRANCH"
    else
        git init
        git remote add origin "$REPO_URL" 2>/dev/null || git remote set-url origin "$REPO_URL"
        git fetch origin "$BRANCH"
        git checkout -f -B "$BRANCH" "origin/$BRANCH"
    fi
     WORK_DIR="$APP_DIR"
else
    echo "⚠️ Current directory is read-only. Using /tmp/discord-bot for update..."
    WORK_DIR="/tmp/discord-bot"
    if [ -d "$WORK_DIR/.git" ]; then
        git -C "$WORK_DIR" fetch origin "$BRANCH"
        git -C "$WORK_DIR" reset --hard "origin/$BRANCH"
    else
        rm -rf "$WORK_DIR"
        git clone --depth 1 -b "$BRANCH" "$REPO_URL" "$WORK_DIR"
    fi
    if [ -f "$APP_DIR/.env" ]; then
        cp "$APP_DIR/.env" "$WORK_DIR/.env"
    fi
fi

echo "📦 Installing/updating requirements (including yt-dlp)..."
pip install -U --no-cache-dir -r "$WORK_DIR/requirements.txt" || true

echo "🚀 Launching Discord Bot..."
exec python3 "$WORK_DIR/discord_bot.py"
