"""
Real Madrid news filter bot - free, no AI needed.

Reads public Telegram channels, keeps only the important news (injuries, transfers,
lineups...), removes duplicates and ads, tags RUMOUR / OFFICIAL, and posts to your
own channel with the photos and videos.

Send the bot a private message to open the settings menu (buttons).
Commands in the private chat:
  /settings          - settings menu
  /add Vinicius, Mbappe   - add players to your list
  /remove Mbappe     - remove a player
  /players           - show your list
  /summary           - preview today's summary

Environment variables:
  SOURCE_CHANNELS  - source channels, comma separated, without @.
                     Add ":rm" to a general football channel to keep only Real Madrid posts.
                     Example: real_madrid, themadridzone, FabrizioRomanoTG:rm
  BOT_TOKEN        - from @BotFather
  TARGET_CHANNEL   - your channel: @my_channel or -100...
  OWNER_ID         - (optional) your Telegram user id. Otherwise the first person
                     who messages the bot becomes the owner.
  TIMEZONE         - (optional) default Asia/Jerusalem

Run:
  python bot.py          # one check (GitHub Actions)
  python bot.py --loop   # runs forever, buttons react instantly (computer/server)
"""
import html
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def parse_sources(raw):
    out = []
    for part in raw.split(","):
        name, _, flag = part.strip().lstrip("@").partition(":")
        if name.strip():
            out.append((name.strip().lstrip("@"), flag.strip().lower() == "rm"))
    return out


SOURCES = parse_sources(os.environ.get("SOURCE_CHANNELS") or os.environ.get("SOURCE_CHANNEL", ""))
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
TARGET = os.environ.get("TARGET_CHANNEL", "")
OWNER_ID = os.environ.get("OWNER_ID") or None
TZ = ZoneInfo(os.environ.get("TIMEZONE") or "Asia/Jerusalem")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

LOOP_INTERVAL = 60      # --loop: seconds between channel checks
QUIET_START, QUIET_END = 23, 8   # silent notifications between these hours
SUMMARY_HOUR = 22       # daily summary is sent after this hour
DEDUPE_HOURS = 48       # how far back to look for duplicates
DEDUPE_HOURS_SHORT = 6  # for lineups/results (they repeat every match)
SIMILARITY = 0.5        # 0-1, lower = more aggressive duplicate removal

# ---------------------------------------------------------------------------
# What counts as important.
# A word ending with * also matches longer words ("injur*" -> injury, injured).
# ---------------------------------------------------------------------------
CATEGORIES = {
    "🚑 Injury": [
        "injur*", "sidelined", "surgery", "hamstring", "muscle*", "strain", "sprain*",
        "ligament*", "acl", "fracture*", "ruled out", "will miss", "to miss", "doubt for",
        "doubtful", "medical report", "medical update", "back in training",
        "return* to training", "fit again", "knock",
    ],
    "✍️ Transfer & Contract": [
        "here we go", "sign*", "transfer*", "loan*", "contract*", "extension", "renew*",
        "bid", "offer*", "release clause", "agreement", "deal", "joins", "free agent",
        "buy-back", "buyback", "departure", "leaves", "leaving", "interest*", "target*",
        "move to", "move for", "swoop", "approach*", "negotiat*", "talks",
    ],
    "📋 Lineup & Squad": [
        "lineup*", "line-up*", "line up", "starting xi", "xi vs", "xi:", "squad list",
        "called up", "squad for", "bench:", "subs:",
    ],
    "🟥 Discipline": [
        "suspen*", "red card", "sent off", "banned", "ban", "fifth yellow", "5th yellow",
    ],
    "👔 Coach & Board": [
        "sacked", "fired", "new manager", "new coach", "head coach", "appointed",
        "press conference", "presser", "florentino", "president",
    ],
    "⚽ Result": [
        "full-time", "full time", "ft:", "final score", "final whistle", "goal!",
        "scores", "won", "beat", "defeat*", "draw", "win against",
    ],
    "📢 Official": ["official*", "confirm*", "statement", "announce*"],
}
PLAYER_EXEMPT = {"📋 Lineup & Squad", "⚽ Result", "👔 Coach & Board"}  # pass the player filter
URGENT = {"🚑 Injury", "✍️ Transfer & Contract", "👔 Coach & Board"}  # official ones ring at night

