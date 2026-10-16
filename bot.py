"""
Real Madrid news bot - every post sorted into a topic. You turn topics on/off.

Reads public Telegram channels, puts every Real Madrid post into ONE topic
(injuries, transfers, match events, quotes, social media...), and posts the topics
you have turned on to your channel - with photos/videos, no duplicates, no ads,
tagged OFFICIAL / RUMOUR / NEWS and how reliable the source is.

Private chat with the bot:
  /settings               - turn topics on/off (buttons)
  /add Vinicius, Mbappe   - your players (for "Only my players")
  /remove Mbappe          - remove a player
  /players                - show your players
  /summary                - preview today's summary
  /backfill 48            - post the last 48 hours again (only topics that are on)
  /status                 - is everything working?

Environment variables:
  SOURCE_CHANNELS  - source channels, comma separated, without @.
                     ":rm" = general football channel, keep only Real Madrid posts.
                     Example: real_madrid, themadridzone, FabrizioRomanoTG:rm
  BOT_TOKEN        - from @BotFather
  TARGET_CHANNEL   - your channel: @my_channel or -100...
  OWNER_ID         - (optional) your Telegram user id. Otherwise the first person
                     who messages the bot becomes the owner.
  TIMEZONE         - (optional) default Asia/Jerusalem
  GROQ_API_KEY     - (optional, free) the AI sorts posts much better - console.groq.com
  ANTHROPIC_API_KEY- (optional, paid) use Claude instead of Groq
  AI_MODEL         - (optional) Groq model, default openai/gpt-oss-120b
  AWAKE_SECONDS    - (optional) how long each run stays awake, default 150

Run:
  python bot.py          # one run (GitHub Actions) - stays awake AWAKE_SECONDS
  python bot.py --loop   # runs forever (computer/server)
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
AWAKE_SECONDS = int(os.environ.get("AWAKE_SECONDS") or 150)
QUIET_START, QUIET_END = 23, 8   # silent notifications between these hours
SUMMARY_HOUR = 22       # daily summary is sent after this hour
DEDUPE_HOURS = 48       # how far back to look for duplicates
DEDUPE_HOURS_SHORT = 6  # for lineups / match events (they repeat every match)
SIMILARITY = 0.5        # 0-1, lower = more aggressive duplicate removal

# ---------------------------------------------------------------------------
# Topics. Every post gets exactly one. (key, button label, what it is, keywords)
# The AI uses the descriptions; the keywords are the backup when the AI is off.
# A keyword ending with * also matches longer words ("injur*" -> injury, injured).
# ---------------------------------------------------------------------------
TOPICS = [
    ("injury", "🚑 Injuries & fitness",
     "injuries, fitness, medical updates, players returning, how long someone is out",
     ["injur*", "sidelined", "surgery", "hamstring", "muscle*", "strain", "sprain*", "ligament*",
      "acl", "fracture*", "ruled out", "will miss", "doubt for", "doubtful", "medical report",
      "medical update", "back in training", "return* to training", "fit again", "knock",
      "recover*", "discomfort"]),
    ("transfer", "✍️ Transfers & contracts",
     "signings, departures, transfer rumours, contracts, renewals, buy-back clauses, targets",
     ["here we go", "sign*", "transfer*", "contract*", "extension", "renew*", "bid", "offer*",
      "release clause", "agreement", "deal", "joins", "free agent", "buy-back", "buyback",
      "departure", "leaves", "leaving", "interest*", "target*", "move to", "move for", "swoop",
      "approach*", "negotiat*", "talks", "fee"]),
    ("lineup", "📋 Lineups & squad",
     "starting XIs, squad lists, call-ups, who is on the bench",
     ["lineup*", "line-up*", "line up", "starting xi", "xi vs", "xi:", "squad list", "called up",
      "squad for", "bench:", "subs:", "convocatoria"]),
    ("match", "⚽ Match events",
     "live match moments and results: goals, assists, half-time, full-time, scores, penalties",
     ["goal", "goal!", "scores", "full-time", "full time", "ft:", "half-time", "half time", "ht:",
      "kick-off", "kick off", "final score", "final whistle", "won", "beat", "defeat*", "draw",
      "win", "assist*", "penalty", "equali*", "brace", "hat-trick", "hattrick"]),
    ("quotes", "🎤 Quotes & press",
     "what coaches, players and directors said: press conferences, interviews, statements to media",
     ["press conference", "presser", "said", "says", "told", "interview", "speaking", "admits",
      "explains", "reveals"]),
    ("coach", "👔 Coach & board",
     "the coach, the president and the board: appointments, sackings, decisions",
     ["sacked", "fired", "new manager", "new coach", "head coach", "appointed", "florentino",
      "perez", "board", "president"]),
    ("discipline", "🟥 Discipline & referees",
     "red cards, suspensions, bans, fines, referee and VAR decisions",
     ["suspen*", "red card", "sent off", "banned", "ban", "fifth yellow", "5th yellow",
      "referee*", "var", "sanction*", "fine", "fined"]),
    ("stats", "📊 Stats & records",
     "numbers, ratings, records, player stats after games",
     ["touches", "passing accuracy", "duels won", "man of the match", "motm", "rating*", "stats",
      "record*", "top scorer", "key passes", "dribbles", "xg", "most goals", "in numbers"]),
    ("fixtures", "📅 Fixtures & previews",
     "upcoming matches, kick-off times, previews, competition draws",
     ["next match", "up next", "preview", "fixture*", "schedule", "calendar", "tomorrow",
      "tonight", "matchday", "kick-off time"]),
    ("academy", "🎓 Academy & loans",
     "Castilla, youth teams, young players, players out on loan",
     ["castilla", "academy", "la fabrica", "youth", "juvenil", "on loan", "loanee", "loaned",
      "loan*"]),
    ("awards", "🏆 Awards",
     "Ballon d'Or, player of the month, nominations, individual prizes",
     ["ballon d'or", "ballon dor", "award*", "player of the month", "nominat*", "golden boy",
      "the best", "prize"]),
    ("club", "🏟️ Club & business",
     "the stadium, kits, sponsors, money, Super League, club elections and business",
     ["stadium", "kit", "jersey", "sponsor*", "revenue", "financ*", "budget", "super league",
      "superleague", "socios", "members", "elections", "tickets", "mock-up*", "mockup*"]),
    ("social", "📸 Social media & lifestyle",
     "Instagram/TikTok posts, private life, birthdays, holidays, visits, family, fashion, cars",
     ["instagram", "tiktok", "posted", "story", "birthday", "vacation", "holiday*", "visit*",
      "hometown", "girlfriend", "wife", "wedding", "fashion", "family"]),
    ("fun", "😂 Fun & fan content",
     "memes, throwbacks, polls, wallpapers, fan reactions, celebrations, hype posts",
     ["meme", "throwback", "on this day", "#otd", "poll", "vote", "quiz", "wallpaper*", "fans",
      "celebrat*", "edit", "goat", "😂", "🤣"]),
    ("rivals", "🌍 Other clubs & ex-players",
     "other clubs' news, rivals (e.g. Barcelona's president), former Real Madrid players elsewhere",
     ["barcelona", "barca", "laporta", "atletico", "former real madrid", "ex-real madrid",
      "former madrid"]),
    ("other", "📰 Other", "anything that fits nowhere else", []),
]
TOPIC_LABEL = {k: label for k, label, _, _ in TOPICS}
TOPIC_KEYS = [k for k, _, _, _ in TOPICS]
FAST_TOPICS = {"lineup", "match"}                    # shorter duplicate window
TEAM_TOPICS = {"lineup", "match", "coach", "fixtures"}  # pass "Only my players"
BIG_TOPICS = {"injury", "transfer", "coach"}         # official ones may ring at night

# Always dropped (ads, gambling, promos) - not a topic
HARD_IGNORE = [
    "promo code", "promocode", "bonus", "casino", "betting", "bet", "odds", "deposit",
    "sponsored", "#ad", "advertis*", "giveaway", "subscribe",
]

OFFICIAL_MARKERS = ["official*", "confirm*", "announce*", "statement", "medical report",
                    "here we go"]
RUMOUR_MARKERS = [
    "report*", "according to", "rumou*", "rumor*", "claim*", "could", "set to", "close to",
    "talks", "interest*", "linked", "consider*", "monitor*", "understand*", "sources",
    "per", "eyeing", "target*", "want*", "plan*", "expected to", "likely", "via",
    "(marca)", "(as)", "(relevo)", "(mundo deportivo)",
]

# For ":rm" sources - the post must mention Real Madrid, a squad player, or one of your players
RM_WORDS = ["real madrid", "realmadrid", "rmcf", "bernabeu", "los blancos", "hala madrid",
            "valdebebas", "castilla", "florentino", "mourinho"]  # add the coach's name if it changes
SQUAD = [  # update when the squad changes - players you /add count too
    "courtois", "lunin",
    "carvajal", "alexander-arnold", "alexander arnold", "militao", "rudiger", "huijsen",
    "asencio", "alaba", "carreras", "fran garcia", "ferland mendy", "mendy",
    "tchouameni", "valverde", "camavinga", "bellingham", "ceballos", "arda guler", "guler",
    "mastantuono", "brahim",
    "vinicius", "vini jr", "vini", "mbappe", "rodrygo", "endrick", "gonzalo garcia",
    "diomande",
    "nico paz", "jacobo ramon",  # sold with buy-back clauses
]
RM_WORDS += SQUAD

# How reliable is whoever reported a rumour
SOURCE_TIERS = {
    "top": ["fabrizio romano", "here we go", "marca", "diario as", "relevo", "the athletic",
            "mario cortegana", "jose felix diaz", "cadena ser", "el larguero", "cope",
            "arancha rodriguez", "matteo moretto", "david ornstein", "real madrid confirm"],
    "ok": ["mundo deportivo", "estadio deportivo", "ok diario", "okdiario", "goal",
           "sky sports", "sky germany", "florian plettenberg", "l'equipe", "rmc sport",
           "bild", "kicker", "gazzetta", "the times", "the guardian", "bbc", "espn"],
    "weak": ["defensa central", "el chiringuito", "chiringuito", "fichajes.net", "fichajes",
             "todofichajes", "el nacional", "caught offside", "football insider", "teamtalk",
             "the sun", "daily mail", "mirror", "express"],
}
TIER_ICON = {"top": "🟢", "ok": "🟠", "weak": "🔴"}

# every post gets exactly one of these
TAGS = {"official": "✅ OFFICIAL", "rumour": "🟡 RUMOUR", "fact": "📰 NEWS"}

OPTIONS = {
    "quiet": ("🌙 Quiet at night (23:00-08:00)", True),
    "summary": ("📝 Daily summary (22:00)", True),
    "players_only": ("🎯 Only my players", False),
}


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


# order the backup keyword sorter tries the topics in (most specific first)
KEYWORD_ORDER = ["injury", "discipline", "transfer", "academy", "lineup", "stats", "match",
                 "awards", "fixtures", "rivals", "coach", "quotes", "club", "social", "fun"]
_WORDS = {k: words for k, _, _, words in TOPICS}
TOPIC_RE = [(k, kw_regex(_WORDS[k])) for k in KEYWORD_ORDER]
HARD_IGNORE_RE = kw_regex(HARD_IGNORE)
OFFICIAL_RE = kw_regex(OFFICIAL_MARKERS)
RUMOUR_RE = kw_regex(RUMOUR_MARKERS)
RM_RE = kw_regex(RM_WORDS)
TIER_RE = [(t, w, kw_regex([w])) for t in ("weak", "top", "ok") for w in SOURCE_TIERS[t]]
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
        self.source = self.tier = self.headline = None  # filled in when sorted


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


def topic_on(state, key):
    return state.get("topics_on", {}).get(key, True)  # every topic starts ON


def opt(state, key):
    return state.get("opts", {}).get(key, OPTIONS[key][1])


def players_re(state):
    return kw_regex(state.get("players", []))


# ---------------------------------------------------------------------------
# Sorting a post into a topic
# ---------------------------------------------------------------------------
def keyword_topic(n):
    """Backup sorter when the AI is off. A score + match words counts as a match event."""
    if SCORE_RE.search(n) and re.search(r"full[- ]time|ft:|half[- ]time|ht:|goal|vs", n):
        return "match"
    for key, rx in TOPIC_RE:
        if rx.search(n):
            return key
    return "other"


def get_status(n, topic):
    if OFFICIAL_RE.search(n):
        return "official"
    if RUMOUR_RE.search(n) and topic in ("transfer", "injury", "coach", "lineup"):
        return "rumour"
    return "fact"


def detect_source(text, src=""):
    """(source name, tier) from who the post quotes - or (None, None)."""
    if src.lower().startswith("fabrizio"):
        return "Fabrizio Romano", "top"
    n = norm(text)
    found = []
    for tier, word, rx in TIER_RE:
        m = rx.search(n)
        if m:
            found.append((m.start(), tier, word))
    if not found:
        return None, None
    _, tier, word = min(found)  # the first outlet mentioned
    pretty = {"cope": "COPE", "bbc": "BBC", "espn": "ESPN", "rmc sport": "RMC Sport",
              "here we go": "Fabrizio Romano", "real madrid confirm": "Real Madrid"}
    return pretty.get(word, word.title()), tier


def find_duplicate(state, toks, names, topic):
    now = time.time()
    hours = DEDUPE_HOURS_SHORT if topic in FAST_TOPICS else DEDUPE_HOURS
    for s in reversed(state.get("sent", [])):
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
# AI sorter (free via Groq, or Claude)
# ---------------------------------------------------------------------------
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


def ai_sort(state, text, src=""):
    """Ask the AI for the topic, status, source, duplicate and a short headline."""
    now = time.time()
    recent = [s for s in state.get("sent", []) if now - s["ts"] < DEDUPE_HOURS * 3600][-12:]
    lines = ["You sort posts for a Real Madrid fan's Telegram news channel.",
             "Put the post into exactly ONE topic - the one that best describes what it is about:"]
    lines += [f'- "{k}": {desc}' for k, _, desc, _ in TOPICS]
    if recent:
        lines += ["", "ALREADY POSTED IN THE CHANNEL (last 2 days):"]
        lines += [f'{i}. {s["headline"]}' for i, s in enumerate(recent, 1)]
    if src:
        lines += ["", f"(The post comes from the Telegram channel @{src}.)"]
    lines += [
        "", "NEW POST:", '"""', text[:1500], '"""', "",
        "Reply with JSON only:",
        '{"topic": one of the topic keys above,',
        ' "ad": true if it is an advertisement, betting promo or "join our channel" post, else false,',
        ' "status": "official" (confirmed by the club or the person, or Fabrizio Romano\'s "here we go") |'
        ' "rumour" (reports, claims, interest, anything not confirmed) | "fact" (it already happened'
        ' and is not in doubt: results, lineups, quotes, training updates, stats, social posts),',
        ' "duplicate_of": the number of the already-posted item that is the SAME story'
        ' (also when this one is the official confirmation of that rumour), or null,',
        ' "big": true only for major news (official signing or departure, serious injury, coach sacked),',
        ' "source": the journalist or outlet a rumour comes from (e.g. "Marca") or null,',
        ' "source_tier": "top" (Fabrizio Romano, Marca, AS, Relevo, The Athletic, COPE, Cadena SER)'
        ' | "ok" (mainstream but less accurate) | "weak" (known for invented stories: Defensa Central,'
        ' El Chiringuito, Fichajes, tabloids) | null,',
        ' "headline": ' + ('"the key news in max 12 words, English, no emojis"' if len(text) > 350
                           else "null") + "}",
    ]
    out = call_ai(state, "\n".join(lines))
    topic = out.get("topic") if out.get("topic") in TOPIC_LABEL else keyword_topic(norm(text))
    status = out.get("status") if out.get("status") in TAGS else "fact"
    dup = None
    d = out.get("duplicate_of")
    if isinstance(d, (int, float)) or (isinstance(d, str) and d.strip().isdigit()):
        if 1 <= int(d) <= len(recent):
            dup = recent[int(d) - 1]
    source = out.get("source") if isinstance(out.get("source"), str) else None
    tier = out.get("source_tier") if out.get("source_tier") in TIER_ICON else None
    head = out.get("headline")
    head = head.strip()[:120] if isinstance(head, str) and head.strip() and len(text) > 350 else None
    return {"topic": topic, "ad": bool(out.get("ad")), "status": status, "dup": dup,
            "big": bool(out.get("big")), "source": source, "tier": tier, "headline": head}


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------
def send_post(post, topic, status, quiet, update, when=None):
    """Send a post to the channel. Returns the first message id."""
    head = f"<b>{esc(TOPIC_LABEL.get(topic, topic))}</b>"
    if when:  # older post sent by /backfill - show the original time
        head = f"🕒 {datetime.fromtimestamp(when, TZ):%a %d %b, %H:%M}\n" + head
    if status in TAGS:
        head += f"  ·  {TAGS[status]}"
    if status == "rumour" and post.source:
        head += f"  ·  {TIER_ICON.get(post.tier, '⚪')} {esc(post.source)}"
    if update:
        head += "  ·  🔁 now official"
    if post.headline:
        head += f"\n<b>{esc(post.headline)}</b>"
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
            res = r["result"] if isinstance(r["result"], list) else [r["result"]]
            first_id = res[0]["message_id"]
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
def process_post(state, post, rm_only, backfill=False):
    """Sort one post into a topic and post it if that topic is on. Returns a log line."""
    if not post.text:
        return "skip: no text"
    n = norm(post.text)
    prx = players_re(state)
    if HARD_IGNORE_RE.search(n):
        return "skip: ad"
    if rm_only and not (RM_RE.search(n) or (prx and prx.search(n))):
        return "skip: not Real Madrid"

    post.source, post.tier = detect_source(post.text, post.src)
    info = None
    if ai_provider():
        try:
            info = ai_sort(state, post.text, post.src)
            health_ok(state, "_ai")
        except Exception as e:
            print(f"  AI unavailable ({e}) - using keywords")
            ai_failed(state, e)
    if info:
        how = "AI"
        if info["ad"]:
            return "skip: ad [AI]"
        topic, status, dup, big = info["topic"], info["status"], info["dup"], info["big"]
        if info["source"]:
            post.source = info["source"][:30]
            post.tier = info["tier"] or detect_source(info["source"])[1] or post.tier
        post.headline = info["headline"]
    else:
        how = "keywords"
        topic = keyword_topic(n)
        status = get_status(n, topic)
        dup, big = None, status == "official" and topic in BIG_TOPICS

    label = TOPIC_LABEL[topic]
    if not topic_on(state, topic):
        return f"skip: {label} is off [{how}]"
    if opt(state, "players_only") and prx and topic not in TEAM_TOPICS and not prx.search(n):
        return f"skip: {label} - not about your players"

    toks, names = tokens(post.text), names_of(post.text)
    dup = dup or find_duplicate(state, toks, names, topic)
    update = False
    if dup:
        if status == "official" and dup.get("status") != "official":
            update = True
            dup["superseded"] = True  # the summary shows only the official version
        else:
            return f"skip: duplicate [{how}]"

    quiet = backfill or (opt(state, "quiet") and in_quiet_hours() and not big)
    when = post.ts if backfill else None
    msg_id = send_post(post, topic, status, quiet, update, when)
    posted = datetime.fromtimestamp(when, TZ) if when else datetime.now(TZ)
    state.setdefault("sent", []).append({
        "ts": time.time(), "date": posted.date().isoformat(),
        "tokens": sorted(toks), "names": sorted(names), "cat": topic, "status": status,
        "headline": post.headline or headline(post.text), "link": my_link(msg_id, post.link),
    })
    stats = state.setdefault("topic_count", {})
    stats[topic] = stats.get(topic, 0) + 1
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
    """Post the last N hours again (started by /backfill). Continues next run if time runs out."""
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

        found, before = {}, until + 1
        for _ in range(40):  # page back through the channel until we pass the cutoff time
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

    notify_owner(state, f"✅ Done! Posted {bf['sent']} posts from the last {bf['hours']} hours.")
    state.pop("backfill", None)
    print(f"[backfill] finished, {bf['sent']} sent")


def summary_text(state):
    now = datetime.now(TZ)
    today = now.date().isoformat()
    items = [s for s in state.get("sent", []) if s["date"] == today and not s.get("superseded")]
    lines = [f"<b>📝 Daily summary · {now:%a %d %b}</b>"]
    if not items:
        lines.append("\nA quiet day - nothing posted.")
    for key in TOPIC_KEYS:
        group = [s for s in items if s.get("cat") == key]
        if not group:
            continue
        lines.append(f"\n<b>{esc(TOPIC_LABEL[key])}</b>")
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
# Health: tell him when something breaks (and when it's fixed)
# ---------------------------------------------------------------------------
SOURCE_ALERT_MIN = 120  # a channel unreadable this long -> alert
AI_ALERT_MIN = 60       # the AI failing this long -> alert


def notify_owner(state, text):
    owner = OWNER_ID or state.get("owner")
    if owner:
        tg("sendMessage", chat_id=int(owner), text=text)


def health_ok(state, key):
    h = state.setdefault("health", {}).get(key)
    if not h:
        return
    if h.get("alerted"):
        name = "The AI sorter" if key == "_ai" else f"@{key}"
        notify_owner(state, f"✅ Fixed: {name} is working again.")
    state["health"].pop(key, None)


def health_fail(state, key, err, alert_after_min, message):
    h = state.setdefault("health", {}).setdefault(key, {"since": time.time()})
    h["err"] = str(err)[:150]
    if not h.get("alerted") and time.time() - h["since"] >= alert_after_min * 60:
        h["alerted"] = True
        notify_owner(state, message)


def ai_failed(state, e):
    if "daily AI limit" in str(e):
        today = datetime.now(TZ).date().isoformat()
        if state.get("limit_alert") != today:
            state["limit_alert"] = today
            notify_owner(state, "🧠 I used today's free AI allowance - until midnight I sort posts "
                                "with keywords (a bit less accurate). It resets by itself.")
        return
    health_fail(state, "_ai", e, AI_ALERT_MIN,
                f"⚠️ The AI sorter hasn't worked for {AI_ALERT_MIN} minutes - I'm sorting with "
                f"keywords meanwhile, so the channel keeps running.\nError: {str(e)[:150]}\n"
                "If it lasts a day, check the GROQ_API_KEY in GitHub.")


def health_text(state):
    h = state.get("health", {})
    if not h:
        return "✅ All sources and the AI are working"
    out = []
    for k, v in h.items():
        mins = int((time.time() - v["since"]) / 60)
        out.append(f"⚠️ {'AI sorter' if k == '_ai' else '@' + k}: failing for {mins} min")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Settings menu (private chat with the bot)
# ---------------------------------------------------------------------------
def menu_text(state):
    players = state.get("players", [])
    off = [TOPIC_LABEL[k] for k in TOPIC_KEYS if not topic_on(state, k)]
    return (
        "⚙️ <b>Topics</b> - every Real Madrid post goes into one topic.\n"
        "Tap to turn a topic on ✅ / off ❌\n\n"
        f"Off right now: {esc(', '.join(off)) if off else 'nothing - you get everything'}\n"
        f"🎯 My players: {esc(', '.join(players)) if players else '(none)'}\n\n"
        "/add Vinicius - add a player · /remove Vinicius\n"
        "/summary - today's summary · /backfill 24 - repost the last 24h\n"
        "/status - is everything working?"
    )


def menu_markup(state):
    rows, row = [], []
    for key in TOPIC_KEYS:
        mark = "✅" if topic_on(state, key) else "❌"
        row.append({"text": f"{mark} {TOPIC_LABEL[key]}", "callback_data": f"t:{key}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "✅ All topics on", "callback_data": "all:1"},
                 {"text": "❌ All topics off", "callback_data": "all:0"}])
    rows += [[{"text": f"{'✅' if opt(state, k) else '❌'} {name}", "callback_data": f"o:{k}"}]
             for k, (name, _) in OPTIONS.items()]
    return {"inline_keyboard": rows}


def status_text(state):
    prov = ai_provider()
    u = ai_usage(state)
    lines = ["📡 <b>Status</b>", health_text(state)]
    if prov:
        name = "Claude" if prov == "claude" else f"Groq ({AI_MODEL})"
        lines.append(f"🧠 AI sorter: ON - {esc(name)} · today {u['calls']} posts sorted")
    else:
        lines.append("🧠 AI sorter: OFF - sorting with keywords (add GROQ_API_KEY to turn it on)")
    lines.append("Sources: " + ", ".join(f"@{s}" + (" (Madrid only)" if rm else "")
                                         for s, rm in SOURCES))
    counts = state.get("topic_count", {})
    if counts:
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:6]
        lines.append("Most posted: " + ", ".join(f"{TOPIC_LABEL.get(k, k)} {v}" for k, v in top))
    return "\n".join(lines)


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
           text=f"🗑 Removed. My players: {', '.join(state['players']) or '(none)'}")
    elif cmd == "/players":
        tg("sendMessage", chat_id=uid, text=f"🎯 My players: {', '.join(players) or '(none)'}")
    elif cmd == "/backfill":
        hours = int(arg) if arg.strip().isdigit() else 48
        hours = max(1, min(hours, 72))
        state["backfill"] = {
            "hours": hours, "cutoff": time.time() - hours * 3600,
            "until": dict(state.get("last_post", {})), "upto": {}, "done": [], "sent": 0,
        }
        tg("sendMessage", chat_id=uid, text=(
            f"⏳ OK! I'll post the last {hours} hours to the channel (only topics that are on, "
            "silently, with the original time). I'll tell you when it's done."))
    elif cmd == "/summary":
        tg("sendMessage", chat_id=uid, text=summary_text(state), parse_mode="HTML",
           link_preview_options={"is_disabled": True})
    elif cmd == "/status":
        tg("sendMessage", chat_id=uid, text=status_text(state), parse_mode="HTML")
    else:
        show_menu(state, uid)


def handle_callback(state, q):
    kind, _, val = (q.get("data") or ":").partition(":")
    chat, msg_id = q["message"]["chat"]["id"], q["message"]["message_id"]
    if kind in ("tr", "fb", "sk", "up", "noop"):  # buttons from the old version
        tg("answerCallbackQuery", callback_query_id=q["id"],
           text="This button is from the old version - use /settings")
        tg("editMessageReplyMarkup", chat_id=chat, message_id=msg_id,
           reply_markup={"inline_keyboard": []})
        return
    topics_on = state.setdefault("topics_on", {})
    opts = state.setdefault("opts", {})
    if kind == "t" and val in TOPIC_LABEL:
        topics_on[val] = not topic_on(state, val)
        toast = f"{TOPIC_LABEL[val]}: {'ON ✅' if topics_on[val] else 'OFF ❌'}"
    elif kind == "o" and val in OPTIONS:
        opts[val] = not opt(state, val)
        toast = "Saved ✔"
    elif kind == "all":
        for key in TOPIC_KEYS:
            topics_on[key] = val == "1"
        toast = "All topics ON ✅" if val == "1" else "All topics OFF ❌"
    else:
        toast = "?"
    tg("editMessageText", chat_id=chat, message_id=msg_id, text=menu_text(state),
       parse_mode="HTML", reply_markup=menu_markup(state))
    tg("answerCallbackQuery", callback_query_id=q["id"], text=toast)


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
            health_ok(state, src)
        except Exception as e:
            print(f"{src}: error: {e}")
            health_fail(state, src, e, SOURCE_ALERT_MIN,
                        f"⚠️ I can't read @{src} for {SOURCE_ALERT_MIN // 60} hours.\n"
                        f"Maybe it changed its name or became private. Open t.me/s/{src} "
                        f"in a browser to check.\nError: {str(e)[:150]}")
    try:
        run_backfill(state, deadline)
    except Exception as e:
        print(f"backfill error: {e}")
    prune(state)
    maybe_send_summary(state)
    save_state(state)


def migrate(state):
    """One-time clean-up of things the old version stored."""
    if state.get("version") == 2:
        return
    for k in ("enabled", "prefs", "rules", "feedback", "pending_fb", "skipped", "skipped_view",
              "train_pending", "train_stats", "train_seq", "train_in_channel"):
        state.pop(k, None)
    state.get("opts", {}).pop("training", None)
    for s in state.get("sent", []):  # old records used labels, new ones use topic keys
        if s.get("cat") not in TOPIC_LABEL:
            s["cat"] = "other"
    state["version"] = 2


def main():
    missing = [n for n, v in [("SOURCE_CHANNELS", SOURCES), ("BOT_TOKEN", BOT_TOKEN),
                              ("TARGET_CHANNEL", TARGET)] if not v]
    if missing:
        sys.exit(f"missing environment variables: {', '.join(missing)}")

    state = load_state()
    migrate(state)
    tg("setMyCommands", commands=[
        {"command": "settings", "description": "Turn topics on/off"},
        {"command": "add", "description": "Add players: /add Vinicius, Mbappe"},
        {"command": "remove", "description": "Remove a player"},
        {"command": "players", "description": "Show my players"},
        {"command": "summary", "description": "Preview today's summary"},
        {"command": "backfill", "description": "Repost the last 48h (topics that are on)"},
        {"command": "status", "description": "Is everything working?"},
    ])
    print(f"sorter: {ai_provider() or 'keywords'}")

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
