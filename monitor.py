import os
import re
import sys
import json
import requests
import datetime
import traceback
from urllib.parse import urljoin
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


def send_info_to_slack(webhook_url, header, body):
    if not webhook_url:
        return
    safe_body = body.replace("```", "'''")
    if len(safe_body) > 2500:
        safe_body = safe_body[:2500] + "...(truncated)"
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": header[:150]}},
        {"type": "section", "text": {"type": "mrkdwn", "text": safe_body}},
    ]
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


def _ld_type_matches(node, expected_type):
    if not expected_type or not isinstance(node, dict):
        return False
    raw = node.get("@type")
    if raw is None:
        return False
    types = raw if isinstance(raw, list) else [raw]
    short = expected_type.split("/")[-1]
    for typ in types:
        if not isinstance(typ, str):
            continue
        if typ == expected_type or typ == short or typ.endswith("/" + short):
            return True
    return False


def _iter_ld_nodes(data):
    if isinstance(data, list):
        for entry in data:
            yield from _iter_ld_nodes(entry)
    elif isinstance(data, dict):
        graph = data.get("@graph")
        if graph is not None:
            yield from _iter_ld_nodes(graph)
        yield data


def _collect_ld_types(data, found):
    for node in _iter_ld_nodes(data):
        raw = node.get("@type")
        if raw is None:
            continue
        for typ in (raw if isinstance(raw, list) else [raw]):
            if isinstance(typ, str) and typ not in found:
                found.append(typ)


def _normalize_url(url, base_url):
    if isinstance(url, dict):
        url = url.get("url") or url.get("@id")
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    if url.startswith("/"):
        return urljoin(base_url, url)
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return urljoin(base_url, url)


def _item_id_from_url(url, config):
    if not url:
        return None
    id_regex = config.get("id_regex")
    if id_regex:
        match = re.search(id_regex, url)
        if match:
            return match.group(1)
    tail = url.rstrip("/").split("/")[-1]
    return tail if tail.isdigit() else tail or None


def _listing_record(item_id, title, url, description=""):
    return {
        "id": str(item_id),
        "title": (title or "").strip() or f"item-{item_id}",
        "url": url,
        "description": (description or "")[:150],
    }


def _extract_ld_items(data, config, base_url, diagnostics):
    root_type = config.get("root_type", "")
    entity_key = config.get("entity", "")
    list_key = config.get("list")
    item_key = config.get("item", "item")
    title_key = config.get("title", "name")
    url_key = config.get("url", "url")
    desc_key = config.get("desc", "description")
    items = []

    for node in _iter_ld_nodes(data):
        if not _ld_type_matches(node, root_type):
            continue
        if entity_key not in node:
            continue
        diagnostics["itemlist_matched"] = True
        entity_val = node[entity_key]
        if isinstance(entity_val, list):
            elements = entity_val
        elif isinstance(entity_val, dict) and list_key:
            inner = entity_val.get(list_key, [])
            elements = inner if isinstance(inner, list) else []
        else:
            elements = []

        for el in elements:
            if not isinstance(el, dict):
                continue
            nested = el.get(item_key) if item_key else None
            item_obj = nested if isinstance(nested, dict) else el
            title = item_obj.get(title_key)
            url = _normalize_url(item_obj.get(url_key), base_url)
            description = item_obj.get(desc_key, "")
            if not url:
                continue
            item_id = _item_id_from_url(url, config)
            if not item_id:
                continue
            items.append(_listing_record(item_id, title, url, description))
    return items


def _extract_dom_items(page, config, base_url, diagnostics):
    dom = config.get("dom") or {}
    link_selector = dom.get("link_selector")
    if not link_selector:
        return []

    id_regex = dom.get("id_regex", "")
    title_min = int(dom.get("title_min_length", 1))
    desc_max = int(dom.get("desc_max_length", 150))
    items = []
    seen_hrefs = set()

    try:
        links = page.locator(link_selector).all()
    except Exception as e:
        diagnostics["errors"].append(f"DOM link_selector failed: {e}")
        return []

    diagnostics["dom_link_count"] += len(links)

    for link in links:
        try:
            href = link.get_attribute("href") or ""
        except Exception:
            continue
        if not href or href in seen_hrefs:
            continue
        seen_hrefs.add(href)
        url = _normalize_url(href, base_url)
        if not url:
            continue
        item_id = _item_id_from_url(url, dom if dom.get("id_regex") else config)
        if not item_id:
            continue
        try:
            title = (link.inner_text() or "").strip()
        except Exception:
            title = ""
        if len(title) < title_min:
            title = f"item-{item_id}"
        items.append(_listing_record(item_id, title, url, title[:desc_max]))
    return items


def _dedupe_items(items):
    by_id = {}
    for item in items:
        by_id[item["id"]] = item
    return list(by_id.values())


def _empty_phase1_diagnostics(seen_ids):
    return {
        "pages_attempted": 0,
        "ld_json_script_count": 0,
        "ld_json_types": [],
        "itemlist_matched": False,
        "ld_items_count": 0,
        "dom_link_count": 0,
        "dom_items_count": 0,
        "scraped_total": 0,
        "new_count": 0,
        "seen_ids_count": len(seen_ids),
        "errors": [],
        "sources": [],
    }