# Posts with any of these are dropped (lifestyle, ads, gambling, polls...)
IGNORE = [
    "visit*", "hometown", "birthday", "vacation", "holiday*", "instagram", "tiktok",
    "girlfriend", "wedding", "fashion", "promo code", "promocode", "bonus", "casino",
    "betting", "bet", "odds", "deposit", "sponsored", "#ad", "advertis*", "giveaway",
    "subscribe", "kit", "jersey", "mock-up*", "mockup*", "quiz", "poll", "vote",
    "throwback", "on this day", "#otd", "wallpaper*",
]

OFFICIAL_MARKERS = [
    "official*", "confirm*", "announce*", "statement", "medical report", "here we go",
]
RUMOUR_MARKERS = [
    "report*", "according to", "rumou*", "rumor*", "claim*", "could", "set to", "close to",
    "talks", "interest*", "linked", "consider*", "monitor*", "understand*", "sources",
    "per", "eyeing", "target*", "want*", "plan*", "expected to", "likely", "via",
    "(marca)", "(as)", "(relevo)", "(mundo deportivo)",
]

# For ":rm" sources - post must mention Real Madrid (or one of your players)
RM_WORDS = ["real madrid", "realmadrid", "rmcf", "bernabeu", "los blancos", "hala madrid"]

OPTIONS = {
    "quiet": ("🌙 Quiet at night (23:00-08:00)", True),
    "summary": ("📝 Daily summary (22:00)", True),
    "players_only": ("🎯 Only my players", False),
}
MENU_LABELS = list(CATEGORIES.keys())


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------
def norm(s):
    """lowercase + remove accents (Ramón -> ramon, Güler -> guler)"""
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c)).replace("’", "'")


def kw_regex(words):
    parts = []
    for w in words:
        w = norm(w)
        prefix = w.endswith("*")
        w = w.rstrip("*")
        if w:
            parts.append(r"(?<![a-z0-9])" + re.escape(w) + ("" if prefix else r"(?![a-z0-9])"))
    return re.compile("|".join(parts)) if parts else None


CAT_RE = {label: kw_regex(words) for label, words in CATEGORIES.items()}
IGNORE_RE = kw_regex(IGNORE)
OFFICIAL_RE = kw_regex(OFFICIAL_MARKERS)
RUMOUR_RE = kw_regex(RUMOUR_MARKERS)
RM_RE = kw_regex(RM_WORDS)

STOP = set("""the a an and or of to in on for with at by from is are was were be been has have
had will would this that it its as his her their our we you they he she them after before about
into over vs real madrid rmcf breaking news update just more very club player players not but
also out new now today tonight""".split())

COMMON_CAPS = set("""the a an he his she her they it this that we our official officially breaking
here go done deal real madrid rmcf according sources report medical club statement update news
confirmed injury lineup starting full time final after before and but just exclusive understand
told per via la liga laliga champions league uefa copa del rey""".split())


def tokens(text):
    t = re.sub(r"https?://\S+|@\w+|#\w+", " ", norm(text))
    out = set()
    for w in re.findall(r"[a-z0-9]+", t):
        if w in STOP or (len(w) < 3 and not w.isdigit()):
            continue
        for suf in ("ly", "ing", "ed", "es", "s"):
            if len(w) > 4 and w.endswith(suf):
                w = w[: -len(suf)]
                break
        out.add(w)
    return out


