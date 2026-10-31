#!/usr/bin/env python3
"""24x7 Telegram job-alert + quiz bot v2 (runs on GitHub Actions, no server needed).

Modes:
  alerts   -> official SSC API + Bihar sites (every 10 min) + AI search watcher for RRB/Army/Bihar/SSC
              (every 2 h), smart PDF summary, pin big news, then answer doubts in discussion groups
  ca       -> 7:30 AM current-affairs quiz (5 polls) in every channel
  cal      -> 9:30 AM "last date" reminders from notices already posted
  gk       -> 12:30 PM 10 one-liner GK
  roundup  -> 5:30 PM today's updates (Sunday: whole week)
  quiz     -> 8 PM main quiz (6 polls with explanation) + answer recap
  tip      -> 9:30 PM study tip or discussion poll
  selftest -> checks every source and Gemini, posts nothing
Secrets: TELEGRAM_BOT_TOKEN (required), GEMINI_API_KEY (free key, main). GEMINI_API_KEY_PAID optional backup only.
  tick     -> used by the 24x7 loop: runs any daily job that is due (once per day each) + alerts + replies
"""
import json, os, re, sys, time, html, hashlib, datetime, urllib.parse, base64
import requests
import functools
print = functools.partial(print, flush=True)

ROOT = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
STATE_FILE = os.path.join(ROOT, "state", "seen.json")
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI = os.environ.get("GEMINI_API_KEY", "").strip()
DRY = os.environ.get("DRY") == "1"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
TG = f"https://api.telegram.org/bot{TOKEN}"
MAX_ALERTS_PER_RUN = 4          # anti-spam across all channels
MAX_ITEMS_PER_PAGE = 40         # only the newest links of a page are watched
OFFICIAL = tuple(CFG.get("official_domains", ["gov.in", "nic.in", "cdac.in"]))
BIG_WORDS = re.compile(r"\b(notification|advertisement|advt|recruitment|vacanc|result|final result|merit list|"
                       r"admit card|answer key|exam date|schedule)\b", re.I)


def now_ist():
    return datetime.datetime.now(IST)


def load_state():
    try:
        st = json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        st = {}
    for k, d in (("seen", {}), ("quiz_done", []), ("log", []), ("deadlines", []), ("titles", [])):
        st.setdefault(k, d)
    return st