def format_phase1_scrape_failure(diag):
    types = ", ".join(diag["ld_json_types"]) if diag["ld_json_types"] else "none"
    lines = [
        "Phase1: listing scrape returned 0 items",
        "",
        f"Pages tried: {diag['pages_attempted']}",
        f"LD+JSON scripts: {diag['ld_json_script_count']} (types: {types})",
        f"Target list type matched: {'yes' if diag['itemlist_matched'] else 'no'}",
        f"LD items extracted: {diag['ld_items_count']}",
        f"DOM links seen: {diag['dom_link_count']}",
        f"DOM items extracted: {diag['dom_items_count']}",
        f"Parse sources used: {', '.join(diag['sources']) or 'none'}",
        f"Seen IDs loaded: {diag['seen_ids_count']}",
    ]
    if diag["errors"]:
        lines.append("")
        lines.append("Errors:")
        for err in diag["errors"][:12]:
            lines.append(f"- {err}")
    lines.extend([
        "",
        "Likely cause: page structure changed, parse config mismatch, or load timeout.",
        "Check PARSE_CONFIG (LD+JSON keys and optional dom block).",
    ])
    return "\n".join(lines)


def format_phase1_no_new(diag):
    return "\n".join([
        "Phase1: scrape OK, no new items",
        "",
        f"Scraped: {diag['scraped_total']} items (all already in seen_ids)",
        f"Seen IDs: {diag['seen_ids_count']}",
        f"Sources: {', '.join(diag['sources']) or 'n/a'}",
    ])


def format_phase1_seed(diag, seeded_count):
    return "\n".join([
        "Phase1: initial seed completed",
        "",
        f"Stored {seeded_count} IDs in seen_ids (first run).",
        f"Scraped: {diag['scraped_total']} items",
        f"Sources: {', '.join(diag['sources']) or 'n/a'}",
    ])


def scrape_listing(page, config, seen_ids, max_pages, target_url):
    diagnostics = _empty_phase1_diagnostics(seen_ids)
    scraped_all = []
    dom_cfg = config.get("dom") or {}
    use_dom = bool(dom_cfg.get("link_selector"))
    wait_ms = int(config.get("wait_ms", 5000))
    wait_selector = config.get("wait_selector") or dom_cfg.get("wait_selector")
    goto_wait = config.get("goto_wait_until", "domcontentloaded")

    for page_num in range(1, max_pages + 1):
        page_url = (
            f"{target_url}&page={page_num}" if "?" in target_url
            else f"{target_url}?page={page_num}"
        )
        diagnostics["pages_attempted"] += 1
        page_ld_items = []
        page_dom_items = []

        try:
            page.goto(page_url, wait_until=goto_wait, timeout=30000)
            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, timeout=15000)
                except Exception as e:
                    diagnostics["errors"].append(
                        f"page {page_num}: wait_selector timeout: {e}"
                    )
            page.wait_for_timeout(wait_ms)
            base_url = page.url

            script_texts = page.locator('script[type="application/ld+json"]').all_inner_texts()
            diagnostics["ld_json_script_count"] += len(script_texts)

            for script_text in script_texts:
                try:
                    data = json.loads(script_text)
                except json.JSONDecodeError as e:
                    diagnostics["errors"].append(f"page {page_num}: LD+JSON parse: {e}")
                    continue
                _collect_ld_types(data, diagnostics["ld_json_types"])
                page_ld_items.extend(_extract_ld_items(data, config, base_url, diagnostics))

            if use_dom:
                page_dom_items = _extract_dom_items(page, config, base_url, diagnostics)

            page_items = _dedupe_items(page_ld_items + page_dom_items)
            if page_ld_items and "ld_json" not in diagnostics["sources"]:
                diagnostics["sources"].append("ld_json")
            if page_dom_items and "dom" not in diagnostics["sources"]:
                diagnostics["sources"].append("dom")

            diagnostics["ld_items_count"] += len(page_ld_items)
            diagnostics["dom_items_count"] += len(page_dom_items)

            if not page_items:
                diagnostics["errors"].append(
                    f"page {page_num}: 0 items (ld={len(page_ld_items)}, dom={len(page_dom_items)})"
                )
            scraped_all.extend(page_items)

        except Exception as e:
            diagnostics["errors"].append(f"page {page_num}: {type(e).__name__}: {e}")

    scraped_all = _dedupe_items(scraped_all)
    new_items = [x for x in scraped_all if x["id"] not in seen_ids]
    diagnostics["scraped_total"] = len(scraped_all)
    diagnostics["new_count"] = len(new_items)
    return new_items, diagnostics


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
                    matched = False
                    for node in _iter_ld_nodes(ld):
                        if _ld_type_matches(node, ld_type):
                            ld = node
                            matched = True
                            break
                    if not matched:
                        continue
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
        NOTIFY_NO_NEW_HEADER = os.environ.get("NOTIFY_NO_NEW_HEADER", "No new items")
        NOTIFY_SEED_HEADER = os.environ.get("NOTIFY_SEED_HEADER", "Initial seed done")
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
        phase1_diag = _empty_phase1_diagnostics(seen_ids)

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
                new_items, phase1_diag = scrape_listing(
                    pg, parse_cfg, seen_ids, MAX_PAGES, TARGET_URL,
                )
            finally:
                browser.close()

        if phase1_diag["scraped_total"] == 0:
            send_error_to_slack(
                SLACK_WEBHOOK_URL,
                format_phase1_scrape_failure(phase1_diag),
                current_phase="phase1",
            )
            secure_exit()

        if not new_items:
            send_info_to_slack(
                SLACK_WEBHOOK_URL,
                NOTIFY_NO_NEW_HEADER,
                format_phase1_no_new(phase1_diag),
            )
            sys.exit(0)

        if len(seen_ids) == 0:
            for item in new_items:
                seen_ids.add(item["id"])
            save_seen_ids(seen_ids)
            send_info_to_slack(
                SLACK_WEBHOOK_URL,
                NOTIFY_SEED_HEADER,
                format_phase1_seed(phase1_diag, len(new_items)),
            )
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