def names_of(text):
    """Proper nouns (rough): capitalised words that aren't common words"""
    t = unicodedata.normalize("NFKD", text)
    t = "".join(c for c in t if not unicodedata.combining(c))
    return {w.lower() for w in re.findall(r"\b[A-Z][a-z]{2,}\b", t)} - COMMON_CAPS


def similarity(a, b):
    if not a or not b:
        return 0.0
    inter = len(a & b)
    jac = inter / len(a | b)
    small = min(len(a), len(b))
    cont = inter / small if small >= 5 else 0.0
    return max(jac, cont * 0.85)


def esc(s):
    return html.escape(s, quote=False)


def headline(text, n=110):
    line = next((l.strip() for l in text.splitlines() if l.strip()), "")
    return line if len(line) <= n else line[: n - 1] + "…"


# ---------------------------------------------------------------------------
# Telegram API
# ---------------------------------------------------------------------------
def tg(method, **params):
    try:
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
                          json=params, timeout=60)
        data = r.json()
    except Exception as e:
        print(f"Telegram {method} error: {e}")
        return {"ok": False}
    if r.status_code == 429:
        time.sleep(data.get("parameters", {}).get("retry_after", 5) + 1)
        return tg(method, **params)
    if not data.get("ok"):
        print(f"Telegram {method} failed: {data.get('description')}")
    return data


# ---------------------------------------------------------------------------
# Reading channels (public web view t.me/s/<name>)
# ---------------------------------------------------------------------------
class Post:
    def __init__(self, src, pid, text, media, too_big):
        self.src, self.id, self.text, self.media, self.too_big = src, pid, text, media, too_big
        self.link = f"https://t.me/{src}/{pid}"


def parse_page(src, page_html):
    soup = BeautifulSoup(page_html, "html.parser")
    posts = []
    for msg in soup.select("div.tgme_widget_message[data-post]"):
        try:
            pid = int(msg["data-post"].split("/")[-1])
        except ValueError:
            continue
        text = ""
        text_el = msg.select_one("div.tgme_widget_message_text")
        if text_el:
            for br in text_el.find_all("br"):
                br.replace_with("\n")
            text = text_el.get_text().strip()
        media, too_big = [], False
        for el in msg.select(".tgme_widget_message_photo_wrap, .tgme_widget_message_video_player"):
            if "tgme_widget_message_video_player" in el.get("class", []):
                v = el.select_one("video")
                if v and v.get("src"):
                    media.append(("video", v["src"]))
                else:
                    too_big = True
            else:
                m = re.search(r"background-image:url\('([^']+)'\)", el.get("style", ""))
                if m:
                    media.append(("photo", m.group(1)))
        if msg.select_one(".message_media_not_supported"):
            too_big = True
        posts.append(Post(src, pid, text, media, too_big))
    return sorted(posts, key=lambda p: p.id)


def fetch_posts(src):
    url = f"https://t.me/s/{src}"
    r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    posts = parse_page(src, r.text)
    if not posts and "tgme_channel_info" not in r.text:
        raise RuntimeError(f"can't read {url} - is the channel public and the name right?")
    return posts


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def is_enabled(state, label):
    return state.get("enabled", {}).get(label, True)


def opt(state, key):
    return state.get("opts", {}).get(key, OPTIONS[key][1])


def players_re(state):
    return kw_regex(state.get("players", []))


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------
def classify(n):
    for label, rx in CAT_RE.items():
        if rx.search(n):
            return label
    return None


def get_status(n, label):
    if OFFICIAL_RE.search(n):
        return "official"
    if RUMOUR_RE.search(n):
        return "rumour"
    return "fact" if label in ("📋 Lineup & Squad", "⚽ Result") else "none"


def find_duplicate(state, toks, names, label):
    now = time.time()
    for s in reversed(state.get("sent", [])):
        hours = DEDUPE_HOURS_SHORT if label in ("📋 Lineup & Squad", "⚽ Result") else DEDUPE_HOURS
        if now - s["ts"] > hours * 3600:
            continue
        old_names = set(s["names"])
        if names and old_names and not (names & old_names):
            continue  # about different people -> not the same story
        if similarity(toks, set(s["tokens"])) >= SIMILARITY:
            return s
    return None


