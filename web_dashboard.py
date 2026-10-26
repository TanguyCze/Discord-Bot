import os
import json
import time
import uuid
import logging
from typing import Dict, Any, List, Optional
import discord
from aiohttp import web

logger = logging.getLogger("DiscordBot.WebUI")

###############################################################################
# ─────────────────────── PERSISTENT CONFIG & STATE ───────────────────────── #
###############################################################################


def get_config_path() -> str:
    custom = os.getenv("BOT_CONFIG_PATH")
    if custom:
        return custom
    cwd = os.getcwd()
    if os.access(cwd, os.W_OK):
        return os.path.join(cwd, "bot_data.json")
    return "/tmp/bot_data.json"


DEFAULT_CONFIG: Dict[str, Any] = {
    "lobbies": {},  # { str(voice_channel_id): {"target_category_id": int|None, "name_template": "🔊 {user}"} }
    "drops_settings": {
        "cooldown_hours": 48,
        "min_drop_streams": 2,
        "inactive_checks_to_reset": 6,
    },
    "feeds": [],  # List of feed dicts
    "campaign_history": {},  # { "feed_id::game_name": { "active": bool, "last_notified_ts": float, "inactive_streak": int } }
}


class ConfigManager:
    def __init__(self) -> None:
        self.path = get_config_path()
        self.data: Dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        if os.path.isfile(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    self.data = loaded
            except Exception as e:
                logger.error("Failed to load %s: %s", self.path, e)

        # Ensure defaults
        for k, v in DEFAULT_CONFIG.items():
            if k not in self.data:
                self.data[k] = json.loads(json.dumps(v))

    def save(self) -> None:
        try:
            tmp_path = self.path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, self.path)
        except Exception as e:
            logger.error("Failed to save config to %s: %s", self.path, e)

    def ensure_default_migration(
        self,
        legacy_lobby_id: int,
        legacy_notify_channel_id: int,
        default_games: List[str],
    ) -> None:
        changed = False
        if legacy_lobby_id and str(legacy_lobby_id) not in self.data["lobbies"]:
            self.data["lobbies"][str(legacy_lobby_id)] = {
                "target_category_id": None,
                "name_template": "🔊 {user}",
            }
            changed = True

        if not self.data["feeds"] and legacy_notify_channel_id:
            self.data["feeds"].append(
                {
                    "id": "default_feed",
                    "name": "Default Drops Feed",
                    "enabled": True,
                    "target_type": "channel",
                    "target_id": str(legacy_notify_channel_id),
                    "games": list(default_games),
                    "edit_single_message": False,
                    "last_message_id": None,
                }
            )
            changed = True

        if changed:
            self.save()


