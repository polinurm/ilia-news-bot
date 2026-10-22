import feedparser, requests, json, os, re, time, calendar, base64, gzip
from datetime import datetime, timezone
from urllib.parse import quote, urljoin, urlparse
from bs4 import BeautifulSoup
import trafilatura
from googlenewsdecoder import gnewsdecoder

# ============ НАСТРОЙКИ ============
TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
MAX_AGE_HOURS = 24
HEARTBEAT_HOURS = 24      # тихое сообщение "бот работает" раз в N часов (0 = выключить)
SILENT_FIRST_RUN = False  # True = первый проход по новому источнику молчит
STRICT_TEXT = True
STATE_FILE = "state.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0 Safari/537.36"

BLOCKED = ["irbis.spb.ru", "prospect.com.ru"]
OFFICIAL = ["usfigureskating.org", "isu.org", "olympics.com",
            "isu-skating.com", "goldenskate.com", "anythinggoe.com"]

GOOGLE_QUERIES = ['"Ilia Malinin"', '"Илья Малинин"', 'Malinin figure skating',
                  'マリニン フィギュア']
GOOGLE_SITES = {
    "US Figure Skating": 'site:usfigureskating.org Malinin',
    "ISU": 'site:isu.org Malinin',
    "Olympics": 'site:olympics.com Malinin',
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
}
# Sitemap: название: (адрес sitemap, фрагмент в адресах статей). Адреса угаданы.
SITEMAPS = {
    "ISU (sitemap)": ("https://www.isu.org/sitemap.xml", "/news"),
    "Olympics (sitemap)": ("https://www.olympics.com/sitemap.xml", "/news/"),
}
# ====================================

stats = {"news": 0, "src": 0}
errors = []
FIRST_EVER = not os.path.exists(STATE_FILE)


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


