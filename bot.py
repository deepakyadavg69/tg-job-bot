#!/usr/bin/env python3
"""24x7 Telegram job-alert + quiz bot (runs on GitHub Actions, no server needed).

Modes:
  python bot.py alerts   -> check official sites, post new notices with PDF
  python bot.py quiz     -> 8 PM main quiz (6 polls with explanation) in every channel
  python bot.py ca       -> 7:30 AM current-affairs quiz (5 polls)
  python bot.py gk       -> 12:30 PM one-liner GK card
Secrets (repo Settings > Secrets > Actions): TELEGRAM_BOT_TOKEN (required), GEMINI_API_KEY (for quiz/GK).
"""
import json, os, re, sys, time, html, hashlib, datetime, urllib.parse
import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
STATE_FILE = os.path.join(ROOT, "state", "seen.json")
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI = os.environ.get("GEMINI_API_KEY", "").strip()
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
TG = f"https://api.telegram.org/bot{TOKEN}"
MAX_ALERTS_PER_RUN = 4  # anti-spam


def now_ist():
    return datetime.datetime.now(IST)


def load_state():
    try:
        return json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        return {"seen": {}, "quiz_done": []}


def save_state(st):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    # keep file small
    for k, v in st["seen"].items():
        st["seen"][k] = v[-400:]
    st["quiz_done"] = st.get("quiz_done", [])[-300:]
    json.dump(st, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def channels():
    return {k: v for k, v in CFG["channels"].items() if v.get("chat_id")}


# ---------------- Telegram ----------------
def tg(method, data=None, files=None):
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
            return j
        except Exception as e:
            print("TG exception", e)
            time.sleep(3)
    return {}


def send_text(chat, text):
    return tg("sendMessage", {"chat_id": chat, "text": text[:4000], "parse_mode": "HTML",
                              "disable_web_page_preview": "true"})


def send_pdf(chat, pdf_url, caption, filename):
    try:
        r = requests.get(pdf_url, headers=UA, timeout=60)
        if r.ok and len(r.content) < 19_000_000 and r.content[:4] == b"%PDF":
            return tg("sendDocument", {"chat_id": chat, "caption": caption[:1024], "parse_mode": "HTML"},
                      files={"document": (filename[:60] or "notice.pdf", r.content, "application/pdf")})
    except Exception as e:
        print("pdf download failed", e)
    return send_text(chat, caption + f"\n📄 PDF: {html.escape(pdf_url)}")


def send_quiz(chat, q):
    opts = [str(o)[:100] for o in q["options"]][:4]
    if len(opts) < 2:
        return
    ans = int(q.get("answer", 0))
    if not 0 <= ans < len(opts):
        return
    return tg("sendPoll", {
        "chat_id": chat, "question": str(q["question"])[:300], "options": json.dumps(opts, ensure_ascii=False),
        "type": "quiz", "correct_option_id": ans, "is_anonymous": "true",
        "explanation": str(q.get("explanation", ""))[:200]})


# ---------------- Alerts ----------------
TYPE_LABEL = {"notice-boards": "Notice", "results": "Result", "admit-card": "Admit Card", "answer-key": "Answer Key"}
TYPE_HI = {"notice-boards": "नया नोटिस जारी", "results": "रिजल्ट जारी", "admit-card": "एडमिट कार्ड / स्टेटस जारी",
           "answer-key": "आंसर की जारी"}
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
            print("ssc bad", ct, j)
            continue
        for d in j.get("data", []):
            atts = [a for a in d.get("attachments") or [] if a.get("path")]
            pdf = ("https://ssc.gov.in/api/attachment/" + atts[0]["path"].replace("\\", "/")) if atts else ""
            out.append({"id": f"ssc:{d['id']}", "type": ct, "title": re.sub(r"\s+", " ", d.get("headline", "")).strip(),
                        "pdf": urllib.parse.quote(pdf, safe=":/") if pdf else "",
                        "file": atts[0]["fileName"] if atts else "notice.pdf"})
    return out


def ssc_caption(it):
    tags = " ".join(sorted({t for k, t in EXAM_TAGS if k.lower() in it["title"].lower()})[:2])
    hi = TYPE_HI.get(it["type"], "नया अपडेट")
    hi_line = ai_hindi_line(it["title"]) or hi
    return (f"🔔 <b>SSC UPDATE | {TYPE_LABEL.get(it['type'], 'Notice')}</b>\n\n"
            f"📌 {html.escape(it['title'][:350])}\n"
            f"📝 {html.escape(hi_line)}\n"
            f"📅 {now_ist():%d %b %Y}\n"
            f"🔗 Official: ssc.gov.in\n"
            f"➡️ PDF dhyan se padhein, doubt comment me poochhein.\n\n#SSC {tags}").strip()


PDF_RE = re.compile(r'<a[^>]+href=["\']([^"\']+?\.pdf[^"\']*)["\'][^>]*>(.*?)</a>', re.I | re.S)


def page_items(p):
    try:
        r = requests.get(p["url"], headers=UA, timeout=40, verify=False)
        txt = r.text
    except Exception as e:
        print("page fetch failed", p["url"], e)
        return []
    out = []
    for href, label in PDF_RE.findall(txt):
        label = re.sub(r"<[^>]+>", " ", label)
        label = re.sub(r"\s+", " ", html.unescape(label)).strip()
        if len(label) < 8:
            continue
        url = urllib.parse.urljoin(p["url"], html.unescape(href))
        out.append({"id": f"{p['src']}:" + hashlib.md5(url.encode()).hexdigest()[:12], "title": label[:300],
                    "pdf": urllib.parse.quote(url, safe=":/?=&%"), "file": url.split("/")[-1].split("?")[0]})
    return out


def page_caption(p, it):
    ch = CFG["channels"][p["channel"]]
    hi_line = ai_hindi_line(it["title"]) or "नया आधिकारिक अपडेट जारी"
    host = urllib.parse.urlparse(p["url"]).netloc.replace("www.", "")
    return (f"🔔 <b>{html.escape(p['src'])} UPDATE</b>\n\n"
            f"📌 {html.escape(it['title'])}\n"
            f"📝 {html.escape(hi_line)}\n"
            f"📅 {now_ist():%d %b %Y}\n"
            f"🔗 Official: {host}\n"
            f"➡️ PDF dhyan se padhein, doubt comment me poochhein.\n\n{ch['tag']}")


def run_alerts():
    st = load_state()
    chs = channels()
    posted = 0
    jobs = []
    if "ssc" in chs:
        jobs.append(("ssc", "ssc", ssc_items(), ssc_caption, None))
    for p in CFG["pages"]:
        if p["channel"] in chs:
            jobs.append((p["src"], p["channel"], page_items(p), None, p))
    for key, chk, items, capf, p in jobs:
        seen = st["seen"].setdefault(key, [])
        first_run = len(seen) == 0
        new = [i for i in items if i["id"] not in seen]
        if first_run:
            # seed silently, so old notices are not spammed
            seen.extend(i["id"] for i in items)
            print("seeded", key, len(items))
            continue
        for it in reversed(new):  # oldest first
            if posted >= MAX_ALERTS_PER_RUN:
                break
            cap = capf(it) if capf else page_caption(p, it)
            chat = chs[chk]["chat_id"]
            res = send_pdf(chat, it["pdf"], cap, it["file"]) if it["pdf"] else send_text(chat, cap)
            if res and res.get("ok"):
                seen.append(it["id"])
                posted += 1
                time.sleep(3)
    save_state(st)
    print("alerts posted", posted)


# ---------------- Gemini ----------------
def gemini(prompt, search=False):
    if not GEMINI:
        return ""
    body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.7}}
    if search:
        body["tools"] = [{"google_search": {}}]
    else:
        body["generationConfig"]["responseMimeType"] = "application/json"
    for model in ("gemini-2.5-flash", "gemini-2.0-flash"):
        try:
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                              headers={"x-goog-api-key": GEMINI, "Content-Type": "application/json"},
                              json=body, timeout=120)
            j = r.json()
            parts = j["candidates"][0]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts)
        except Exception as e:
            print("gemini failed", model, str(e)[:200])
    return ""


