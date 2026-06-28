#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
update_news.py
===============
اسکریپت خودکار به‌روزرسانی آرشیو اخبار تکنولوژی و هوش مصنوعی فارسی.

این اسکریپت:
1. فید RSS سایت‌های OpenAI News، TechCrunch (بخش AI) و MIT Technology Review را می‌خواند.
2. خبرهایی که هنوز در دیتاست (news-dataset.json) ثبت نشده‌اند را پیدا می‌کند.
3. هر خبر تازه را با Gemini API (رایگان) خلاصه و به فارسی روان ترجمه می‌کند.
4. رکوردهای جدید را به news-dataset.json اضافه می‌کند (بدون حذف داده‌های قبلی).

اجرا:
    python3 update_news.py

نیازمندی‌ها:
    pip install requests feedparser

متغیر محیطی لازم:
    GEMINI_API_KEY  -> کلید رایگان از https://aistudio.google.com/app/apikey
"""

import os
import re
import json
import time
import hashlib
import datetime as dt
from pathlib import Path

import feedparser
import requests

# ----------------------------------------------------------------------------
# تنظیمات
# ----------------------------------------------------------------------------
DATASET_PATH = Path(__file__).parent / "news-dataset.json"
HTML_PATH = Path(__file__).parent / "index.html"

FEEDS = {
    "OpenAI News": "https://openai.com/news/rss.xml",
    "TechCrunch": "https://techcrunch.com/category/artificial-intelligence/feed/",
    "MIT Technology Review": "https://www.technologyreview.com/feed/",
}

# هر فید حداکثر چند آیتم تازه را در یک اجرا پردازش کند (برای کنترل مصرف API رایگان)
MAX_ITEMS_PER_FEED = 6

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-2.5-flash"  # نسخه‌ی فعال و رایگان (2.0-flash از ۱ ژوئن ۲۰۲۶ منسوخ شده)
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
)


# ----------------------------------------------------------------------------
# کمک‌تابع‌ها
# ----------------------------------------------------------------------------
def load_dataset() -> dict:
    if DATASET_PATH.exists():
        with open(DATASET_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "meta": {
            "name": "آرشیو اخبار تکنولوژی و هوش مصنوعی",
            "description": (
                "دیتاست خبرهای روزانه‌ی تکنولوژی و هوش مصنوعی، استخراج‌شده و "
                "ترجمه‌شده به فارسی، برای استفاده در تحلیل روند آینده‌ی فناوری"
            ),
            "created_at": dt.date.today().isoformat(),
            "last_updated": dt.date.today().isoformat(),
            "schema_version": 1,
            "sources": list(FEEDS.keys()),
            "total_articles": 0,
        },
        "articles": [],
    }


def save_dataset(data: dict) -> None:
    data["meta"]["last_updated"] = dt.date.today().isoformat()
    data["meta"]["total_articles"] = len(data["articles"])
    with open(DATASET_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def make_id(source_url: str) -> str:
    """شناسه‌ی یکتا و پایدار بر اساس لینک خبر، برای تشخیص تکراری‌ها."""
    return hashlib.sha1(source_url.encode("utf-8")).hexdigest()[:16]


def clean_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    return re.sub(r"\s+", " ", text).strip()


def parse_date(entry) -> str:
    for key in ("published_parsed", "updated_parsed"):
        val = entry.get(key)
        if val:
            return dt.date(val.tm_year, val.tm_mon, val.tm_mday).isoformat()
    return dt.date.today().isoformat()


# ----------------------------------------------------------------------------
# گام ۱: خواندن فیدها و پیدا کردن خبرهای تازه
# ----------------------------------------------------------------------------
def fetch_candidates(existing_ids: set) -> list:
    candidates = []
    for source_name, feed_url in FEEDS.items():
        try:
            parsed = feedparser.parse(feed_url)
        except Exception as e:
            print(f"[هشدار] خطا در خواندن فید {source_name}: {e}")
            continue

        count = 0
        for entry in parsed.entries:
            if count >= MAX_ITEMS_PER_FEED:
                break
            link = entry.get("link", "")
            if not link:
                continue
            article_id = make_id(link)
            if article_id in existing_ids:
                continue  # قبلاً ثبت شده

            candidates.append(
                {
                    "id": article_id,
                    "source": source_name,
                    "source_url": link,
                    "date": parse_date(entry),
                    "title_en": clean_html(entry.get("title", "")),
                    "raw_summary_en": clean_html(
                        entry.get("summary", entry.get("description", ""))
                    ),
                }
            )
            count += 1

    return candidates


# ----------------------------------------------------------------------------
# گام ۲: ترجمه و خلاصه‌سازی با Gemini
# ----------------------------------------------------------------------------
TRANSLATE_PROMPT_TEMPLATE = """تو یک خبرنگار حرفه‌ای تکنولوژی هستی که برای یک آرشیو فارسی‌زبان خبر می‌نویسی.
متن زیر یک خبر انگلیسی درباره‌ی تکنولوژی یا هوش مصنوعی است. وظیفه‌ی تو:

۱. یک عنوان فارسی روان و خبری بنویس (نه ترجمه‌ی لغت‌به‌لغت، بلکه طبیعی و جذاب).
۲. یک خلاصه‌ی فارسی روان در حد ۲ تا ۴ جمله بنویس که اصل خبر را برساند.
۳. یک دسته‌بندی موضوعی فارسی کوتاه انتخاب کن (مثلاً: "سیاست و تنظیم‌گری هوش مصنوعی"، "سخت‌افزار و تراشه"،
   "سرمایه‌گذاری و استارتاپ"، "پژوهش و مدل‌های هوش مصنوعی"، "امنیت سایبری"، "حریم خصوصی"،
   "کاربرد هوش مصنوعی در علم و پزشکی"، "پذیرش سازمانی هوش مصنوعی"، "ایمنی و خطرات هوش مصنوعی"،
   "تحلیل صنعت هوش مصنوعی"، یا دسته‌ی مناسب دیگر در همین سیاق).
۴. سه تا پنج برچسب کلیدی (tags) فارسی یا انگلیسی (نام شرکت‌ها/محصولات به انگلیسی بمانند) ارائه بده.

فقط خروجی JSON خام زیر را برگردان، بدون هیچ توضیح اضافه و بدون backtick:
{{
  "title_fa": "...",
  "summary_fa": "...",
  "category": "...",
  "tags": ["...", "...", "..."]
}}

عنوان انگلیسی خبر: {title}
خلاصه‌ی انگلیسی خبر: {summary}
"""


def translate_with_gemini(title_en: str, raw_summary_en: str) -> dict:
    prompt = TRANSLATE_PROMPT_TEMPLATE.format(title=title_en, summary=raw_summary_en)
    payload = {"contents": [{"parts": [{"text": prompt}]}]}

    max_retries = 3
    backoff = 8  # ثانیه - افزایش تدریجی در صورت برخورد مجدد با 429

    for attempt in range(1, max_retries + 1):
        resp = requests.post(GEMINI_URL, json=payload, timeout=30)
        if resp.status_code == 429:
            if attempt == max_retries:
                resp.raise_for_status()
            print(f"    [محدودیت نرخ] تلاش {attempt} ناموفق، {backoff} ثانیه صبر می‌کنم...")
            time.sleep(backoff)
            backoff *= 2
            continue
        resp.raise_for_status()
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        text = text.strip()
        text = re.sub(r"^```json\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
        return json.loads(text)

    raise RuntimeError("تمام تلاش‌های ترجمه با خطای محدودیت نرخ مواجه شدند.")


# ----------------------------------------------------------------------------
# اجرای اصلی
# ----------------------------------------------------------------------------
def main():
    if not GEMINI_API_KEY:
        print("[خطا] متغیر محیطی GEMINI_API_KEY تنظیم نشده است. متوقف می‌شوم.")
        return

    data = load_dataset()
    existing_ids = {a["id"] for a in data["articles"] if "id" in a}

    candidates = fetch_candidates(existing_ids)
    print(f"{len(candidates)} خبر تازه (هنوز ترجمه‌نشده) پیدا شد.")

    added = 0
    for cand in candidates:
        try:
            translated = translate_with_gemini(cand["title_en"], cand["raw_summary_en"])
        except Exception as e:
            print(f"[هشدار] ترجمه‌ی خبر «{cand['title_en']}» ناموفق بود: {e}")
            continue

        article = {
            "id": cand["id"],
            "date": cand["date"],
            "source": cand["source"],
            "source_url": cand["source_url"],
            "category": translated.get("category", "متفرقه"),
            "title_fa": translated.get("title_fa", cand["title_en"]),
            "title_en": cand["title_en"],
            "summary_fa": translated.get("summary_fa", ""),
            "tags": translated.get("tags", []),
        }
        data["articles"].append(article)
        added += 1
        print(f"  + {article['title_fa']}")
        time.sleep(6.5)  # احترام به محدودیت نرخ ۱۰ درخواست در دقیقه‌ی Gemini 2.5 Flash

    if added > 0:
        save_dataset(data)
        print(f"\n{added} خبر تازه به دیتاست اضافه شد. مجموع: {len(data['articles'])} خبر.")
    else:
        print("\nهیچ خبر تازه‌ای برای اضافه‌کردن پیدا نشد.")


if __name__ == "__main__":
    main()