###############################################################################
# ──────────────────────────── WEB UI HTML/JS ─────────────────────────────── #
###############################################################################

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Discord Bot Control Panel</title>
  <style>
    :root {
      --bg: #0f111a;
      --panel: #181b28;
      --panel-alt: #1f2335;
      --border: #2b3048;
      --accent: #5865F2;
      --accent-hover: #4752C4;
      --twitch: #9146FF;
      --success: #2ecc71;
      --danger: #e74c3c;
      --warning: #f1c40f;
      --text: #f2f4f8;
      --muted: #9aa2b9;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
      padding-bottom: 60px;
    }
    header {
      background: var(--panel);
      border-bottom: 1px solid var(--border);
      padding: 16px 28px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 12px;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
      font-size: 1.2rem;
      font-weight: 700;
    }
    .badge {
      font-size: 0.78rem;
      padding: 4px 10px;
      border-radius: 999px;
      background: rgba(46, 204, 113, 0.15);
      color: var(--success);
      border: 1px solid rgba(46, 204, 113, 0.35);
    }
    .container {
      max-width: 1220px;
      margin: 24px auto;
      padding: 0 20px;
    }
    .tabs {
      display: flex;
      gap: 8px;
      margin-bottom: 24px;
      border-bottom: 1px solid var(--border);
      padding-bottom: 12px;
      flex-wrap: wrap;
    }
    .tab-btn {
      background: var(--panel);
      color: var(--muted);
      border: 1px solid var(--border);
      padding: 10px 18px;
      border-radius: 8px;
      cursor: pointer;
      font-weight: 600;
      font-size: 0.92rem;
      transition: all 0.15s;
    }
    .tab-btn.active, .tab-btn:hover {
      background: var(--accent);
      color: #fff;
      border-color: var(--accent);
    }
    .tab-content { display: none; }
    .tab-content.active { display: block; }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
      gap: 20px;
    }
    .card {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 20px;
      margin-bottom: 20px;
    }
    .card h2 {
      font-size: 1.08rem;
      margin-bottom: 6px;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .card p.sub {
      color: var(--muted);
      font-size: 0.86rem;
      margin-bottom: 16px;
    }
    label {
      display: block;
      font-size: 0.84rem;
      font-weight: 600;
      color: var(--muted);
      margin-bottom: 6px;
      margin-top: 12px;
    }
    input, select, textarea {
      width: 100%;
      background: var(--panel-alt);
      border: 1px solid var(--border);
      color: var(--text);
      padding: 9px 12px;
      border-radius: 8px;
      font-size: 0.92rem;
    }
    input:focus, select:focus {
      outline: none;
      border-color: var(--accent);
    }
    .row {
      display: flex;
      gap: 10px;
      align-items: center;
    }
    .btn {
      background: var(--accent);
      color: #fff;
      border: none;
      padding: 9px 16px;
      border-radius: 8px;
      cursor: pointer;
      font-weight: 600;
      font-size: 0.88rem;
      transition: opacity 0.15s;
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }
    .btn:hover { opacity: 0.9; }
    .btn-twitch { background: var(--twitch); }
    .btn-success { background: var(--success); color: #0b1a10; }
    .btn-danger { background: var(--danger); }
    .btn-outline {
      background: transparent;
      border: 1px solid var(--border);
      color: var(--text);
    }
    .btn-sm { padding: 5px 10px; font-size: 0.8rem; }
    .pill-list {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 8px;
    }
    .pill {
      background: var(--panel-alt);
      border: 1px solid var(--border);
      border-radius: 999px;
      padding: 4px 11px;
      font-size: 0.82rem;
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }
    .pill button {
      background: none;
      border: none;
      color: var(--danger);
      cursor: pointer;
      font-weight: bold;
    }
    .quick-game {
      cursor: pointer;
      user-select: none;
      transition: border-color 0.15s;
    }
    .quick-game:hover { border-color: var(--twitch); }
    .channel-group {
      margin-bottom: 14px;
      background: var(--panel-alt);
      border-radius: 8px;
      padding: 12px;
      border: 1px solid var(--border);
    }
    .channel-group h4 {
      font-size: 0.82rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--muted);
      margin-bottom: 8px;
    }
    .chan-item {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 6px 8px;
      border-radius: 6px;
      font-size: 0.9rem;
    }
    .chan-item:hover { background: rgba(255,255,255,0.04); }
    .feed-item {
      background: var(--panel-alt);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 14px;
      margin-bottom: 12px;
    }
    .feed-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 8px;
      flex-wrap: wrap;
      gap: 8px;
    }
    #toast {
      position: fixed;
      bottom: 20px;
      right: 20px;
      background: var(--panel-alt);
      border: 1px solid var(--accent);
      color: var(--text);
      padding: 12px 20px;
      border-radius: 10px;
      box-shadow: 0 8px 24px rgba(0,0,0,0.4);
      display: none;
      z-index: 9999;
    }
    .checkbox-row {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-top: 12px;
      cursor: pointer;
    }
    .checkbox-row input { width: auto; }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <span>🤖 Discord Bot Web Control Panel</span>
      <span class="badge" id="bot-status">Connecting...</span>
    </div>
    <div class="row">
      <label style="margin:0;">Server:</label>
      <select id="guild-select" onchange="onGuildChange()" style="width: 220px;"></select>
      <button class="btn btn-outline btn-sm" onclick="loadState()">🔄 Refresh</button>
    </div>
  </header>

  <div class="container">
    <div class="tabs">
      <button class="tab-btn active" onclick="switchTab('drops', this)">🎁 Custom Drops Feeds & Anti-Spam</button>
      <button class="tab-btn" onclick="switchTab('channels', this)">🛠️ Channel & Category Manager</button>
      <button class="tab-btn" onclick="switchTab('lobbies', this)">🔊 Join-to-Create Lobbies</button>
    </div>

    <!-- TAB 1: DROPS FEEDS & ANTI-SPAM -->
    <div id="tab-drops" class="tab-content active">
      <div class="grid">
        <div>
          <div class="card">
            <h2>➕ Create / Update Custom Drops Feed</h2>
            <p class="sub">Route only the games you care about to your personal DM or a private channel so other members aren't spammed.</p>

            <label>Feed Name</label>
            <input type="text" id="feed-name" placeholder="e.g. My Personal Drops Feed" value="My Personal Drops" />

            <label>Where should alerts be sent?</label>
            <select id="feed-target-type" onchange="onTargetTypeChange()">
              <option value="dm">💬 Direct Message (DM to a specific User)</option>
              <option value="channel">📢 Specific Discord Channel</option>
              <option value="create_private">🔒 Create a NEW Private Channel for me automatically</option>
            </select>

            <div id="target-dm-box">
              <label>Select Member to DM (or type Discord User ID)</label>
              <select id="feed-dm-member" onchange="document.getElementById('feed-target-id').value = this.value"></select>
              <input type="text" id="feed-target-id" placeholder="Discord User ID" style="margin-top:6px;" />
            </div>

            <div id="target-channel-box" style="display:none;">
              <label>Select Text Channel</label>
              <select id="feed-channel-select"></select>
            </div>

            <div id="target-private-box" style="display:none;">
              <label>Category for New Private Channel</label>
              <select id="feed-private-category"></select>
              <label>New Private Channel Name</label>
              <input type="text" id="feed-private-name" value="🎁-my-drops" />
              <label>Who gets access to this Private Channel?</label>
              <select id="feed-private-member"></select>
            </div>

            <label>Interested Games (Click to add or type custom Twitch game name)</label>
            <div class="row">
              <input type="text" id="game-input" placeholder="e.g. Rust, Escape from Tarkov..." onkeydown="if(event.key==='Enter'){addGamePill();event.preventDefault();}" />
              <button class="btn btn-twitch" type="button" onclick="addGamePill()">Add</button>
            </div>
            <div class="pill-list" style="margin-top:6px;">
              <span class="pill quick-game" onclick="quickAddGame('Rust')">+ Rust</span>
              <span class="pill quick-game" onclick="quickAddGame('League of Legends')">+ League of Legends</span>
              <span class="pill quick-game" onclick="quickAddGame('Dark and Darker')">+ Dark and Darker</span>
              <span class="pill quick-game" onclick="quickAddGame('Escape from Tarkov')">+ Escape from Tarkov</span>
              <span class="pill quick-game" onclick="quickAddGame('Valorant')">+ Valorant</span>
              <span class="pill quick-game" onclick="quickAddGame('Warframe')">+ Warframe</span>
              <span class="pill quick-game" onclick="quickAddGame('Path of Exile 2')">+ Path of Exile 2</span>
            </div>

            <label>Selected Games for this Feed:</label>
            <div id="selected-games" class="pill-list"></div>

            <label class="checkbox-row">
              <input type="checkbox" id="feed-single-msg" />
              <span><b>Single Message Mode (Zero Spam):</b> Edit one persistent status message instead of sending new messages</span>
            </label>

            <div style="margin-top: 18px;">
              <button class="btn btn-twitch" onclick="saveFeed()">💾 Create Drops Feed</button>
            </div>
          </div>

          <div class="card">
            <h2>🛡️ Global Anti-Spam Settings</h2>
            <p class="sub">Prevents duplicate alerts when streamers go offline/online or when the bot restarts on Pterodactyl.</p>

            <label>Minimum Cooldown Between Alerts for the Same Game (Hours)</label>
            <input type="number" id="spam-cooldown" min="1" max="720" value="48" />

            <label>Minimum Concurrent Streams with "DropsEnabled" to Trigger Alert</label>
            <input type="number" id="spam-min-streams" min="1" max="20" value="2" />

            <label>Consecutive Empty Checks (10m each) Before Marking Campaign Over</label>
            <input type="number" id="spam-inactive-checks" min="1" max="144" value="6" />

            <div style="margin-top: 16px;" class="row">
              <button class="btn btn-success" onclick="saveSpamSettings()">💾 Save Anti-Spam Rules</button>
              <button class="btn btn-outline" onclick="triggerCheckNow()">🔍 Run Twitch Check Now</button>
            </div>
          </div>
        </div>

        <div>
          <div class="card">
            <h2>📡 Active Custom Drops Feeds</h2>
            <p class="sub">Manage your feeds, send a test DM/message, or clear campaign cooldowns.</p>
            <div id="feeds-list"></div>
          </div>

          <div class="card">
            <h2>🕓 Campaign History & Cooldown Lock</h2>
            <p class="sub">Games listed here won't re-alert until their cooldown expires or you reset them.</p>
            <div id="history-list"></div>
          </div>
        </div>
      </div>
    </div>

    <!-- TAB 2: CHANNEL MANAGER -->
    <div id="tab-channels" class="tab-content">
      <div class="grid">
        <div>
          <div class="card">
            <h2>➕ Create Channel in Chosen Category</h2>
            <p class="sub">Create a Text, Voice, or Category channel in the exact place you want, with optional private visibility.</p>

            <label>Channel Type</label>
            <select id="new-chan-type" onchange="onNewChanTypeChange()">
              <option value="text">💬 Text Channel (#)</option>
              <option value="voice">🔊 Voice Channel</option>
              <option value="category">📁 New Category</option>
            </select>

            <div id="new-chan-cat-box">
              <label>Place Inside Which Category?</label>
              <select id="new-chan-category"></select>
            </div>

            <label>Channel Name</label>
            <input type="text" id="new-chan-name" placeholder="e.g. drops-alerts or squad-voice" />

            <div id="new-chan-topic-box">
              <label>Channel Topic (Optional)</label>
              <input type="text" id="new-chan-topic" placeholder="Channel description..." />
            </div>

            <div id="new-chan-limit-box" style="display:none;">
              <label>Voice User Limit (0 = Unlimited)</label>
              <input type="number" id="new-chan-limit" min="0" max="99" value="0" />
            </div>

            <label class="checkbox-row">
              <input type="checkbox" id="new-chan-private" onchange="document.getElementById('private-member-box').style.display = this.checked ? 'block' : 'none'" />
              <span>🔒 Make this a <b>Private Channel</b> (Hidden from @everyone)</span>
            </label>

            <div id="private-member-box" style="display:none;">
              <label>Grant Access to Member (Optional)</label>
              <select id="new-chan-member"></select>
            </div>

            <div style="margin-top: 18px;">
              <button class="btn" onclick="createChannel()">➕ Create Channel</button>
            </div>
          </div>
        </div>

        <div>
          <div class="card">
            <h2>📂 Existing Server Channels</h2>
            <p class="sub">Move channels between categories, rename them, or delete unwanted channels.</p>
            <div id="channels-tree"></div>
          </div>
        </div>
      </div>
    </div>

    <!-- TAB 3: JOIN TO CREATE LOBBIES -->
    <div id="tab-lobbies" class="tab-content">
      <div class="grid">
        <div class="card">
          <h2>🔊 Configure "Join-to-Create" Voice Lobbies</h2>
          <p class="sub">Choose which voice channel(s) spawn a temporary room when joined, and choose which Category the created channel is placed in.</p>

          <label>Select Trigger Voice Channel (Lobby)</label>
          <select id="lobby-voice-select"></select>

          <label>Where should temporary rooms be created?</label>
          <select id="lobby-target-cat"></select>

          <label>Created Channel Name Template (use <code>{user}</code> for member name)</label>
          <input type="text" id="lobby-template" value="🔊 {user}" />

          <div style="margin-top:16px;">
            <button class="btn" onclick="addLobby()">💾 Save Lobby Rule</button>
          </div>
        </div>

        <div class="card">
          <h2>📋 Active Join-to-Create Lobbies</h2>
          <p class="sub">Users joining any of these voice channels will automatically get their own room in the chosen category.</p>
          <div id="lobbies-list"></div>
        </div>
      </div>
    </div>
  </div>

  <div id="toast"></div>

  <script>
    let STATE = null;
    let currentGames = ["Rust", "League of Legends", "Dark and Darker"];

    function showToast(msg, isError = false) {
      const t = document.getElementById("toast");
      t.textContent = msg;
      t.style.borderColor = isError ? "var(--danger)" : "var(--success)";
      t.style.display = "block";
      setTimeout(() => { t.style.display = "none"; }, 3500);
    }

    function switchTab(name, btn) {
      document.querySelectorAll(".tab-content").forEach(el => el.classList.remove("active"));
      document.querySelectorAll(".tab-btn").forEach(el => el.classList.remove("active"));
      document.getElementById("tab-" + name).classList.add("active");
      btn.classList.add("active");
    }

    async function api(path, method = "GET", body = null) {
      const opts = { method, headers: { "Content-Type": "application/json" } };
      const params = new URLSearchParams(window.location.search);
      if (params.get("key")) {
        opts.headers["X-Auth-Key"] = params.get("key");
      }
      if (body) opts.body = JSON.stringify(body);
      const res = await fetch(path, opts);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Request failed");
      return data;
    }

    function renderGamePills() {
      const box = document.getElementById("selected-games");
      box.innerHTML = "";
      currentGames.forEach((g, idx) => {
        const span = document.createElement("span");
        span.className = "pill";
        span.innerHTML = `${g} <button onclick="removeGamePill(${idx})">&times;</button>`;
        box.appendChild(span);
      });
    }

    function addGamePill() {
      const inp = document.getElementById("game-input");
      const val = inp.value.trim();
      if (val && !currentGames.some(x => x.toLowerCase() === val.toLowerCase())) {
        currentGames.push(val);
        renderGamePills();
      }
      inp.value = "";
    }

    function quickAddGame(name) {
      if (!currentGames.some(x => x.toLowerCase() === name.toLowerCase())) {
        currentGames.push(name);
        renderGamePills();
      }
    }

    function removeGamePill(idx) {
      currentGames.splice(idx, 1);
      renderGamePills();
    }

    function onTargetTypeChange() {
      const t = document.getElementById("feed-target-type").value;
      document.getElementById("target-dm-box").style.display = t === "dm" ? "block" : "none";
      document.getElementById("target-channel-box").style.display = t === "channel" ? "block" : "none";
      document.getElementById("target-private-box").style.display = t === "create_private" ? "block" : "none";
    }

    function onNewChanTypeChange() {
      const t = document.getElementById("new-chan-type").value;
      document.getElementById("new-chan-cat-box").style.display = t === "category" ? "none" : "block";
      document.getElementById("new-chan-topic-box").style.display = t === "text" ? "block" : "none";
      document.getElementById("new-chan-limit-box").style.display = t === "voice" ? "block" : "none";
    }

    async function loadState() {
      try {
        const prevGuild = document.getElementById("guild-select").value;
        STATE = await api("/api/state");
        document.getElementById("bot-status").textContent = `Online as ${STATE.bot_name}`;

        const gSel = document.getElementById("guild-select");
        gSel.innerHTML = "";
        STATE.guilds.forEach(g => {
          const opt = document.createElement("option");
          opt.value = g.id;
          opt.textContent = g.name;
          gSel.appendChild(opt);
        });
        if (prevGuild && STATE.guilds.some(g => g.id === prevGuild)) {
          gSel.value = prevGuild;
        }

        // Spam settings
        document.getElementById("spam-cooldown").value = STATE.config.drops_settings.cooldown_hours;
        document.getElementById("spam-min-streams").value = STATE.config.drops_settings.min_drop_streams;
        document.getElementById("spam-inactive-checks").value = STATE.config.drops_settings.inactive_checks_to_reset;

        onGuildChange();
        renderFeeds();
        renderHistory();
        renderGamePills();
      } catch (e) {
        showToast(e.message, true);
      }
    }

    function getCurrentGuild() {
      if (!STATE || !STATE.guilds.length) return null;
      const gid = document.getElementById("guild-select").value;
      return STATE.guilds.find(g => g.id === gid) || STATE.guilds[0];
    }

    function onGuildChange() {
      const g = getCurrentGuild();
      if (!g) return;

      // Populate members
      const memberSelects = ["feed-dm-member", "feed-private-member", "new-chan-member"];
      memberSelects.forEach(id => {
        const el = document.getElementById(id);
        el.innerHTML = '<option value="">-- Select Member --</option>';
        g.members.forEach(m => {
          const o = document.createElement("option");
          o.value = m.id;
          o.textContent = `${m.display_name} (@${m.name})`;
          el.appendChild(o);
        });
      });

      // Populate text channels
      const cSel = document.getElementById("feed-channel-select");
      cSel.innerHTML = "";
      g.channels.filter(c => c.type === "text").forEach(c => {
        const o = document.createElement("option");
        o.value = c.id;
        o.textContent = `#${c.name} (${c.category_name || "No Category"})`;
        cSel.appendChild(o);
      });

      // Populate categories
      const catSelects = ["feed-private-category", "new-chan-category", "lobby-target-cat"];
      catSelects.forEach(id => {
        const el = document.getElementById(id);
        el.innerHTML = '<option value="">-- No Category (Top Level) --</option>';
        if (id === "lobby-target-cat") {
          el.innerHTML = '<option value="">-- Same Category as Lobby Channel --</option>';
        }
        g.categories.forEach(cat => {
          const o = document.createElement("option");
          o.value = cat.id;
          o.textContent = `📁 ${cat.name}`;
          el.appendChild(o);
        });
      });

      // Populate voice channels for lobby
      const vSel = document.getElementById("lobby-voice-select");
      vSel.innerHTML = "";
      g.channels.filter(c => c.type === "voice").forEach(c => {
        const o = document.createElement("option");
        o.value = c.id;
        o.textContent = `🔊 ${c.name} (${c.category_name || "No Category"})`;
        vSel.appendChild(o);
      });

      renderChannelsTree(g);
      renderLobbies(g);
    }

    function renderFeeds() {
      const box = document.getElementById("feeds-list");
      box.innerHTML = "";
      const feeds = STATE.config.feeds || [];
      if (!feeds.length) {
        box.innerHTML = '<p style="color:var(--muted);">No custom feeds configured yet. Create one on the left!</p>';
        return;
      }

      feeds.forEach(f => {
        const div = document.createElement("div");
        div.className = "feed-item";
        const destBadge = f.target_type === "dm" ? `💬 DM (User ID: ${f.target_id})` : `📢 Channel (ID: ${f.target_id})`;
        const modeBadge = f.edit_single_message ? "✏️ Edits 1 Message" : "🔔 Cooldown Alert";
        const gamesHtml = (f.games || []).map(g => `<span class="pill">${g}</span>`).join(" ");
        div.innerHTML = `
          <div class="feed-header">
            <div>
              <strong>${f.name}</strong>
              <span style="font-size:0.8rem; color:var(--muted); margin-left:8px;">${destBadge} • ${modeBadge}</span>
            </div>
            <div class="row">
              <button class="btn btn-outline btn-sm" onclick="testFeed('${f.id}')">📨 Test Alert</button>
              <button class="btn btn-danger btn-sm" onclick="deleteFeed('${f.id}')">🗑️</button>
            </div>
          </div>
          <div class="pill-list">${gamesHtml}</div>
        `;
        box.appendChild(div);
      });
    }

    function renderHistory() {
      const box = document.getElementById("history-list");
      box.innerHTML = "";
      const hist = STATE.config.campaign_history || {};
      const keys = Object.keys(hist);
      if (!keys.length) {
        box.innerHTML = '<p style="color:var(--muted);">No alerts sent yet.</p>';
        return;
      }

      keys.forEach(k => {
        const item = hist[k];
        const [fid, gname] = k.split("::");
        const lastDate = item.last_notified_ts ? new Date(item.last_notified_ts * 1000).toLocaleString() : "Never";
        const status = item.active ? "🟢 Active Campaign" : "⚪ Inactive";
        const div = document.createElement("div");
        div.className = "chan-item";
        div.innerHTML = `
          <div>
            <strong>${gname}</strong> <span style="color:var(--muted); font-size:0.82rem;">(${fid}) — ${status} • Last alert: ${lastDate}</span>
          </div>
          <button class="btn btn-outline btn-sm" onclick="resetHistory('${k}')">Reset Cooldown</button>
        `;
        box.appendChild(div);
      });
    }

    function renderChannelsTree(g) {
      const box = document.getElementById("channels-tree");
      box.innerHTML = "";

      const groups = {};
      g.categories.forEach(cat => { groups[cat.id] = { name: `📁 ${cat.name}`, id: cat.id, items: [] }; });
      groups["none"] = { name: "Uncategorized", id: "", items: [] };

      g.channels.forEach(c => {
        const key = c.category_id && groups[c.category_id] ? c.category_id : "none";
        groups[key].items.push(c);
      });

      Object.values(groups).forEach(grp => {
        if (!grp.items.length && grp.id === "") return;
        const wrap = document.createElement("div");
        wrap.className = "channel-group";
        const delCatBtn = grp.id ? `<button class="btn btn-danger btn-sm" style="float:right; padding:2px 8px;" onclick="deleteChannel('${grp.id}')">Delete Category</button>` : "";
        wrap.innerHTML = `<h4>${grp.name} ${delCatBtn}</h4>`;

        grp.items.forEach(c => {
          const icon = c.type === "voice" ? "🔊" : "#";
          const row = document.createElement("div");
          row.className = "chan-item";

          const catOpts = ['<option value="">No Category</option>'].concat(
            g.categories.map(cat => `<option value="${cat.id}" ${cat.id === c.category_id ? "selected" : ""}>📁 ${cat.name}</option>`)
          ).join("");

          row.innerHTML = `
            <span>${icon} <strong>${c.name}</strong></span>
            <div class="row">
              <select style="width:165px; padding:4px 8px; font-size:0.8rem;" onchange="moveChannel('${c.id}', this.value)">
                ${catOpts}
              </select>
              <button class="btn btn-danger btn-sm" onclick="deleteChannel('${c.id}')">🗑️</button>
            </div>
          `;
          wrap.appendChild(row);
        });

        box.appendChild(wrap);
      });
    }

    function renderLobbies(g) {
      const box = document.getElementById("lobbies-list");
      box.innerHTML = "";
      const lobbies = STATE.config.lobbies || {};
      const keys = Object.keys(lobbies);
      if (!keys.length) {
        box.innerHTML = '<p style="color:var(--muted);">No Join-to-Create lobbies configured.</p>';
        return;
      }

      keys.forEach(cid => {
        const cfg = lobbies[cid];
        const ch = g.channels.find(c => c.id === cid);
        const cat = g.categories.find(c => c.id === String(cfg.target_category_id || ""));
        const chName = ch ? `🔊 ${ch.name}` : `Channel ID ${cid}`;
        const catName = cat ? `📁 ${cat.name}` : "Same Category as Lobby";

        const div = document.createElement("div");
        div.className = "feed-item";
        div.innerHTML = `
          <div class="feed-header">
            <div>
              <strong>${chName}</strong> ➡️ Spawns in: <b>${catName}</b>
              <div style="font-size:0.82rem; color:var(--muted);">Template: <code>${cfg.name_template || "🔊 {user}"}</code></div>
            </div>
            <button class="btn btn-danger btn-sm" onclick="deleteLobby('${cid}')">🗑️</button>
          </div>
        `;
        box.appendChild(div);
      });
    }

    async function saveFeed() {
      try {
        const g = getCurrentGuild();
        const tType = document.getElementById("feed-target-type").value;
        const payload = {
          guild_id: g ? g.id : null,
          name: document.getElementById("feed-name").value.trim() || "Custom Drops Feed",
          target_type: tType,
          target_id: tType === "dm" ? document.getElementById("feed-target-id").value.trim() : document.getElementById("feed-channel-select").value,
          private_category_id: document.getElementById("feed-private-category").value,
          private_channel_name: document.getElementById("feed-private-name").value.trim(),
          private_member_id: document.getElementById("feed-private-member").value,
          games: currentGames,
          edit_single_message: document.getElementById("feed-single-msg").checked
        };
        await api("/api/feeds", "POST", payload);
        showToast("✅ Custom Drops Feed saved!");
        await loadState();
      } catch (e) {
        showToast(e.message, true);
      }
    }

    async function deleteFeed(id) {
      try {
        await api(`/api/feeds/${id}`, "DELETE");
        showToast("🗑️ Feed removed.");
        await loadState();
      } catch (e) {
        showToast(e.message, true);
      }
    }

    async function testFeed(id) {
      try {
        await api(`/api/feeds/${id}/test`, "POST");
        showToast("📨 Test drop notification sent!");
        await loadState();
      } catch (e) {
        showToast(e.message, true);
      }
    }

    async function saveSpamSettings() {
      try {
        await api("/api/spam_settings", "POST", {
          cooldown_hours: Number(document.getElementById("spam-cooldown").value),
          min_drop_streams: Number(document.getElementById("spam-min-streams").value),
          inactive_checks_to_reset: Number(document.getElementById("spam-inactive-checks").value),
        });
        showToast("✅ Anti-spam settings updated!");
        await loadState();
      } catch (e) {
        showToast(e.message, true);
      }
    }

    async function triggerCheckNow() {
      try {
        await api("/api/drops/check_now", "POST");
        showToast("🔍 Triggered immediate Twitch Drops check!");
        setTimeout(loadState, 2500);
      } catch (e) {
        showToast(e.message, true);
      }
    }

    async function resetHistory(key) {
      try {
        await api("/api/history/reset", "POST", { key });
        showToast("✅ Cooldown reset for " + key);
        await loadState();
      } catch (e) {
        showToast(e.message, true);
      }
    }

    async function createChannel() {
      try {
        const g = getCurrentGuild();
        if (!g) return;
        await api("/api/channels/create", "POST", {
          guild_id: g.id,
          type: document.getElementById("new-chan-type").value,
          category_id: document.getElementById("new-chan-category").value || null,
          name: document.getElementById("new-chan-name").value.trim(),
          topic: document.getElementById("new-chan-topic").value.trim(),
          user_limit: Number(document.getElementById("new-chan-limit").value || 0),
          is_private: document.getElementById("new-chan-private").checked,
          member_id: document.getElementById("new-chan-member").value || null,
        });
        document.getElementById("new-chan-name").value = "";
        showToast("✅ Channel created!");
        await loadState();
      } catch (e) {
        showToast(e.message, true);
      }
    }

    async function moveChannel(channelId, categoryId) {
      try {
        const g = getCurrentGuild();
        await api("/api/channels/move", "POST", {
          guild_id: g.id,
          channel_id: channelId,
          category_id: categoryId || null,
        });
        showToast("✅ Channel moved!");
        await loadState();
      } catch (e) {
        showToast(e.message, true);
      }
    }

    async function deleteChannel(channelId) {
      if (!confirm("Are you sure you want to delete this channel?")) return;
      try {
        const g = getCurrentGuild();
        await api("/api/channels/delete", "POST", {
          guild_id: g.id,
          channel_id: channelId,
        });
        showToast("🗑️ Channel deleted.");
        await loadState();
      } catch (e) {
        showToast(e.message, true);
      }
    }

    async function addLobby() {
      try {
        await api("/api/lobbies", "POST", {
          voice_channel_id: document.getElementById("lobby-voice-select").value,
          target_category_id: document.getElementById("lobby-target-cat").value || null,
          name_template: document.getElementById("lobby-template").value.trim() || "🔊 {user}",
        });
        showToast("✅ Lobby rule saved!");
        await loadState();
      } catch (e) {
        showToast(e.message, true);
      }
    }

    async function deleteLobby(cid) {
      try {
        await api(`/api/lobbies/${cid}`, "DELETE");
        showToast("🗑️ Lobby rule deleted.");
        await loadState();
      } catch (e) {
        showToast(e.message, true);
      }
    }

    loadState();
  </script>
</body>
</html>
"""


###############################################################################
# ────────────────────────── AIOHTTP WEB SERVER ───────────────────────────── #
###############################################################################


def check_auth(request: web.Request) -> bool:
    secret = os.getenv("WEB_PASSWORD", "").strip()
    if not secret:
        return True
    key = request.headers.get("X-Auth-Key") or request.query.get("key") or ""
    return key == secret


def create_web_app(bot: discord.Client, config_mgr: ConfigManager, trigger_drops_cb) -> web.Application:
    app = web.Application()

    async def index(request: web.Request) -> web.Response:
        if not check_auth(request):
            return web.Response(text="Unauthorized. Append ?key=YOUR_WEB_PASSWORD to URL.", status=401)
        return web.Response(text=DASHBOARD_HTML, content_type="text/html")

    async def get_state(request: web.Request) -> web.Response:
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        guilds_data = []
        for g in bot.guilds:
            categories = [
                {"id": str(c.id), "name": c.name, "position": c.position}
                for c in sorted(g.categories, key=lambda x: x.position)
            ]
            channels = []
            for c in sorted(g.channels, key=lambda x: x.position):
                if isinstance(c, discord.TextChannel):
                    ctype = "text"
                elif isinstance(c, discord.VoiceChannel):
                    ctype = "voice"
                else:
                    continue
                channels.append(
                    {
                        "id": str(c.id),
                        "name": c.name,
                        "type": ctype,
                        "category_id": str(c.category_id) if c.category_id else None,
                        "category_name": c.category.name if c.category else None,
                    }
                )
            members = [
                {"id": str(m.id), "name": m.name, "display_name": m.display_name}
                for m in g.members
                if not m.bot
            ][:250]

            guilds_data.append(
                {
                    "id": str(g.id),
                    "name": g.name,
                    "categories": categories,
                    "channels": channels,
                    "members": members,
                }
            )

        return web.json_response(
            {
                "bot_name": str(bot.user) if bot.user else "Bot",
                "guilds": guilds_data,
                "config": config_mgr.data,
            }
        )

    async def add_feed(request: web.Request) -> web.Response:
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        body = await request.json()
        t_type = body.get("target_type", "dm")
        target_id = str(body.get("target_id", "")).strip()
        games = [g.strip() for g in (body.get("games") or []) if g.strip()]
        if not games:
            return web.json_response({"error": "Please select at least one game."}, status=400)

        # Handle automatic private channel creation
        if t_type == "create_private":
            guild_id = int(body.get("guild_id") or 0)
            guild = bot.get_guild(guild_id)
            if not guild:
                return web.json_response({"error": "Server not found."}, status=400)

            cat_id = body.get("private_category_id")
            category = guild.get_channel(int(cat_id)) if cat_id else None
            chan_name = body.get("private_channel_name") or "🎁-my-drops"
            member_id = body.get("private_member_id")

            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True),
            }
            if member_id:
                member = guild.get_member(int(member_id))
                if member:
                    overwrites[member] = discord.PermissionOverwrite(
                        view_channel=True, read_message_history=True, send_messages=True
                    )

            new_chan = await guild.create_text_channel(
                name=chan_name,
                category=category if isinstance(category, discord.CategoryChannel) else None,
                overwrites=overwrites,
                topic="Personal Twitch Drops Feed (Managed by Bot Web UI)",
            )
            t_type = "channel"
            target_id = str(new_chan.id)

        if not target_id:
            return web.json_response({"error": "Target User ID or Channel ID is required."}, status=400)

        feed = {
            "id": "feed_" + uuid.uuid4().hex[:8],
            "name": body.get("name") or "Custom Drops Feed",
            "enabled": True,
            "target_type": t_type,
            "target_id": target_id,
            "games": games,
            "edit_single_message": bool(body.get("edit_single_message", False)),
            "last_message_id": None,
        }
        config_mgr.data["feeds"].append(feed)
        config_mgr.save()
        return web.json_response({"ok": True, "feed": feed})

    async def delete_feed(request: web.Request) -> web.Response:
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        fid = request.match_info["id"]
        config_mgr.data["feeds"] = [f for f in config_mgr.data["feeds"] if f.get("id") != fid]
        config_mgr.save()
        return web.json_response({"ok": True})

    async def test_feed(request: web.Request) -> web.Response:
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        fid = request.match_info["id"]
        feed = next((f for f in config_mgr.data["feeds"] if f.get("id") == fid), None)
        if not feed:
            return web.json_response({"error": "Feed not found"}, status=404)

        embed = discord.Embed(
            title="🧪 Test Drops Feed Notification",
            description=(
                f"Your custom feed **{feed['name']}** is working!\n\n"
                f"**Monitored Games:** {', '.join(feed.get('games', []))}\n"
                f"**Mode:** {'Single Updating Embed' if feed.get('edit_single_message') else 'Cooldown Protected Alerts'}"
            ),
            color=0x9146FF,
        )
        try:
            if feed["target_type"] == "dm":
                user = bot.get_user(int(feed["target_id"])) or await bot.fetch_user(int(feed["target_id"]))
                await user.send(embed=embed)
            else:
                ch = bot.get_channel(int(feed["target_id"])) or await bot.fetch_channel(int(feed["target_id"]))
                await ch.send(embed=embed)
            return web.json_response({"ok": True})
        except Exception as e:
            return web.json_response({"error": f"Failed to send test message: {e}"}, status=400)

    async def update_spam_settings(request: web.Request) -> web.Response:
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        body = await request.json()
        ds = config_mgr.data["drops_settings"]
        ds["cooldown_hours"] = max(1, int(body.get("cooldown_hours", 48)))
        ds["min_drop_streams"] = max(1, int(body.get("min_drop_streams", 2)))
        ds["inactive_checks_to_reset"] = max(1, int(body.get("inactive_checks_to_reset", 6)))
        config_mgr.save()
        return web.json_response({"ok": True})

    async def check_now(request: web.Request) -> web.Response:
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        bot.loop.create_task(trigger_drops_cb())
        return web.json_response({"ok": True})

    async def reset_history(request: web.Request) -> web.Response:
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        body = await request.json()
        key = body.get("key")
        if key and key in config_mgr.data["campaign_history"]:
            config_mgr.data["campaign_history"].pop(key, None)
            config_mgr.save()
        return web.json_response({"ok": True})

    async def create_channel(request: web.Request) -> web.Response:
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        body = await request.json()
        guild = bot.get_guild(int(body.get("guild_id") or 0))
        if not guild:
            return web.json_response({"error": "Guild not found"}, status=404)

        name = (body.get("name") or "").strip()
        if not name:
            return web.json_response({"error": "Channel name is required"}, status=400)

        ctype = body.get("type", "text")
        cat_id = body.get("category_id")
        category = guild.get_channel(int(cat_id)) if cat_id else None

        overwrites = {}
        if body.get("is_private"):
            overwrites[guild.default_role] = discord.PermissionOverwrite(view_channel=False)
            overwrites[guild.me] = discord.PermissionOverwrite(view_channel=True, send_messages=True, connect=True)
            member_id = body.get("member_id")
            if member_id:
                member = guild.get_member(int(member_id))
                if member:
                    overwrites[member] = discord.PermissionOverwrite(
                        view_channel=True, send_messages=True, connect=True
                    )

        try:
            if ctype == "category":
                await guild.create_category(name=name, overwrites=overwrites or discord.utils.MISSING)
            elif ctype == "voice":
                limit = max(0, min(99, int(body.get("user_limit") or 0)))
                await guild.create_voice_channel(
                    name=name,
                    category=category if isinstance(category, discord.CategoryChannel) else None,
                    user_limit=limit or None,
                    overwrites=overwrites or discord.utils.MISSING,
                )
            else:
                topic = (body.get("topic") or "").strip() or None
                await guild.create_text_channel(
                    name=name,
                    category=category if isinstance(category, discord.CategoryChannel) else None,
                    topic=topic,
                    overwrites=overwrites or discord.utils.MISSING,
                )
            return web.json_response({"ok": True})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=400)

    async def move_channel(request: web.Request) -> web.Response:
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        body = await request.json()
        guild = bot.get_guild(int(body.get("guild_id") or 0))
        if not guild:
            return web.json_response({"error": "Guild not found"}, status=404)
        ch = guild.get_channel(int(body.get("channel_id") or 0))
        if not ch:
            return web.json_response({"error": "Channel not found"}, status=404)
        cat_id = body.get("category_id")
        category = guild.get_channel(int(cat_id)) if cat_id else None
        try:
            await ch.edit(category=category if isinstance(category, discord.CategoryChannel) else None)
            return web.json_response({"ok": True})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=400)

    async def delete_channel(request: web.Request) -> web.Response:
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        body = await request.json()
        guild = bot.get_guild(int(body.get("guild_id") or 0))
        if not guild:
            return web.json_response({"error": "Guild not found"}, status=404)
        ch = guild.get_channel(int(body.get("channel_id") or 0))
        if not ch:
            return web.json_response({"error": "Channel not found"}, status=404)
        try:
            await ch.delete()
            return web.json_response({"ok": True})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=400)

    async def save_lobby(request: web.Request) -> web.Response:
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        body = await request.json()
        vc_id = str(body.get("voice_channel_id") or "").strip()
        if not vc_id:
            return web.json_response({"error": "Voice channel is required"}, status=400)
        cat_id = body.get("target_category_id")
        config_mgr.data["lobbies"][vc_id] = {
            "target_category_id": int(cat_id) if cat_id else None,
            "name_template": body.get("name_template") or "🔊 {user}",
        }
        config_mgr.save()
        return web.json_response({"ok": True})

    async def delete_lobby(request: web.Request) -> web.Response:
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        cid = request.match_info["id"]
        config_mgr.data["lobbies"].pop(cid, None)
        config_mgr.save()
        return web.json_response({"ok": True})

    app.router.add_get("/", index)
    app.router.add_get("/api/state", get_state)
    app.router.add_post("/api/feeds", add_feed)
    app.router.add_delete("/api/feeds/{id}", delete_feed)
    app.router.add_post("/api/feeds/{id}/test", test_feed)
    app.router.add_post("/api/spam_settings", update_spam_settings)
    app.router.add_post("/api/drops/check_now", check_now)
    app.router.add_post("/api/history/reset", reset_history)
    app.router.add_post("/api/channels/create", create_channel)
    app.router.add_post("/api/channels/move", move_channel)
    app.router.add_post("/api/channels/delete", delete_channel)
    app.router.add_post("/api/lobbies", save_lobby)
    app.router.add_delete("/api/lobbies/{id}", delete_lobby)

    return app
