import os
import re
import sys
import json
import time
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


def finish_run(webhook_url, header, body):
    send_info_to_slack(webhook_url, header, body)
    sys.exit(0)


_SCORE_KEYS = ("screening_score", "safety_score", "score")


def _score_for_sort(item):
    for key in _SCORE_KEYS:
        value = item.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                continue
    return -1.0


def _resolve_item_url(item, url_template):
    url = item.get("url")
    if url and isinstance(url, str):
        return url
    item_id = item.get("id")
    if item_id and url_template:
        return url_template.replace("{id}", str(item_id))
    return None


def _slack_link(url, label):
    safe_url = str(url).replace(">", "%3E")
    safe_label = str(label).replace("|", "/")
    return f"<{safe_url}|{safe_label}>"


def _format_slack_field_value(key, value, entry, url_template):
    if key in ("id", "url"):
        link_url = value if key == "url" and value else _resolve_item_url(entry, url_template)
        if link_url:
            label = entry.get("id", value) if key == "url" else value
            return _slack_link(link_url, label)
    text = str(value)
    if len(text) > 500:
        text = text[:500] + "...(truncated)"
    return text


def _url_by_id_from_items(items):
    return {
        str(item["id"]): item["url"]
        for item in items
        if isinstance(item, dict) and item.get("id") and item.get("url")
    }


def _enrich_items_urls(items, url_by_id, url_template):
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("url"):
            continue
        item_id = str(item.get("id", ""))
        if item_id and item_id in url_by_id:
            item["url"] = url_by_id[item_id]
        elif item_id and url_template:
            item["url"] = url_template.replace("{id}", item_id)


def _format_ai_sample(items, sample_max, prefer_keys=None, url_template=None):
    if not isinstance(items, list) or not items:
        return []
    keys = prefer_keys or (
        "id", "pass", "screening_score", "safety_score", "score", "reason",
    )
    ranked = sorted(
        [x for x in items if isinstance(x, dict)],
        key=_score_for_sort,
        reverse=True,
    )
    lines = []
    for item in ranked[:sample_max]:
        parts = []
        for key in keys:
            if key not in item:
                continue
            value = item[key]
            if key == "id":
                url = _resolve_item_url(item, url_template)
                text = _slack_link(url, value) if url else str(value)
            else:
                text = str(value)
                if len(text) > 100:
                    text = text[:100] + "..."
            parts.append(f"{key}={text}")
        if parts:
            lines.append("  - " + ", ".join(parts))
    remaining = len(ranked) - sample_max
    if remaining > 0:
        lines.append(f"  - ... and {remaining} more")
    return lines


def format_run_summary(exit_reason, **ctx):
    lines = [f"Exit: {exit_reason}", ""]
    phase1_diag = ctx.get("phase1_diag")
    if phase1_diag:
        sources = ", ".join(phase1_diag.get("sources") or []) or "n/a"
        lines.extend([
            "Phase1:",
            f"  scraped: {phase1_diag.get('scraped_total', 0)}",
            f"  new: {phase1_diag.get('new_count', 0)}",
            f"  sources: {sources}",
            "",
        ])
    if "phase2_input" in ctx:
        lines.extend([
            "Phase2:",
            f"  input: {ctx['phase2_input']}",
            f"  passed: {ctx.get('phase2_passed', 0)}",
            f"  pass_field: {ctx.get('pass_field', 'pass')}",
            "",
        ])
    if "phase4_input" in ctx:
        lines.extend([
            "Phase4:",
            f"  input: {ctx['phase4_input']}",
            f"  passed: {ctx.get('phase4_passed', 0)}",
            f"  pass_field: {ctx.get('pass_field', 'pass')}",
            "",
        ])
    if "phase5_total" in ctx:
        lines.extend([
            "Phase5:",
            f"  results: {ctx['phase5_total']}",
            f"  notified (pass=true, score>={ctx.get('pass_threshold', '?')}): "
            f"{ctx.get('notified_count', 0)}",
            "",
        ])
    partial = ctx.get("partial_result")
    sample_max = ctx.get("sample_max", 5)
    if partial:
        url_by_id = ctx.get("url_by_id") or {}
        _enrich_items_urls(partial, url_by_id, ctx.get("url_template"))
        score_key = next(
            (k for k in _SCORE_KEYS if isinstance(partial, list) and partial
             and isinstance(partial[0], dict) and k in partial[0]),
            "score",
        )
        sample_lines = _format_ai_sample(
            partial,
            sample_max,
            ctx.get("sample_keys"),
            url_template=ctx.get("url_template"),
        )
        if sample_lines:
            lines.append(f"Top {sample_max} by {score_key} (pass not required):")
            lines.extend(sample_lines)
            lines.append("")
    if "seen_before" in ctx and "seen_after" in ctx:
        lines.append(
            f"seen_ids: {ctx['seen_before']} -> {ctx['seen_after']}"
            + (" (saved)" if ctx.get("seen_saved") else "")
        )
    if "gemini_calls" in ctx:
        lines.append(f"gemini_calls: {ctx['gemini_calls']}")
    return "\n".join(lines).strip()


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


