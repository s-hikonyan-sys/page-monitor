import os
import sys
import json
import requests
import datetime
import traceback
from playwright.sync_api import sync_playwright
from google import genai
from google.genai import types

STATE_FILE = "seen_ids.txt"
API_STATE_FILE = "api_state.json"


def _get_nested(obj, path):
    for key in path.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def send_error_to_slack(webhook_url, error_message, current_phase=None, partial_result=None):
    if not webhook_url:
        return
    safe_error = error_message.replace("```", "'''")
    if len(safe_error) > 2500:
        safe_error = "...\n" + safe_error[-2500:]
    phase_label = f"[{current_phase}] " if current_phase else ""
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "Error"}},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"{phase_label}```\n{safe_error}\n```"},
        },
    ]
    if partial_result:
        safe_partial = json.dumps(partial_result, ensure_ascii=False)
        if len(safe_partial) > 1800:
            safe_partial = safe_partial[:1800] + "...(truncated)"
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"Partial result:\n```\n{safe_partial}\n```"},
        })
    try:
        requests.post(webhook_url, json={"blocks": blocks}, timeout=10)
    except Exception:
        pass


def secure_exit():
    sys.exit(1)


def load_seen_ids():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return set(line.strip() for line in f if line.strip())
    return set()


def save_seen_ids(seen_ids):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        for item_id in list(seen_ids)[-3000:]:
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
        raise ValueError("API keys not configured.")
    state = load_api_state()
    now = datetime.datetime.utcnow()
    adjusted_now = now - datetime.timedelta(hours=reset_hour_utc)
    current_date_str = adjusted_now.strftime("%Y-%m-%d")
    if state.get("last_reset_date") != current_date_str:
        state["usage_count"] = 0
        state["last_reset_date"] = current_date_str
    if state["usage_count"] >= max_usage:
        state["current_index"] = (state["current_index"] + 1) % len(keys)
        state["usage_count"] = 0
    state["current_index"] = state["current_index"] % len(keys)
    return keys[state["current_index"]], state


def call_gemini(keys_str, max_usage, reset_hour_utc, prompt_text, model_name):
    current_key, api_state = get_current_api_key(keys_str, max_usage, reset_hour_utc)
    client = genai.Client(api_key=current_key)
    response = client.models.generate_content(
        model=model_name,
        contents=prompt_text,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    api_state["usage_count"] += 1
    save_api_state(api_state)
    return response.text


def validate_json_list(text, required_fields, phase_tag, pass_field=None):
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"[{phase_tag}] JSON parse error: {e}\n{text[:500]}")
    if not isinstance(data, list):
        raise ValueError(f"[{phase_tag}] Response is not a JSON array: {type(data).__name__}")
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"[{phase_tag}] Item [{i}] is not an object")
        for field in required_fields:
            if field not in item:
                raise ValueError(f"[{phase_tag}] Item [{i}] missing required field: '{field}'")
        if pass_field and pass_field in item and not isinstance(item[pass_field], bool):
            raise ValueError(
                f"[{phase_tag}] Item [{i}] field '{pass_field}' must be boolean true/false, "
                f"got {type(item[pass_field]).__name__}: {item[pass_field]!r}"
            )
    return data


def filter_passed(items, pass_field):
    return [x for x in items if x.get(pass_field) is True]


def merge_items_by_id(*item_lists):
    merged = {}
    for lst in item_lists:
        for item in lst:
            item_id = item.get("id")
            if not item_id:
                continue
            if item_id not in merged:
                merged[item_id] = dict(item)
            else:
                merged[item_id].update(item)
    return list(merged.values())


def build_phase_prompt(base_prompt, phase_prompt, data):
    return f"{base_prompt}\n\n{phase_prompt}\n\n{json.dumps(data, ensure_ascii=False)}"


