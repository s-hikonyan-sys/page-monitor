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

_SUFFIX = os.environ.get("STATE_SUFFIX", "")
STATE_FILE = f"seen_ids{_SUFFIX}.txt"
API_STATE_FILE = "api_state.json"

_BROWSER_RUNTIME_PATCH = """
(function () {
  try {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
  } catch (e) {}

  try {
    if (!window.chrome) { window.chrome = {}; }
    if (!window.chrome.runtime) {
      window.chrome.runtime = {
        id: undefined,
        connect: function(){},
        sendMessage: function(){},
        onMessage: { addListener: function(){}, removeListener: function(){} },
        onConnect: { addListener: function(){}, removeListener: function(){} },
      };
    }
    window.chrome.loadTimes = function() { return null; };
    window.chrome.csi = function() { return { onloadT: Date.now(), pageT: Date.now(), startE: Date.now(), tran: 15 }; };
    window.chrome.app = { isInstalled: false, InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' }, RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' } };
  } catch (e) {}

  try {
    const fakeMime = (type, desc, suffixes) => {
      const m = Object.create(MimeType.prototype);
      Object.defineProperties(m, {
        type: { get: () => type }, description: { get: () => desc }, suffixes: { get: () => suffixes }, enabledPlugin: { get: () => null },
      });
      return m;
    };
    const fakePlugin = (name, desc, filename, mimes) => {
      const p = Object.create(Plugin.prototype);
      Object.defineProperties(p, {
        name: { get: () => name }, description: { get: () => desc }, filename: { get: () => filename }, length: { get: () => mimes.length },
      });
      mimes.forEach((m, i) => { p[i] = m; });
      return p;
    };
    const pdfMime = fakeMime('application/pdf', 'Portable Document Format', 'pdf');
    const plugins = [
      fakePlugin('Chrome PDF Plugin', 'Portable Document Format', 'internal-pdf-viewer', [pdfMime]),
      fakePlugin('Chrome PDF Viewer', '', 'mhjfbmdgcfjbbpaeojofohoefgiehjai', [pdfMime]),
      fakePlugin('Native Client', '', 'internal-nacl-plugin', []),
    ];
    const pa = Object.create(PluginArray.prototype);
    Object.defineProperty(pa, 'length', { get: () => plugins.length });
    plugins.forEach((pl, i) => { pa[i] = pl; pa[pl.name] = pl; });
    pa.item = (i) => pa[i];
    pa.namedItem = (n) => pa[n];
    Object.defineProperty(navigator, 'plugins', { get: () => pa });
    Object.defineProperty(navigator, 'mimeTypes', { get: () => { const ma = Object.create(MimeTypeArray.prototype); ma[0] = pdfMime; Object.defineProperty(ma, 'length', { get: () => 1 }); return ma; } });
  } catch (e) {}

  try {
    Object.defineProperty(navigator, 'languages', { get: () => ['ja-JP', 'ja', 'en-US', 'en'] });
    Object.defineProperty(navigator, 'language', { get: () => 'ja-JP' });
  } catch (e) {}

  try {
    const origQuery = navigator.permissions.query.bind(navigator.permissions);
    navigator.permissions.query = (params) => {
      if (params && params.name === 'notifications') {
        return Promise.resolve({ state: Notification.permission, onchange: null });
      }
      return origQuery(params);
    };
  } catch (e) {}

  try {
    Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 1 });
  } catch (e) {}

  try {
    const origGetParam = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(param) {
      if (param === 37445) return 'Intel Inc.';
      if (param === 37446) return 'Intel Iris OpenGL Engine';
      return origGetParam.apply(this, arguments);
    };
    const orig2 = WebGL2RenderingContext.prototype.getParameter;
    WebGL2RenderingContext.prototype.getParameter = function(param) {
      if (param === 37445) return 'Intel Inc.';
      if (param === 37446) return 'Intel Iris OpenGL Engine';
      return orig2.apply(this, arguments);
    };
  } catch (e) {}

  try {
    const origContentWindow = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, 'contentWindow');
    if (origContentWindow) {
      Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
        get: function() {
          const w = origContentWindow.get.call(this);
          if (w && !w.chrome) { try { w.chrome = window.chrome; } catch (ex) {} }
          return w;
        },
      });
    }
  } catch (e) {}

  try {
    Object.defineProperty(screen, 'colorDepth', { get: () => 24 });
    Object.defineProperty(screen, 'pixelDepth', { get: () => 24 });
  } catch (e) {}

  try {
    Object.defineProperty(window, 'devicePixelRatio', { get: () => 1 });
  } catch (e) {}

  try {
    const origErr = Error;
    window.Error = function(...args) {
      const err = new origErr(...args);
      if (err.stack) {
        err.stack = err.stack.split('\\n').filter(l => !l.includes('puppeteer') && !l.includes('playwright')).join('\\n');
      }
      return err;
    };
    Object.setPrototypeOf(window.Error, origErr);
    Object.defineProperty(window.Error, 'stackTraceLimit', { get: () => origErr.stackTraceLimit, set: (v) => { origErr.stackTraceLimit = v; } });
  } catch (e) {}
})();
"""


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
    _log_progress("run exit status=1")
    sys.exit(1)


def _log_progress(message):
    print(f"[monitor] {message}", flush=True)


def finish_run(webhook_url, header, body):
    _log_progress("run complete status=0")
    send_info_to_slack(webhook_url, header, body)
    sys.exit(0)


_SCORE_KEYS = ("screening_score", "safety_score", "score")
_CTX_METRICS_KEY = "_ctx_metrics"
_INTERNAL_SCRAPE_KEYS = frozenset({_CTX_METRICS_KEY})


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
_PARTY_REVIEW_KEYS = ("id", "title", "description", "meta1", "proposal_text")
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
            "Phase5-B (review-b):",
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
            "Phase7-B (final review-b):",
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
        return getattr(exc, "code", None) == 429
    return False