def _chunk_list(items, batch_size):
    size = max(1, int(batch_size))
    for start in range(0, len(items), size):
        yield items[start:start + size]


def call_gemini_in_batches(
    keys_str,
    max_usage,
    reset_hour_utc,
    base_prompt,
    phase_prompt,
    items,
    model_name,
    phase_tag,
    required_fields,
    pass_field=None,
    batch_size=20,
    batch_interval_sec=60,
):
    """Split items into batches, call the model per batch, merge JSON array results."""
    if not items:
        return [], 0

    all_results = []
    api_calls = 0
    batches = list(_chunk_list(items, batch_size))
    total_batches = len(batches)

    for batch_index, batch in enumerate(batches):
        if batch_index > 0 and batch_interval_sec > 0:
            time.sleep(batch_interval_sec)

        raw = call_gemini(
            keys_str,
            max_usage,
            reset_hour_utc,
            build_phase_prompt(base_prompt, phase_prompt, batch),
            model_name,
        )
        api_calls += 1
        batch_result = validate_json_list(
            raw,
            required_fields,
            phase_tag,
            pass_field=pass_field,
            expected_count=len(batch),
        )
        input_ids = {str(x["id"]) for x in batch if x.get("id") is not None}
        output_ids = {
            str(x["id"]) for x in batch_result if x.get("id") is not None
        }
        if input_ids != output_ids:
            missing = sorted(input_ids - output_ids)[:10]
            extra = sorted(output_ids - input_ids)[:10]
            raise ValueError(
                f"[{phase_tag}] batch {batch_index + 1}/{total_batches} "
                f"id mismatch: missing={missing} extra={extra}"
            )
        all_results.extend(batch_result)

    return all_results, api_calls


def validate_json_list(
    text, required_fields, phase_tag, pass_field=None, expected_count=None,
):
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"[{phase_tag}] JSON parse error: {e}\n{text[:500]}")
    if not isinstance(data, list):
        raise ValueError(f"[{phase_tag}] Response is not a JSON array: {type(data).__name__}")
    if expected_count is not None and len(data) != expected_count:
        raise ValueError(
            f"[{phase_tag}] Expected {expected_count} items, got {len(data)}"
        )
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


_PAGE_STATE_EVAL = """
(args) => {
    const root = window[args.globalName];
    if (root == null) {
        return { ok: false, reason: "global_missing" };
    }
    let cur = root;
    for (const part of args.path.split(".")) {
        if (!part) continue;
        if (cur == null || typeof cur !== "object") {
            return { ok: false, reason: "path_break", part };
        }
        cur = cur[part];
    }
    if (!Array.isArray(cur)) {
        return { ok: false, reason: "not_array", valueType: typeof cur };
    }
    return { ok: true, rows: cur };
}
"""