def scrape_listing(page, config, seen_ids, max_pages, target_url):
    new_items = []
    for page_num in range(1, max_pages + 1):
        page_url = (
            f"{target_url}&page={page_num}" if "?" in target_url
            else f"{target_url}?page={page_num}"
        )
        try:
            page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(4000)
            for script_text in page.locator('script[type="application/ld+json"]').all_inner_texts():
                data = json.loads(script_text)
                if data.get("@type") == config.get("root_type") and config.get("entity") in data:
                    entity_val = data[config.get("entity")]
                    list_key = config.get("list")
                    if isinstance(entity_val, list):
                        elements = entity_val
                    elif isinstance(entity_val, dict) and list_key:
                        inner = entity_val.get(list_key, [])
                        elements = inner if isinstance(inner, list) else []
                    else:
                        elements = []
                    for el in elements:
                        item = el.get(config.get("item"), {})
                        title = item.get(config.get("title"))
                        url = item.get(config.get("url"))
                        description = item.get(config.get("desc"), "")
                        if title and url:
                            item_id = url.rstrip("/").split("/")[-1]
                            if item_id not in seen_ids:
                                new_items.append({
                                    "id": item_id,
                                    "title": title,
                                    "url": url,
                                    "description": description[:150],
                                })
        except Exception:
            continue
    return new_items


def scrape_detail(page, items, detail_cfg):
    ld_type = detail_cfg.get("ld_type", "")
    desc_path = detail_cfg.get("description_path", "description")
    rv_path = detail_cfg.get("rating_value_path")
    rc_path = detail_cfg.get("review_count_path")
    price_path = detail_cfg.get("price_path")
    rating_label = detail_cfg.get("rating_label", "meta1")
    rating_fmt = detail_cfg.get("rating_format", "{v}/{c}")
    results = []
    for item in items:
        detail = dict(item)
        try:
            page.goto(item["url"], wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)
            for script_text in page.locator('script[type="application/ld+json"]').all_inner_texts():
                try:
                    ld = json.loads(script_text)
                    if ld.get("@type") == ld_type:
                        if desc_path:
                            detail["description"] = (
                                _get_nested(ld, desc_path) or item.get("description", "")
                            )[:600]
                        rv = _get_nested(ld, rv_path) if rv_path else None
                        rc = _get_nested(ld, rc_path) if rc_path else None
                        price = _get_nested(ld, price_path) if price_path else None
                        detail[rating_label] = (
                            rating_fmt.replace("{v}", str(rv)).replace("{c}", str(rc))
                            if rv and rc else "N/A"
                        )
                        if price:
                            detail["price"] = str(price)
                        break
                except Exception:
                    continue
        except Exception:
            detail[rating_label] = "N/A"
        results.append(detail)
    return results


