"""
Real Madrid news filter bot - with an AI "brain" that learns your taste (free via Groq).

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
  /brain             - what the bot has learned about you
  (forward a post)   - rate it 👍/👎 so the bot learns
  (any plain text)   - saved as a preference, e.g. "I don't care about loaned players"

Environment variables:
  SOURCE_CHANNELS  - source channels, comma separated, without @.
                     Add ":rm" to a general football channel to keep only Real Madrid posts.
                     Example: real_madrid, themadridzone, FabrizioRomanoTG:rm
  BOT_TOKEN        - from @BotFather
  TARGET_CHANNEL   - your channel: @my_channel or -100...
  OWNER_ID         - (optional) your Telegram user id. Otherwise the first person
                     who messages the bot becomes the owner.
  TIMEZONE         - (optional) default Asia/Jerusalem
  GROQ_API_KEY     - (optional, free) turns on the AI brain - console.groq.com
  ANTHROPIC_API_KEY- (optional, paid) use Claude instead of Groq
  AI_MODEL         - (optional) Groq model, default openai/gpt-oss-120b

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
GROQ_KEY = os.environ.get("GROQ_API_KEY") or None
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY") or None
AI_MODEL = os.environ.get("AI_MODEL") or "openai/gpt-oss-120b"
CLAUDE_MODEL = "claude-haiku-5-5"
AI_DAILY_CALLS = 900        # stay under Groq's free 1,000 requests/day
AI_DAILY_TOKENS = 185_000   # ...and 200,000 tokens/day
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

LOOP_INTERVAL = 60      # seconds between channel checks while awake
AWAKE_SECONDS = int(os.environ.get("AWAKE_SECONDS") or 150)  # each run stays awake this long
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
        "press conference", "presser", "florentino", "perez",
    ],
    "⚽ Result": [
        "full-time", "full time", "ft:", "final score", "final whistle", "goal!",
        "scores", "won", "beat", "defeat*", "draw", "win against",
    ],
    "📢 Official": ["official*", "confirm*", "statement", "announce*"],
    "📰 Other": [],  # only the AI uses this - important news that fits nowhere else
}
PLAYER_EXEMPT = {"📋 Lineup & Squad", "⚽ Result", "👔 Coach & Board"}  # pass the player filter
NEEDS_RM = {"👔 Coach & Board", "📢 Official"}  # must mention Real Madrid (not ex-players elsewhere)
URGENT = {"🚑 Injury", "✍️ Transfer & Contract", "👔 Coach & Board"}  # official ones ring at night

# Always dropped, even before the AI sees them (ads, gambling, promos)
HARD_IGNORE = [
    "promo code", "promocode", "bonus", "casino", "betting", "bet", "odds", "deposit",
    "sponsored", "#ad", "advertis*", "giveaway", "subscribe",
]
# Dropped by the keyword filter (the AI judges these itself)
SOFT_IGNORE = [
    "visit*", "hometown", "birthday", "vacation", "holiday*", "instagram", "tiktok",
    "girlfriend", "wedding", "fashion", "kit", "jersey", "mock-up*", "mockup*", "quiz",
    "poll", "vote", "throwback", "on this day", "#otd", "wallpaper*",
    "laporta",  # Barcelona president quotes
]
IGNORE = HARD_IGNORE + SOFT_IGNORE

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
RM_WORDS = ["real madrid", "realmadrid", "rmcf", "bernabeu", "los blancos", "hala madrid",
            "valdebebas", "castilla", "florentino", "mourinho"]  # add the coach's name if it changes

# Player-stat posts ("94 touches, 99% passing accuracy...") are dropped - unless they
# also carry real news (a final score, an injury, a transfer...)
STAT_WORDS = ["touches", "passing accuracy", "duels won", "man of the match", "motm",
              "rating*", "heatmap", "stats", "key passes", "dribbles"]

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
HARD_IGNORE_RE = kw_regex(HARD_IGNORE)
SOFT_IGNORE_RE = kw_regex(SOFT_IGNORE)
OFFICIAL_RE = kw_regex(OFFICIAL_MARKERS)
RUMOUR_RE = kw_regex(RUMOUR_MARKERS)
RM_RE = kw_regex(RM_WORDS)
STAT_RE = kw_regex(STAT_WORDS)
SCORE_RE = re.compile(r"(?<![\d.:/])\d{1,2}\s*[-–]\s*\d{1,2}(?![\d.:/%])")

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
    def __init__(self, src, pid, text, media, too_big, ts=None):
        self.src, self.id, self.text, self.media, self.too_big = src, pid, text, media, too_big
        self.ts = ts  # when it was posted (unix time) or None
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
        ts = None
        t = msg.select_one("time[datetime]")
        if t:
            try:
                ts = datetime.fromisoformat(t["datetime"]).timestamp()
            except ValueError:
                pass
        posts.append(Post(src, pid, text, media, too_big, ts))
    return sorted(posts, key=lambda p: p.id)


def fetch_posts(src, before=None):
    """Latest ~20 posts, or the ~20 posts just before post id `before`"""
    url = f"https://t.me/s/{src}" + (f"?before={before}" if before else "")
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
        if rx and rx.search(n):
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
# AI brain
# ---------------------------------------------------------------------------
GOAL = """You are the brain of a Telegram bot that runs ONE fan's private Real Madrid news channel.
His goal: a fast, noise-free channel with only the Real Madrid news that actually matters -
so he always knows what is happening at the club without scrolling through junk.
When unsure, think: would a serious Real Madrid fan be glad to get a notification about this?"""

BASE_TASTE = """DEFAULT TASTE
Send:
- Injuries, fitness news, players returning, how long someone is out
- Transfers, contracts, renewals, departures, buy-back clauses - involving Real Madrid's
  current squad, academy players the club still controls, or serious targets