def _probe_listing_page(page, config):
    state_cfg = config.get("page_state") or {}
    probe_cfg = config.get("probe") or {}
    return page.evaluate(
        """
        (args) => {
            const html = document.documentElement.outerHTML;
            const substrings = (args.substrings || []).map((s) => ({
                s,
                found: s ? html.includes(s) : false,
            }));
            let stateInfo = null;
            if (args.globalName) {
                const root = window[args.globalName];
                if (root == null) {
                    stateInfo = { exists: false };
                } else {
                    let cur = root;
                    let brokenAt = null;
                    for (const part of (args.path || "").split(".")) {
                        if (!part) continue;
                        if (cur == null || typeof cur !== "object") {
                            brokenAt = part;
                            cur = null;
                            break;
                        }
                        cur = cur[part];
                    }
                    stateInfo = {
                        exists: true,
                        pathBrokenAt: brokenAt,
                        pathIsArray: Array.isArray(cur),
                        listLength: Array.isArray(cur) ? cur.length : null,
                    };
                }
            }
            return {
                title: document.title,
                url: location.href,
                htmlLength: html.length,
                substrings,
                stateInfo,
            };
        }
        """,
        {
            "globalName": state_cfg.get("global"),
            "path": state_cfg.get("path", ""),
            "substrings": probe_cfg.get("html_substrings") or [],
        },
    )


def _extract_page_state_items(page, config, base_url, diagnostics):
    state_cfg = config.get("page_state") or {}
    global_name = state_cfg.get("global")
    data_path = state_cfg.get("path")
    if not global_name or not data_path:
        return []

    id_key = state_cfg.get("id_key", "id")
    title_key = state_cfg.get("title_key", "title")
    desc_key = state_cfg.get("desc_key", "")
    url_key = state_cfg.get("url_key", "")
    url_template = state_cfg.get("url_template", "")
    desc_max = int(state_cfg.get("desc_max_length", 150))

    try:
        result = page.evaluate(
            _PAGE_STATE_EVAL,
            {"globalName": global_name, "path": data_path},
        )
    except Exception as e:
        diagnostics["errors"].append(f"page_state evaluate failed: {e}")
        return []

    if not result.get("ok"):
        reason = result.get("reason", "unknown")
        diagnostics["page_state_errors"].append(reason)
        if reason == "path_break":
            diagnostics["errors"].append(
                f"page_state path break at '{result.get('part')}'"
            )
        elif reason == "global_missing":
            diagnostics["errors"].append("page_state global not found on window")
        elif reason == "not_array":
            diagnostics["errors"].append(
                f"page_state path is not an array (type={result.get('valueType')})"
            )
        return []

    rows = result.get("rows") or []
    diagnostics["page_state_list_length"] = max(
        diagnostics.get("page_state_list_length", 0),
        len(rows),
    )
    items = []
    id_cfg = {**config, **state_cfg}

    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_id = row.get(id_key)
        if raw_id is None:
            continue
        item_id = str(raw_id)
        title = row.get(title_key) or ""
        desc_text = row.get(desc_key) or "" if desc_key else ""
        description = _build_listing_description(title, desc_text, desc_max)
        url = None
        if url_key:
            url = _normalize_url(row.get(url_key), base_url)
        if not url and url_template:
            url = url_template.replace("{id}", item_id)
        if not url:
            continue
        if not _item_id_from_url(url, id_cfg):
            item_id = _item_id_from_url(url, id_cfg) or item_id
        items.append(_listing_record(item_id, title, url, str(description)[:desc_max]))

    return items


def _build_listing_description(title, desc, desc_max):
    title = (title or "").strip()
    desc = (desc or "").strip()
    if desc and title and desc != title:
        combined = f"{title} — {desc}"
    else:
        combined = desc or title
    return combined[:desc_max]