def main():
    SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")
    partial_result = None
    current_phase = "init"

    try:
        TARGET_URL = os.environ.get("TARGET_URL")
        KEYS_STR = os.environ.get("GEMINI_API_KEYS")
        AI_PROMPT = os.environ.get("AI_PROMPT")
        PARSE_CONFIG = os.environ.get("PARSE_CONFIG")
        DETAIL_CONFIG = os.environ.get("DETAIL_CONFIG")
        PROMPT_PHASE2 = os.environ.get("PROMPT_PHASE2")
        PROMPT_PHASE4 = os.environ.get("PROMPT_PHASE4")
        PROMPT_PHASE5 = os.environ.get("PROMPT_PHASE5")
        PHASE2_FIELDS = os.environ.get("PHASE2_FIELDS", "")
        PHASE4_FIELDS = os.environ.get("PHASE4_FIELDS", "")
        PHASE5_FIELDS = os.environ.get("PHASE5_FIELDS", "")
        PHASE2_PASS_FIELD = os.environ.get("PHASE2_PASS_FIELD", "pass")
        PHASE4_PASS_FIELD = os.environ.get("PHASE4_PASS_FIELD", "pass")
        NOTIFY_HEADER = os.environ.get("NOTIFY_HEADER", "New results")
        NOTIFY_FIELDS = os.environ.get("NOTIFY_FIELDS", "[]")
        GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
        PASS_THRESHOLD = int(os.environ.get("PASS_THRESHOLD", "90"))
        MAX_PAGES = int(os.environ.get("MAX_PAGES", "1"))
        MAX_API_USAGE = int(os.environ.get("MAX_API_USAGE", "1000"))
        RESET_HOUR_UTC = int(os.environ.get("RESET_HOUR_UTC", "0"))

        if not all([TARGET_URL, KEYS_STR, SLACK_WEBHOOK_URL, AI_PROMPT, PARSE_CONFIG]):
            raise ValueError("Required environment variables are not configured.")

        parse_cfg = json.loads(PARSE_CONFIG)
        notify_fields = json.loads(NOTIFY_FIELDS)
        seen_ids = load_seen_ids()
        new_items = []
        detailed_items = []

        current_phase = "phase1"
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            )
            pg = context.new_page()
            try:
                new_items = scrape_listing(pg, parse_cfg, seen_ids, MAX_PAGES, TARGET_URL)
            finally:
                browser.close()

        if not new_items:
            sys.exit(0)

        if len(seen_ids) == 0:
            for item in new_items:
                seen_ids.add(item["id"])
            save_seen_ids(seen_ids)
            sys.exit(0)

        if PROMPT_PHASE2:
            current_phase = "phase2"
            phase2_fields = [f.strip() for f in PHASE2_FIELDS.split(",") if f.strip()]
            raw = call_gemini(
                KEYS_STR, MAX_API_USAGE, RESET_HOUR_UTC,
                build_phase_prompt(AI_PROMPT, PROMPT_PHASE2, new_items),
                GEMINI_MODEL,
            )
            result2 = validate_json_list(
                raw, phase2_fields, current_phase, pass_field=PHASE2_PASS_FIELD,
            )
            partial_result = result2
            passed2 = filter_passed(result2, PHASE2_PASS_FIELD)
            if not passed2:
                for item in new_items:
                    seen_ids.add(item["id"])
                save_seen_ids(seen_ids)
                sys.exit(0)
        else:
            passed2 = new_items

        if DETAIL_CONFIG and PROMPT_PHASE4:
            current_phase = "phase3"
            detail_cfg = json.loads(DETAIL_CONFIG)
            passed2_ids = {x["id"] for x in passed2}
            items_for_detail = [x for x in new_items if x["id"] in passed2_ids]
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    )
                )
                pg = context.new_page()
                try:
                    detailed_items = scrape_detail(pg, items_for_detail, detail_cfg)
                finally:
                    browser.close()

            current_phase = "phase4"
            phase4_fields = [f.strip() for f in PHASE4_FIELDS.split(",") if f.strip()]
            raw = call_gemini(
                KEYS_STR, MAX_API_USAGE, RESET_HOUR_UTC,
                build_phase_prompt(AI_PROMPT, PROMPT_PHASE4, detailed_items),
                GEMINI_MODEL,
            )
            result4 = validate_json_list(
                raw, phase4_fields, current_phase, pass_field=PHASE4_PASS_FIELD,
            )
            partial_result = result4
            result4_merged = merge_items_by_id(detailed_items, result4)
            passed4 = filter_passed(result4_merged, PHASE4_PASS_FIELD)
            if not passed4:
                for item in new_items:
                    seen_ids.add(item["id"])
                save_seen_ids(seen_ids)
                sys.exit(0)
        else:
            passed4 = passed2

        if PROMPT_PHASE5:
            current_phase = "phase5"
            phase5_fields = [f.strip() for f in PHASE5_FIELDS.split(",") if f.strip()]
            phase5_input = merge_items_by_id(detailed_items, passed4) if detailed_items else passed4
            raw = call_gemini(
                KEYS_STR, MAX_API_USAGE, RESET_HOUR_UTC,
                build_phase_prompt(AI_PROMPT, PROMPT_PHASE5, phase5_input),
                GEMINI_MODEL,
            )
            result5 = validate_json_list(raw, phase5_fields, current_phase)
            result5 = merge_items_by_id(phase5_input, result5)
            partial_result = result5
        else:
            result5 = merge_items_by_id(detailed_items, passed4) if detailed_items else passed4

        for item in new_items:
            seen_ids.add(item["id"])
        save_seen_ids(seen_ids)

        if not result5:
            sys.exit(0)

        blocks = [
            {"type": "header", "text": {"type": "plain_text", "text": NOTIFY_HEADER}},
        ]
        for entry in result5:
            score = entry.get("score", 0)
            if isinstance(score, (int, float)) and score < PASS_THRESHOLD:
                continue
            lines = []
            for nf in notify_fields:
                label = nf.get("label", nf.get("key", ""))
                key = nf.get("key", "")
                value = entry.get(key, "")
                if value:
                    text = str(value)
                    if len(text) > 500:
                        text = text[:500] + "...(truncated)"
                    lines.append(f"*{label}:* {text}")
            text_body = "\n".join(lines)
            if text_body:
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text_body}})
                blocks.append({"type": "divider"})

        if len(blocks) > 1:
            requests.post(SLACK_WEBHOOK_URL, json={"blocks": blocks}, timeout=10)

    except Exception:
        error_msg = traceback.format_exc()
        send_error_to_slack(SLACK_WEBHOOK_URL, error_msg, current_phase, partial_result)
        secure_exit()


if __name__ == "__main__":
    main()