- Official lineups and squad lists for Real Madrid matches
- Real Madrid first-team results and goals
- Suspensions, red cards, disciplinary decisions
- Real Madrid coach and board news, and the real news inside press conferences
- Official Real Madrid announcements
Skip:
- Stat lists and ratings with no other news ("94 touches, 99% passing...")
- Lifestyle: social media, birthdays, holidays, visits, family, fashion, cars
- Former Real Madrid players' news at other clubs
- Other clubs' news, and rival officials' quotes or arguments (e.g. Barcelona's president)
- Opinions, fan reactions, memes, polls, throwbacks, kit leaks, wallpapers, celebrations
- Ads, betting, promotions, 'join our channel' posts"""


def ai_provider():
    if ANTHROPIC_KEY:
        return "claude"
    if GROQ_KEY:
        return "groq"
    return None


def ai_usage(state):
    today = datetime.now(TZ).date().isoformat()
    u = state.setdefault("ai_usage", {})
    if u.get("date") != today:
        u.clear()
        u.update(date=today, calls=0, tokens=0)
    return u


def ai_budget_ok(state):
    u = ai_usage(state)
    if ai_provider() == "claude":
        return u["calls"] < 3000
    return u["calls"] < AI_DAILY_CALLS and u["tokens"] < AI_DAILY_TOKENS


def call_ai(state, prompt):
    """Ask the AI, return its JSON answer as a dict. Raises on any problem."""
    if not ai_budget_ok(state):
        raise RuntimeError("daily AI limit reached")
    if ai_provider() == "claude":
        r = requests.post("https://api.anthropic.com/v1/messages", timeout=60, headers={
            "x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
            "content-type": "application/json"}, json={
            "model": CLAUDE_MODEL, "max_tokens": 600,
            "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        d = r.json()
        out = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
        used = sum(d.get("usage", {}).get(k, 0) for k in ("input_tokens", "output_tokens"))
    else:
        body = {"model": AI_MODEL, "temperature": 0.1, "max_completion_tokens": 1200,
                "reasoning_effort": "low",
                "messages": [{"role": "user", "content": prompt}]}
        for attempt in range(3):
            r = requests.post("https://api.groq.com/openai/v1/chat/completions", timeout=60,
                              headers={"Authorization": f"Bearer {GROQ_KEY}"}, json=body)
            if r.status_code == 429:
                wait = float(r.headers.get("retry-after") or 30)
                if wait > 25 or attempt == 2:
                    raise RuntimeError(f"AI rate limit, retry in {wait:.0f}s")
                time.sleep(wait + 1)
                continue
            if r.status_code == 400 and "reasoning_effort" in body:
                body.pop("reasoning_effort")  # model doesn't support it
                continue
            break
        r.raise_for_status()
        d = r.json()
        out = d["choices"][0]["message"].get("content") or ""
        used = d.get("usage", {}).get("total_tokens", 0)
    u = ai_usage(state)
    u["calls"] += 1
    u["tokens"] += used
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        raise ValueError(f"AI gave no JSON: {out[:100]!r}")
    return json.loads(m.group(0))


def taste_text(state):
    """Everything the bot knows about what he likes - goes into every AI question."""
    parts = [GOAL, "", BASE_TASTE]
    prefs = state.get("prefs", [])
    if prefs:
        parts += ["", "HIS OWN WORDS - these override everything above:"]
        parts += [f"- {p}" for p in prefs]
    rules = state.get("rules", [])
    if rules:
        parts += ["", "LEARNED FROM HIS FEEDBACK:"] + [f"- {r}" for r in rules]
    fb = state.get("feedback", [])[-8:]
    if fb:
        parts += ["", "RECENT POSTS HE RATED:"]
        for f in fb:
            parts.append(f'{"WANTED" if f["v"] == "+" else "NOT WANTED"}: {f["t"][:200]}')
    players = state.get("players", [])
    if players:
        parts += ["", f"His favourite players: {', '.join(players)}"]
    return "\n".join(parts)


def ai_judge(state, text):
    now = time.time()
    recent = [s for s in state.get("sent", []) if now - s["ts"] < DEDUPE_HOURS * 3600][-12:]
    labels = ", ".join(f'"{l}"' for l in CATEGORIES)
    lines = [taste_text(state)]
    if recent:
        lines += ["", "ALREADY SENT TO HIS CHANNEL (last 2 days):"]
        lines += [f'{i}. [{s["status"]}] {s["headline"]}' for i, s in enumerate(recent, 1)]
    lines += [
        "", "NEW POST:", '"""', text[:1500], '"""', "",
        "Decide what to do with the new post. Reply with JSON only:",
        '{"send": true or false,',
        f' "category": one of {labels},',
        ' "status": "official" (confirmed by the club, or Fabrizio Romano\'s "here we go") |'
        ' "rumour" (reports, claims, interest, "could") | "fact" (lineups, results) | "none",',
        ' "duplicate_of": the number of the already-sent item that is the SAME story'
        ' (also when this one is the official confirmation of that rumour), or null if it is new'
        ' or adds important new details,',
        ' "big": true only for major news worth waking him up at night'
        ' (official signing or departure, serious injury, coach sacked),',
        ' "reason": "why, max 8 words"}',
    ]
    out = call_ai(state, "\n".join(lines))
    label = out.get("category")
    if label not in CATEGORIES:
        label = classify(norm(text)) or "📰 Other"
    status = out.get("status") if out.get("status") in ("official", "rumour", "fact", "none") else "none"
    dup = None
    d = out.get("duplicate_of")
    if isinstance(d, (int, float)) or (isinstance(d, str) and d.strip().isdigit()):
        i = int(d)
        if 1 <= i <= len(recent):
            dup = recent[i - 1]
    return (bool(out.get("send")), label, status, dup, bool(out.get("big")),
            str(out.get("reason", ""))[:80])


def learn(state, text, verdict):
    """After he rates a post, update the short list of learned rules. Returns what changed."""
    if not ai_provider():
        return None
    rules = state.get("rules", [])
    prefs = state.get("prefs", [])
    prompt = "\n".join([
        "You maintain a short list of rules describing one Real Madrid fan's news taste,",
        "used by a bot that filters news for his channel. Default taste:", BASE_TASTE, "",
        "His own words (never contradict these): " + ("; ".join(prefs) or "(none)"), "",
        "Current learned rules:", *([f"{i}. {r}" for i, r in enumerate(rules, 1)] or ["(none)"]), "",
        f"He just rated this post {'👍 WANTED' if verdict == '+' else '👎 NOT WANTED'}:",
        '"""', text[:900], '"""', "",
        "Update the rules so the bot gets posts like this right next time.",
        "Rules must be general (a TYPE of news, not this exact story), max 15 words each,",
        "max 12 rules, merge overlapping ones, drop rules this rating proves wrong.",
        "If the default taste already covers it, you may leave the rules unchanged.",
        'Reply JSON only: {"rules": [...], "learned": "what you added or changed, max 15 words, or empty"}',
    ])
    out = call_ai(state, prompt)
    new = [str(r).strip()[:160] for r in out.get("rules", []) if str(r).strip()][:12]
    if new or not rules:
        state["rules"] = new
    return str(out.get("learned") or "").strip()


def remember_skip(state, post, reason):
    """Keep the last skipped posts so he can say 'you should have sent this' (/skipped)."""
    sk = state.setdefault("skipped", [])
    sk.append({"src": post.src, "id": post.id, "t": post.text[:1200], "m": post.media[:4],
               "r": reason, "h": headline(post.text, 80)})
    del sk[:-15]


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------
TAGS = {"official": "✅ OFFICIAL", "rumour": "🟡 RUMOUR"}


def send_post(post, label, status, quiet, update, when=None):
    head = f"<b>{esc(label)}</b>"
    if when:  # older post sent by /backfill - show the original time
        head = f"🕒 {datetime.fromtimestamp(when, TZ):%a %d %b, %H:%M}\n" + head
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
def process_post(state, post, rm_only, backfill=False, force=None):
    """Decide about one post and send it if it's worth it. Returns a short log line.
    force=(label, status) skips the judging (used when he asks to post a skipped item)."""
    if not post.text:
        return "skip: no text"
    n = norm(post.text)
    prx = players_re(state)
    toks, names = tokens(post.text), names_of(post.text)
    big = False
    if force:
        label, status = force
        dup, how = None, "you"
    else:
        if HARD_IGNORE_RE.search(n):
            return "skip: ad/promo"
        if rm_only and not (RM_RE.search(n) or (prx and prx.search(n))):
            return "skip: not Real Madrid"

        verdict = None
        if ai_provider():
            try:
                verdict = ai_judge(state, post.text)
            except Exception as e:
                print(f"  AI unavailable ({e}) - using keywords")
        if verdict:
            send, label, status, dup, big, reason = verdict
            how = "AI"
            if not send:
                remember_skip(state, post, reason)
                return f"skip (AI): {reason}"
            if not dup:
                dup = find_duplicate(state, toks, names, label)
        else:
            how = "keywords"
            if SOFT_IGNORE_RE.search(n):
                return "skip: lifestyle"
            label = classify(n)
            if not label:
                remember_skip(state, post, "no important keywords")
                return "skip: not important"
            if label in NEEDS_RM and not (RM_RE.search(n) or (prx and prx.search(n))):
                return "skip: not about Real Madrid"
            if label == "⚽ Result" and STAT_RE.search(n) and not SCORE_RE.search(n):
                return "skip: player stats"
            status = get_status(n, label)
            dup = find_duplicate(state, toks, names, label)
            big = status == "official" and label in URGENT

        if not is_enabled(state, label):
            return f"skip: {label} is off"
        if opt(state, "players_only") and prx and label not in PLAYER_EXEMPT and not prx.search(n):
            return "skip: not about my players"

    update = False
    if dup:
        if status == "official" and dup["status"] != "official":
            update = True
            dup["superseded"] = True  # the summary shows only the official version
        else:
            return f"skip: duplicate [{how}]"

    quiet = backfill or (opt(state, "quiet") and in_quiet_hours() and not big)
    when = post.ts if backfill else None
    msg_id = send_post(post, label, status, quiet, update, when)
    posted = datetime.fromtimestamp(when, TZ) if when else datetime.now(TZ)
    state.setdefault("sent", []).append({
        "ts": time.time(), "date": posted.date().isoformat(),
        "tokens": sorted(toks), "names": sorted(names), "cat": label, "status": status,
        "headline": headline(post.text), "link": my_link(msg_id, post.link),
    })
    return f"SENT {label} {status} [{how}]" + (" (silent)" if quiet else "")


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


def run_backfill(state, deadline):
    """Send important posts from the last N hours (started by /backfill).
    Works in pieces: if time runs out it continues in the next run."""
    bf = state.get("backfill")
    if not bf:
        return
    for src, rm_only in SOURCES:
        if src in bf["done"]:
            continue
        until = bf["until"].get(src)
        if until is None:  # source added after the request
            bf["done"].append(src)
            continue
        upto = bf["upto"].get(src, 0)

        # page back through the channel until we pass the cutoff time
        found, before = {}, until + 1
        for _ in range(40):
            page = fetch_posts(src, before=before)
            if not page:
                break
            for p in page:
                if upto < p.id <= until and (p.ts is None or p.ts >= bf["cutoff"]):
                    found[p.id] = p
            oldest = page[0]
            if (oldest.ts and oldest.ts < bf["cutoff"]) or oldest.id <= upto or oldest.id >= before:
                break
            before = oldest.id
            time.sleep(1)

        for p in sorted(found.values(), key=lambda p: p.id):
            if time.time() > deadline:
                print("[backfill] out of time, continuing next run")
                save_state(state)
                return
            res = process_post(state, p, rm_only, backfill=True)
            print(f"[backfill] {src}/{p.id}: {res}")
            if res.startswith("SENT"):
                bf["sent"] += 1
                time.sleep(3)  # Telegram allows ~20 posts a minute in a channel
            bf["upto"][src] = p.id
            save_state(state)
        bf["done"].append(src)

    owner = OWNER_ID or state.get("owner")
    if owner:
        tg("sendMessage", chat_id=int(owner),
           text=f"✅ Done! Sent {bf['sent']} important posts from the last {bf['hours']} hours.")
    state.pop("backfill", None)
    print(f"[backfill] finished, {bf['sent']} sent")


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
        "/backfill 48 - post the important news from the last 48 hours\n"
        "/summary - preview today's summary\n\n"
        "🧠 <b>Teach me</b>\n"
        "• Forward me any post → rate it 👍/👎\n"
        "• Just write what you want, e.g. <i>I don't care about loaned players</i>\n"
        "• /skipped - posts I didn't send (tell me if I missed something)\n"
        "• /brain - what I've learned about you"
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


def clean_forward(t):
    """Remove our own header/footer lines from a forwarded channel post."""
    keep = []
    for line in t.splitlines():
        s = line.strip()
        if s.startswith(("🕒", "🔗")) or any(l in s for l in CATEGORIES):
            continue
        keep.append(line)
    return "\n".join(keep).strip()


def brain_text(state):
    prov = ai_provider()
    u = ai_usage(state)
    fb = state.get("feedback", [])
    lines = ["🧠 <b>My brain</b>"]
    if prov:
        name = "Claude" if prov == "claude" else f"Groq ({AI_MODEL})"
        lines.append(f"Status: ON - {esc(name)}")
        lines.append(f"Today: {u['calls']} AI checks, {u['tokens']:,} tokens")
    else:
        lines.append("Status: OFF - using the keyword filter (add GROQ_API_KEY to turn me on)")
    lines.append(f"Posts you rated: {sum(f['v'] == '-' for f in fb)} 👎, "
                 f"{sum(f['v'] == '+' for f in fb)} 👍")
    prefs = state.get("prefs", [])
    lines.append("\n<b>Your words</b> (/unprefer 2 to remove):")
    lines += [f"{i}. {esc(p)}" for i, p in enumerate(prefs, 1)] or ["(none yet - just write to me)"]
    rules = state.get("rules", [])
    lines.append("\n<b>What I learned from your ratings</b> (/forget 2 or /forget all):")
    lines += [f"{i}. {esc(r)}" for i, r in enumerate(rules, 1)] or ["(nothing yet - forward me posts)"]
    return "\n".join(lines)


def skipped_markup(sk):
    rows, row = [], []
    for i, _ in enumerate(sk, 1):
        row.append({"text": f"👍 {i}", "callback_data": f"sk:{i - 1}"})
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return {"inline_keyboard": rows}


def handle_message(state, m):
    uid, text = m["from"]["id"], (m.get("text") or "").strip()

    # a forwarded post -> ask whether he wants posts like it
    if m.get("forward_origin") or m.get("forward_date") or m.get("forward_from_chat"):
        ftext = clean_forward(m.get("text") or m.get("caption") or "")
        if not ftext:
            tg("sendMessage", chat_id=uid, text="I can only learn from posts that have text.")
            return
        pend = state.setdefault("pending_fb", {})
        key = str(m["message_id"])
        pend[key] = ftext[:1200]
        while len(pend) > 20:
            pend.pop(next(iter(pend)))
        tg("sendMessage", chat_id=uid, text="What about posts like this one?",
           reply_parameters={"message_id": m["message_id"]},
           reply_markup={"inline_keyboard": [[
               {"text": "👎 Not interested", "callback_data": f"fb:{key}:-"},
               {"text": "👍 More like this", "callback_data": f"fb:{key}:+"}]]})
        return

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
    elif cmd == "/backfill":
        hours = int(arg) if arg.strip().isdigit() else 48
        hours = max(1, min(hours, 72))
        state["backfill"] = {
            "hours": hours, "cutoff": time.time() - hours * 3600,
            "until": dict(state.get("last_post", {})), "upto": {}, "done": [], "sent": 0,
        }
        tg("sendMessage", chat_id=uid, text=(
            f"⏳ OK! In the next few minutes I'll post the important news from the last "
            f"{hours} hours to the channel (silently, with the original time). "
            "I'll tell you when it's done."))
    elif cmd == "/summary":
        tg("sendMessage", chat_id=uid, text=summary_text(state), parse_mode="HTML",
           link_preview_options={"is_disabled": True})
    elif cmd == "/brain":
        tg("sendMessage", chat_id=uid, text=brain_text(state), parse_mode="HTML")
    elif cmd == "/prefer" and arg.strip():
        state.setdefault("prefs", []).append(arg.strip()[:200])
        tg("sendMessage", chat_id=uid, text=f"🧠 Saved: \"{arg.strip()[:200]}\"\nSee everything with /brain")
    elif cmd == "/unprefer":
        prefs = state.setdefault("prefs", [])
        i = int(arg) if arg.strip().isdigit() else 0
        if 1 <= i <= len(prefs):
            gone = prefs.pop(i - 1)
            tg("sendMessage", chat_id=uid, text=f"🗑 Removed: \"{gone}\"")
        else:
            tg("sendMessage", chat_id=uid, text="Which one? See the numbers in /brain, then e.g. /unprefer 2")
    elif cmd == "/forget":
        rules = state.setdefault("rules", [])
        if arg.strip().lower() == "all":
            state["rules"], state["feedback"] = [], []
            tg("sendMessage", chat_id=uid, text="🧹 Forgot everything I learned from your ratings.")
        elif arg.strip().isdigit() and 1 <= int(arg) <= len(rules):
            gone = rules.pop(int(arg) - 1)
            tg("sendMessage", chat_id=uid, text=f"🗑 Forgot: \"{gone}\"")
        else:
            tg("sendMessage", chat_id=uid, text="Which one? See the numbers in /brain, then e.g. /forget 2")
    elif cmd == "/skipped":
        sk = state.get("skipped", [])[-10:]
        if not sk:
            tg("sendMessage", chat_id=uid, text="Nothing skipped recently.")
        else:
            lines = ["🙈 <b>Posts I didn't send</b> - tap 👍 if I should have (I'll post it and learn):\n"]
            for i, x in enumerate(sk, 1):
                lines.append(f'{i}. <a href="https://t.me/{x["src"]}/{x["id"]}">{esc(x["h"])}</a>'
                             f'\n    <i>{esc(x["r"])}</i>')
            state["skipped_view"] = sk
            tg("sendMessage", chat_id=uid, text="\n".join(lines), parse_mode="HTML",
               link_preview_options={"is_disabled": True}, reply_markup=skipped_markup(sk))
    elif text and not text.startswith("/"):
        # plain words = a preference in his own words
        prefs = state.setdefault("prefs", [])
        prefs.append(text[:200])
        tg("sendMessage", chat_id=uid, parse_mode="HTML",
           text=f"🧠 Got it, I'll remember:\n<i>{esc(text[:200])}</i>\n\n"
                "(/brain shows everything I know · /settings for the menu)",
           reply_markup={"inline_keyboard": [[
               {"text": "↩️ Undo", "callback_data": f"up:{len(prefs) - 1}"}]]})
    else:
        show_menu(state, uid)


def handle_callback(state, q):
    kind, _, val = (q.get("data") or ":").partition(":")
    chat, msg_id = q["message"]["chat"]["id"], q["message"]["message_id"]

    if kind == "fb":  # rated a forwarded post
        key, _, v = val.partition(":")
        t = state.get("pending_fb", {}).pop(key, None)
        tg("answerCallbackQuery", callback_query_id=q["id"], text="Thanks!")
        if t is None:
            tg("editMessageText", chat_id=chat, message_id=msg_id, text="Already saved ✔")
            return
        fb = state.setdefault("feedback", [])
        fb.append({"t": t, "v": v})
        del fb[:-30]
        msg = "👍 Got it - more like this." if v == "+" else "👎 Got it - I'll skip posts like this."
        try:
            learned = learn(state, t, v)
            if learned:
                msg += f"\n🧠 Learned: {learned}"
        except Exception as e:
            print(f"learn failed: {e}")
        if not ai_provider():
            msg += "\n(I'll use this once the AI brain is turned on.)"
        tg("editMessageText", chat_id=chat, message_id=msg_id, text=msg)
        return

    if kind == "up":  # undo a preference
        prefs = state.setdefault("prefs", [])
        i = int(val) if val.isdigit() else -1
        if 0 <= i < len(prefs):
            prefs.pop(i)
        tg("answerCallbackQuery", callback_query_id=q["id"], text="Undone")
        tg("editMessageText", chat_id=chat, message_id=msg_id, text="↩️ Undone - forgotten.")
        return

    if kind == "sk":  # "you should have sent this"
        view = state.get("skipped_view", [])
        i = int(val) if val.isdigit() else -1
        tg("answerCallbackQuery", callback_query_id=q["id"], text="Posting it and learning…")
        if 0 <= i < len(view) and not view[i].get("done"):
            x = view[i]
            x["done"] = True
            fb = state.setdefault("feedback", [])
            fb.append({"t": x["t"][:1200], "v": "+"})
            del fb[:-30]
            post = Post(x["src"], x["id"], x["t"], [tuple(mm) for mm in x.get("m", [])], False)
            n = norm(x["t"])
            label = classify(n) or "📰 Other"
            res = process_post(state, post, False, force=(label, get_status(n, label)))
            msg = f"✅ Posted #{i + 1} to the channel." if res.startswith("SENT") else f"Couldn't post #{i + 1}."
            try:
                learned = learn(state, x["t"], "+")
                if learned:
                    msg += f"\n🧠 Learned: {learned}"
            except Exception as e:
                print(f"learn failed: {e}")
            tg("sendMessage", chat_id=chat, text=msg)
        return

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
    tg("editMessageReplyMarkup", chat_id=chat, message_id=msg_id, reply_markup=menu_markup(state))
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
def check_all(state, deadline=None):
    deadline = min(deadline or time.time() + 200, time.time() + 200)
    for src, rm_only in SOURCES:
        try:
            check_source(state, src, rm_only)
        except Exception as e:
            print(f"{src}: error: {e}")
    try:
        run_backfill(state, deadline)
    except Exception as e:
        print(f"backfill error: {e}")
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
        {"command": "backfill", "description": "Post important news from the last 48h"},
        {"command": "brain", "description": "What I've learned about your taste"},
        {"command": "skipped", "description": "Posts I didn't send - tell me if I missed one"},
    ])
    print(f"brain: {ai_provider() or 'off (keyword filter)'}")

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
        # stay awake for AWAKE_SECONDS: answer messages within seconds, check channels every minute
        end = time.time() + AWAKE_SECONDS
        last_check = 0.0
        while True:
            if time.time() - last_check >= LOOP_INTERVAL:
                check_all(state, deadline=end)
                last_check = time.time()
            left = end - time.time()
            if left <= 1:
                break
            try:
                handle_updates(state, timeout=int(min(20, left)))  # waits for messages
            except Exception as e:
                print(f"error: {e}")
                time.sleep(3)
        save_state(state)


if __name__ == "__main__":
    main()