def _is_transient_error(exc):
    return getattr(exc, "code", None) in (500, 503)


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

    retry_delay_sec = int(os.environ.get("GEMINI_429_RETRY_SEC") or "60")
    transient_max_retries = int(os.environ.get("GEMINI_503_MAX_RETRIES") or "100")
    transient_retry_sec = int(os.environ.get("GEMINI_503_RETRY_SEC") or "60")
    max_key_attempts = len(key_list)
    rate_limit_key_attempts = 0
    transient_attempts = 0

    while True:
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
            if _is_rate_limit_error(e):
                _record_api_attempt(api_state)
                rate_limit_key_attempts += 1
                if rate_limit_key_attempts >= max_key_attempts:
                    raise
                _rotate_api_key_index(api_state, len(key_list))
                time.sleep(_parse_429_retry_seconds(e, retry_delay_sec))
                transient_attempts = 0
                continue
            if _is_transient_error(e):
                transient_attempts += 1
                if transient_attempts > transient_max_retries:
                    raise
                time.sleep(transient_retry_sec)
                continue
            raise


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

_SKILL_EXCLUDE_KEYWORDS = ("pythonの", "MQL4", "MT4")

_PHASE7B_FIELD_ALIASES = {
    "final_risk_score": "final_reception_score",
    "reception_score": "final_reception_score",
    "final_risk_findings": "final_credibility_bad",
    "buyer_pain_point": "final_buyer_pain_point",
    "credibility_good": "final_credibility_good",
    "credibility_bad": "final_credibility_bad",
}


def _item_text_for_skill_exclude(item):
    parts = [item.get("title"), item.get("description"), item.get("meta1")]
    return " ".join(str(part) for part in parts if part)


def _skill_exclude_keyword(item):
    text = _item_text_for_skill_exclude(item)
    if "pythonの" in text:
        return "pythonの"
    text_upper = text.upper()
    for keyword in _SKILL_EXCLUDE_KEYWORDS[1:]:
        if keyword in text_upper:
            return keyword
    return None


def _partition_skill_excluded(items):
    kept = []
    excluded = []
    for item in items:
        if not isinstance(item, dict):
            kept.append(item)
            continue
        keyword = _skill_exclude_keyword(item)
        if keyword:
            excluded.append((item, keyword))
        else:
            kept.append(item)
    return kept, excluded


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


def _normalize_phase7b_item(item):
    if not isinstance(item, dict):
        return item
    normalized = dict(item)
    for alias, canonical in _PHASE7B_FIELD_ALIASES.items():
        if canonical not in normalized and alias in normalized:
            normalized[canonical] = normalized[alias]
    if "final_reception_score" not in normalized:
        for key in ("final_risk_score", "reception_score", "score"):
            if key in normalized:
                normalized["final_reception_score"] = normalized[key]
                break
    if "final_buyer_pain_point" not in normalized:
        normalized["final_buyer_pain_point"] = "（自動補完・発注者視点の課題は未記載）"
    if "final_credibility_good" not in normalized:
        normalized["final_credibility_good"] = "- 特になし"
    if "final_credibility_bad" not in normalized:
        for key in ("final_risk_findings", "credibility_bad"):
            if key in normalized:
                normalized["final_credibility_bad"] = normalized[key]
                break
        if "final_credibility_bad" not in normalized:
            normalized["final_credibility_bad"] = "- 特になし"
    return normalized


def _normalize_gemini_item(item, required_fields, phase_tag=None):
    if not isinstance(item, dict):
        return item
    normalized = dict(item)
    if phase_tag == "phase7-b":
        normalized = _normalize_phase7b_item(normalized)
    if "reason" in required_fields and "reason" not in normalized:
        normalized["reason"] = _default_reason_text(normalized)
    return normalized


def _backfill_items_from_source(items, source_items, fields):
    if not items or not source_items or not fields:
        return items
    by_id = {}
    for src in source_items:
        if not isinstance(src, dict):
            continue
        src_id = src.get("id")
        if src_id is not None:
            by_id[str(src_id)] = src
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = item.get("id")
        if item_id is None:
            continue
        src = by_id.get(str(item_id))
        if not src:
            continue
        for field in fields:
            if field not in item and field in src:
                item[field] = src[field]
    return items


def validate_json_list(
    text,
    required_fields,
    phase_tag,
    pass_field=None,
    source_items=None,
    source_fields=None,
):
    data = _parse_gemini_json_array(text, phase_tag)
    if source_items and source_fields:
        _backfill_items_from_source(data, source_items, source_fields)
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"[{phase_tag}] Item [{i}] is not an object")
        item = _normalize_gemini_item(item, required_fields, phase_tag=phase_tag)
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


def _pass_value_is_true(value):
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return False


def filter_passed(items, pass_field):
    field = (pass_field or "pass").strip()
    return [x for x in items if _pass_value_is_true(x.get(field))]


def _extract_summary_panel(page, panel_cfg):
    root = (panel_cfg or {}).get("root") or ""
    column = (panel_cfg or {}).get("column") or ""
    if not root or not column:
        return {}
    try:
        return page.evaluate(
            """
            (args) => {
                const out = {};
                const root = document.querySelector(args.root);
                if (!root) return out;
                root.querySelectorAll(args.column).forEach((col) => {
                    const ps = [...col.querySelectorAll('p')]
                        .map((p) => (p.innerText || '').trim())
                        .filter(Boolean);
                    if (ps.length >= 2) {
                        out[ps[0]] = ps[1];
                    }
                });
                return out;
            }
            """,
            {"root": root, "column": column},
        ) or {}
    except Exception:
        return {}


def _detail_page_url(url, detail_cfg):
    nav = detail_cfg.get("detail_nav") or {}
    query = (nav.get("query") or "").strip()
    if not query:
        return url
    key = query.split("=", 1)[0]
    if f"{key}=" in url:
        return url
    if "?" in url:
        return f"{url}&{query}"
    return f"{url}?{query}"