def send(text, silent=False):
    try:
        out = fetch(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                    post={"chat_id": str(CHAT_ID).strip(), "text": text,
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
            d.get("tries", {}), d.get("sf", {}), d.get("beat", time.time()))

seen, init, yt_cache, tries, site_feed, last_beat = load_state()


def save_state():
    json.dump({"seen": list(seen)[-5000:], "init": sorted(init), "yt": yt_cache,
               "tries": tries, "sf": site_feed, "beat": last_beat},
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
    return "менее часа назад" if age < 1 else f"{age:.0f} ч назад"


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


def body_text(text, min_len=80):
    if not STRICT_TEXT:
        return text
    return "\n".join(p for p in text.split("\n") if len(p) >= min_len)


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


def real_url(link):
    if "news.google.com" not in link:
        return link
    try:
        u = decode_google_link(link)
        if u:
            return u
    except Exception as err:
        print("  своя расшифровка не сработала:", err)
    try:
        res = gnewsdecoder(link, interval=1)
        if res.get("status"):
            return res["decoded_url"]
    except Exception:
        pass
    return link


def check_relevance(item):
    url = real_url(item["link"])
    if any(b in url.lower() for b in BLOCKED):
        return False, url, "сайт в чёрном списке", True
    official = any(d in f"{item['domain']} {url}".lower() for d in OFFICIAL)
    text, html = "", ""
    if "news.google.com" in url:
        print("  нет прямой ссылки, текст не прочитан")
    else:
        try:
            html = fetch(url).decode("utf-8", errors="ignore")
            text = extract_main(html)
            if not text:
                print("  страница скачалась, но текст не извлёкся:", url)
        except Exception as err:
            print("  не скачала статью:", url, "|", err)
    if text:
        ok, why = judge(item["title"], text, official)
        extra = ""
        if ok:
            extra = " | " + snippet(body_text(text) or text)
        elif SURNAME_RE.search(html):
            extra = " | фамилия есть только вне основного текста (боковые блоки)"
        return ok, url, f"{why}, текст {len(text)} зн.{extra}", True
    ok, why = judge(item["title"], item["summary"], official)
    return ok, url, f"{why} (текст не прочитан)", False


def is_blocked(item):
    blob = f"{item['domain']} {item['title']} {item['link']}".lower()
    return any(b in blob for b in BLOCKED)


# ---------- источники ----------
def youtube_feed(url):
    if url not in yt_cache:
        if url in YOUTUBE_IDS:
            yt_cache[url] = YOUTUBE_IDS[url]
        else:
            r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
                             cookies={"CONSENT": "YES+1", "SOCS": "CAI"}, timeout=(10, 30))
            html = r.text
            m = (re.search(r'channel_id=(UC[\w-]{22})', html)
                 or re.search(r'"externalId":"(UC[\w-]{22})"', html)
                 or re.search(r'"channelId":"(UC[\w-]{22})"', html)
                 or re.search(r'channel/(UC[\w-]{22})', html))
            if not m:
                raise ValueError("не нашла channel_id, впиши его вручную в YOUTUBE_IDS")
            yt_cache[url] = m.group(1)
    return f"https://www.youtube.com/feeds/videos.xml?channel_id={yt_cache[url]}"


def google_rss(query, lang="ru"):
    q = quote(f"{query} when:{MAX_AGE_HOURS}h")
    if lang == "ru":
        return f"https://news.google.com/rss/search?q={q}&hl=ru&gl=RU&ceid=RU:ru"
    if lang == "ja":
        return f"https://news.google.com/rss/search?q={q}&hl=ja&gl=JP&ceid=JP:ja"
    return f"https://news.google.com/rss/search?q={q}&hl=en&gl=US&ceid=US:en"


def query_lang(q):
    if re.search("[ぁ-んァ-ン一-龥]", q):
        return "ja"
    if re.search("[а-яА-Я]", q):
        return "ru"
    return "en"


def build_feeds():
    feeds = {}
    for q in GOOGLE_QUERIES:
        feeds[f"Google News: {q.strip(chr(34))}"] = google_rss(q, query_lang(q))
    for name, query in GOOGLE_SITES.items():
        feeds[f"{name} (сайт)"] = google_rss(query, "en")
    for r in REDDIT:
        kind, name = r.split("/", 1)
        path = f"r/{name}/new" if kind == "r" else f"user/{name}"
        feeds[f"Reddit: {r}"] = f"https://www.reddit.com/{path}/.rss"
    return feeds


def entry_to_item(e):
    src = e.get("source") or {}
    link = e.get("link", "")
    return {
        "link": link,
        "title": e.get("title", ""),
        "summary": BeautifulSoup(e.get("summary", "") or "", "html.parser").get_text(" ", strip=True),
        "domain": urlparse(src.get("href") or link).netloc.lower(),
        "age": age_hours(e),
    }


# ---------- обработка ----------
def handle_items(name, items, verify=False):
    first = (SILENT_FIRST_RUN or name.startswith("Сайт:")) and name not in init
    for it in items:
        link = it["link"]
        if not link or link in seen:
            continue
        age = it["age"]
        if first or (age is not None and age > MAX_AGE_HOURS):
            seen[link] = None
            continue
        if is_blocked(it):
            seen[link] = None
            print("Чёрный список:", it["title"])
            continue
        url, why = link, ""
        if verify:
            ok, url, why, checked = check_relevance(it)
            if not ok:
                if not checked:
                    tries[link] = tries.get(link, 0) + 1
                    if tries[link] < 3:
                        print(f"Не удалось прочитать, повторю позже ({tries[link]}/3): {it['title']}")
                        continue
                tries.pop(link, None)
                seen[link] = None
                print(f"Не по теме [{name}] ({fmt_age(age)}): {it['title']} | {why}")
                continue
        tries.pop(link, None)
        seen[link] = None
        if send(f"🔔 {name} · {fmt_age(age)}\n{it['title']}\n{url}"):
            stats["news"] += 1
            print(f"Отправлено ({fmt_age(age)}): {it['title']}" + (f" | {why}" if why else ""))
    init.add(name)


def check_feeds():
    feeds = build_feeds()
    for url in YOUTUBE:
        name = f"YouTube: {url.rstrip('/').split('/')[-1]}"
        try:
            feeds[name] = youtube_feed(url)
        except Exception as err:
            stats["src"] += 1
            errors.append(name)
            print("Ошибка YouTube:", err)
    for name, url in feeds.items():
        stats["src"] += 1
        try:
            entries = feedparser.parse(fetch(url)).entries
            verify = name.startswith("Google News") or name.endswith("(сайт)")
            handle_items(name, [entry_to_item(e) for e in entries], verify)
        except Exception as err:
            errors.append(name)
            print("Ошибка ленты:", name, "|", err)
        time.sleep(1)


def find_feed_urls(page_url, html):
    soup = BeautifulSoup(html, "html.parser")
    urls = []
    for l in soup.find_all("link", href=True):
        t = (l.get("type") or "").lower()
        if "alternate" in (l.get("rel") or []) and ("rss" in t or "atom" in t):
            urls.append(urljoin(page_url, l["href"]))
    p = urlparse(page_url)
    base = f"{p.scheme}://{p.netloc}"
    urls += [page_url.rstrip("/") + "/feed/", base + "/feed/", base + "/rss", base + "/feed"]
    return list(dict.fromkeys(urls))


def check_sites():
    for name, url in SITES.items():
        stats["src"] += 1
        try:
            html = fetch(url).decode("utf-8", errors="ignore")
            candidates = [site_feed[name]] if name in site_feed else find_feed_urls(url, html)
            feed_items = None
            for fu in candidates:
                try:
                    entries = feedparser.parse(fetch(fu)).entries
                except Exception:
                    continue
                if entries:
                    site_feed[name] = fu
                    feed_items = [entry_to_item(e) for e in entries]
                    break
            if feed_items:
                print(f"  {name}: RSS {site_feed[name]}, записей {len(feed_items)}")
                handle_items(f"{name} (сайт)", feed_items, verify=True)
            else:
                host = urlparse(url).netloc
                items, got = [], set()
                for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
                    href = urljoin(url, a["href"]).split("#")[0].split("?")[0]
                    slug = urlparse(href).path.rstrip("/").split("/")[-1]
                    if urlparse(href).netloc == host and slug.count("-") >= 3 and href not in got:
                        got.add(href)
                        items.append({"link": href, "title": a.get_text(" ", strip=True) or slug,
                                      "summary": "", "domain": host, "age": None})
                print(f"  {name}: RSS не найден, ссылок на статьи со страницы: {len(items)}")
                if not items:
                    raise ValueError("ни RSS, ни ссылок на статьи (возможно, страницу собирает JavaScript)")
                handle_items(f"Сайт: {name}", items, verify=True)
        except Exception as err:
            errors.append(f"Сайт: {name}")
            print("Ошибка сайта:", name, "|", err)
        time.sleep(1)


# ---------- sitemap ----------
def lastmod_age(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (time.time() - dt.timestamp()) / 3600
    except Exception:
        return None


def read_xml(url):
    data = fetch(url, timeout=40)
    if url.endswith(".gz"):
        data = gzip.decompress(data)
    return BeautifulSoup(data, "xml")


def check_sitemaps():
    for name, (url, pattern) in SITEMAPS.items():
        stats["src"] += 1
        try:
            soup = read_xml(url)
            pages = [soup]
            if soup.find("sitemapindex"):
                kids = []
                for s in soup.find_all("sitemap"):
                    if s.loc:
                        lm = s.lastmod.text if s.lastmod else ""
                        kids.append((("news" in s.loc.text.lower()), lm, s.loc.text.strip()))
                kids.sort(reverse=True)
                pages = []
                for _, _, u in kids[:3]:
                    try:
                        pages.append(read_xml(u))
                    except Exception as err:
                        print("  не открылся дочерний sitemap:", u, "|", str(err)[:80])
            items, total = [], 0
            for pg in pages:
                for u in pg.find_all("url"):
                    if not u.loc:
                        continue
                    total += 1
                    loc = u.loc.text.strip()
                    age = lastmod_age(u.lastmod.text if u.lastmod else "")
                    if pattern in loc and age is not None and age <= MAX_AGE_HOURS:
                        items.append({"link": loc, "title": urlparse(loc).path.rstrip("/").split("/")[-1],
                                      "summary": "", "domain": urlparse(loc).netloc, "age": age})
            print(f"  {name}: адресов в sitemap {total}, свежих подходящих {len(items)}")
            if total == 0:
                raise ValueError("sitemap пустой или не открылся")
            handle_items(f"Сайт: {name}", items, verify=True)
        except Exception as err:
            errors.append(f"Сайт: {name}")
            print("Ошибка sitemap:", name, "|", str(err)[:200])
        time.sleep(1)


# ---------- запуск (один проход) ----------
def main():
    global last_beat
    if FIRST_EVER:
        send("🤖 Бот запущен на GitHub Actions.")
    try:
        check_feeds()
        check_sites()
        check_sitemaps()
    except Exception as err:
        print("Сбой цикла:", err)

    ok = stats["src"] - len(errors)
    print(f"[{datetime.now():%H:%M:%S}] Источников {ok}/{stats['src']}, новых уведомлений {stats['news']}")

    if FIRST_EVER:
        msg = f"✅ Первый запуск завершён. Работают источников: {ok} из {stats['src']}."
        if errors:
            msg += "\nНе ответили: " + ", ".join(errors)
        send(msg)
    elif HEARTBEAT_HOURS and time.time() - last_beat >= HEARTBEAT_HOURS * 3600:
        msg = "✅ Бот работает."
        if errors:
            msg += "\nСейчас не отвечают: " + ", ".join(errors)
        send(msg, silent=True)
        last_beat = time.time()


try:
    main()
finally:
    save_state()
