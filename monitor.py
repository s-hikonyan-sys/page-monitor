import os
import sys
import json
import requests
import datetime
from playwright.sync_api import sync_playwright
import google.generativeai as genai

STATE_FILE = "seen_ids.txt"
API_STATE_FILE = "api_state.json"

def secure_exit():
    sys.exit(1)

def load_seen_ids():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return set(line.strip() for line in f if line.strip())
    return set()

def save_seen_ids(seen_ids):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        for item_id in list(seen_ids)[-3000:]: # 複数ページ対応で保持数を増やす
            f.write(f"{item_id}\n")

def load_api_state():
    if os.path.exists(API_STATE_FILE):
        with open(API_STATE_FILE, "r") as f:
            return json.load(f)
    return {"current_index": 0, "usage_count": 0, "last_reset_date": ""}

def save_api_state(state):
    with open(API_STATE_FILE, "w") as f:
        json.dump(state, f)

def get_current_api_key(keys_str, max_usage, reset_hour_utc):
    keys = [k.strip() for k in keys_str.split(",") if k.strip()]
    if not keys:
        secure_exit()
        
    state = load_api_state()
    
    # リセット日時の判定
    now = datetime.datetime.utcnow()
    adjusted_now = now - datetime.timedelta(hours=reset_hour_utc)
    current_date_str = adjusted_now.strftime("%Y-%m-%d")
    
    if state.get("last_reset_date") != current_date_str:
        state["usage_count"] = 0
        state["last_reset_date"] = current_date_str
        
    # 上限を超えていれば次のキーへシフト
    if state["usage_count"] >= max_usage:
        state["current_index"] = (state["current_index"] + 1) % len(keys)
        state["usage_count"] = 0
        
    state["current_index"] = state["current_index"] % len(keys)
    
    return keys[state["current_index"]], state

def main():
    try:
        TARGET_URL = os.environ.get("TARGET_URL")
        KEYS_STR = os.environ.get("GEMINI_API_KEYS")
        SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")
        AI_PROMPT = os.environ.get("AI_PROMPT")
        PARSE_CONFIG = os.environ.get("PARSE_CONFIG")
        
        MAX_PAGES = int(os.environ.get("MAX_PAGES", "1"))
        MAX_API_USAGE = int(os.environ.get("MAX_API_USAGE", "1000"))
        RESET_HOUR_UTC = int(os.environ.get("RESET_HOUR_UTC", "0"))

        if not all([TARGET_URL, KEYS_STR, SLACK_WEBHOOK_URL, AI_PROMPT, PARSE_CONFIG]):
            secure_exit()

        try:
            config = json.loads(PARSE_CONFIG)
        except Exception:
            secure_exit()

        seen_ids = load_seen_ids()
        new_items = []

        # 1. ページネーションスクレイピング
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = context.new_page()
            
            for page_num in range(1, MAX_PAGES + 1):
                page_url = f"{TARGET_URL}&page={page_num}" if "?" in TARGET_URL else f"{TARGET_URL}?page={page_num}"
                try:
                    page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(4000)
                    scripts = page.locator('script[type="application/ld+json"]').all_inner_texts()
                    
                    for script_text in scripts:
                        data = json.loads(script_text)
                        if data.get('@type') == config.get("root_type") and config.get("entity") in data:
                            elements = data[config.get("entity")].get(config.get("list"), [])
                            for el in elements:
                                item = el.get(config.get("item"), {})
                                title = item.get(config.get("title"))
                                url = item.get(config.get("url"))
                                description = item.get(config.get("desc"), '')

                                if title and url:
                                    item_id = url.rstrip('/').split('/')[-1]
                                    if item_id not in seen_ids:
                                        new_items.append({
                                            "id": item_id,
                                            "title": title,
                                            "url": url,
                                            "description": description[:100]
                                        })
                except Exception:
                    continue # ページ読み込み失敗時はスキップして次ページへ
            browser.close()

        if not new_items:
            sys.exit(0)

        # 2. キーローテーション & Gemini API 判定処理
        current_key, api_state = get_current_api_key(KEYS_STR, MAX_API_USAGE, RESET_HOUR_UTC)
        
        genai.configure(api_key=current_key)
        model = genai.GenerativeModel(
            model_name='gemini-1.5-flash',
            generation_config={"response_mime_type": "application/json"}
        )

        prompt_text = f"{AI_PROMPT}\n\n【対象データ】\n{json.dumps(new_items, ensure_ascii=False)}"
        response = model.generate_content(prompt_text)
        
        # API利用回数をカウントアップ
        api_state["usage_count"] += 1
        save_api_state(api_state)
        
        result_json = json.loads(response.text)
        
        # 3. ID保存とSlack通知
        for item in new_items:
            seen_ids.add(item["id"])
        save_seen_ids(seen_ids)

        if not isinstance(result_json, list) or len(result_json) == 0:
            sys.exit(0)

        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "🎯 新規の検証対象を発見しました"
                }
            }
        ]

        for job in result_json:
            score = job.get("score", 0)
            if score >= 80:
                blocks.append({
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*{job.get('title', 'タイトルなし')}*\n*URL:* {job.get('url', '#')}\n*安全度:* {score}点\n*AI推奨理由:* {job.get('reason', '記載なし')}"
                    }
                })
                blocks.append({"type": "divider"})

        if len(blocks) > 1:
            slack_payload = {"blocks": blocks}
            requests.post(SLACK_WEBHOOK_URL, json=slack_payload, timeout=10)

    except Exception:
        secure_exit()

if __name__ == "__main__":
    main()
[project]
name = "page-monitor"
version = "0.1.0"
description = "Automated page monitoring and AI screening pipeline"
readme = "README.md"
requires-python = ">=3.11"
dependencies = [
    "playwright==1.42.0",
    "google-generativeai==0.4.1",
    "requests==2.31.0"
]

[build-system]
requires = ["setuptools>=61.0"]
build-backend = "setuptools.build_meta"

# 今後ローカルでRuffやBlack等のLinter/Formatterを使う場合の設定予約スペース
[tool.ruff]
line-length = 120