def _extract_party_panel(page, party_cfg):
    if not party_cfg:
        return {}
    style = (party_cfg.get("style") or "").strip()
    try:
        if style == "grid_auth":
            return page.evaluate(
                """
                (cfg) => {
                    const out = {};
                    const root = document.querySelector(cfg.root);
                    if (!root) return out;
                    root.querySelectorAll(cfg.column).forEach((col) => {
                        const heading = col.querySelector(cfg.heading);
                        const value = col.querySelector(cfg.value);
                        const h = (heading?.innerText || '').trim();
                        const v = (value?.innerText || '').trim();
                        if (h && v) out[h] = v;
                    });
                    const authRoot = document.querySelector(cfg.auth_root);
                    if (authRoot) {
                        authRoot.querySelectorAll(cfg.auth_row).forEach((row) => {
                            const h = (row.querySelector(cfg.auth_heading)?.innerText || '').trim();
                            if (!h) return;
                            const done = !!row.querySelector(cfg.auth_done);
                            (cfg.auth_flags || []).forEach((flag) => {
                                const alt = flag.heading_alt || '';
                                if (h.includes(flag.heading_match) || (alt && h.includes(alt))) {
                                    out[flag.key] = done ? '済' : '未';
                                }
                            });
                        });
                    }
                    return out;
                }
                """,
                party_cfg,
            ) or {}
        if style == "party_feedback":
            return page.evaluate(
                """
                (cfg) => {
                    const out = {};
                    const root = document.querySelector(cfg.root);
                    if (!root) return out;
                    const good = (root.querySelector(cfg.good)?.innerText || '').trim();
                    const bad = (root.querySelector(cfg.bad)?.innerText || '').trim();
                    if (good !== '') out.good = good;
                    if (bad !== '') out.bad = bad;
                    if (good !== '' || bad !== '') {
                        out.eval_goodbad = `Good${good || '0'}/Bad${bad || '0'}`;
                    }
                    const rate = (root.querySelector(cfg.rate)?.innerText || '').trim();
                    if (rate) out.order_rate = rate;
                    const awarded = (root.querySelector(cfg.awarded)?.innerText || '').trim();
                    const posted = (root.querySelector(cfg.posted)?.innerText || '').trim();
                    if (awarded && posted) out.order_count = `${awarded}/${posted}`;
                    const auths = [];
                    root.querySelectorAll(cfg.auth_row).forEach((row) => {
                        const label = (row.querySelector(cfg.auth_label)?.innerText || '').trim();
                        if (!label) return;
                        const dt = row.querySelector('dt');
                        const done = !!(dt && cfg.auth_done && dt.querySelector(cfg.auth_done));
                        if (done) auths.push(label.replace(/確認$/, ''));
                        (cfg.auth_flags || []).forEach((flag) => {
                            if (label.includes(flag.label_match)) {
                                out[flag.key] = done ? '済' : '未';
                            }
                        });
                    });
                    if (auths.length) out.auth_list = auths.join(',');
                    return out;
                }
                """,
                party_cfg,
            ) or {}
    except Exception:
        return {}
    return {}


def _extract_rating_panel(page, rating_cfg):
    if not rating_cfg:
        return {}
    try:
        return page.evaluate(
            """
            (cfg) => {
                const out = {};
                const ratingEl = document.querySelector(cfg.rating);
                const countEl = document.querySelector(cfg.count);
                const rating = (ratingEl?.innerText || '').trim();
                const countRaw = (countEl?.innerText || '').trim();
                if (rating) out.rating = rating;
                if (countRaw) out.review_count_raw = countRaw;
                return out;
            }
            """,
            rating_cfg,
        ) or {}
    except Exception:
        return {}


