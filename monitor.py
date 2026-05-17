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
from google.genai.errors import ClientError

STATE_FILE = "seen_ids.txt"
API_STATE_FILE = "api_state.json"


def _get_nested(obj, path):
    for key in path.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def send_error_to_slack(
    webhook_url,
    error_message,
    current_phase=None,
    partial_result=None,
    gemini_raw=None,
):
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
    if gemini_raw:
        safe_raw = str(gemini_raw).replace("```", "'''")
        if len(safe_raw) > 1800:
            safe_raw = safe_raw[:1800] + "...(truncated)"
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"Gemini raw (head):\n```\n{safe_raw}\n```",
            },
        })
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


_SLACK_LIMIT_PROPOSAL = 1200
_SLACK_LIMIT_LONG = 800
_SLACK_LIMIT_DEFAULT = 500
_SLACK_BULLET_KEYS = frozenset({
    "risk_findings",
    "risk_revision_guidance",
    "risk_advisory_notes",
    "revision_guidance",
    "advisory_notes",
    "final_risk_findings",
    "credibility_good",
    "credibility_bad",
    "final_credibility_good",
    "final_credibility_bad",
})
_SLACK_LONG_TEXT_KEYS = _SLACK_BULLET_KEYS | frozenset({
    "buyer_pain_point",
    "final_buyer_pain_point",
    "technical_requirements",
    "implementation_challenges",
    "proposed_solutions_to_implementation_challenges",
})


def _bullet_line(text):
    s = str(text).strip()
    if not s:
        return ""
    for prefix in ("- ", "・", "• ", "* "):
        if s.startswith(prefix):
            s = s[len(prefix):].strip()
            break
    if s.startswith("-") and len(s) > 1 and s[1] != "-":
        s = s[1:].strip()
    return f"- {s}"