def parse_json(txt):
    if not txt:
        return None
    m = re.search(r"(\[.*\]|\{.*\})", txt, re.S)
    try:
        return json.loads(m.group(1)) if m else None
    except Exception:
        return None


def ai_hindi_line(title):
    out = parse_json(gemini("Translate this Indian govt exam notice title into ONE short simple Hindi line "
                            "(max 18 words), no new facts. Reply JSON {\"hi\": \"...\"}.\nTitle: " + title))
    return (out or {}).get("hi", "") if isinstance(out, dict) else ""


QUIZ_RULES = ("Each item: {\"question\": \"bilingual Hindi / English, max 250 chars\", \"options\": [4 short options, "
              "each max 80 chars], \"answer\": index 0-3, \"explanation\": \"1-2 lines Hindi+English, max 180 chars\"}. "
              "Facts must be accurate and verifiable; no doubtful questions. Return only a JSON array.")


def get_questions(focus, n, kind, avoid):
    if kind == "ca":
        today = now_ist().strftime("%d %B %Y")
        prompt = (f"Using Google Search, make {n} current-affairs MCQs on important Indian/world news of the last 2 days "
                  f"(today is {today}) useful for {focus}. " + QUIZ_RULES)
        data = parse_json(gemini(prompt, search=True))
    else:
        prompt = (f"Make {n} exam-level MCQs for {focus}. Mix: 2 static GK/history/polity/geography, 1 science, "
                  f"1 reasoning, 1 maths, rest English/Hindi grammar or PYQ style. Avoid these topics: "
                  f"{', '.join(avoid[-40:])}. " + QUIZ_RULES)
        data = parse_json(gemini(prompt))
    return [q for q in (data or []) if isinstance(q, dict) and "question" in q][:n]