def _parse_float(value):
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)", text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _parse_int(value):
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    m = re.search(r"(\d+)", text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _parse_pct(value):
    num = _parse_float(value)
    if num is None:
        return None
    if "%" in str(value):
        return num
    if num <= 1.0:
        return num * 100.0
    return num


def _parse_meta1_star(meta1):
    if not meta1:
        return None, None
    text = str(meta1)
    m = re.search(r"★([\d.]+).*?（(\d+)）", text)
    if not m:
        m = re.search(r"★([\d.]+).*?\((\d+)\)", text)
    if not m:
        return None, None
    try:
        return float(m.group(1)), int(m.group(2))
    except ValueError:
        return None, None


def _normalize_ctx_metrics(party_data, rating_data, meta1):
    party_data = party_data or {}
    rating_data = rating_data or {}
    metrics = {}

    rating = _parse_float(rating_data.get("rating"))
    review_count = _parse_int(rating_data.get("review_count_raw"))
    if rating is None or review_count is None:
        meta_rating, meta_reviews = _parse_meta1_star(meta1)
        rating = rating if rating is not None else meta_rating
        review_count = review_count if review_count is not None else meta_reviews
    if rating is not None:
        metrics["rating"] = rating
    if review_count is not None:
        metrics["review_count"] = review_count

    for key in ("id_verify", "nda"):
        if party_data.get(key) not in (None, ""):
            metrics[key] = party_data.get(key)

    if party_data.get("good") not in (None, ""):
        metrics["good"] = _parse_int(party_data.get("good")) or 0
    if party_data.get("bad") not in (None, ""):
        metrics["bad"] = _parse_int(party_data.get("bad")) or 0

    if party_data.get("order_rate") not in (None, ""):
        metrics["order_rate_pct"] = _parse_pct(party_data.get("order_rate"))

    for jp_key, field in (
        ("発注件数", "order_total"),
        ("発注率", "order_rate_pct"),
        ("取引完了率", "completion_rate_pct"),
    ):
        if party_data.get(jp_key) not in (None, ""):
            if field.endswith("_pct"):
                metrics[field] = _parse_pct(party_data.get(jp_key))
            else:
                metrics[field] = _parse_int(party_data.get(jp_key))

    awarded = _parse_int(party_data.get("awarded"))
    posted = _parse_int(party_data.get("posted"))
    if awarded is not None and posted is not None:
        metrics["order_awarded"] = awarded
        metrics["order_posted"] = posted

    order_count = party_data.get("order_count")
    if order_count and "order_awarded" not in metrics:
        parts = str(order_count).split("/")
        if len(parts) == 2:
            metrics["order_awarded"] = _parse_int(parts[0])
            metrics["order_posted"] = _parse_int(parts[1])

    return metrics


def _attach_ctx_metrics(detail, party_data, rating_data, detail_cfg):
    rating_label = detail_cfg.get("rating_label", "meta1")
    meta1 = detail.get(rating_label, "")
    detail[_CTX_METRICS_KEY] = _normalize_ctx_metrics(party_data, rating_data, meta1)
    return detail


def _strip_internal_scrape_fields(items):
    cleaned = []
    for item in items:
        if not isinstance(item, dict):
            cleaned.append(item)
            continue
        cleaned.append(
            {k: v for k, v in item.items() if k not in _INTERNAL_SCRAPE_KEYS},
        )
    return cleaned


def _metrics_by_id(items):
    out = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        item_id = item.get("id")
        if item_id:
            out[item_id] = dict(item.get(_CTX_METRICS_KEY) or {})
    return out


def _context_scoring_unavailable(metrics, profile):
    if not metrics:
        return True
    if profile == "ratio_baseline":
        good = metrics.get("good")
        bad = metrics.get("bad")
        if good is None and bad is None:
            return True
        return (int(good or 0) + int(bad or 0)) < 1
    if metrics.get("rating") is None and metrics.get("order_total") is None:
        if metrics.get("id_verify") is None and metrics.get("good") is None:
            return True
    return False


def _auth_bonus_eligible(orders, reviews, cfg):
    min_orders_auth = int(cfg.get("min_orders_auth", 3))
    min_reviews_auth = int(cfg.get("min_reviews_auth", 5))
    order_count = 0 if orders is None else int(orders)
    review_count = 0 if reviews is None else int(reviews)
    return order_count >= min_orders_auth or review_count >= min_reviews_auth


def _party_score_rating_baseline(metrics, cfg):
    total = 0
    parts = []
    min_orders = int(cfg.get("min_orders", 10))
    min_reviews = int(cfg.get("min_reviews", 3))
    baseline = float(cfg.get("baseline", 4.8))

    rating = metrics.get("rating")
    reviews = metrics.get("review_count")
    orders = metrics.get("order_total")
    order_rate = metrics.get("order_rate_pct")
    completion = metrics.get("completion_rate_pct")

    if orders is not None:
        if orders == 0:
            total -= 10
            parts.append("first-10")
        elif 1 <= orders <= 3:
            total -= 5
            parts.append("loword-5")

    if (
        rating is not None
        and reviews is not None
        and reviews >= min_reviews
        and rating < 4.5
    ):
        total -= 12
        parts.append("★-12")

    if (
        rating is not None
        and reviews is not None
        and orders is not None
        and orders >= min_orders
        and reviews >= min_reviews
    ):
        delta = rating - baseline
        if delta >= 0:
            rating_adj = min(15, round(delta * delta * 200))
            total += rating_adj
            if rating_adj:
                parts.append(f"★{rating_adj:+d}")

    if orders is not None and orders >= 5:
        if completion is not None and completion < 75:
            total -= 10
            parts.append("comp-10")
        if orders >= 10 and completion is not None and completion < 60:
            total -= 5
            parts.append("comp-5")
        if order_rate is not None and order_rate < 50:
            total -= 5
            parts.append("orate-5")

    if _auth_bonus_eligible(orders, reviews, cfg):
        if metrics.get("id_verify") == "済":
            total += 2
            parts.append("id+2")
        if metrics.get("nda") == "済":
            total += 2
            parts.append("nda+2")

    if orders is not None and orders >= 5 and order_rate is not None and order_rate >= 70:
        total += 2
        parts.append("orate+2")
    if orders is not None and orders >= 10 and completion is not None and completion >= 85:
        total += 2
        parts.append("comp+2")

    return total, "+".join(parts) if parts else "0"


def _party_score_ratio_baseline(metrics, cfg):
    total = 0
    parts = []
    min_feedback = int(cfg.get("min_feedback", 5))
    baseline = float(cfg.get("ratio_baseline", 0.96))

    good = int(metrics.get("good") or 0)
    bad = int(metrics.get("bad") or 0)
    feedback_total = good + bad
    order_rate = metrics.get("order_rate_pct")
    awarded = metrics.get("order_awarded")
    posted = metrics.get("order_posted")

    if posted is not None and posted >= 3 and (awarded is None or awarded == 0):
        total -= 10
        parts.append("first-10")

    if (
        posted is not None
        and posted >= 5
        and order_rate is not None
        and order_rate < 35
    ):
        total -= 5
        parts.append("orate-5")

    if feedback_total >= min_feedback:
        ratio = good / feedback_total
        delta = ratio - baseline
        if delta >= 0:
            ratio_adj = min(15, round(delta * delta * 3000))
            total += ratio_adj
            if ratio_adj:
                parts.append(f"ratio{ratio_adj:+d}")
        elif ratio < 0.90:
            ratio_adj = max(-15, -round((0.90 - ratio) ** 2 * 500))
            total += ratio_adj
            if ratio_adj:
                parts.append(f"ratio{ratio_adj:+d}")

    auth_ok = (
        (awarded is not None and awarded >= 3)
        or feedback_total >= min_feedback
    )
    if auth_ok:
        if metrics.get("id_verify") == "済":
            total += 2
            parts.append("id+2")
        if metrics.get("nda") == "済":
            total += 2
            parts.append("nda+2")

    if order_rate is not None and order_rate >= 50:
        total += 2
        parts.append("orate+2")
    if awarded is not None and awarded >= 10:
        total += 2
        parts.append("ord+2")

    return total, "+".join(parts) if parts else "0"


def _compute_context_adjustment(metrics, scoring_cfg):
    scoring_cfg = scoring_cfg or {}
    profile = (scoring_cfg.get("profile") or "rating_baseline").strip()
    if _context_scoring_unavailable(metrics, profile):
        return 0, "不足"
    if profile == "ratio_baseline":
        total, breakdown = _party_score_ratio_baseline(metrics, scoring_cfg)
    else:
        total, breakdown = _party_score_rating_baseline(metrics, scoring_cfg)
    clip = scoring_cfg.get("clip") or [-20, 15]
    lo = int(clip[0]) if len(clip) > 0 else -20
    hi = int(clip[1]) if len(clip) > 1 else 15
    total = max(lo, min(hi, total))
    return total, breakdown


def _hard_fail_reason(reason):
    markers = (
        "絶対防壁", "スキル外", "新規一括", "新規システム", "機材なし",
        "スキャナ", "自炊", "上限35", "上限25", "上限40",
    )
    text = str(reason or "")
    return any(marker in text for marker in markers)


def _recalc_pass_after_context(item, final_score, pass_threshold):
    if _hard_fail_reason(item.get("reason")):
        return False
    return final_score >= pass_threshold


def _append_context_adj_reason(item, base_score, final_score, adj, breakdown):
    reason = str(item.get("reason") or "").strip()
    adj_text = (
        f"ctx_adj={adj}({breakdown})"
        if adj
        else "ctx_adj=0"
    )
    score_text = f"base={base_score}→final={final_score}"
    suffix = f"{adj_text};{score_text}"
    if not reason:
        return suffix[:180]
    combined = f"{reason};{suffix}"
    return combined[:180]


def _apply_context_scores(items, metrics_by_id, detail_cfg, pass_threshold, pass_field):
    scoring_cfg = detail_cfg.get("context_scoring") or {}
    if not scoring_cfg:
        return items
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = item.get("id")
        metrics = metrics_by_id.get(item_id) or {}
        base_score = item.get("safety_score", 0)
        if not isinstance(base_score, (int, float)):
            try:
                base_score = int(base_score)
            except (TypeError, ValueError):
                base_score = 0
        else:
            base_score = int(base_score)
        adj, breakdown = _compute_context_adjustment(metrics, scoring_cfg)
        final_score = max(0, min(100, base_score + adj))
        item["safety_score"] = final_score
        item[pass_field] = _recalc_pass_after_context(item, final_score, pass_threshold)
        item["reason"] = _append_context_adj_reason(
            item, base_score, final_score, adj, breakdown,
        )
    return items


def _merge_party_context(merge_cfg, dl_pairs, panel_data, party_data, rating_label):
    if not merge_cfg:
        return None
    sep = merge_cfg.get("sep", "; ")
    segments = merge_cfg.get("segments") or []
    parts = []
    for seg in segments:
        seg_type = seg.get("type")
        fmt = seg.get("fmt", "{value}")
        if seg_type == "dl":
            dt = seg.get("dt") or ""
            value = (dl_pairs or {}).get(dt, "")
            if value:
                parts.append(fmt.replace("{value}", value).replace("{key}", dt))
        elif seg_type == "panel":
            keys = seg.get("keys")
            items = panel_data or {}
            iter_keys = keys if keys else list(items.keys())
            for key in iter_keys:
                value = items.get(key)
                if value:
                    parts.append(
                        fmt.replace("{value}", value).replace("{key}", key),
                    )
        elif seg_type == "party":
            key = seg.get("key") or ""
            pdata = party_data or {}
            value = pdata.get(key, "")
            if value:
                rendered = fmt.replace("{value}", str(value)).replace("{key}", key)
                rendered = rendered.replace("{good}", str(pdata.get("good", "")))
                rendered = rendered.replace("{bad}", str(pdata.get("bad", "")))
                parts.append(rendered)
    merged = sep.join(p for p in parts if p)
    if merged:
        return merged
    fallback = (merge_cfg.get("fallback") or "").strip()
    return fallback or None


def _apply_detail_context_merge(
    detail, dom_desc_cfg, dl_pairs, panel_data, party_data, rating_label,
):
    merge_cfg = dom_desc_cfg.get("context_merge") or {}
    if not merge_cfg:
        return detail
    merge_target = (merge_cfg.get("target") or rating_label).strip()
    missing_context = (dom_desc_cfg.get("missing_context") or "N/A").strip()
    mode = (merge_cfg.get("mode") or "replace").strip()
    merged = _merge_party_context(
        merge_cfg, dl_pairs, panel_data, party_data, rating_label,
    )
    if not merged:
        if merge_target not in detail:
            detail[merge_target] = missing_context
        return detail
    existing = detail.get(merge_target)
    if mode == "append" and existing and str(existing).strip() not in ("", "N/A"):
        sep = merge_cfg.get("sep", "; ")
        detail[merge_target] = f"{existing}{sep}{merged}"
    else:
        detail[merge_target] = merged
    return detail


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
        if pass_field and not _pass_value_is_true(entry.get((pass_field or "pass").strip())):
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
    if _probe_indicates_unsettled(probe):
        lines.extend([
            "",
            "Likely cause: navigation unsettled; runtime settle rounds exhausted.",
            "Check PARSE_CONFIG if the page structure changed.",
        ])
    else:
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


def _make_browser_context(p):
    """Chromium ブラウザ + コンテキストを生成する"""
    _outbound_relay = os.environ.get("BROWSER_PROXY") or None
    browser = p.chromium.launch(
        headless=True,
        proxy={"server": _outbound_relay} if _outbound_relay else None,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-infobars",
            "--disable-background-networking",
            "--disable-default-apps",
            "--disable-extensions",
            "--disable-sync",
            "--disable-translate",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-client-side-phishing-detection",
            "--disable-hang-monitor",
            "--disable-popup-blocking",
            "--mute-audio",
            "--no-first-run",
            "--safebrowsing-disable-auto-update",
            "--metrics-recording-only",
            "--use-mock-keychain",
            "--lang=ja-JP",
            "--window-size=1920,1080",
            "--force-color-profile=srgb",
        ],
    )
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1920, "height": 1080},
        locale="ja-JP",
        timezone_id="Asia/Tokyo",
        color_scheme="light",
        extra_http_headers={
            "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;"
                "q=0.9,image/avif,image/webp,image/apng,*/*;"
                "q=0.8,application/signed-exchange;v=b3;q=0.7"
            ),
            "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Upgrade-Insecure-Requests": "1",
        },
    )
    context.add_init_script(_BROWSER_RUNTIME_PATCH)
    return browser, context