def save_state(st):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    for k, v in st["seen"].items():
        st["seen"][k] = v[-1500:]
    st["quiz_done"] = st["quiz_done"][-400:]
    st["log"] = st["log"][-400:]
    st["titles"] = st["titles"][-800:]
    today = now_ist().date().isoformat()
    st["deadlines"] = [d for d in st["deadlines"] if d.get("date", "") >= today][-200:]
    json.dump(st, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def channels():
    return {k: v for k, v in CFG["channels"].items() if v.get("chat_id")}


def esc(s):
    return html.escape(str(s or ""), quote=False)


def title_key(t):
    return re.sub(r"[^a-z0-9]", "", str(t).lower())[:70]


# ---------------- Telegram ----------------
def tg(method, data=None, files=None):
    if DRY:
        print("DRY", method, json.dumps(data, ensure_ascii=False)[:600])
        return {"ok": True, "result": {"message_id": 0}}
    for attempt in range(3):
        try:
            r = requests.post(f"{TG}/{method}", data=data, files=files, timeout=60)
            j = r.json()
            if j.get("ok"):
                return j
            if j.get("error_code") == 429:
                time.sleep(j.get("parameters", {}).get("retry_after", 5) + 1)
                continue
            print("TG error", method, j)
            # HTML parse problem -> retry once as plain text
            if "parse" in str(j.get("description", "")).lower() and data and data.get("parse_mode"):
                data = dict(data)
                data.pop("parse_mode", None)
                for f in ("text", "caption"):
                    if f in data:
                        data[f] = re.sub(r"<[^>]+>", "", data[f])
                continue
            return j
        except Exception as e:
            print("TG exception", e)
            time.sleep(3)
    return {}


def send_text(chat, text, silent=False, buttons=None):
    d = {"chat_id": chat, "text": text[:4000], "parse_mode": "HTML",
         "disable_web_page_preview": "true", "disable_notification": str(silent).lower()}
    if buttons:
        d["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    return tg("sendMessage", d)


def pin(chat, res):
    try:
        mid = res["result"]["message_id"]
        tg("pinChatMessage", {"chat_id": chat, "message_id": mid, "disable_notification": "true"})
    except Exception:
        pass


def download_pdf(url):
    try:
        r = requests.get(url, headers=UA, timeout=30, verify=False)
        if r.ok and r.content[:4] == b"%PDF" and len(r.content) < 19_000_000:
            return r.content
    except Exception as e:
        print("pdf download failed", url[:120], str(e)[:120])
    return None


def send_doc(chat, data, caption, filename, buttons=None):
    if not filename.lower().endswith(".pdf"):
        filename = (filename or "notice")[:50] + ".pdf"
    d = {"chat_id": chat, "caption": caption[:1024], "parse_mode": "HTML"}
    if buttons:
        d["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    return tg("sendDocument", d,
              files={"document": (filename[-60:], data, "application/pdf")})


def send_quiz(chat, q):
    opts = [str(o).strip()[:100] for o in q.get("options", [])][:4]
    if len(opts) < 2 or len(set(opts)) != len(opts):
        return None
    try:
        ans = int(q.get("answer", 0))
    except Exception:
        return None
    if not 0 <= ans < len(opts):
        return None
    return tg("sendPoll", {
        "chat_id": chat, "question": str(q["question"])[:300], "options": json.dumps(opts, ensure_ascii=False),
        "type": "quiz", "correct_option_id": ans, "is_anonymous": "true",
        "explanation": str(q.get("explanation", ""))[:200]})


# ---------------- Gemini (free key first; paid key only if one is ever added as backup) ----------------
KEYS = [k for k in dict.fromkeys([GEMINI, os.environ.get("GEMINI_API_KEY_PAID", "").strip()]) if k]
FALLBACK_MODELS = ["gemini-flash-lite-latest", "gemini-flash-latest"]
_MODELS = []
START = time.time()
AI_BUDGET = 14 * 60   # seconds of AI work allowed per run, so a slow API never blocks the bot
EXHAUSTED = set()     # (key_index, "search"|"plain") that hit quota in this run


def models():
    """Pick the newest available Flash models automatically (Google retires old names often)."""
    if _MODELS:
        return _MODELS
    found = []
    for key in KEYS:
        try:
            j = requests.get("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
                             headers={"x-goog-api-key": key}, timeout=30).json()
            for m in j.get("models", []):
                name = m.get("name", "").replace("models/", "")
                if "generateContent" not in m.get("supportedGenerationMethods", []):
                    continue
                if not re.fullmatch(r"gemini-\d+(\.\d+)?-flash(-lite)?", name):
                    continue
                ver = float(re.search(r"gemini-(\d+(?:\.\d+)?)", name).group(1))
                found.append(((ver, "lite" not in name), name))
            if found:
                break
        except Exception as e:
            print("model list failed", str(e)[:120])
    found.sort(reverse=True)
    # lite first: fast and cheap; full Flash as backup
    best = [n for (v, full), n in found if not full][:1] + [n for (v, full), n in found if full][:2]
    for n in best + FALLBACK_MODELS:
        if n not in _MODELS:
            _MODELS.append(n)
    print("gemini models:", _MODELS[:4], "keys:", len(KEYS))
    return _MODELS


def _call(key, model, body):
    return requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                         headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                         json=body, timeout=(15, 80))


def gemini(prompt, search=False, pdf=None, as_json=True, temp=0.4, nosearch_ok=False):
    if not KEYS or time.time() - START > AI_BUDGET:
        return ""
    mode = "search" if search else "plain"
    parts = [{"text": prompt}]
    if pdf:
        parts.insert(0, {"inline_data": {"mime_type": "application/pdf", "data": base64.b64encode(pdf).decode()}})
    body = {"contents": [{"parts": parts}], "generationConfig": {"temperature": temp}}
    if search:
        body["tools"] = [{"google_search": {}}]
    elif as_json:
        body["generationConfig"]["responseMimeType"] = "application/json"
    for ki, key in enumerate(KEYS):
        if (ki, mode) in EXHAUSTED:
            continue
        limited = 0
        for model in list(models())[:3]:
            for attempt in range(2):
                try:
                    r = _call(key, model, body)
                    if r.status_code == 429:
                        if attempt == 0 and "quota" not in r.text.lower():
                            time.sleep(15)
                            continue
                        limited += 1
                        print("gemini 429 key", ki, model, r.text[:120].replace("\n", " "))
                        break
                    if r.status_code == 404 and model in _MODELS and len(_MODELS) > 1:
                        _MODELS.remove(model)   # retired model, skip it for the rest of the run
                    j = r.json()
                    txt = "".join(p.get("text", "") for p in j["candidates"][0]["content"]["parts"])
                    if txt.strip():
                        return txt
                except Exception as e:
                    print("gemini failed key", ki, model, str(e)[:160])
                break
        if limited >= 2:
            EXHAUSTED.add((ki, mode))
    if search and nosearch_ok:
        return gemini(prompt, False, pdf, False, temp)
    return ""


def parse_json(txt):
    if not txt:
        return None
    txt = re.sub(r"^```(json)?|```$", "", txt.strip(), flags=re.M)
    for pat in (r"(\[.*\])", r"(\{.*\})"):
        m = re.search(pat, txt, re.S)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                pass
    return None


SUMMARY_PROMPT = (
    "You are writing a Telegram alert for Indian govt-job aspirants. Read this official notice{src} and reply JSON: "
    "{{\"hi_title\": \"one short simple Hindi line saying what this notice is (max 18 words)\", "
    "\"points\": [\"up to 6 short Hindi points, ONLY facts written in the notice: post name, total vacancies, "
    "qualification, age limit, fee, apply start/last date, exam date, how to download\"], "
    "\"last_date\": \"YYYY-MM-DD of the last date to apply/submit if clearly given, else empty\", "
    "\"big\": true if this is a new recruitment notification, final result, admit card or exam schedule, else false}}. "
    "Never add facts not in the notice. Title: {title}")


def smart_summary(title, pdf=None):
    out = parse_json(gemini(SUMMARY_PROMPT.format(src="" if pdf else " title", title=title[:400]), pdf=pdf))
    if not isinstance(out, dict):
        return {"hi_title": "", "points": [], "last_date": "", "big": bool(BIG_WORDS.search(title))}
    pts = [str(p).strip() for p in (out.get("points") or []) if str(p).strip()][:6]
    ld = str(out.get("last_date") or "")
    if not re.fullmatch(r"20\d\d-\d\d-\d\d", ld):
        ld = ""
    return {"hi_title": str(out.get("hi_title") or "")[:200], "points": pts if pdf else [], "last_date": ld,
            "big": bool(out.get("big")) or bool(BIG_WORDS.search(title) and re.search(r"result|admit|notification|advt|advertisement", title, re.I))}


# ---------------- Sources ----------------
TYPE_LABEL = {"notice-boards": "Notice", "results": "Result", "admit-card": "Admit Card", "answer-key": "Answer Key"}
EXAM_TAGS = [("Combined Graduate", "#CGL"), ("CGL", "#CGL"), ("Higher Secondary", "#CHSL"), ("CHSL", "#CHSL"),
             ("Multi-Tasking", "#MTS"), ("MTS", "#MTS"), ("Constable (GD)", "#GD"), ("GD", "#GD"),
             ("Sub-Inspector", "#CPO"), ("Junior Engineer", "#JE"), ("Stenographer", "#Steno"),
             ("Selection Post", "#SelectionPost"), ("Hindi Translator", "#JHT")]


def ssc_items():
    base = "https://ssc.gov.in/api/general-website/portal/"
    out = []
    for ct in CFG["ssc_types"]:
        path = "notice-boards" if ct == "notice-boards" else "records"
        params = {"page": 1, "limit": 10, "contentType": ct, "key": "createdAt", "order": "DESC",
                  "isAttachment": "true", "language": "english",
                  "attributes": "id,headline,examId,contentType,redirectUrl,startDate,endDate,language,createdAt"}
        if ct != "notice-boards":
            params["pageType"] = "filter"
        try:
            j = requests.get(base + path, params=params, headers=UA, timeout=40).json()
        except Exception as e:
            print("ssc fetch failed", ct, e)
            continue
        if str(j.get("statusCode")) != "200":
            print("ssc bad", ct, str(j)[:200])
            continue
        for d in j.get("data", []):
            atts = [a for a in d.get("attachments") or [] if a.get("path")]
            pdf = ("https://ssc.gov.in/api/attachment/" + atts[0]["path"].replace("\\", "/")) if atts else ""
            out.append({"id": f"ssc:{d['id']}", "type": ct, "src": "SSC", "channel": "ssc",
                        "title": re.sub(r"\s+", " ", d.get("headline", "")).strip(),
                        "pdf": urllib.parse.quote(pdf, safe=":/") if pdf else "", "link": "https://ssc.gov.in",
                        "file": atts[0].get("fileName", "notice.pdf") if atts else "notice.pdf"})
    return out


LINK_RE = re.compile(r'<a[^>]+href=["\']([^"\']+?\.pdf[^"\']*)["\'][^>]*>(.*?)</a>', re.I | re.S)


def page_items(p):
    try:
        r = requests.get(p["url"], headers=UA, timeout=40, verify=False)
        txt = r.text
    except Exception as e:
        print("page fetch failed", p["url"], str(e)[:120])
        return None
    out, urls = [], set()
    for href, label in LINK_RE.findall(txt):
        label = re.sub(r"<[^>]+>", " ", label)
        label = re.sub(r"\s+", " ", html.unescape(label)).strip()
        if len(label) < 8:
            continue
        url = urllib.parse.urljoin(p["url"], html.unescape(href).strip())
        if url in urls:
            continue
        urls.add(url)
        out.append({"id": f"{p['src']}:" + hashlib.md5(url.encode()).hexdigest()[:12], "title": label[:300],
                    "src": p["src"], "channel": p["channel"], "type": "notice",
                    "pdf": urllib.parse.quote(url, safe=":/?=&%"), "link": p["url"],
                    "file": url.split("/")[-1].split("?")[0]})
        if len(out) >= MAX_ITEMS_PER_PAGE:
            break
    return out


def is_official(url):
    try:
        host = urllib.parse.urlparse(url).netloc.lower().split(":")[0]
    except Exception:
        return False
    return any(host == d or host.endswith("." + d) for d in OFFICIAL)


def watch_items(key, what, st):
    """Gemini + Google Search finds fresh official notices from sites GitHub can't open directly."""
    today = now_ist()
    prompt = (f"Today is {today:%d %B %Y} (India). Using Google Search, find official notices published in the last 3 days "
              f"about: {what}. Include only items you actually found, each with its OFFICIAL government web page or PDF "
              "link (domain ending gov.in, nic.in or cdac.in). Skip news-site-only items, rumours, and anything older. "
              "Reply ONLY a JSON array (max 4) of {\"title\": \"exact notice title in English\", \"url\": \"official link\", "
              "\"date\": \"YYYY-MM-DD published\", \"hi_title\": \"one simple Hindi line\", "
              "\"points\": [\"up to 5 short Hindi facts from the notice: posts, vacancies, qualification, last date\"], "
              "\"last_date\": \"YYYY-MM-DD or empty\", \"big\": true/false}. If nothing new, reply [].")
    data = parse_json(gemini(prompt, search=True, temp=0.1))
    out = []
    cutoff = (today - datetime.timedelta(days=4)).date().isoformat()
    for d in data if isinstance(data, list) else []:
        if not isinstance(d, dict):
            continue
        url, title = str(d.get("url", "")).strip(), str(d.get("title", "")).strip()
        date = str(d.get("date", ""))
        if not title or not is_official(url) or not re.fullmatch(r"20\d\d-\d\d-\d\d", date) or date < cutoff \
                or date > today.date().isoformat():
            print("watch skip", key, title[:60], url[:80], date)
            continue
        ld = str(d.get("last_date") or "")
        out.append({"id": "w:" + hashlib.md5((url + title_key(title)).encode()).hexdigest()[:12], "title": title[:300],
                    "src": key.upper() if key != "army" else "Defence", "channel": key, "type": "watch",
                    "pdf": url if url.lower().split("?")[0].endswith(".pdf") else "", "link": url, "file": url.split("/")[-1],
                    "pre": {"hi_title": str(d.get("hi_title", ""))[:200],
                            "points": [str(x) for x in (d.get("points") or [])][:5],
                            "last_date": ld if re.fullmatch(r"20\d\d-\d\d-\d\d", ld) else "", "big": bool(d.get("big"))}})
    return out


# ---------------- Aggregator sources (for sites that block foreign servers) ----------------
CH_RULES = [
    ("army", r"\b(army|agniveer|agnipath|navy|naval|air ?force|airmen|agniveervayu|coast guard|bsf|crpf|cisf|itbp|ssb\b|"
             r"assam rifles|nda\b|cds\b|capf|territorial army|tgc|tes\b)"),
    ("rrb", r"\b(rrb|railway|rrc|ntpc|group d|alp\b|rpf|metro rail|ircon|dfccil)"),
    ("bihar", r"\b(bihar|bpsc|bssc|csbc|bpssc|bceceb|shsb|patna)"),
    ("ssc", r"\b(ssc\b|staff selection)"),
]


def classify(title):
    t = title.lower()
    if re.search(r"\b(nta|ugc|csir|neet|jee)\b", t):
        return None
    for ch, rx in CH_RULES:
        if re.search(rx, t, re.I):
            return ch
    return None


def agg_items(a):
    try:
        r = requests.get(a["url"], headers=UA, timeout=40)
        if r.status_code != 200:
            print("agg bad", a["src"], r.status_code)
            return None
        txt = r.text
    except Exception as e:
        print("agg fetch failed", a["src"], str(e)[:120])
        return None
    host = re.escape(a["host"])
    out, keys = [], set()
    for href, label in re.findall(r'<a[^>]+href="(https?://(?:www\.)?' + host + r'/[^"#]+)"[^>]*>(.*?)</a>', txt, re.S):
        label = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", label))).strip(" -|")
        label = re.sub(r"\s*[\u2013-]\s*(apply here|out|apply now|click here)$", "", label, flags=re.I)
        if len(label) < 12 or href.rstrip("/").count("/") < 3:
            continue
        if not re.search(r"form|result|admit|answer key|exam|notification|recruitment|vacanc|merit|date|status|"
                         r"syllabus|cut ?off|\d{3,}", label, re.I):
            continue   # menu/category links, not an update
        ch = classify(label)
        k = title_key(label)
        if not ch or k in keys:
            continue
        keys.add(k)
        out.append({"id": f"{a['src']}:" + hashlib.md5(k.encode()).hexdigest()[:12], "title": label[:250],
                    "src": {"army": "Defence", "rrb": "Railway", "bihar": "Bihar", "ssc": "SSC"}[ch],
                    "channel": ch, "type": "agg", "pdf": "", "link": href, "file": ""})
    print("agg", a["src"], len(out))
    return out


def agg_detail(url):
    """Official links + plain text of the update page."""
    try:
        t = requests.get(url, headers=UA, timeout=40).text
    except Exception as e:
        print("detail failed", url[:100], str(e)[:100])
        return [], ""
    links = []
    for h in re.findall(r'href="([^"]+)"', t):
        h = html.unescape(h).strip()
        if h.startswith("http") and is_official(h) and h not in links:
            links.append(h)
    body = re.sub(r"<script.*?</script>|<style.*?</style>", " ", t, flags=re.S | re.I)
    body = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", body)))
    i = body.find("Important Dates")
    return links, body[max(0, i - 300) if i >= 0 else 0:][:7000]


AGG_PROMPT = ("Below is text about an Indian govt exam update titled '{title}'. Reply JSON {{\"hi_title\": \"one short "
              "simple Hindi line saying what this update is (max 18 words)\", \"points\": [\"up to 6 short Hindi points "
              "with ONLY facts present in the text: total posts, qualification, age limit, fee, apply start/last date, exam "
              "date\"], \"last_date\": \"YYYY-MM-DD last date to apply if given, else empty\", \"big\": true if new "
              "recruitment form, result or admit card}}. Write in your own words, never mention any website name.\n\n{text}")


def agg_summary(title, text):
    out = parse_json(gemini(AGG_PROMPT.format(title=title, text=text[:6000])))
    if isinstance(out, dict):
        ld = str(out.get("last_date") or "")
        return {"hi_title": str(out.get("hi_title") or "")[:200],
                "points": [re.sub(r"(?i)sarkari ?result|sarkariexam|freejobalert", "", str(p)) for p in (out.get("points") or [])][:6],
                "last_date": ld if re.fullmatch(r"20\d\d-\d\d-\d\d", ld) else "", "big": bool(out.get("big"))}
    # no AI: pull the last date with a simple pattern
    m = re.search(r"Last Date[^:]{0,40}:\s*(\d{2})/(\d{2})/(20\d{2})", text)
    return {"hi_title": "", "points": [], "last_date": f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else "",
            "big": bool(re.search(r"online form|result|admit card", title, re.I))}


# ---------------- Alerts ----------------
def caption(it, sm):
    ch = CFG["channels"][it["channel"]]
    if it["channel"] == "ssc" and it["src"] == "SSC":
        tags = " ".join(sorted({t for k, t in EXAM_TAGS if k.lower() in it["title"].lower()})[:2])
        head = f"SSC UPDATE | {TYPE_LABEL.get(it['type'], 'Notice')}"
    else:
        tags = ""
        head = f"{it['src']} UPDATE"
    emoji = "🚨" if sm.get("big") else "🔔"
    lines = [f"{emoji} <b>{esc(head)}</b>", "", f"📌 {esc(it['title'][:350])}"]
    if sm.get("hi_title"):
        lines.append(f"📝 {esc(sm['hi_title'])}")
    if sm.get("points"):
        lines.append("")
        lines += [f"▪️ {esc(p[:160])}" for p in sm["points"]]
    if sm.get("last_date") and sm["last_date"] >= now_ist().date().isoformat():
        try:
            lines.append(f"\n⏰ Last date: {datetime.date.fromisoformat(sm['last_date']):%d %b %Y}")
        except Exception:
            pass
    host = urllib.parse.urlparse(it["link"]).netloc.replace("www.", "")
    link = it["link"] if it["type"] in ("watch", "agg") else f"https://{host}"
    lines += ["", "👇 Official link neeche button me hai. Apply se pehle notice zaroor padhein.",
              "", f"{ch['tag']} {tags}".strip()]
    return "\n".join(lines)


def buttons_for(it):
    """Inline buttons: notice PDF, official website/apply page, and the discussion group."""
    row, seen = [], set()
    host = urllib.parse.urlparse(it.get("link") or it.get("pdf") or "").netloc
    cands = [("📄 Official Notice", it.get("pdf")), ("📄 Official Notice" if not it.get("pdf") else "🌐 Official Website",
             it["link"] if it["type"] in ("watch", "agg") else (f"https://{host}" if host else ""))]
    for extra in it.get("extra_links", [])[:1]:
        cands.append(("✅ Apply / Login", extra))
    for label, url in cands:
        if url and url.startswith("http") and url not in seen:
            seen.add(url)
            row.append([{"text": label, "url": url}])
    g = CFG["channels"][it["channel"]].get("group_link")
    if g:
        row.append([{"text": "💬 Doubt? Group me poochho", "url": g}])
    return row[:4]


def post_item(st, it):
    chat = CFG["channels"][it["channel"]]["chat_id"]
    if it["type"] == "agg":
        links, text = agg_detail(it["link"])
        pdfs = [l for l in links if ".pdf" in l.lower()]
        if not links:
            print("no official link, skip", it["title"][:60])
            return "skip"
        it["link"] = (pdfs or links)[0]
        it["pdf"] = pdfs[0] if pdfs else ""
        it["extra_links"] = [l for l in links if l not in pdfs and re.search(r"apply|regist|login|candidate|recruit", l, re.I)]
        if pdfs:
            it["link"] = next((l for l in links if l not in pdfs), it["link"])
        it["file"] = it["pdf"].split("/")[-1].split("?")[0] if pdfs else ""
        it["pre"] = agg_summary(it["title"], text)
    pdf = download_pdf(it["pdf"]) if it.get("pdf") else None
    sm = it.get("pre") or smart_summary(it["title"], pdf if pdf and len(pdf) < 15_000_000 else None)
    cap = caption(it, sm)
    btn = buttons_for(it)
    if pdf and len(cap) <= 1024:
        res = send_doc(chat, pdf, cap, it["file"], btn)
    else:
        res = send_text(chat, cap, buttons=btn)
        if pdf and res and res.get("ok"):
            send_doc(chat, pdf, f"📄 {esc(it['title'][:200])}", it["file"])
    if not (res and res.get("ok")):
        return False
    if sm.get("big"):
        pin(chat, res)
    day = now_ist().date().isoformat()
    st["log"].append({"ch": it["channel"], "day": day, "title": sm.get("hi_title") or it["title"][:150],
                      "src": it["src"]})
    st["titles"].append(title_key(it["title"]))
    if sm.get("last_date") and sm["last_date"] >= day:
        st["deadlines"].append({"ch": it["channel"], "date": sm["last_date"],
                                "title": (sm.get("hi_title") or it["title"])[:150]})
    return True


def health(st, key, ok):
    """Count failures per source; after ~6 hours of failure open a GitHub issue (you get an email)."""
    f = st.setdefault("fail", {})
    if ok:
        f.pop(key, None)
        return
    f[key] = f.get(key, 0) + 1
    if f[key] == 36:
        report_issue(f"Source down: {key}", f"Bot could not read {key} for about 6 hours. It keeps retrying; "
                     "other sources and backups keep working.")


def report_issue(title, body):
    tok, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not tok or not repo or DRY:
        print("ISSUE", title)
        return
    try:
        requests.post(f"https://api.github.com/repos/{repo}/issues", timeout=30,
                      headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"},
                      json={"title": title, "body": body})
    except Exception as e:
        print("issue failed", e)


def run_alerts(force_watch=False):
    st = load_state()
    chs = channels()
    posted = 0
    jobs = []
    if "ssc" in chs:
        items = ssc_items()
        health(st, "ssc", bool(items))
        jobs.append(("ssc", items or None))
    for p in CFG["pages"]:
        if p["channel"] in chs:
            items = page_items(p)
            health(st, p["src"], bool(items))
            jobs.append((p["src"], items or None))
    # aggregators: primary first, the next one only as backup; all keep their seen-lists fresh
    fail = st.get("fail", {})
    official_down = {"ssc": fail.get("ssc", 0) >= 6,
                     "bihar": sum(fail.get(p["src"], 0) >= 6 for p in CFG["pages"] if p["channel"] == "bihar") >= 2}
    primary_ok = False
    for n, a in enumerate(CFG.get("aggregators", [])):
        items = agg_items(a)
        health(st, a["src"], bool(items))
        if not items:
            continue
        active = not primary_ok
        primary_ok = True
        for i in items:
            # SSC/Bihar have their own official feeds; aggregator posts there only while those are down
            if not active or (i["channel"] in official_down and not official_down[i["channel"]]) or i["channel"] not in chs:
                i["mute"] = True
        jobs.append((a["src"], items))
    t = now_ist()
    if KEYS and (force_watch or (t.hour % 4 == 1 and t.minute < 10)):
        for key, what in CFG.get("ai_watch", {}).items():
            if key in chs:
                jobs.append(("watch:" + key, watch_items(key, what, st)))
    per_ch = {}
    for key, items in jobs:
        if items is None:      # fetch failed; try next run
            continue
        seen = st["seen"].setdefault(key, [])
        if not seen:
            seen.extend(i["id"] for i in items)   # first sight: seed silently, never spam old notices
            seen.append("_seeded")
            st["titles"].extend(title_key(i["title"]) for i in items)
            print("seeded", key, len(items))
            continue
        new = [i for i in items if i["id"] not in seen]
        for it in reversed(new):  # oldest first
            if it.get("mute") or title_key(it["title"]) in st["titles"]:
                seen.append(it["id"])
                continue
            if posted >= MAX_ALERTS_PER_RUN or per_ch.get(it["channel"], 0) >= 2:
                continue   # stays unseen, goes out next run
            res = post_item(st, it)
            if res:
                seen.append(it["id"])
                if res is True:
                    posted += 1
                    per_ch[it["channel"]] = per_ch.get(it["channel"], 0) + 1
                    time.sleep(3)
    st["last_run_day"] = now_ist().date().isoformat()   # daily commit keeps GitHub schedules alive
    save_state(st)
    print("alerts posted", posted)


# ---------------- Quizzes / content ----------------
QUIZ_RULES = ("Each item: {\"question\": \"Hindi question with key English term in brackets, max 250 chars\", "
              "\"options\": [4 distinct short options, each max 80 chars], \"answer\": index 0-3, "
              "\"explanation\": \"1-2 lines Hindi, max 180 chars\"}. Double-check every answer; maths answers must be "
              "computed. Facts must be accurate and verifiable; no doubtful or outdated questions. Return only a JSON array.")


NEWS_FEEDS = [
    "https://www.pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3",
    "https://www.pib.gov.in/RssMain.aspx?ModId=6&Lang=2&Regid=3",
    "https://news.google.com/rss/search?q=India+(appointed+OR+scheme+OR+award+OR+launched+OR+exercise+OR+summit)+when:2d"
    "&hl=en-IN&gl=IN&ceid=IN:en",
    "https://indianexpress.com/section/india/feed/",
]
_NEWS = []


def news_digest():
    """Latest headlines from free feeds (PIB is official). Cached for the run."""
    if _NEWS:
        return _NEWS[0]
    lines = []
    for u in NEWS_FEEDS:
        try:
            x = requests.get(u, headers=UA, timeout=30).content.decode("utf-8", "ignore")
            for t in re.findall(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", x, re.S)[1:40]:
                t = re.sub(r"\s+", " ", html.unescape(t)).strip()
                if len(t) > 25 and t not in lines:
                    lines.append(t)
        except Exception as e:
            print("feed failed", u[:50], str(e)[:80])
    print("news headlines", len(lines))
    _NEWS.append("\n".join("- " + l for l in lines[:120]) if len(lines) >= 10 else "")
    return _NEWS[0]


def get_questions(focus, n, kind, avoid):
    if kind == "ca":
        news = news_digest()
        if news:   # free: real headlines from news feeds, no paid search needed
            prompt = (f"Today is {now_ist():%d %B %Y}. From ONLY the news headlines below, make {n} current-affairs MCQs "
                      f"useful for {focus} (appointments, schemes, awards, defence, sports, reports, places). Every fact "
                      f"must come from the headlines; skip politics/crime/opinion. " + QUIZ_RULES + "\n\nHEADLINES:\n" + news)
            data = parse_json(gemini(prompt, temp=0.3))
        else:
            data = None
        if not data:
            prompt = (f"Using Google Search, make {n} current-affairs MCQs on important India/world news of the last 3 "
                      f"days (today is {now_ist():%d %B %Y}) useful for {focus}. " + QUIZ_RULES)
            data = parse_json(gemini(prompt, search=True, temp=0.3))
    else:
        prompt = (f"Make {n} MCQs for {focus} taken from REAL previous year questions (PYQ) of these exams, strictly "
                  f"inside the official syllabus, nothing out of syllabus. Mix: 2 static GK (history/polity/geography/"
                  f"economy), 1 science, 1 reasoning, 1 arithmetic maths, 1 Hindi/English or exam-specific topic. At the "
                  f"end of each explanation add the exam and year like '(SSC CGL 2022)' only if you are sure, else "
                  f"'(PYQ pattern)'. Avoid these already-asked questions: {' | '.join(avoid[-60:])}. " + QUIZ_RULES)
        data = parse_json(gemini(prompt, temp=0.8))
    return [q for q in (data or []) if isinstance(q, dict) and "question" in q][:n]


def bank_questions(st, key, n):
    try:
        bank = json.load(open(os.path.join(ROOT, "quiz_bank.json"), encoding="utf-8"))
    except Exception:
        return []
    pos = st.setdefault("bank_pos", {}).get(key, 0)
    qs = [bank[(pos + i) % len(bank)] for i in range(min(n, len(bank)))]
    st["bank_pos"][key] = (pos + n) % len(bank)
    print("using quiz bank for", key)
    return qs


def run_quiz(kind):
    st = load_state()
    for key, ch in channels().items():
        n = 5 if kind == "ca" else 6
        qs = get_questions(ch["focus"], n, kind, st["quiz_done"])
        if not qs and kind == "quiz":
            qs = bank_questions(st, key, n)   # AI down: use the saved question bank
        if not qs:
            print("no questions for", key)
            continue
        head = "☀️ <b>सुबह का Current Affairs Quiz</b>" if kind == "ca" else "🎯 <b>आज का Main Quiz (8 PM)</b>"
        send_text(ch["chat_id"], f"{head}\n📅 {now_ist():%d %b %Y} | {len(qs)} सवाल, जवाब देकर खुद को परखें 👇")
        recap = []
        for q in qs:
            if not send_quiz(ch["chat_id"], q):
                continue
            try:
                recap.append(f"{len(recap)+1}. <b>{esc(q['options'][int(q['answer'])])}</b> — {esc(str(q.get('explanation',''))[:120])}")
            except Exception:
                pass
            st["quiz_done"].append(str(q["question"])[:50])
            time.sleep(15 if kind == "quiz" else 4)
        if kind == "quiz" and recap:
            send_text(ch["chat_id"], "✅ <b>आज के जवाब</b>\n\n" + "\n".join(recap) +
                      f"\n\nकितने सही हुए? Group में बताइए 👇\n{ch['tag']}", silent=True)
        time.sleep(3)
    save_state(st)


def run_gk():
    for key, ch in channels().items():
        data = parse_json(gemini(f"Make 10 accurate one-liner GK facts (question - answer) for {ch['focus']}, "
                                 "Hindi with key English terms, short, exam-useful. JSON array of {\"q\": \"...\", \"a\": \"...\"}.",
                                 temp=0.8))
        if not isinstance(data, list) or not data:
            continue
        lines = [f"{i}. {esc(d.get('q',''))} — <b>{esc(d.get('a',''))}</b>"
                 for i, d in enumerate([d for d in data if isinstance(d, dict)][:10], 1)]
        send_text(ch["chat_id"], "📚 <b>आज के 10 One-Liner GK</b>\n\n" + "\n".join(lines) +
                  f"\n\nSave कर लो, revision में काम आएगा। {ch['tag']}", silent=True)
        time.sleep(3)


def run_capost():
    """Daily current-affairs digest from free feeds (PIB official + news), 8:30 AM and 6:30 PM."""
    news = news_digest()
    if not news:
        return
    data = parse_json(gemini(
        f"Today is {now_ist():%d %B %Y}. From ONLY these headlines pick the 8 most exam-useful current affairs for "
        "SSC/Railway/Bihar/Defence exams (appointments, schemes, awards, defence, sports, reports, summits, firsts). "
        "Skip politics fights, crime, opinion. JSON array of {\"hi\": \"one clear Hindi line with key English terms, "
        "max 160 chars, only facts in the headline\"}.\n\nHEADLINES:\n" + news, temp=0.2))
    items = [str(d.get("hi", "")).strip() for d in (data or []) if isinstance(d, dict) and d.get("hi")][:8]
    if len(items) < 4:
        return
    head = "🌅 <b>सुबह का Current Affairs</b>" if now_ist().hour < 12 else "🌆 <b>शाम का Current Affairs</b>"
    body = head + f" | {now_ist():%d %b %Y}\n\n" + "\n".join(f"{i}. {esc(t)}" for i, t in enumerate(items, 1))
    for key, ch in channels().items():
        send_text(ch["chat_id"], body + f"\n\nरात 8 बजे के Quiz में इनसे सवाल आ सकते हैं। {ch['tag']}", silent=True)
        time.sleep(2)


def run_cal():
    st = load_state()
    today = now_ist().date()
    end = (today + datetime.timedelta(days=10)).isoformat()
    for key, ch in channels().items():
        ds = sorted({(d["date"], d["title"]) for d in st["deadlines"] if d["ch"] == key and today.isoformat() <= d["date"] <= end})
        if not ds:
            continue
        lines = []
        for date, title in ds[:10]:
            dd = datetime.date.fromisoformat(date)
            left = (dd - today).days
            when = "आज आखिरी दिन!" if left == 0 else f"{left} दिन बचे"
            lines.append(f"⏰ {dd:%d %b} ({when}) — {esc(title)}")
        send_text(ch["chat_id"], "📅 <b>Last Date Reminder</b>\n\n" + "\n".join(lines) +
                  f"\n\nआखिरी दिन का इंतज़ार मत करो, आज ही apply करो। {ch['tag']}", silent=True)
        time.sleep(2)


def run_roundup():
    st = load_state()
    t = now_ist()
    sunday = t.weekday() == 6
    start = (t.date() - datetime.timedelta(days=6 if sunday else 0)).isoformat()
    for key, ch in channels().items():
        items = [l for l in st["log"] if l["ch"] == key and l["day"] >= start]
        if not items:
            continue
        head = "🗓️ <b>इस हफ्ते के सभी Updates</b>" if sunday else "🧾 <b>आज के सभी Updates</b>"
        lines = [f"{i}. {esc(l['title'])} ({esc(l['src'])})" for i, l in enumerate(items[-15:], 1)]
        send_text(ch["chat_id"], head + "\n\n" + "\n".join(lines) +
                  f"\n\nकुछ छूट गया हो तो ऊपर channel में देखें। {ch['tag']}", silent=True)
        time.sleep(2)


def run_tip():
    poll_day = now_ist().toordinal() % 2 == 0
    for key, ch in channels().items():
        if poll_day:
            d = parse_json(gemini(f"Make one friendly discussion poll for Telegram aspirants of {ch['focus']} "
                                  "(about preparation, strategy, or which subject is hardest; no facts needed). Hindi. "
                                  "JSON {\"question\": \"max 200 chars\", \"options\": [3-4 options max 60 chars]}", temp=0.9))
            if isinstance(d, dict) and d.get("question") and 2 <= len(d.get("options") or []) <= 10:
                tg("sendPoll", {"chat_id": ch["chat_id"], "question": str(d["question"])[:300],
                                "options": json.dumps([str(o)[:100] for o in d["options"][:4]], ensure_ascii=False),
                                "is_anonymous": "true", "disable_notification": "true"})
        else:
            d = parse_json(gemini(f"Write one practical study tip for {ch['focus']} aspirants in simple Hindi, "
                                  "4-5 short lines, honest, no false promises, no links. JSON {\"title\": \"...\", \"tip\": \"...\"}",
                                  temp=0.9))
            if isinstance(d, dict) and d.get("tip"):
                send_text(ch["chat_id"], f"💡 <b>{esc(d.get('title') or 'आज की Study Tip')}</b>\n\n{esc(d['tip'])}"
                          f"\n\n{ch['tag']}", silent=True)
        time.sleep(3)


# ---------------- Doubts in discussion groups ----------------
def run_replies():
    if not KEYS:
        return
    st = load_state()
    j = tg("getUpdates", {"offset": st.get("update_offset", 0), "timeout": 0, "allowed_updates": json.dumps(["message"])})
    groups = {str(v.get("group_id")): v for v in CFG["channels"].values() if v.get("group_id")}
    answered = 0
    for u in (j.get("result", []) if j and isinstance(j.get("result"), list) else []):
        st["update_offset"] = u["update_id"] + 1
        m = u.get("message") or {}
        chat = str(m.get("chat", {}).get("id"))
        text = (m.get("text") or "").strip()
        frm = m.get("from", {})
        if chat not in groups or not text or frm.get("is_bot") or m.get("is_automatic_forward") or m.get("sender_chat"):
            continue
        if time.time() - m.get("date", 0) > 6 * 3600:   # too old, skip
            continue
        is_q = "?" in text or re.search(r"\b(kab|kya|kaise|kitna|kitni|kaun|kyu|kyon|when|what|how|which|date|admit|"
                                        r"result|cutoff|syllabus|form|age|fees?|eligib)\b", text, re.I)
        if not is_q or len(text) < 6 or answered >= 8:
            continue
        ctx = (m.get("reply_to_message") or {}).get("text") or (m.get("reply_to_message") or {}).get("caption") or ""
        ans = gemini("You help students in an Indian govt-job Telegram group about "
                     f"{groups[chat]['focus']}. Answer in short simple Hinglish (max 60 words). Use Google Search for "
                     "dates/facts. If not sure or no official info, say so and suggest the official website. Never invent "
                     "dates, cutoffs or rumours. No links except official .gov.in/.nic.in sites. Plain text only.\n"
                     f"Post context: {ctx[:800]}\nQuestion: {text[:500]}", search=True, temp=0.2, nosearch_ok=True)
        ans = re.sub(r"[*#`]", "", ans or "").strip()
        if ans:
            tg("sendMessage", {"chat_id": chat, "text": ans[:900], "reply_to_message_id": m.get("message_id"),
                               "disable_web_page_preview": "true"})
            answered += 1
            time.sleep(2)
    save_state(st)
    print("replies", answered)


# ---------------- Self test ----------------
def run_selftest():
    global DRY
    DRY = True
    print("SSC items:", len(ssc_items()))
    for p in CFG["pages"]:
        its = page_items(p)
        print(p["src"], "items:", None if its is None else len(its), (its or [{}])[0].get("title", "")[:80])
    print("Gemini:", (gemini("Reply JSON {\"ok\": true}") or "FAILED")[:80])
    print("Gemini search:", (gemini("Using Google Search, what is today's date in India? One line.", search=True,
                                     as_json=False) or "FAILED")[:120].replace("\n", " "))
    print("keys usable:", len(KEYS), "exhausted:", sorted(EXHAUSTED))
    for a in CFG.get("aggregators", []):
        its = agg_items(a)
        print(a["src"], "items:", None if its is None else len(its))
    st = load_state()
    for key, what in CFG.get("ai_watch", {}).items():
        its = watch_items(key, what, st)
        print("watch", key, len(its), [i["title"][:70] + " | " + i["link"][:70] for i in its])
    if TOKEN:
        DRY = False
        me = tg("getMe")
        print("bot:", (me.get("result") or {}).get("username"))
        for k, ch in channels().items():
            r = tg("getChatMember", {"chat_id": ch["chat_id"], "user_id": (me.get("result") or {}).get("id")})
            print("admin in", k, (r.get("result") or {}).get("status"))


# ---------------- 24x7 loop scheduler ----------------
# Daily jobs in IST. The loop calls "tick" every ~10 min; each job runs once per day inside its window.
SCHEDULE = [("ca", "07:30"), ("capost", "08:30"), ("cal", "09:30"), ("gk", "12:30"), ("roundup", "17:30"),
            ("capost", "18:30"), ("quiz", "19:55"), ("tip", "21:30")]
WINDOW_MIN = 150   # a job that is more than 2.5 h late is skipped for that day instead of posting very late


def due_jobs(st):
    t = now_ist()
    today = t.date().isoformat()
    first = "sched" not in st
    sch = st.setdefault("sched", {})
    due = []
    for mode, hm in SCHEDULE:
        h, m = map(int, hm.split(":"))
        at = t.replace(hour=h, minute=m, second=0, microsecond=0)
        slot = f"{mode}@{hm}"
        if at <= t and sch.get(slot) != today:
            sch[slot] = today            # marked before running: a crash means a skip, never a double post
            if not first and (t - at).total_seconds() <= WINDOW_MIN * 60:
                due.append(mode)
    return due


def run_tick():
    st = load_state()
    due = due_jobs(st)
    save_state(st)
    for mode in due:
        print("daily job:", mode)
        try:
            MODES[mode]()
        except Exception as e:
            print("daily job failed", mode, repr(e)[:200])
    try:
        run_alerts()
    except Exception as e:
        print("alerts failed", repr(e)[:200])
    try:
        run_replies()
    except Exception as e:
        print("replies failed", repr(e)[:200])


MODES = {"alerts": lambda: (run_alerts(), run_replies()), "watch": lambda: run_alerts(force_watch=True),
         "replies": run_replies, "quiz": lambda: run_quiz("quiz"), "ca": lambda: run_quiz("ca"), "gk": run_gk,
         "capost": run_capost, "cal": run_cal, "roundup": run_roundup, "tip": run_tip, "selftest": run_selftest,
         "tick": run_tick}


if __name__ == "__main__":
    import urllib3
    urllib3.disable_warnings()
    mode = sys.argv[1] if len(sys.argv) > 1 else "alerts"
    if not TOKEN and mode != "selftest":
        sys.exit("TELEGRAM_BOT_TOKEN secret missing")
    MODES[mode]()
