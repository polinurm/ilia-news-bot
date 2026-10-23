import feedparser, requests, json, os, re, time, calendar, base64, gzip
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from urllib.parse import quote, urljoin, urlparse
from bs4 import BeautifulSoup
import trafilatura
from googlenewsdecoder import gnewsdecoder

# ============ НАСТРОЙКИ ============
TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]                                   # канал с новостями
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID") or CHAT_ID        # личный чат для сводки
DIGEST_HOUR = 9           # во сколько (по Москве) присылать ежедневную сводку
MAX_AGE_HOURS = 24
SILENT_FIRST_RUN = False  # True = первый проход по новому источнику молчит
STRICT_TEXT = True
STATE_FILE = "state.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0 Safari/537.36"

BLOCKED = ["irbis.spb.ru", "prospect.com.ru"]
OFFICIAL = ["usfigureskating.org", "isu.org", "olympics.com",
            "isu-skating.com", "goldenskate.com", "anythinggoe.com",
            "mainichi.jp", "kyodonews.jp"]

GOOGLE_QUERIES = ['"Ilia Malinin"', '"Илья Малинин"', 'Malinin figure skating',
                  'マリニン フィギュア']
GOOGLE_SITES = {
    "US Figure Skating": 'site:usfigureskating.org Malinin',
    "ISU": 'site:isu.org Malinin',
    "Olympics": 'site:olympics.com Malinin',
    "Mainichi": 'site:mainichi.jp マリニン',
    "Kyodo": 'site:kyodonews.jp マリニン',
}
YOUTUBE = ["https://www.youtube.com/@IliaQuadgodMalinin"]
YOUTUBE_IDS = {}
REDDIT = ["r/IliaQuadg0dMalinin"]

# Сайты: бот ищет RSS, а если его нет, читает ссылки на статьи со страницы
SITES = {
    "ISU News": "https://isu-skating.com/en/news/",
    "GoldenSkate": "https://www.goldenskate.com/category/news/",
    "AnythingGOE": "https://anythinggoe.com/",
    "USFS": "https://usfigureskating.org/news",
    "Kyodo DeepEdge": "https://deepedgeplus.kyodonews.jp/",
}
# Sitemap: название: (адрес sitemap, фрагмент в адресах статей). Адреса угаданы.
SITEMAPS = {
    "ISU (sitemap)": ("https://www.isu.org/sitemap.xml", "/news"),
    "Olympics (sitemap)": ("https://www.olympics.com/sitemap.xml", "/news/"),
}
# ====================================

stats = {"news": 0, "src": 0}
errors = []


# ---------- сеть ----------
def fetch(url, post=None, timeout=30, fail=True):
    last = "пустой ответ"
    for _ in range(2):
        try:
            if post is not None:
                r = requests.post(url, json=post, headers={"User-Agent": UA}, timeout=(10, timeout))
            else:
                r = requests.get(url, headers={"User-Agent": UA}, timeout=(10, timeout))
            if fail:
                r.raise_for_status()
            if r.content:
                return r.content
        except requests.RequestException as err:
            last = str(err)
        time.sleep(3)
    raise RuntimeError(last)


def send(text, silent=False, to=None):
    try:
        out = fetch(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                    post={"chat_id": str(to or CHAT_ID).strip(), "text": text,
                          "disable_notification": silent}, fail=False)
        resp = json.loads(out)
        if not resp.get("ok"):
            print("Telegram отклонил сообщение:", resp.get("description"))
            return False
        time.sleep(1)
        return True
    except Exception as err:
        print("Ошибка отправки в Telegram:", err)
        return False


# ---------- состояние ----------
def load_state():
    try:
        d = json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        d = {}
    return (dict.fromkeys(d.get("seen", [])), set(d.get("init", [])), d.get("yt", {}),
            d.get("tries", {}), d.get("sf", {}), d.get("day", {}), d.get("digest_date", ""))

seen, init, yt_cache, tries, site_feed, day, digest_date = load_state()
day.setdefault("runs", 0)
day.setdefault("errs", {})
day.setdefault("rej", [])


def save_state():
    json.dump({"seen": list(seen)[-5000:], "init": sorted(init), "yt": yt_cache,
               "tries": tries, "sf": site_feed, "day": day, "digest_date": digest_date},
              open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False)


# ---------- возраст записи ----------
def age_hours(entry):
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if not t:
        return None
    return (time.time() - calendar.timegm(t)) / 3600


def fmt_age(age):
    if age is None:
        return "дата неизвестна"
    if age < 1:
        mins = round(age * 60)
        return "только что" if mins < 1 else f"{mins} мин назад"
    return f"{age:.0f} ч назад"


# ---------- релевантность ----------
NAME_RE = re.compile("|".join([
    r"\bIl(?:ia|ya|iya)\s+Malinin\b",
    r"\bMalinin,?\s+Il(?:ia|ya|iya)\b",
    r"\bИл(?:ья|ьи|ье|ью|ьёй|ьей)\s+Малинин(?:а|у|ым|е)?\b",
    r"\bМалинин(?:а|у|ым|е)?,?\s+Ил(?:ья|ьи|ье|ью|ьёй|ьей)\b",
    r"イ[リヂ][アヤ][・･\s]*マリニン",
]), re.IGNORECASE)