def _runtime_settle_limits():
    return (
        int(os.environ.get("RUNTIME_SETTLE_ROUNDS") or "100"),
        int(os.environ.get("RUNTIME_SETTLE_PAUSE_SEC") or "30"),
    )


def _probe_indicates_unsettled(probe):
    if not probe:
        return False
    title = (probe.get("title") or "").lower()
    if "forbidden" in title:
        return True
    _unsettled_page_patterns = (
        "human verification", "verification", "verify", "captcha",
        "challenge", "just a moment", "checking your browser",
        "please wait", "bot detection", "access denied",
    )
    if any(kw in title for kw in _unsettled_page_patterns):
        return True
    if probe.get("htmlLength", 0) < 500:
        return True
    substrings = probe.get("substrings") or []
    if substrings and not any(s.get("found") for s in substrings):
        return True
    return False


def _navigation_unsettled(response, page, config):
    status = response.status if response is not None else None
    if status in (403, 429, 502, 503):
        return True
    try:
        probe = _probe_listing_page(page, config)
    except Exception:
        return True
    return _probe_indicates_unsettled(probe)


def _replace_page(context, page):
    try:
        page.close()
    except Exception:
        pass
    return context.new_page()


def _load_page_with_settle(context, page, url, goto_wait, config, diagnostics, page_num):
    max_rounds, pause_sec = _runtime_settle_limits()
    _page_transition_ms = int((config.get("page_options") or {}).get("transition_wait_ms", 0))
    response = None
    for round_num in range(1, max_rounds + 2):
        response = page.goto(url, wait_until=goto_wait, timeout=30000)
        if _page_transition_ms > 0:
            try:
                probe_early = _probe_listing_page(page, config)
            except Exception:
                probe_early = None
            if _probe_indicates_unsettled(probe_early):
                diagnostics["errors"].append(
                    f"page {page_num}: page transition in progress, holding {_page_transition_ms}ms"
                )
                page.wait_for_timeout(_page_transition_ms)
        if not _navigation_unsettled(response, page, config):
            if round_num > 1:
                diagnostics["errors"].append(
                    f"page {page_num}: runtime settle ok on round {round_num}",
                )
                _log_progress(
                    f"phase1 settle done page={page_num} round={round_num}",
                )
            return page, response
        if round_num == 1:
            _log_progress(
                f"phase1 settle start page={page_num} max_rounds={max_rounds}",
            )
        elif round_num == 2:
            _log_progress(
                f"phase1 settle retry page={page_num} (up to {max_rounds} rounds)",
            )
        if round_num <= max_rounds:
            diagnostics["errors"].append(
                f"page {page_num}: runtime settle round {round_num}/{max_rounds}",
            )
            time.sleep(pause_sec)
            page = _replace_page(context, page)
        else:
            diagnostics["errors"].append(
                f"page {page_num}: runtime settle exhausted ({max_rounds} rounds)",
            )
            _log_progress(
                f"phase1 settle exhausted page={page_num} rounds={max_rounds}",
            )
    return page, response