def in_quiet_hours():
    h = datetime.now(TZ).hour
    return h >= QUIET_START or h < QUIET_END


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------
TAGS = {"official": "✅ OFFICIAL", "rumour": "🟡 RUMOUR"}


def send_post(post, label, status, quiet, update):
    head = f"<b>{esc(label)}</b>"
    if status in TAGS:
        head += f"  ·  {TAGS[status]}"
    if update:
        head += "  ·  🔁 now official"
    foot = f'\n\n🔗 <a href="{post.link}">@{post.src}</a>'
    if post.too_big:
        foot += "  ·  🎬 video in source"
    body = esc(post.text)
    if len(head) + len(body) + len(foot) > 4000:
        body = body[: 4000 - len(head) - len(foot)] + "…"
    full = f"{head}\n\n{body}{foot}"
    common = {"chat_id": TARGET, "disable_notification": quiet}

    media = post.media[:10]
    if media:
        caption = full if len(full) <= 1000 else None
        if len(media) == 1:
            kind, url = media[0]
            params = dict(common, **{kind: url, "parse_mode": "HTML"})
            if caption:
                params["caption"] = caption
            r = tg("sendPhoto" if kind == "photo" else "sendVideo", **params)
        else:
            items = [{"type": k, "media": u} for k, u in media]
            if caption:
                items[0].update(caption=caption, parse_mode="HTML")
            r = tg("sendMediaGroup", media=items, **common)
        if r.get("ok"):
            res = r["result"]
            first_id = (res[0] if isinstance(res, list) else res)["message_id"]
            if not caption:  # text too long for a caption -> send it right after
                tg("sendMessage", text=full, parse_mode="HTML",
                   link_preview_options={"is_disabled": True},
                   reply_parameters={"message_id": first_id}, **common)
            return first_id
        print("  media failed, sending text with a preview of the source instead")

    r = tg("sendMessage", text=full, parse_mode="HTML",
           link_preview_options={"is_disabled": not (media or post.too_big)}, **common)
    if not r.get("ok"):
        raise RuntimeError(f"sending to channel failed: {r.get('description')}")
    return r["result"]["message_id"]


def my_link(msg_id, fallback):
    if TARGET.startswith("@"):
        return f"https://t.me/{TARGET[1:]}/{msg_id}"
    return fallback


# ---------------------------------------------------------------------------
# Processing posts
# ---------------------------------------------------------------------------
def process_post(state, post, rm_only):
    if not post.text:
        return "skip: no text"
    n = norm(post.text)
    if IGNORE_RE.search(n):
        return "skip: lifestyle/ad"
    prx = players_re(state)
    if rm_only and not (RM_RE.search(n) or (prx and prx.search(n))):
        return "skip: not Real Madrid"
    label = classify(n)
    if not label:
        return "skip: not important"
    if not is_enabled(state, label):
        return f"skip: {label} is off"
    if opt(state, "players_only") and prx and label not in PLAYER_EXEMPT and not prx.search(n):
        return "skip: not about my players"

    status = get_status(n, label)
    toks, names = tokens(post.text), names_of(post.text)
    dup = find_duplicate(state, toks, names, label)
    update = False
    if dup:
        if status == "official" and dup["status"] != "official":
            update = True
            dup["superseded"] = True  # the summary shows only the official version
        else:
            return "skip: duplicate"

    quiet = opt(state, "quiet") and in_quiet_hours() and not (
        status == "official" and label in URGENT)
    msg_id = send_post(post, label, status, quiet, update)
    state.setdefault("sent", []).append({
        "ts": time.time(), "date": datetime.now(TZ).date().isoformat(),
        "tokens": sorted(toks), "names": sorted(names), "cat": label, "status": status,
        "headline": headline(post.text), "link": my_link(msg_id, post.link),
    })
    return f"SENT {label} {status}" + (" (silent)" if quiet else "")


