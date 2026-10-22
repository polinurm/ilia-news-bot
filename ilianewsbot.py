import feedparser, requests, json, os, re, time, calendar
from urllib.parse import quote, urljoin
from bs4 import BeautifulSoup

# ============ НАСТРОЙКИ ============
TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
MAX_AGE_HOURS = 48
STATE_FILE = "state.json"
UA = "Mozilla/5.0 (compatible; NewsBot/1.0)"

TWITCH_CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID", "")
TWITCH_SECRET = os.environ.get("TWITCH_SECRET", "")
TWITCH = []

GOOGLE_QUERIES = ["Ilia Malinin", "Илья Малинин"]
GOOGLE_SITES = {
    "US Figure Skating": "site:usfigureskating.org",
    "ISU": "site:isu.org",
    "Olympics": "site:olympics.com Malinin",
}
YOUTUBE = ["https://www.youtube.com/@IliaQuadgodMalinin"]
REDDIT = ["r/IliaQuadg0dMalinin"]
INSTAGRAM = ["ilia_quadg0d_malinin"]   # через RSSHub, может не заработать
TWITTER = []
RSSHUB = "https://rsshub.app"
EXTRA_FEEDS = {
    # "Instagram: Malinin": "https://rss.app/feeds/xxxx.xml",
}
PAGES = {}
# ====================================

FIRST_EVER = not os.path.exists(STATE_FILE)


def load_state():
    if not FIRST_EVER:
        d = json.load(open(STATE_FILE))
        return dict.fromkeys(d["seen"]), set(d["init"]), d["live"], d.get("yt", {})
    return {}, set(), {}, {}

def save_state():
    json.dump({"seen": list(seen)[-3000:], "init": sorted(init),
               "live": live, "yt": yt_cache},
              open(STATE_FILE, "w"), ensure_ascii=False)

seen, init, live, yt_cache = load_state()


def send(text):
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": text}, timeout=15)
        time.sleep(1)
    except requests.RequestException as err:
        print("Ошибка отправки:", err)


def get(url, **kw):
    last = None
    for _ in range(2):
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=(10, 30), **kw)
            r.raise_for_status()
            return r
        except requests.RequestException as err:
            last = err
            time.sleep(3)
    raise last


def is_fresh(entry):
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if not t:
        return True
    return (time.time() - calendar.timegm(t)) / 3600 <= MAX_AGE_HOURS


def youtube_feed(url):
    if url not in yt_cache:
        html = get(url).text
        m = re.search(r'"channelId":"(UC[\w-]{22})"', html) or \
            re.search(r'channel/(UC[\w-]{22})', html)
        if not m:
            raise ValueError("не нашла channel_id")
        yt_cache[url] = m.group(1)
    return f"https://www.youtube.com/feeds/videos.xml?channel_id={yt_cache[url]}"


def google_rss(query, lang="ru"):
    q = quote(f"{query} when:2d")
    if lang == "ru":
        return f"https://news.google.com/rss/search?q={q}&hl=ru&gl=RU&ceid=RU:ru"
    return f"https://news.google.com/rss/search?q={q}&hl=en&gl=US&ceid=US:en"


def build_feeds():
    feeds = dict(EXTRA_FEEDS)
    for q in GOOGLE_QUERIES:
        feeds[f"Google News: {q}"] = google_rss(q)
    for name, query in GOOGLE_SITES.items():
        feeds[f"{name} (сайт)"] = google_rss(query, lang="en")
    for r in REDDIT:
        kind, name = r.split("/", 1)
        path = f"r/{name}/new" if kind == "r" else f"user/{name}"
        feeds[f"Reddit: {r}"] = f"https://www.reddit.com/{path}/.rss"
    for u in INSTAGRAM:
        feeds[f"Instagram: {u}"] = f"{RSSHUB}/instagram/user/{u}"
    for u in TWITTER:
        feeds[f"X: {u}"] = f"{RSSHUB}/twitter/user/{u}"
    return feeds


def handle_items(name, items):
    first = name not in init
    for link, title, fresh in items:
        if link not in seen:
            seen[link] = None
            if fresh and not first:
                send(f"🔔 {name}\n{title}\n{link}")
    init.add(name)


def check_feeds():
    feeds = build_feeds()
    for url in YOUTUBE:
        try:
            feeds[f"YouTube: {url.rstrip('/').split('/')[-1]}"] = youtube_feed(url)
        except Exception as err:
            print("YouTube ошибка:", url, err)
    for name, url in feeds.items():
        try:
            entries = feedparser.parse(get(url).content).entries
            handle_items(name, [(e.link, e.get("title", ""), is_fresh(e)) for e in entries])
        except Exception as err:
            print("Ошибка ленты:", name, err)
        time.sleep(1)


def check_pages():
    for name, (url, pattern) in PAGES.items():
        try:
            soup = BeautifulSoup(get(url).text, "html.parser")
            items = []
            for a in soup.find_all("a", href=True):
                href = urljoin(url, a["href"])
                text = a.get_text(strip=True)
                if pattern in href and text:
                    items.append((href, text, True))
            handle_items(f"Сайт: {name}", items)
        except Exception as err:
            print("Ошибка страницы:", name, err)


def check_twitch():
    if not (TWITCH and TWITCH_CLIENT_ID and TWITCH_SECRET):
        return
    try:
        tok = requests.post("https://id.twitch.tv/oauth2/token", data={
            "client_id": TWITCH_CLIENT_ID, "client_secret": TWITCH_SECRET,
            "grant_type": "client_credentials"}, timeout=15).json()["access_token"]
        r = requests.get("https://api.twitch.tv/helix/streams",
                         params=[("user_login", l) for l in TWITCH],
                         headers={"Client-Id": TWITCH_CLIENT_ID,
                                  "Authorization": f"Bearer {tok}"}, timeout=15)
        r.raise_for_status()
        online = {s["user_login"].lower(): s for s in r.json()["data"]}
        for login in TWITCH:
            s = online.get(login.lower())
            if s and live.get(login) != s["id"]:
                send(f"🟣 Twitch: {s['user_name']} в эфире\n{s['title']}\nhttps://twitch.tv/{login}")
            live[login] = s["id"] if s else None
    except Exception as err:
        print("Ошибка Twitch:", err)


if FIRST_EVER:
    send("✅ Бот запущен. Уведомления начнутся с новых публикаций.")

check_feeds()
check_pages()
check_twitch()
save_state()