def _scrape_listing_once(page, config, seen_ids, max_pages, target_url):
    diagnostics = _empty_phase1_diagnostics(seen_ids)
    scraped_all = []
    context = page.context
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
            page, _response = _load_page_with_settle(
                context, page, page_url, goto_wait, config, diagnostics, page_num,
            )
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


def scrape_listing(page, config, seen_ids, max_pages, target_url):
    context = page.context
    max_rounds, pause_sec = _runtime_settle_limits()
    last_result = ([], _empty_phase1_diagnostics(seen_ids))

    for outer_round in range(1, max_rounds + 2):
        new_items, diagnostics = _scrape_listing_once(
            page, config, seen_ids, max_pages, target_url,
        )
        last_result = (new_items, diagnostics)
        if diagnostics["scraped_total"] > 0:
            return new_items, diagnostics
        if not _probe_indicates_unsettled(diagnostics.get("runtime_probe")):
            return new_items, diagnostics
        if outer_round <= max_rounds:
            diagnostics["errors"].append(
                f"listing runtime settle outer round {outer_round}/{max_rounds}",
            )
            time.sleep(pause_sec)
            page = _replace_page(context, page)
        else:
            diagnostics["errors"].append(
                f"listing runtime settle exhausted ({max_rounds} outer rounds)",
            )
            return new_items, diagnostics

    return last_result


def _scrape_detail_dom(page, detail, detail_cfg):
    """DOM ベースの詳細ページ取得（LD+JSON が存在しないサイト向け）。
    detail_cfg に dom_desc セクションがある場合に呼ばれる。
    dl_selector で指定した <dl> の <dt>/<dd> ペアを dt_dd_map に従ってフィールドに格納する。
    """
    dom_desc_cfg = detail_cfg.get("dom_desc") or {}
    if not dom_desc_cfg:
        return detail
    dl_sel = dom_desc_cfg.get("dl_selector", "dl")
    dt_dd_map = dom_desc_cfg.get("dt_dd_map") or {}
    desc_max = int(dom_desc_cfg.get("desc_max", 600))
    rating_label = detail_cfg.get("rating_label", "meta1")
    missing_context = (dom_desc_cfg.get("missing_context") or "N/A").strip()
    dl_pairs = {}
    try:
        if dt_dd_map:
            extracted = page.evaluate(
                """
                (args) => {
                    const mapped = {};
                    const pairs = {};
                    for (const dl of document.querySelectorAll(args.dlSelector)) {
                        let lastDt = null;
                        for (const el of dl.querySelectorAll("dt, dd")) {
                            if (el.tagName === "DT") {
                                lastDt = (el.innerText || "").trim();
                            } else if (el.tagName === "DD" && lastDt !== null) {
                                const value = (el.innerText || "").trim();
                                pairs[lastDt] = value;
                                if (args.dtDdMap[lastDt] && !mapped[args.dtDdMap[lastDt]]) {
                                    mapped[args.dtDdMap[lastDt]] = value;
                                }
                                lastDt = null;
                            }
                        }
                    }
                    return { mapped, pairs };
                }
                """,
                {"dlSelector": dl_sel, "dtDdMap": dt_dd_map},
            ) or {}
            mapped = extracted.get("mapped") or {}
            dl_pairs = extracted.get("pairs") or {}
            for src_field, value in mapped.items():
                if src_field == "description":
                    detail["description"] = (value or detail.get("description", ""))[:desc_max]
                elif src_field == "price":
                    if value:
                        detail["price"] = value
                else:
                    detail[src_field] = value
        panel_data = _extract_summary_panel(page, dom_desc_cfg.get("summary_panel"))
        party_data = _extract_party_panel(page, dom_desc_cfg.get("party_panel"))
        rating_data = _extract_rating_panel(page, dom_desc_cfg.get("rating_panel"))
        detail = _apply_detail_context_merge(
            detail, dom_desc_cfg, dl_pairs, panel_data, party_data, rating_label,
        )
        detail = _attach_ctx_metrics(detail, party_data, rating_data, detail_cfg)
    except Exception:
        if rating_label not in detail:
            detail[rating_label] = missing_context
    return detail