def _normalize_slack_bullet_text(value):
    """findings / guidance を Slack 向けの箇条書き文字列に正規化する。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, list):
        lines = [_bullet_line(item) for item in value if item is not None and str(item).strip()]
        return "\n".join(line for line in lines if line)
    if isinstance(value, dict):
        lines = []
        for k, v in value.items():
            if v is None or v == "":
                continue
            lines.append(_bullet_line(f"{k}: {v}"))
        return "\n".join(lines)
    text = str(value).strip()
    if not text:
        return ""
    if text.startswith("[") or text.startswith("{"):
        try:
            parsed = json.loads(text)
            return _normalize_slack_bullet_text(parsed)
        except json.JSONDecodeError:
            pass
    text = text.replace("\\n", "\n")
    lines = []
    for raw in text.split("\n"):
        line = _bullet_line(raw)
        if line:
            lines.append(line)
    return "\n".join(lines) if lines else text

_PROPOSAL_INPUT_KEYS = (
    "id", "title", "url", "description", "meta1", "work_estimate", "delivery_text",
)
_PM_REVIEW_INPUT_KEYS = (
    "id", "description", "meta1", "work_estimate", "delivery_text",
    "proposal_text", "technical_requirements", "implementation_challenges",
)
_BUYER_REVIEW_KEYS = ("id", "title", "description", "meta1", "proposal_text")
_REVISION_INPUT_KEYS = (
    "id", "title", "description", "meta1", "delivery_text", "proposal_text",
    "risk_revision_guidance", "revision_guidance",
)
_PHASE7A_INPUT_KEYS = (
    "id", "title", "description", "meta1", "work_estimate", "delivery_text",
    "proposal_text",
)


def _format_slack_field_value(key, value, entry, url_template):
    if key in ("id", "url"):
        link_url = value if key == "url" and value else _resolve_item_url(entry, url_template)
        if link_url:
            label = entry.get("id", value) if key == "url" else value
            return _slack_link(link_url, label)
    if key in _SLACK_BULLET_KEYS:
        text = _normalize_slack_bullet_text(value)
    elif isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    if key in ("proposal_text", "proposal_text_draft"):
        limit = _SLACK_LIMIT_PROPOSAL
    elif key in _SLACK_LONG_TEXT_KEYS:
        limit = _SLACK_LIMIT_LONG
    else:
        limit = _SLACK_LIMIT_DEFAULT
    if len(text) > limit:
        text = text[:limit] + "...(truncated)"
    return text


def _url_by_id_from_items(items):
    out = {}
    for item in items:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        url = item.get("url")
        if url and not _url_value_needs_fixup(url):
            out[str(item["id"])] = url
    return out


def _url_value_needs_fixup(value):
    if not value or not isinstance(value, str):
        return True
    s = value.strip()
    if s.startswith("http://") or s.startswith("https://"):
        return False
    return True


def _enrich_items_urls(items, url_by_id, url_template):
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if url and not _url_value_needs_fixup(url):
            continue
        item_id = str(item.get("id", ""))
        if item_id and item_id in url_by_id:
            item["url"] = url_by_id[item_id]
        elif item_id and url_template:
            item["url"] = url_template.replace("{id}", item_id)


def _format_final_reception_summary(items):
    parts = []
    for item in items:
        if not isinstance(item, dict):
            continue
        score = item.get("final_reception_score")
        if isinstance(score, (int, float)):
            parts.append(f"{item.get('id', '?')}={int(score)}")
    return ", ".join(parts) if parts else None


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
                limit = 180 if key == "reason" else 100
                if len(text) > limit:
                    text = text[:limit] + "..."
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
    if ctx.get("phase5_draft") is not None:
        lines.extend([
            "Phase5-draft:",
            f"  items: {ctx['phase5_draft']}",
            "",
        ])
    if ctx.get("phase5a") is not None:
        lines.extend([
            "Phase5-A (risk):",
            f"  items: {ctx['phase5a']}",
            "",
        ])
    if ctx.get("phase5b") is not None:
        lines.extend([
            "Phase5-B (buyer):",
            f"  items: {ctx['phase5b']}",
            "",
        ])
    if ctx.get("phase6") is not None:
        lines.extend([
            "Phase6 (revision):",
            f"  items: {ctx['phase6']}",
            "",
        ])
    if ctx.get("phase7a") is not None:
        lines.extend([
            "Phase7-A (final risk):",
            f"  items: {ctx['phase7a']}",
            "",
        ])
    if ctx.get("phase7b") is not None:
        lines.extend([
            "Phase7-B (final buyer):",
            f"  items: {ctx['phase7b']}",
            "",
        ])
    if "phase5_total" in ctx:
        lines.extend([
            "Phase5 (final):",
            f"  results: {ctx['phase5_total']}",
            f"  notified (phase4 pass + safety_score>={ctx.get('pass_threshold', '?')}): "
            f"{ctx.get('notified_count', 0)}",
        ])
        if ctx.get("final_reception_summary"):
            lines.append(
                f"  final_reception_score (7-B): {ctx['final_reception_summary']}",
            )
        lines.append("")
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


def _env_flag_true(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def _persist_seen_ids_enabled():
    return not _env_flag_true("SKIP_SEEN_IDS_SAVE")


def _maybe_save_seen_ids(seen_ids):
    if _persist_seen_ids_enabled():
        save_seen_ids(seen_ids)
        return True
    return False


def _ignore_seen_ids():
    return _env_flag_true("IGNORE_SEEN_IDS")


def _is_initial_seed_run():
    """初回 seed は state ファイルが空のときのみ。IGNORE_SEEN_IDS 時は seed せずパイプライン続行。"""
    if _ignore_seen_ids():
        return False
    return len(load_seen_ids()) == 0


def load_api_state():
    if os.path.exists(API_STATE_FILE):
        with open(API_STATE_FILE, "r") as f:
            return json.load(f)
    return {"current_index": 0, "usage_count": 0, "last_reset_date": ""}


def save_api_state(state):
    with open(API_STATE_FILE, "w") as f:
        json.dump(state, f)


def get_current_api_key(keys_str, max_usage, reset_hour_utc):
    keys = _parse_key_list(keys_str)
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


def _parse_key_list(keys_str):
    return [k.strip() for k in keys_str.split(",") if k.strip()]


def _record_api_attempt(api_state):
    api_state["usage_count"] = int(api_state.get("usage_count", 0)) + 1
    save_api_state(api_state)


def _rotate_api_key_index(api_state, key_count):
    api_state["current_index"] = (int(api_state.get("current_index", 0)) + 1) % key_count
    api_state["usage_count"] = 0
    save_api_state(api_state)


def _is_rate_limit_error(exc):
    if isinstance(exc, ClientError):
        return getattr(exc, "status_code", None) == 429
    return False


def _parse_429_retry_seconds(exc, default_sec):
    try:
        response_json = getattr(exc, "response_json", None) or {}
        error = response_json.get("error", {})
        for detail in error.get("details", []):
            if "RetryInfo" in str(detail.get("@type", "")):
                delay = detail.get("retryDelay", "")
                if isinstance(delay, str) and delay.endswith("s"):
                    return max(1, int(float(delay[:-1])))
    except (TypeError, ValueError, AttributeError):
        pass
    return default_sec


def call_gemini(keys_str, max_usage, reset_hour_utc, prompt_text, model_name):
    key_list = _parse_key_list(keys_str)
    if not key_list:
        raise ValueError("API keys not configured.")

    retry_delay_sec = int(os.environ.get("GEMINI_429_RETRY_SEC", "60"))
    max_attempts = len(key_list)
    last_exc = None

    for _ in range(max_attempts):
        current_key, api_state = get_current_api_key(keys_str, max_usage, reset_hour_utc)
        try:
            client = genai.Client(api_key=current_key)
            response = client.models.generate_content(
                model=model_name,
                contents=prompt_text,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                ),
            )
            _record_api_attempt(api_state)
            return response.text
        except Exception as e:
            last_exc = e
            _record_api_attempt(api_state)
            if _is_rate_limit_error(e):
                _rotate_api_key_index(api_state, len(key_list))
                time.sleep(_parse_429_retry_seconds(e, retry_delay_sec))
                continue
            raise

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("Gemini API call failed")


def _strip_json_code_fence(text):
    text = (text or "").strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _parse_gemini_json_array(text, phase_tag):
    cleaned = _strip_json_code_fence(text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"[{phase_tag}] JSON parse error: {e}\n{cleaned[:500]}")
    if isinstance(data, dict):
        for key in ("items", "results", "data", "array"):
            nested = data.get(key)
            if isinstance(nested, list):
                data = nested
                break
    if not isinstance(data, list):
        raise ValueError(
            f"[{phase_tag}] Response is not a JSON array: {type(data).__name__}"
        )
    return data


_REASON_FIELD_ALIASES = ("rejection_reason", "screening_reason", "summary", "note")


def _default_reason_text(item):
    for key in _REASON_FIELD_ALIASES:
        value = item.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()[:80]
    score = item.get("screening_score", item.get("safety_score", item.get("score")))
    pass_value = item.get("pass")
    if score is not None and pass_value is not None:
        return f"score={score}, pass={pass_value}"[:80]
    if pass_value is not None:
        return f"pass={pass_value}"[:80]
    if score is not None:
        return f"score={score}"[:80]
    return "（理由未記載・自動補完）"


def _normalize_gemini_item(item, required_fields):
    if not isinstance(item, dict):
        return item
    normalized = dict(item)
    if "reason" in required_fields and "reason" not in normalized:
        normalized["reason"] = _default_reason_text(normalized)
    return normalized


def validate_json_list(text, required_fields, phase_tag, pass_field=None):
    data = _parse_gemini_json_array(text, phase_tag)
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"[{phase_tag}] Item [{i}] is not an object")
        item = _normalize_gemini_item(item, required_fields)
        data[i] = item
        for field in required_fields:
            if field not in item:
                raise ValueError(
                    f"[{phase_tag}] Item [{i}] missing required field: '{field}'"
                )
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
    parts = [p.strip() for p in (base_prompt or "", phase_prompt or "") if p and p.strip()]
    body = "\n\n".join(parts)
    payload = json.dumps(data, ensure_ascii=False)
    return f"{body}\n\n{payload}" if body else payload


def _pick_item_fields(item, keys, proposal_key=None):
    if not isinstance(item, dict):
        return {}
    out = {}
    for key in keys:
        if key == "proposal_text" and proposal_key:
            val = item.get(proposal_key) or item.get("proposal_text")
        else:
            val = item.get(key)
        if val is not None and val != "":
            out[key] = val
    if "id" in item:
        out["id"] = item["id"]
    return out


def _build_slim_list(items, keys, proposal_key=None):
    return [
        slim
        for item in items
        if isinstance(item, dict)
        for slim in [_pick_item_fields(item, keys, proposal_key)]
        if slim.get("id")
    ]


def _format_notify_entries(items, notify_field_specs, url_template, pass_field=None, pass_threshold=None):
    blocks_body = []
    for entry in items:
        if pass_field and entry.get(pass_field) is not True:
            continue
        if pass_threshold is not None:
            score = entry.get("score", entry.get("safety_score", 0))
            if not isinstance(score, (int, float)) or score < pass_threshold:
                continue
        lines = []
        for nf in notify_field_specs:
            label = nf.get("label", nf.get("key", ""))
            key = nf.get("key", "")
            value = entry.get(key, "")
            if value:
                text = _format_slack_field_value(key, value, entry, url_template)
                if key in _SLACK_BULLET_KEYS and "\n" in text:
                    lines.append(f"*{label}:*\n{text}")
                else:
                    lines.append(f"*{label}:* {text}")
        if lines:
            blocks_body.append("\n".join(lines))
    return blocks_body


def _send_notify_blocks(webhook_url, header, entries_text_list):
    if not webhook_url or not entries_text_list:
        return
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": header[:150]}}]
    for text_body in entries_text_list:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text_body}})
        blocks.append({"type": "divider"})
    requests.post(webhook_url, json={"blocks": blocks}, timeout=10)


def _parse_csv_fields(fields_str):
    return [f.strip() for f in (fields_str or "").split(",") if f.strip()]


def _format_phase4_passed_slack(passed4, url_template, sample_max=3):
    ranked = sorted(
        [x for x in passed4 if isinstance(x, dict)],
        key=_score_for_sort,
        reverse=True,
    )
    lines = [f"Phase4 passed: {len(passed4)} item(s)", ""]
    for item in ranked[:sample_max]:
        item_id = item.get("id", "?")
        url = _resolve_item_url(item, url_template)
        head = _slack_link(url, item_id) if url else str(item_id)
        safety = item.get("safety_score", "?")
        reason = str(item.get("reason", ""))[:180]
        we = item.get("work_estimate")
        we_text = (
            json.dumps(we, ensure_ascii=False)[:200]
            if isinstance(we, dict) else str(we)[:200]
        )
        lines.append(f"- {head} safety={safety}")
        if we_text:
            lines.append(f"  work_estimate: {we_text}")
        dt = item.get("delivery_text")
        if dt:
            lines.append(f"  delivery_text: {str(dt)[:120]}")
        if reason:
            lines.append(f"  reason: {reason}")
    remaining = len(ranked) - sample_max
    if remaining > 0:
        lines.append(f"- ... and {remaining} more")
    return "\n".join(lines)


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
    last_gemini_raw = None
    current_phase = "init"

    try:
        TARGET_URL = os.environ.get("TARGET_URL")
        KEYS_STR = os.environ.get("GEMINI_API_KEYS")
        AI_PROMPT = os.environ.get("AI_PROMPT")
        AI_PROMPT_PROPOSAL = os.environ.get("AI_PROMPT_PROPOSAL", "")
        AI_PROMPT_PM = os.environ.get("AI_PROMPT_PM", "")
        PARSE_CONFIG = os.environ.get("PARSE_CONFIG")
        DETAIL_CONFIG = os.environ.get("DETAIL_CONFIG")
        PROMPT_PHASE2 = os.environ.get("PROMPT_PHASE2")
        PROMPT_PHASE4 = os.environ.get("PROMPT_PHASE4")
        PROMPT_PHASE5 = os.environ.get("PROMPT_PHASE5")
        PROMPT_PHASE5_REVIEW_A = os.environ.get("PROMPT_PHASE5_REVIEW_A")
        PROMPT_PHASE5_REVIEW_B = os.environ.get("PROMPT_PHASE5_REVIEW_B")
        PROMPT_PHASE6_REVISION = os.environ.get("PROMPT_PHASE6_REVISION")
        PROMPT_PHASE7_REVIEW_A = os.environ.get("PROMPT_PHASE7_REVIEW_A")
        PROMPT_PHASE7_REVIEW_B = os.environ.get("PROMPT_PHASE7_REVIEW_B")
        PHASE2_FIELDS = os.environ.get("PHASE2_FIELDS", "")
        PHASE4_FIELDS = os.environ.get("PHASE4_FIELDS", "")
        PHASE5_FIELDS = os.environ.get("PHASE5_FIELDS", "")
        PHASE5_REVIEW_A_FIELDS = os.environ.get("PHASE5_REVIEW_A_FIELDS", "")
        PHASE5_REVIEW_B_FIELDS = os.environ.get("PHASE5_REVIEW_B_FIELDS", "")
        PHASE6_FIELDS = os.environ.get("PHASE6_FIELDS", "")
        PHASE7_REVIEW_A_FIELDS = os.environ.get("PHASE7_REVIEW_A_FIELDS", "")
        PHASE7_REVIEW_B_FIELDS = os.environ.get("PHASE7_REVIEW_B_FIELDS", "")
        NOTIFY_PHASE4_PASSED_HEADER = os.environ.get("NOTIFY_PHASE4_PASSED_HEADER", "")
        NOTIFY_PHASE5_DRAFT_REVIEW_HEADER = os.environ.get(
            "NOTIFY_PHASE5_DRAFT_REVIEW_HEADER", "",
        )
        NOTIFY_PHASE6_HEADER = os.environ.get("NOTIFY_PHASE6_HEADER", "")
        NOTIFY_PHASE7_HEADER = os.environ.get("NOTIFY_PHASE7_HEADER", "")
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
        PASS_THRESHOLD = int(os.environ.get("PASS_THRESHOLD", "80"))
        MAX_PAGES = int(os.environ.get("MAX_PAGES", "1"))
        MAX_API_USAGE = int(os.environ.get("MAX_API_USAGE", "1000"))
        RESET_HOUR_UTC = int(os.environ.get("RESET_HOUR_UTC", "0"))
        NOTIFY_SUMMARY_SAMPLE_MAX = int(os.environ.get("NOTIFY_SUMMARY_SAMPLE_MAX", "5"))
        gemini_calls = 0
        seen_ids_on_disk = load_seen_ids()
        seen_ids_before = len(seen_ids_on_disk)
        seen_ids = set() if _ignore_seen_ids() else seen_ids_on_disk

        if not all([TARGET_URL, KEYS_STR, SLACK_WEBHOOK_URL, AI_PROMPT, PARSE_CONFIG]):
            raise ValueError("Required environment variables are not configured.")
        if PROMPT_PHASE5 and not AI_PROMPT_PROPOSAL:
            raise ValueError(
                "AI_PROMPT_PROPOSAL is required when PROMPT_PHASE5 is configured.",
            )
        if PROMPT_PHASE5_REVIEW_A and not AI_PROMPT_PM:
            raise ValueError(
                "AI_PROMPT_PM is required when PROMPT_PHASE5_REVIEW_A is configured.",
            )
        if PROMPT_PHASE7_REVIEW_A and not AI_PROMPT_PM:
            raise ValueError(
                "AI_PROMPT_PM is required when PROMPT_PHASE7_REVIEW_A is configured.",
            )

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

        if _is_initial_seed_run():
            for item in new_items:
                seen_ids.add(item["id"])
            seen_saved = _maybe_save_seen_ids(seen_ids)
            finish_run(
                SLACK_WEBHOOK_URL,
                NOTIFY_SEED_HEADER,
                format_run_summary(
                    "phase1_seed",
                    phase1_diag=phase1_diag,
                    seen_before=0,
                    seen_after=len(seen_ids),
                    seen_saved=seen_saved,
                    gemini_calls=gemini_calls,
                    sample_max=NOTIFY_SUMMARY_SAMPLE_MAX,
                ) + "\n\n" + format_phase1_seed(phase1_diag, len(new_items)),
            )

        if PROMPT_PHASE2:
            current_phase = "phase2"
            phase2_fields = [f.strip() for f in PHASE2_FIELDS.split(",") if f.strip()]
            raw = call_gemini(
                KEYS_STR, MAX_API_USAGE, RESET_HOUR_UTC,
                build_phase_prompt(AI_PROMPT, PROMPT_PHASE2, new_items),
                GEMINI_MODEL,
            )
            last_gemini_raw = raw
            gemini_calls += 1
            result2 = validate_json_list(
                raw, phase2_fields, current_phase, pass_field=PHASE2_PASS_FIELD,
            )
            partial_result = result2
            passed2 = filter_passed(result2, PHASE2_PASS_FIELD)
            if not passed2:
                for item in new_items:
                    seen_ids.add(item["id"])
                seen_saved = _maybe_save_seen_ids(seen_ids)
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
                        seen_saved=seen_saved,
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
            raw = call_gemini(
                KEYS_STR, MAX_API_USAGE, RESET_HOUR_UTC,
                build_phase_prompt(AI_PROMPT, PROMPT_PHASE4, detailed_items),
                GEMINI_MODEL,
            )
            last_gemini_raw = raw
            gemini_calls += 1
            result4 = validate_json_list(
                raw, phase4_fields, current_phase, pass_field=PHASE4_PASS_FIELD,
            )
            partial_result = result4
            result4_merged = merge_items_by_id(detailed_items, result4)
            passed4 = filter_passed(result4_merged, PHASE4_PASS_FIELD)
            if not passed4:
                for item in new_items:
                    seen_ids.add(item["id"])
                seen_saved = _maybe_save_seen_ids(seen_ids)
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
                        seen_saved=seen_saved,
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

        if NOTIFY_PHASE4_PASSED_HEADER and passed4 and PROMPT_PHASE5:
            send_info_to_slack(
                SLACK_WEBHOOK_URL,
                NOTIFY_PHASE4_PASSED_HEADER,
                _format_phase4_passed_slack(
                    passed4,
                    NOTIFY_URL_TEMPLATE,
                    sample_max=NOTIFY_SUMMARY_SAMPLE_MAX,
                ),
            )

        notify_pass_field = (
            PHASE4_PASS_FIELD if (DETAIL_CONFIG and PROMPT_PHASE4) else PHASE2_PASS_FIELD
        )

        phase5_draft_count = 0
        phase5a_count = 0
        phase5b_count = 0
        phase6_count = 0
        phase7a_count = 0
        phase7b_count = 0

        _notify_draft_review_fields = [
            {"label": "タイトル", "key": "title"},
            {"label": "URL", "key": "url"},
            {"label": "提案文（初稿）", "key": "proposal_text_draft"},
            {"label": "リスクスコア", "key": "risk_score"},
            {"label": "リスク所見", "key": "risk_findings"},
            {"label": "修正指示（5-A・発注者向け）", "key": "risk_revision_guidance"},
            {"label": "備考（5-A・修正に使わない）", "key": "risk_advisory_notes"},
            {"label": "受注期待度", "key": "reception_score"},
            {"label": "発注者の背景", "key": "buyer_pain_point"},
            {"label": "信頼できる点", "key": "credibility_good"},
            {"label": "不安要素", "key": "credibility_bad"},
            {"label": "修正指示（5-B）", "key": "revision_guidance"},
            {"label": "備考（5-B・修正に使わない）", "key": "advisory_notes"},
        ]
        _notify_phase6_fields = [
            {"label": "タイトル", "key": "title"},
            {"label": "URL", "key": "url"},
            {"label": "提案文（修正後）", "key": "proposal_text"},
        ]
        _notify_phase7_fields = [
            {"label": "タイトル", "key": "title"},
            {"label": "URL", "key": "url"},
            {"label": "提案文（修正後）", "key": "proposal_text"},
            {"label": "最終リスクスコア", "key": "final_risk_score"},
            {"label": "最終リスク所見", "key": "final_risk_findings"},
            {"label": "最終受注期待度", "key": "final_reception_score"},
            {"label": "最終・発注者背景", "key": "final_buyer_pain_point"},
            {"label": "最終・信頼できる点", "key": "final_credibility_good"},
            {"label": "最終・不安要素", "key": "final_credibility_bad"},
        ]

        if PROMPT_PHASE5:
            url_by_id = {**url_by_id, **_url_by_id_from_items(passed4)}
            phase5_fields = _parse_csv_fields(PHASE5_FIELDS)
            proposal_input = _build_slim_list(passed4, _PROPOSAL_INPUT_KEYS)

            current_phase = "phase5-draft"
            raw = call_gemini(
                KEYS_STR, MAX_API_USAGE, RESET_HOUR_UTC,
                build_phase_prompt(
                    AI_PROMPT_PROPOSAL, PROMPT_PHASE5, proposal_input,
                ),
                GEMINI_MODEL,
            )
            last_gemini_raw = raw
            gemini_calls += 1
            result5_draft = validate_json_list(raw, phase5_fields, current_phase)
            _enrich_items_urls(result5_draft, url_by_id, NOTIFY_URL_TEMPLATE)
            result5_merged = merge_items_by_id(passed4, result5_draft)
            url_by_id = {**url_by_id, **_url_by_id_from_items(result5_merged)}
            phase5_draft_count = len(result5_merged)
            partial_result = result5_merged

            result5a_list = []
            if PROMPT_PHASE5_REVIEW_A:
                current_phase = "phase5-a"
                review_a_fields = _parse_csv_fields(PHASE5_REVIEW_A_FIELDS)
                pm_input = _build_slim_list(result5_merged, _PM_REVIEW_INPUT_KEYS)
                raw = call_gemini(
                    KEYS_STR, MAX_API_USAGE, RESET_HOUR_UTC,
                    build_phase_prompt(
                        AI_PROMPT_PM, PROMPT_PHASE5_REVIEW_A, pm_input,
                    ),
                    GEMINI_MODEL,
                )
                last_gemini_raw = raw
                gemini_calls += 1
                result5a_list = validate_json_list(raw, review_a_fields, current_phase)
                phase5a_count = len(result5a_list)

            result5b_list = []
            if PROMPT_PHASE5_REVIEW_B:
                current_phase = "phase5-b"
                review_b_fields = _parse_csv_fields(PHASE5_REVIEW_B_FIELDS)
                buyer_input = _build_slim_list(result5_merged, _BUYER_REVIEW_KEYS)
                raw = call_gemini(
                    KEYS_STR, MAX_API_USAGE, RESET_HOUR_UTC,
                    build_phase_prompt("", PROMPT_PHASE5_REVIEW_B, buyer_input),
                    GEMINI_MODEL,
                )
                last_gemini_raw = raw
                gemini_calls += 1
                result5b_list = validate_json_list(raw, review_b_fields, current_phase)
                phase5b_count = len(result5b_list)

            result5 = merge_items_by_id(
                result5_merged,
                result5a_list,
                result5b_list,
            )
            for item in result5:
                if isinstance(item, dict) and item.get("proposal_text"):
                    item["proposal_text_draft"] = item["proposal_text"]
            _enrich_items_urls(result5, url_by_id, NOTIFY_URL_TEMPLATE)

            if NOTIFY_PHASE5_DRAFT_REVIEW_HEADER:
                draft_bodies = _format_notify_entries(
                    result5,
                    _notify_draft_review_fields,
                    NOTIFY_URL_TEMPLATE,
                    notify_pass_field,
                    PASS_THRESHOLD,
                )
                _send_notify_blocks(
                    SLACK_WEBHOOK_URL,
                    NOTIFY_PHASE5_DRAFT_REVIEW_HEADER,
                    draft_bodies,
                )

            if PROMPT_PHASE6_REVISION:
                current_phase = "phase6"
                phase6_fields = _parse_csv_fields(PHASE6_FIELDS)
                revision_input = _build_slim_list(result5, _REVISION_INPUT_KEYS)
                raw = call_gemini(
                    KEYS_STR, MAX_API_USAGE, RESET_HOUR_UTC,
                    build_phase_prompt(
                        AI_PROMPT_PROPOSAL, PROMPT_PHASE6_REVISION, revision_input,
                    ),
                    GEMINI_MODEL,
                )
                last_gemini_raw = raw
                gemini_calls += 1
                result6_list = validate_json_list(raw, phase6_fields, current_phase)
                _enrich_items_urls(result6_list, url_by_id, NOTIFY_URL_TEMPLATE)
                phase6_count = len(result6_list)
                result5 = merge_items_by_id(result5, result6_list)
                _enrich_items_urls(result5, url_by_id, NOTIFY_URL_TEMPLATE)
                for item in result5:
                    if isinstance(item, dict) and not item.get("proposal_text_draft"):
                        item["proposal_text_draft"] = item.get("proposal_text", "")

                if NOTIFY_PHASE6_HEADER:
                    phase6_bodies = _format_notify_entries(
                        result5,
                        _notify_phase6_fields,
                        NOTIFY_URL_TEMPLATE,
                        notify_pass_field,
                        PASS_THRESHOLD,
                    )
                    _send_notify_blocks(
                        SLACK_WEBHOOK_URL, NOTIFY_PHASE6_HEADER, phase6_bodies,
                    )

            result7a_list = []
            if PROMPT_PHASE7_REVIEW_A:
                current_phase = "phase7-a"
                phase7a_fields = _parse_csv_fields(PHASE7_REVIEW_A_FIELDS)
                phase7a_input = _build_slim_list(result5, _PHASE7A_INPUT_KEYS)
                raw = call_gemini(
                    KEYS_STR, MAX_API_USAGE, RESET_HOUR_UTC,
                    build_phase_prompt(
                        AI_PROMPT_PM, PROMPT_PHASE7_REVIEW_A, phase7a_input,
                    ),
                    GEMINI_MODEL,
                )
                last_gemini_raw = raw
                gemini_calls += 1
                result7a_list = validate_json_list(raw, phase7a_fields, current_phase)
                phase7a_count = len(result7a_list)

            result7b_list = []
            if PROMPT_PHASE7_REVIEW_B:
                current_phase = "phase7-b"
                phase7b_fields = _parse_csv_fields(PHASE7_REVIEW_B_FIELDS)
                buyer7_input = _build_slim_list(result5, _BUYER_REVIEW_KEYS)
                raw = call_gemini(
                    KEYS_STR, MAX_API_USAGE, RESET_HOUR_UTC,
                    build_phase_prompt("", PROMPT_PHASE7_REVIEW_B, buyer7_input),
                    GEMINI_MODEL,
                )
                last_gemini_raw = raw
                gemini_calls += 1
                result7b_list = validate_json_list(raw, phase7b_fields, current_phase)
                phase7b_count = len(result7b_list)

            if result7a_list or result7b_list:
                result5 = merge_items_by_id(result5, result7a_list, result7b_list)

            if NOTIFY_PHASE7_HEADER and (result7a_list or result7b_list):
                phase7_bodies = _format_notify_entries(
                    result5,
                    _notify_phase7_fields,
                    NOTIFY_URL_TEMPLATE,
                    notify_pass_field,
                    PASS_THRESHOLD,
                )
                _send_notify_blocks(
                    SLACK_WEBHOOK_URL, NOTIFY_PHASE7_HEADER, phase7_bodies,
                )

            partial_result = result5
        else:
            result5 = passed4

        for item in new_items:
            seen_ids.add(item["id"])
        seen_saved = _maybe_save_seen_ids(seen_ids)
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
                    seen_saved=seen_saved,
                    gemini_calls=gemini_calls,
                    sample_max=NOTIFY_SUMMARY_SAMPLE_MAX,
                    url_template=NOTIFY_URL_TEMPLATE,
                    url_by_id=url_by_id,
                ),
            )

        notified_count = 0
        for entry in result5:
            if entry.get(notify_pass_field) is not True:
                continue
            score = entry.get("score", entry.get("safety_score", 0))
            if not isinstance(score, (int, float)) or score < PASS_THRESHOLD:
                continue
            notified_count += 1

        final_reception_summary = _format_final_reception_summary(result5)
        summary_body = format_run_summary(
            "success" if notified_count > 0 else "below_threshold",
            phase1_diag=phase1_diag,
            phase2_input=len(new_items),
            phase2_passed=len(passed2) if PROMPT_PHASE2 else len(new_items),
            phase4_input=len(detailed_items) if detailed_items else 0,
            phase4_passed=len(passed4) if (DETAIL_CONFIG and PROMPT_PHASE4) else 0,
            phase5_draft=phase5_draft_count if PROMPT_PHASE5 else None,
            phase5a=phase5a_count if PROMPT_PHASE5_REVIEW_A else None,
            phase5b=phase5b_count if PROMPT_PHASE5_REVIEW_B else None,
            phase6=phase6_count if PROMPT_PHASE6_REVISION else None,
            phase7a=phase7a_count if PROMPT_PHASE7_REVIEW_A else None,
            phase7b=phase7b_count if PROMPT_PHASE7_REVIEW_B else None,
            phase5_total=len(result5),
            notified_count=notified_count,
            final_reception_summary=final_reception_summary,
            pass_threshold=PASS_THRESHOLD,
            partial_result=result5 if notified_count == 0 else None,
            seen_before=seen_ids_before,
            seen_after=seen_ids_after,
            seen_saved=seen_saved,
            gemini_calls=gemini_calls,
            sample_max=NOTIFY_SUMMARY_SAMPLE_MAX,
            sample_keys=("id", "score", "pass", "reason"),
            url_template=NOTIFY_URL_TEMPLATE,
            url_by_id=url_by_id,
        )

        if notified_count > 0:
            header = NOTIFY_SUCCESS_SUMMARY_HEADER or "Run complete"
            send_info_to_slack(SLACK_WEBHOOK_URL, header, summary_body)
        else:
            finish_run(
                SLACK_WEBHOOK_URL, NOTIFY_BELOW_THRESHOLD_HEADER, summary_body,
            )

    except Exception:
        error_msg = traceback.format_exc()
        send_error_to_slack(
            SLACK_WEBHOOK_URL,
            error_msg,
            current_phase,
            partial_result,
            gemini_raw=locals().get("last_gemini_raw"),
        )
        secure_exit()


if __name__ == "__main__":
    main()