def check_source(state, src, rm_only):
    last = state.setdefault("last_post", {})
    posts = fetch_posts(src)
    if not posts:
        return
    if src not in last:  # first run: don't flood with old posts
        last[src] = posts[-1].id
        print(f"{src}: first run, starting after post {posts[-1].id}")
        return
    for post in posts:
        if post.id <= last[src]:
            continue
        print(f"{src}/{post.id}: {process_post(state, post, rm_only)}")
        last[src] = post.id
        save_state(state)
        time.sleep(1)


def summary_text(state):
    now = datetime.now(TZ)
    today = now.date().isoformat()
    items = [s for s in state.get("sent", []) if s["date"] == today and not s.get("superseded")]
    lines = [f"<b>📝 Daily summary · {now:%a %d %b}</b>"]
    if not items:
        lines.append("\nA quiet day - nothing important.")
    for label in CATEGORIES:
        group = [s for s in items if s["cat"] == label]
        if not group:
            continue
        lines.append(f"\n<b>{esc(label)}</b>")
        for s in group:
            tag = {"official": "✅ ", "rumour": "🟡 "}.get(s["status"], "")
            line = f'• {tag}<a href="{s["link"]}">{esc(s["headline"])}</a>'
            if sum(len(l) for l in lines) + len(line) > 3800:
                lines.append("…")
                return "\n".join(lines)
            lines.append(line)
    return "\n".join(lines)


def maybe_send_summary(state):
    now = datetime.now(TZ)
    today = now.date().isoformat()
    if not opt(state, "summary") or now.hour < SUMMARY_HOUR or state.get("summary_date") == today:
        return
    tg("sendMessage", chat_id=TARGET, text=summary_text(state), parse_mode="HTML",
       link_preview_options={"is_disabled": True})
    state["summary_date"] = today
    print("daily summary sent")


def prune(state):
    cutoff = time.time() - max(DEDUPE_HOURS, 26) * 3600
    state["sent"] = [s for s in state.get("sent", []) if s["ts"] >= cutoff]


# ---------------------------------------------------------------------------
# Settings menu (private chat with the bot)
# ---------------------------------------------------------------------------
def menu_text(state):
    players = state.get("players", [])
    p = ", ".join(players) if players else "(empty)"
    return (
        "⚙️ <b>Settings</b> - tap to turn on ✅ / off ❌\n\n"
        f"🎯 My players: {esc(p)}\n"
        "/add Vinicius, Mbappe - add players\n"
        "/remove Mbappe - remove\n"
        "/summary - preview today's summary"
    )


def menu_markup(state):
    rows = [[{"text": f"{'✅' if is_enabled(state, l) else '❌'} {l}", "callback_data": f"t:{i}"}]
            for i, l in enumerate(MENU_LABELS)]
    rows += [[{"text": f"{'✅' if opt(state, k) else '❌'} {name}", "callback_data": f"o:{k}"}]
             for k, (name, _) in OPTIONS.items()]
    rows.append([{"text": "✅ All topics on", "callback_data": "all:1"},
                 {"text": "❌ All topics off", "callback_data": "all:0"}])
    return {"inline_keyboard": rows}


def is_owner(state, uid):
    owner = OWNER_ID or state.get("owner")
    if owner is None:
        state["owner"] = uid
        return True
    return int(owner) == uid


def show_menu(state, chat_id):
    tg("sendMessage", chat_id=chat_id, text=menu_text(state), parse_mode="HTML",
       reply_markup=menu_markup(state))