def _extract_dom_items(page, config, base_url, diagnostics):
    dom = config.get("dom") or {}
    link_selector = dom.get("link_selector")
    if not link_selector:
        return []

    id_regex = dom.get("id_regex", "")
    title_min = int(dom.get("title_min_length", 1))
    desc_max = int(dom.get("desc_max_length", 300))
    ancestor_selector = dom.get("ancestor_selector", "")
    desc_selector = dom.get("desc_selector", "")
    title_selector = dom.get("title_selector", "")
    items = []
    seen_hrefs = set()
    id_cfg = dom if dom.get("id_regex") else config

    if ancestor_selector or desc_selector or title_selector:
        try:
            raw_rows = page.evaluate(
                """
                (args) => {
                    const rows = [];
                    const seen = new Set();
                    for (const link of document.querySelectorAll(args.linkSelector)) {
                        const href = link.href || link.getAttribute("href") || "";
                        if (!href || seen.has(href)) continue;
                        seen.add(href);
                        const card = args.ancestorSelector
                            ? link.closest(args.ancestorSelector)
                            : link.parentElement;
                        let title = (link.innerText || "").trim();
                        if (args.titleSelector && card) {
                            const t = card.querySelector(args.titleSelector);
                            if (t) title = (t.innerText || "").trim() || title;
                        }
                        let desc = "";
                        if (args.descSelector && card) {
                            const d = card.querySelector(args.descSelector);
                            desc = d ? (d.innerText || "").trim() : "";
                        }
                        rows.push({ href, title, desc });
                    }
                    return rows;
                }
                """,
                {
                    "linkSelector": link_selector,
                    "ancestorSelector": ancestor_selector,
                    "descSelector": desc_selector,
                    "titleSelector": title_selector,
                },
            )
        except Exception as e:
            diagnostics["errors"].append(f"DOM structured extract failed: {e}")
            raw_rows = []
        diagnostics["dom_link_count"] += len(raw_rows)
        for row in raw_rows:
            href = row.get("href") or ""
            if href in seen_hrefs:
                continue
            seen_hrefs.add(href)
            url = _normalize_url(href, base_url)
            if not url:
                continue
            item_id = _item_id_from_url(url, id_cfg)
            if not item_id:
                continue
            title = (row.get("title") or "").strip()
            if len(title) < title_min:
                title = f"item-{item_id}"
            description = _build_listing_description(
                title, row.get("desc") or "", desc_max,
            )
            items.append(_listing_record(item_id, title, url, description))
        return items

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
        item_id = _item_id_from_url(url, id_cfg)
        if not item_id:
            continue
        try:
            title = (link.inner_text() or "").strip()
        except Exception:
            title = ""
        if len(title) < title_min:
            title = f"item-{item_id}"
        description = _build_listing_description(title, "", desc_max)
        items.append(_listing_record(item_id, title, url, description))
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
        "page_state_items_count": 0,
        "page_state_list_length": 0,
        "page_state_errors": [],
        "scraped_total": 0,
        "new_count": 0,
        "seen_ids_count": len(seen_ids),
        "errors": [],
        "sources": [],
        "runtime_probe": None,
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
        f"Page state list length: {diag.get('page_state_list_length', 0)}",
        f"Page state items extracted: {diag.get('page_state_items_count', 0)}",
        f"Parse sources used: {', '.join(diag['sources']) or 'none'}",
        f"Seen IDs loaded: {diag['seen_ids_count']}",
    ]
    probe = diag.get("runtime_probe")
    if probe:
        lines.append("")
        lines.append("Runtime probe (last page):")
        lines.append(f"- Title: {probe.get('title', '')[:120]}")
        lines.append(f"- URL: {probe.get('url', '')[:200]}")
        lines.append(f"- HTML length: {probe.get('htmlLength', 0)}")
        state_info = probe.get("stateInfo")
        if state_info is not None:
            if state_info.get("exists"):
                lines.append(
                    f"- Page state global: yes, path array={state_info.get('pathIsArray')}, "
                    f"listLength={state_info.get('listLength')}"
                )
                if state_info.get("pathBrokenAt"):
                    lines.append(f"- Page state path broken at: {state_info['pathBrokenAt']}")
            else:
                lines.append("- Page state global: not found on window")
        for entry in probe.get("substrings") or []:
            label = entry.get("s", "")
            found = entry.get("found")
            lines.append(f"- HTML contains {label!r}: {'yes' if found else 'no'}")
    if diag.get("page_state_errors"):
        lines.append("")
        lines.append("Page state errors: " + ", ".join(diag["page_state_errors"]))
    if diag["errors"]:
        lines.append("")
        lines.append("Errors:")
        for err in diag["errors"][:12]:
            lines.append(f"- {err}")
    lines.extend([
        "",
        "Likely cause: page structure changed, parse config mismatch, headless HTML diff, or load timeout.",
        "Check PARSE_CONFIG (page_state / dom / LD+JSON) and probe.html_substrings.",
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
    use_page_state = bool((config.get("page_state") or {}).get("global"))
    wait_ms = int(config.get("wait_ms", 5000))
    wait_selector = config.get("wait_selector") or dom_cfg.get("wait_selector")
    wait_selector_state = config.get("wait_selector_state") or dom_cfg.get(
        "wait_selector_state", "visible"
    )
    goto_wait = config.get("goto_wait_until", "domcontentloaded")

    for page_num in range(1, max_pages + 1):
        page_url = (
            f"{target_url}&page={page_num}" if "?" in target_url
            else f"{target_url}?page={page_num}"
        )
        diagnostics["pages_attempted"] += 1
        page_ld_items = []
        page_dom_items = []
        page_state_items = []

        try:
            page.goto(page_url, wait_until=goto_wait, timeout=30000)
            if wait_selector:
                try:
                    page.wait_for_selector(
                        wait_selector,
                        timeout=15000,
                        state=wait_selector_state,
                    )
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

            if use_page_state:
                page_state_items = _extract_page_state_items(
                    page, config, base_url, diagnostics,
                )

            if use_dom:
                page_dom_items = _extract_dom_items(page, config, base_url, diagnostics)

            page_items = _dedupe_items(page_ld_items + page_dom_items + page_state_items)
            if page_ld_items and "ld_json" not in diagnostics["sources"]:
                diagnostics["sources"].append("ld_json")
            if page_dom_items and "dom" not in diagnostics["sources"]:
                diagnostics["sources"].append("dom")
            if page_state_items and "page_state" not in diagnostics["sources"]:
                diagnostics["sources"].append("page_state")

            diagnostics["ld_items_count"] += len(page_ld_items)
            diagnostics["dom_items_count"] += len(page_dom_items)
            diagnostics["page_state_items_count"] += len(page_state_items)

            if not page_items:
                diagnostics["errors"].append(
                    f"page {page_num}: 0 items "
                    f"(ld={len(page_ld_items)}, dom={len(page_dom_items)}, "
                    f"state={len(page_state_items)})"
                )
                try:
                    diagnostics["runtime_probe"] = _probe_listing_page(page, config)
                except Exception as e:
                    diagnostics["errors"].append(f"page {page_num}: runtime probe failed: {e}")
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
        NOTIFY_PHASE2_REJECTED_HEADER = os.environ.get(
            "NOTIFY_PHASE2_REJECTED_HEADER", "Run complete: phase2 all rejected",
        )
        NOTIFY_PHASE4_REJECTED_HEADER = os.environ.get(
            "NOTIFY_PHASE4_REJECTED_HEADER", "Run complete: phase4 all rejected",
        )
        NOTIFY_PHASE5_EMPTY_HEADER = os.environ.get(
            "NOTIFY_PHASE5_EMPTY_HEADER", "Run complete: phase5 empty",
        )
        NOTIFY_BELOW_THRESHOLD_HEADER = os.environ.get(
            "NOTIFY_BELOW_THRESHOLD_HEADER", "Run complete: below score threshold",
        )
        NOTIFY_SUCCESS_SUMMARY_HEADER = os.environ.get("NOTIFY_SUCCESS_SUMMARY_HEADER", "")
        NOTIFY_FIELDS = os.environ.get("NOTIFY_FIELDS", "[]")
        GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
        PASS_THRESHOLD = int(os.environ.get("PASS_THRESHOLD", "90"))
        MAX_PAGES = int(os.environ.get("MAX_PAGES", "1"))
        MAX_API_USAGE = int(os.environ.get("MAX_API_USAGE", "1000"))
        RESET_HOUR_UTC = int(os.environ.get("RESET_HOUR_UTC", "0"))
        NOTIFY_SUMMARY_SAMPLE_MAX = int(os.environ.get("NOTIFY_SUMMARY_SAMPLE_MAX", "5"))
        GEMINI_BATCH_SIZE = int(os.environ.get("GEMINI_BATCH_SIZE", "20"))
        GEMINI_BATCH_INTERVAL_SEC = int(
            os.environ.get("GEMINI_BATCH_INTERVAL_SEC", "60"),
        )
        gemini_calls = 0
        seen_ids = load_seen_ids()
        seen_ids_before = len(seen_ids)

        if not all([TARGET_URL, KEYS_STR, SLACK_WEBHOOK_URL, AI_PROMPT, PARSE_CONFIG]):
            raise ValueError("Required environment variables are not configured.")

        parse_cfg = json.loads(PARSE_CONFIG)
        notify_fields = json.loads(NOTIFY_FIELDS)
        NOTIFY_URL_TEMPLATE = os.environ.get("NOTIFY_URL_TEMPLATE", "") or (
            (parse_cfg.get("page_state") or {}).get("url_template", "")
        )
        new_items = []
        url_by_id = {}
        detailed_items = []
        phase1_diag = _empty_phase1_diagnostics(seen_ids)

        current_phase = "phase1"
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                locale="ja-JP",
            )
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
            )
            pg = context.new_page()
            try:
                new_items, phase1_diag = scrape_listing(
                    pg, parse_cfg, seen_ids, MAX_PAGES, TARGET_URL,
                )
            finally:
                browser.close()

        url_by_id = _url_by_id_from_items(new_items)

        if phase1_diag["scraped_total"] == 0:
            send_error_to_slack(
                SLACK_WEBHOOK_URL,
                format_phase1_scrape_failure(phase1_diag),
                current_phase="phase1",
            )
            secure_exit()

        if not new_items:
            finish_run(
                SLACK_WEBHOOK_URL,
                NOTIFY_NO_NEW_HEADER,
                format_run_summary(
                    "phase1_no_new",
                    phase1_diag=phase1_diag,
                    seen_before=seen_ids_before,
                    seen_after=seen_ids_before,
                    gemini_calls=gemini_calls,
                    sample_max=NOTIFY_SUMMARY_SAMPLE_MAX,
                ) + "\n\n" + format_phase1_no_new(phase1_diag),
            )

        if len(seen_ids) == 0:
            for item in new_items:
                seen_ids.add(item["id"])
            save_seen_ids(seen_ids)
            finish_run(
                SLACK_WEBHOOK_URL,
                NOTIFY_SEED_HEADER,
                format_run_summary(
                    "phase1_seed",
                    phase1_diag=phase1_diag,
                    seen_before=0,
                    seen_after=len(seen_ids),
                    seen_saved=True,
                    gemini_calls=gemini_calls,
                    sample_max=NOTIFY_SUMMARY_SAMPLE_MAX,
                ) + "\n\n" + format_phase1_seed(phase1_diag, len(new_items)),
            )

        if PROMPT_PHASE2:
            current_phase = "phase2"
            phase2_fields = [f.strip() for f in PHASE2_FIELDS.split(",") if f.strip()]
            result2, phase2_calls = call_gemini_in_batches(
                KEYS_STR,
                MAX_API_USAGE,
                RESET_HOUR_UTC,
                AI_PROMPT,
                PROMPT_PHASE2,
                new_items,
                GEMINI_MODEL,
                current_phase,
                phase2_fields,
                pass_field=PHASE2_PASS_FIELD,
                batch_size=GEMINI_BATCH_SIZE,
                batch_interval_sec=GEMINI_BATCH_INTERVAL_SEC,
            )
            gemini_calls += phase2_calls
            partial_result = result2
            passed2 = filter_passed(result2, PHASE2_PASS_FIELD)
            if not passed2:
                for item in new_items:
                    seen_ids.add(item["id"])
                save_seen_ids(seen_ids)
                finish_run(
                    SLACK_WEBHOOK_URL,
                    NOTIFY_PHASE2_REJECTED_HEADER,
                    format_run_summary(
                        "phase2_all_rejected",
                        phase1_diag=phase1_diag,
                        phase2_input=len(new_items),
                        phase2_passed=0,
                        pass_field=PHASE2_PASS_FIELD,
                        partial_result=result2,
                        seen_before=seen_ids_before,
                        seen_after=len(seen_ids),
                        seen_saved=True,
                        gemini_calls=gemini_calls,
                        sample_max=NOTIFY_SUMMARY_SAMPLE_MAX,
                        url_template=NOTIFY_URL_TEMPLATE,
                        url_by_id=url_by_id,
                    ),
                )
        else:
            passed2 = new_items

        if DETAIL_CONFIG and PROMPT_PHASE4:
            current_phase = "phase3"
            detail_cfg = json.loads(DETAIL_CONFIG)
            passed2_ids = {x["id"] for x in passed2}
            items_for_detail = [x for x in new_items if x["id"] in passed2_ids]
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    ),
                    locale="ja-JP",
                )
                context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
                )
                pg = context.new_page()
                try:
                    detailed_items = scrape_detail(pg, items_for_detail, detail_cfg)
                finally:
                    browser.close()

            current_phase = "phase4"
            phase4_fields = [f.strip() for f in PHASE4_FIELDS.split(",") if f.strip()]
            result4, phase4_calls = call_gemini_in_batches(
                KEYS_STR,
                MAX_API_USAGE,
                RESET_HOUR_UTC,
                AI_PROMPT,
                PROMPT_PHASE4,
                detailed_items,
                GEMINI_MODEL,
                current_phase,
                phase4_fields,
                pass_field=PHASE4_PASS_FIELD,
                batch_size=GEMINI_BATCH_SIZE,
                batch_interval_sec=GEMINI_BATCH_INTERVAL_SEC,
            )
            gemini_calls += phase4_calls
            partial_result = result4
            result4_merged = merge_items_by_id(detailed_items, result4)
            passed4 = filter_passed(result4_merged, PHASE4_PASS_FIELD)
            if not passed4:
                for item in new_items:
                    seen_ids.add(item["id"])
                save_seen_ids(seen_ids)
                finish_run(
                    SLACK_WEBHOOK_URL,
                    NOTIFY_PHASE4_REJECTED_HEADER,
                    format_run_summary(
                        "phase4_all_rejected",
                        phase1_diag=phase1_diag,
                        phase2_input=len(new_items),
                        phase2_passed=len(passed2),
                        phase4_input=len(detailed_items),
                        phase4_passed=0,
                        pass_field=PHASE4_PASS_FIELD,
                        partial_result=result4,
                        seen_before=seen_ids_before,
                        seen_after=len(seen_ids),
                        seen_saved=True,
                        gemini_calls=gemini_calls,
                        sample_max=NOTIFY_SUMMARY_SAMPLE_MAX,
                        sample_keys=(
                            "id", "pass", "safety_score", "reason",
                        ),
                        url_template=NOTIFY_URL_TEMPLATE,
                        url_by_id=url_by_id,
                    ),
                )
        else:
            passed4 = passed2

        notify_pass_field = (
            PHASE4_PASS_FIELD if (DETAIL_CONFIG and PROMPT_PHASE4) else PHASE2_PASS_FIELD
        )

        if PROMPT_PHASE5:
            current_phase = "phase5"
            phase5_fields = [f.strip() for f in PHASE5_FIELDS.split(",") if f.strip()]
            phase5_input = passed4
            result5, phase5_calls = call_gemini_in_batches(
                KEYS_STR,
                MAX_API_USAGE,
                RESET_HOUR_UTC,
                AI_PROMPT,
                PROMPT_PHASE5,
                phase5_input,
                GEMINI_MODEL,
                current_phase,
                phase5_fields,
                batch_size=GEMINI_BATCH_SIZE,
                batch_interval_sec=GEMINI_BATCH_INTERVAL_SEC,
            )
            gemini_calls += phase5_calls
            result5 = merge_items_by_id(phase5_input, result5)
            partial_result = result5
        else:
            result5 = passed4

        for item in new_items:
            seen_ids.add(item["id"])
        save_seen_ids(seen_ids)
        seen_ids_after = len(seen_ids)

        if not result5:
            finish_run(
                SLACK_WEBHOOK_URL,
                NOTIFY_PHASE5_EMPTY_HEADER,
                format_run_summary(
                    "phase5_empty",
                    phase1_diag=phase1_diag,
                    phase2_input=len(new_items),
                    phase2_passed=len(passed2) if PROMPT_PHASE2 else len(new_items),
                    phase5_total=0,
                    seen_before=seen_ids_before,
                    seen_after=seen_ids_after,
                    seen_saved=True,
                    gemini_calls=gemini_calls,
                    sample_max=NOTIFY_SUMMARY_SAMPLE_MAX,
                    url_template=NOTIFY_URL_TEMPLATE,
                    url_by_id=url_by_id,
                ),
            )

        blocks = [
            {"type": "header", "text": {"type": "plain_text", "text": NOTIFY_HEADER}},
        ]
        notified_count = 0
        for entry in result5:
            if entry.get(notify_pass_field) is not True:
                continue
            score = entry.get("score", entry.get("safety_score", 0))
            if not isinstance(score, (int, float)) or score < PASS_THRESHOLD:
                continue
            notified_count += 1
            lines = []
            for nf in notify_fields:
                label = nf.get("label", nf.get("key", ""))
                key = nf.get("key", "")
                value = entry.get(key, "")
                if value:
                    text = _format_slack_field_value(
                        key, value, entry, NOTIFY_URL_TEMPLATE,
                    )
                    lines.append(f"*{label}:* {text}")
            text_body = "\n".join(lines)
            if text_body:
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text_body}})
                blocks.append({"type": "divider"})

        summary_body = format_run_summary(
            "success" if len(blocks) > 1 else "below_threshold",
            phase1_diag=phase1_diag,
            phase2_input=len(new_items),
            phase2_passed=len(passed2) if PROMPT_PHASE2 else len(new_items),
            phase4_input=len(detailed_items) if detailed_items else 0,
            phase4_passed=len(passed4) if (DETAIL_CONFIG and PROMPT_PHASE4) else 0,
            phase5_total=len(result5),
            notified_count=notified_count,
            pass_threshold=PASS_THRESHOLD,
            partial_result=result5 if len(blocks) <= 1 else None,
            seen_before=seen_ids_before,
            seen_after=seen_ids_after,
            seen_saved=True,
            gemini_calls=gemini_calls,
            sample_max=NOTIFY_SUMMARY_SAMPLE_MAX,
            sample_keys=("id", "score", "pass", "reason"),
            url_template=NOTIFY_URL_TEMPLATE,
            url_by_id=url_by_id,
        )

        if len(blocks) > 1:
            requests.post(SLACK_WEBHOOK_URL, json={"blocks": blocks}, timeout=10)
            if NOTIFY_SUCCESS_SUMMARY_HEADER:
                send_info_to_slack(
                    SLACK_WEBHOOK_URL, NOTIFY_SUCCESS_SUMMARY_HEADER, summary_body,
                )
        else:
            finish_run(
                SLACK_WEBHOOK_URL, NOTIFY_BELOW_THRESHOLD_HEADER, summary_body,
            )

    except Exception:
        error_msg = traceback.format_exc()
        send_error_to_slack(SLACK_WEBHOOK_URL, error_msg, current_phase, partial_result)
        secure_exit()


if __name__ == "__main__":
    main()