def run_quiz(kind):
    st = load_state()
    for key, ch in channels().items():
        n = 5 if kind == "ca" else 6
        qs = get_questions(ch["focus"], n, kind, st.get("quiz_done", []))
        if not qs:
            print("no questions for", key)
            continue
        head = "☀️ <b>Morning Current Affairs Quiz</b>" if kind == "ca" else "🎯 <b>Aaj ka Main Quiz (8 PM)</b>"
        send_text(ch["chat_id"], f"{head}\n{now_ist():%d %b %Y} | {len(qs)} sawal, jawab dekar check karein 👇")
        recap = []
        for i, q in enumerate(qs, 1):
            send_quiz(ch["chat_id"], q)
            try:
                recap.append(f"{i}. {html.escape(str(q['options'][int(q['answer'])]))} — {html.escape(str(q.get('explanation',''))[:120])}")
            except Exception:
                pass
            st.setdefault("quiz_done", []).append(str(q["question"])[:40])
            time.sleep(20 if kind == "quiz" else 5)
        if kind == "quiz" and recap:
            send_text(ch["chat_id"], "✅ <b>Answer Recap</b>\n\n" + "\n".join(recap) +
                      "\n\nKitne sahi hue? Comment me batayein 👇 " + ch["tag"])
    save_state(st)


def run_gk():
    for key, ch in channels().items():
        data = parse_json(gemini(f"Make 10 accurate one-liner GK facts (question - answer) for {ch['focus']}, "
                                 "bilingual Hindi/English, short. JSON array of {\"q\": \"...\", \"a\": \"...\"}."))
        if not data:
            continue
        lines = [f"{i}. {html.escape(str(d.get('q','')))} — <b>{html.escape(str(d.get('a','')))}</b>"
                 for i, d in enumerate(data[:10], 1)]
        send_text(ch["chat_id"], "📚 <b>Aaj ke 10 One-Liner GK</b>\n\n" + "\n".join(lines) +
                  f"\n\nSave kar lo, revision me kaam aayega. {ch['tag']}")
        time.sleep(3)


# ---------------- Comment replies (doubts in discussion groups) ----------------
def run_replies():
    """Answer students' questions in linked discussion groups (runs with every alerts run)."""
    if not GEMINI:
        return
    st = load_state()
    off = st.get("update_offset", 0)
    j = tg("getUpdates", {"offset": off, "timeout": 0, "allowed_updates": json.dumps(["message"])})
    groups = {str(v.get("group_id")): v for v in CFG["channels"].values() if v.get("group_id")}
    answered = 0
    for u in j.get("result", []) if j else []:
        st["update_offset"] = u["update_id"] + 1
        m = u.get("message") or {}
        chat = str(m.get("chat", {}).get("id"))
        text = (m.get("text") or "").strip()
        frm = m.get("from", {})
        if chat not in groups or not text or frm.get("is_bot") or m.get("is_automatic_forward"):
            continue
        if m.get("sender_chat"):  # posted as channel/anonymous admin
            continue
        is_q = "?" in text or re.search(r"\b(kab|kya|kaise|kitna|kaun|kyu|kyon|when|what|how|which|date|admit|result|cutoff|syllabus)\b", text, re.I)
        if not is_q or len(text) < 6 or answered >= 8:
            continue
        ctx = (m.get("reply_to_message") or {}).get("text") or (m.get("reply_to_message") or {}).get("caption") or ""
        ans = gemini("You are a helpful, honest assistant for an Indian govt-job Telegram group about "
                     f"{groups[chat]['focus']}. Answer the student's question in short simple Hinglish (max 60 words). "
                     "Use Google Search for dates/facts. If not sure or no official info, say so and suggest checking the "
                     "official website. Never invent dates, cutoffs or rumours. No links except official sites.\n"
                     f"Post context: {ctx[:800]}\nQuestion: {text[:500]}", search=True)
        ans = re.sub(r"[*#`]", "", ans or "").strip()
        if ans:
            tg("sendMessage", {"chat_id": chat, "text": ans[:900], "reply_to_message_id": m.get("message_id"),
                               "disable_web_page_preview": "true"})
            answered += 1
            time.sleep(2)
    save_state(st)
    print("replies", answered)


if __name__ == "__main__":
    if not TOKEN:
        sys.exit("TELEGRAM_BOT_TOKEN secret missing")
    import urllib3
    urllib3.disable_warnings()
    mode = sys.argv[1] if len(sys.argv) > 1 else "alerts"
    {"alerts": lambda: (run_alerts(), run_replies()), "replies": run_replies, "quiz": lambda: run_quiz("quiz"), "ca": lambda: run_quiz("ca"), "gk": run_gk}[mode]()