def _enrich_detail_dom_context(page, detail, detail_cfg):
    dom_desc_cfg = detail_cfg.get("dom_desc") or {}
    if not dom_desc_cfg:
        return detail
    rating_label = detail_cfg.get("rating_label", "meta1")
    panel_data = _extract_summary_panel(page, dom_desc_cfg.get("summary_panel"))
    party_data = _extract_party_panel(page, dom_desc_cfg.get("party_panel"))
    rating_data = _extract_rating_panel(page, dom_desc_cfg.get("rating_panel"))
    detail = _apply_detail_context_merge(
        detail, dom_desc_cfg, {}, panel_data, party_data, rating_label,
    )
    return _attach_ctx_metrics(detail, party_data, rating_data, detail_cfg)


def scrape_detail(page, items, detail_cfg):
    ld_type = detail_cfg.get("ld_type", "")
    desc_path = detail_cfg.get("description_path", "description")
    rv_path = detail_cfg.get("rating_value_path")
    rc_path = detail_cfg.get("review_count_path")
    price_path = detail_cfg.get("price_path")
    rating_label = detail_cfg.get("rating_label", "meta1")
    rating_fmt = detail_cfg.get("rating_format", "{v}/{c}")
    use_dom_desc = bool(detail_cfg.get("dom_desc"))
    results = []
    for item in items:
        detail = dict(item)
        try:
            page_url = _detail_page_url(item["url"], detail_cfg)
            page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)
            ld_matched = False
            if ld_type or desc_path or rv_path or rc_path or price_path:
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
                        ld_matched = True
                        break
                    except Exception:
                        continue
            if use_dom_desc:
                if ld_matched:
                    detail = _enrich_detail_dom_context(page, detail, detail_cfg)
                else:
                    detail = _scrape_detail_dom(page, detail, detail_cfg)
        except Exception:
            detail[rating_label] = "N/A"
        if _CTX_METRICS_KEY not in detail:
            detail[_CTX_METRICS_KEY] = {}
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
        PHASE2_PASS_FIELD = (os.environ.get("PHASE2_PASS_FIELD") or "pass").strip()
        PHASE4_PASS_FIELD = (os.environ.get("PHASE4_PASS_FIELD") or "pass").strip()
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
        _log_progress("init done")
        NOTIFY_URL_TEMPLATE = os.environ.get("NOTIFY_URL_TEMPLATE", "") or (
            (parse_cfg.get("page_state") or {}).get("url_template", "")
        )
        new_items = []
        url_by_id = {}
        detailed_items = []
        phase1_diag = _empty_phase1_diagnostics(seen_ids)

        current_phase = "phase1"
        _log_progress("phase1 start")
        with sync_playwright() as p:
            browser, context = _make_browser_context(p)
            pg = context.new_page()
            try:
                new_items, phase1_diag = scrape_listing(
                    pg, parse_cfg, seen_ids, MAX_PAGES, TARGET_URL,
                )
            finally:
                browser.close()

        _log_progress(
            f"phase1 done scraped={phase1_diag['scraped_total']} new={len(new_items)}",
        )
        url_by_id = _url_by_id_from_items(new_items)

        if phase1_diag["scraped_total"] == 0:
            _log_progress("phase1 failed scraped=0")
            send_error_to_slack(
                SLACK_WEBHOOK_URL,
                format_phase1_scrape_failure(phase1_diag),
                current_phase="phase1",
            )
            secure_exit()

        if not new_items:
            _log_progress("phase1 done no new items")
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
            _log_progress(f"phase1 seed items={len(new_items)}")
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

        skill_excluded_before_phase2 = []
        new_items, skill_excluded_before_phase2 = _partition_skill_excluded(new_items)
        if skill_excluded_before_phase2:
            for item, keyword in skill_excluded_before_phase2:
                seen_ids.add(item["id"])
            _log_progress(
                f"skill_exclude phase1 items={len(skill_excluded_before_phase2)}"
                f" keywords={','.join(sorted({kw for _, kw in skill_excluded_before_phase2}))}",
            )

        if not new_items:
            seen_saved = _maybe_save_seen_ids(seen_ids)
            finish_run(
                SLACK_WEBHOOK_URL,
                NOTIFY_PHASE2_REJECTED_HEADER,
                format_run_summary(
                    "phase2_all_rejected",
                    phase1_diag=phase1_diag,
                    phase2_input=len(skill_excluded_before_phase2),
                    phase2_passed=0,
                    pass_field=PHASE2_PASS_FIELD,
                    seen_before=seen_ids_before,
                    seen_after=len(seen_ids),
                    seen_saved=seen_saved,
                    gemini_calls=gemini_calls,
                    sample_max=NOTIFY_SUMMARY_SAMPLE_MAX,
                    url_template=NOTIFY_URL_TEMPLATE,
                    url_by_id=url_by_id,
                ),
            )

        if PROMPT_PHASE2:
            current_phase = "phase2"
            _log_progress(f"phase2 start items={len(new_items)}")
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
            _log_progress(f"phase2 done passed={len(passed2)}")
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
            _log_progress(f"phase3 start items={len(items_for_detail)}")
            with sync_playwright() as p:
                browser, context = _make_browser_context(p)
                pg = context.new_page()
                try:
                    detailed_items = scrape_detail(pg, items_for_detail, detail_cfg)
                finally:
                    browser.close()

            _log_progress(f"phase3 done detailed={len(detailed_items)}")
            skill_excluded_after_detail = []
            detailed_items, skill_excluded_after_detail = _partition_skill_excluded(
                detailed_items,
            )
            if skill_excluded_after_detail:
                for item, keyword in skill_excluded_after_detail:
                    seen_ids.add(item["id"])
                _log_progress(
                    f"skill_exclude phase3 items={len(skill_excluded_after_detail)}"
                    f" keywords={','.join(sorted({kw for _, kw in skill_excluded_after_detail}))}",
                )
            if not detailed_items:
                seen_saved = _maybe_save_seen_ids(seen_ids)
                finish_run(
                    SLACK_WEBHOOK_URL,
                    NOTIFY_PHASE4_REJECTED_HEADER,
                    format_run_summary(
                        "phase4_all_rejected",
                        phase1_diag=phase1_diag,
                        phase2_input=len(new_items) + len(skill_excluded_before_phase2),
                        phase2_passed=len(passed2),
                        phase4_input=len(skill_excluded_after_detail),
                        phase4_passed=0,
                        pass_field=PHASE4_PASS_FIELD,
                        seen_before=seen_ids_before,
                        seen_after=len(seen_ids),
                        seen_saved=seen_saved,
                        gemini_calls=gemini_calls,
                        sample_max=NOTIFY_SUMMARY_SAMPLE_MAX,
                        url_template=NOTIFY_URL_TEMPLATE,
                        url_by_id=url_by_id,
                    ),
                )
            current_phase = "phase4"
            _log_progress(f"phase4 start items={len(detailed_items)}")
            ctx_metrics_by_id = _metrics_by_id(detailed_items)
            phase4_fields = [f.strip() for f in PHASE4_FIELDS.split(",") if f.strip()]
            raw = call_gemini(
                KEYS_STR, MAX_API_USAGE, RESET_HOUR_UTC,
                build_phase_prompt(
                    AI_PROMPT, PROMPT_PHASE4,
                    _strip_internal_scrape_fields(detailed_items),
                ),
                GEMINI_MODEL,
            )
            last_gemini_raw = raw
            gemini_calls += 1
            result4 = validate_json_list(
                raw,
                phase4_fields,
                current_phase,
                pass_field=PHASE4_PASS_FIELD,
                source_items=detailed_items,
                source_fields=("id", "title", "url", "meta1"),
            )
            result4_merged = merge_items_by_id(detailed_items, result4)
            _apply_context_scores(
                result4_merged,
                ctx_metrics_by_id,
                detail_cfg,
                PASS_THRESHOLD,
                PHASE4_PASS_FIELD,
            )
            partial_result = result4_merged
            passed4 = filter_passed(result4_merged, PHASE4_PASS_FIELD)
            _log_progress(f"phase4 done passed={len(passed4)}")
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
                        partial_result=result4_merged,
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
            {"label": "修正指示（5-A）", "key": "risk_revision_guidance"},
            {"label": "備考（5-A・修正に使わない）", "key": "risk_advisory_notes"},
            {"label": "受注期待度", "key": "reception_score"},
            {"label": "背景（5-B）", "key": "buyer_pain_point"},
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
            {"label": "最終・背景（5-B）", "key": "final_buyer_pain_point"},
            {"label": "最終・信頼できる点", "key": "final_credibility_good"},
            {"label": "最終・不安要素", "key": "final_credibility_bad"},
        ]

        if PROMPT_PHASE5:
            url_by_id = {**url_by_id, **_url_by_id_from_items(passed4)}
            phase5_fields = _parse_csv_fields(PHASE5_FIELDS)
            proposal_input = _build_slim_list(passed4, _PROPOSAL_INPUT_KEYS)

            current_phase = "phase5-draft"
            _log_progress(f"phase5-draft start items={len(proposal_input)}")
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
            _log_progress(f"phase5-draft done items={phase5_draft_count}")

            result5a_list = []
            if PROMPT_PHASE5_REVIEW_A:
                current_phase = "phase5-a"
                _log_progress(f"phase5-a start items={len(result5_merged)}")
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
                _log_progress(f"phase5-a done items={phase5a_count}")

            result5b_list = []
            if PROMPT_PHASE5_REVIEW_B:
                current_phase = "phase5-b"
                _log_progress(f"phase5-b start items={len(result5_merged)}")
                review_b_fields = _parse_csv_fields(PHASE5_REVIEW_B_FIELDS)
                party_review_input = _build_slim_list(result5_merged, _PARTY_REVIEW_KEYS)
                raw = call_gemini(
                    KEYS_STR, MAX_API_USAGE, RESET_HOUR_UTC,
                    build_phase_prompt("", PROMPT_PHASE5_REVIEW_B, party_review_input),
                    GEMINI_MODEL,
                )
                last_gemini_raw = raw
                gemini_calls += 1
                result5b_list = validate_json_list(raw, review_b_fields, current_phase)
                phase5b_count = len(result5b_list)
                _log_progress(f"phase5-b done items={phase5b_count}")

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
                _log_progress(f"phase6 start items={len(result5)}")
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
                _log_progress(f"phase6 done items={phase6_count}")
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
                _log_progress(f"phase7-a start items={len(result5)}")
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
                _log_progress(f"phase7-a done items={phase7a_count}")

            result7b_list = []
            if PROMPT_PHASE7_REVIEW_B:
                current_phase = "phase7-b"
                _log_progress(f"phase7-b start items={len(result5)}")
                phase7b_fields = _parse_csv_fields(PHASE7_REVIEW_B_FIELDS)
                party_review7_input = _build_slim_list(result5, _PARTY_REVIEW_KEYS)
                raw = call_gemini(
                    KEYS_STR, MAX_API_USAGE, RESET_HOUR_UTC,
                    build_phase_prompt("", PROMPT_PHASE7_REVIEW_B, party_review7_input),
                    GEMINI_MODEL,
                )
                last_gemini_raw = raw
                gemini_calls += 1
                result7b_list = validate_json_list(raw, phase7b_fields, current_phase)
                phase7b_count = len(result7b_list)
                _log_progress(f"phase7-b done items={phase7b_count}")

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
            _log_progress("phase5 empty")
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
            if not _pass_value_is_true(entry.get((notify_pass_field or "pass").strip())):
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

        _log_progress(
            f"run summary notified={notified_count} gemini_calls={gemini_calls}",
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
        _log_progress(f"run failed phase={current_phase}")
        traceback.print_exc()
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