def handle_message(state, m):
    uid, text = m["from"]["id"], (m.get("text") or "").strip()
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    players = state.setdefault("players", [])
    names = [x.strip() for x in arg.split(",") if x.strip()]

    if cmd == "/add" and names:
        for n in names:
            if norm(n) not in map(norm, players):
                players.append(n)
        msg = f"✅ Added. My players: {', '.join(players)}"
        if not opt(state, "players_only"):
            msg += "\n\nTurn on 🎯 Only my players in /settings to filter by them."
        tg("sendMessage", chat_id=uid, text=msg)
    elif cmd == "/remove" and names:
        drop = {norm(n) for n in names}
        state["players"] = [p for p in players if norm(p) not in drop]
        tg("sendMessage", chat_id=uid,
           text=f"🗑 Removed. My players: {', '.join(state['players']) or '(empty)'}")
    elif cmd == "/players":
        tg("sendMessage", chat_id=uid, text=f"🎯 My players: {', '.join(players) or '(empty)'}")
    elif cmd == "/summary":
        tg("sendMessage", chat_id=uid, text=summary_text(state), parse_mode="HTML",
           link_preview_options={"is_disabled": True})
    else:
        show_menu(state, uid)


def handle_callback(state, q):
    kind, _, val = (q.get("data") or ":").partition(":")
    enabled = state.setdefault("enabled", {})
    opts = state.setdefault("opts", {})
    if kind == "t" and val.isdigit() and int(val) < len(MENU_LABELS):
        label = MENU_LABELS[int(val)]
        enabled[label] = not is_enabled(state, label)
    elif kind == "o" and val in OPTIONS:
        opts[val] = not opt(state, val)
    elif kind == "all":
        for label in MENU_LABELS:
            enabled[label] = val == "1"
    tg("editMessageReplyMarkup", chat_id=q["message"]["chat"]["id"],
       message_id=q["message"]["message_id"], reply_markup=menu_markup(state))
    tg("answerCallbackQuery", callback_query_id=q["id"], text="Saved ✔")


def handle_updates(state, timeout=0):
    res = tg("getUpdates", offset=state.get("offset", 0), timeout=timeout,
             allowed_updates=["message", "callback_query"])
    for u in res.get("result", []):
        state["offset"] = u["update_id"] + 1
        if "message" in u and u["message"]["chat"]["type"] == "private":
            m = u["message"]
            if is_owner(state, m["from"]["id"]):
                handle_message(state, m)
            else:
                tg("sendMessage", chat_id=m["from"]["id"], text="⛔ This bot is private.")
        elif "callback_query" in u:
            q = u["callback_query"]
            if is_owner(state, q["from"]["id"]):
                handle_callback(state, q)
            else:
                tg("answerCallbackQuery", callback_query_id=q["id"], text="⛔")
    save_state(state)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def check_all(state):
    for src, rm_only in SOURCES:
        try:
            check_source(state, src, rm_only)
        except Exception as e:
            print(f"{src}: error: {e}")
    prune(state)
    maybe_send_summary(state)
    save_state(state)


def main():
    missing = [n for n, v in [("SOURCE_CHANNELS", SOURCES), ("BOT_TOKEN", BOT_TOKEN),
                              ("TARGET_CHANNEL", TARGET)] if not v]
    if missing:
        sys.exit(f"missing environment variables: {', '.join(missing)}")

    state = load_state()
    tg("setMyCommands", commands=[
        {"command": "settings", "description": "Choose topics and options"},
        {"command": "add", "description": "Add players: /add Vinicius, Mbappe"},
        {"command": "remove", "description": "Remove a player"},
        {"command": "players", "description": "Show my players"},
        {"command": "summary", "description": "Preview today's summary"},
    ])

    if "--loop" in sys.argv:
        last_check = 0.0
        while True:
            try:
                handle_updates(state, timeout=20)
                if time.time() - last_check >= LOOP_INTERVAL:
                    check_all(state)
                    last_check = time.time()
            except Exception as e:
                print(f"error: {e}")
                time.sleep(5)
    else:
        handle_updates(state)
        check_all(state)


if __name__ == "__main__":
    main()