SURNAME_RE = re.compile(r"\b(?:Малинин(?:а|у|ым|е|ых)?|Malinin)\b|マリニン", re.IGNORECASE)

SINGER_RE = re.compile(r"Александр(?:а|у|ом|е)?\s+Малинин|Малинин(?:а|у|ым|е)?\s+Александр|"
                       r"Alexand[er]+\s+Malinin", re.IGNORECASE)

CONTEXT_RE = re.compile(r"фигур|катани|Дикиджи|аксел|четверн|Гран-при|\bISU\b|\bИСУ\b|Олимпи|"
                        r"skating|skater|\bquad|axel|Grand Prix|Olympic|"
                        r"フィギュア|スケート|アクセル|五輪|グランプリ", re.IGNORECASE)


def extract_main(html):
    return trafilatura.extract(html, favor_precision=True, include_comments=False,
                               include_tables=False, include_links=False) or ""


def meta_text(html):
    soup = BeautifulSoup(html, "html.parser")
    parts = []
    for m in soup.find_all("meta"):
        key = (m.get("property") or m.get("name") or "").lower()
        if key in ("og:title", "og:description", "description", "twitter:title", "twitter:description"):
            if m.get("content"):
                parts.append(m["content"].strip())
    parts += re.findall(r'"(?:description|articleBody)"\s*:\s*"([^"]{20,})"', html)
    return "\n".join(parts)


def body_text(text, min_len=80):
    if not STRICT_TEXT:
        return text
    return "\n".join(p for p in text.split("\n")
                     if len(p) >= (30 if re.search("[ぁ-んァ-ン一-龥]", p) else min_len))


def snippet(text):
    m = NAME_RE.search(text) or SURNAME_RE.search(text)
    if not m:
        return ""
    return "…" + text[max(0, m.start() - 50): m.end() + 50].replace("\n", " ") + "…"


def judge(title, text, official):
    if len(text) > 30000 and not official:
        return False, "страница-агрегатор (слишком длинный текст)"
    text = body_text(text)
    full = f"{title}\n{text}"
    has_full = bool(NAME_RE.search(full))
    if SINGER_RE.search(full) and not has_full:
        return False, "это певец Александр Малинин"

    n = len(SURNAME_RE.findall(text))
    in_title = bool(SURNAME_RE.search(title))
    in_lead = bool(SURNAME_RE.search(text[:400]))

    if n == 0 and not in_title:
        return False, "имени Ильи нет"
    if not has_full and not official and not CONTEXT_RE.search(full):
        return False, "фамилия без контекста фигурного катания"

    if in_title:
        return True, "имя в заголовке"
    if official:
        return True, "официальный сайт, имя в тексте"
    if in_lead:
        return True, "имя в начале статьи"
    if n >= 2:
        return True, f"имя упомянуто {n} раз"
    return False, "одно упоминание в середине текста"


def decode_google_link(link):
    art = urlparse(link).path.rstrip("/").split("/")[-1]
    try:
        raw = base64.urlsafe_b64decode(art + "=" * (-len(art) % 4))
        s = raw.decode("latin1")
        if s.startswith("\x08\x13\x22"):
            s = s[3:]
        if s.endswith("\xd2\x01\x00"):
            s = s[:-3]
        n = bytearray(s, "latin1")[0]
        s = s[2:n + 1] if n >= 0x80 else s[1:n + 1]
        if s.startswith("http"):
            return s
    except Exception:
        pass

    sess = requests.Session()
    sess.headers["User-Agent"] = UA
    sess.cookies.update({"CONSENT": "YES+cb", "SOCS": "CAI"})
    div = None
    for base in ("https://news.google.com/articles/", "https://news.google.com/rss/articles/"):
        r = sess.get(base + art, timeout=(10, 30))
        soup = BeautifulSoup(r.text, "html.parser")
        div = soup.select_one("c-wiz > div[jscontroller]")
        if div and div.get("data-n-a-sg"):
            break
    if not (div and div.get("data-n-a-sg")):
        title = soup.title.string[:60] if soup.title and soup.title.string else ""
        print("  Google не отдал данные ссылки. Страница:", r.url, "|", title)
        return None

    inner = json.dumps(
        ["garturlreq",
         [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None, None, None, 0, 1],
          "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
         art, int(div["data-n-a-ts"]), div["data-n-a-sg"]],
        separators=(",", ":"))
    body = "f.req=" + quote(json.dumps([[["Fbv4je", inner]]]))
    r = sess.post("https://news.google.com/_/DotsSplashUi/data/batchexecute", data=body,
                  headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
                  timeout=(10, 30))
    parsed = json.loads(r.text.split("\n\n")[1])[:-2]
    return json.loads(parsed[0][2])[1]
