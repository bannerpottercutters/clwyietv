#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""matsca 生图 CLI（官方插件策略无头移植版）。

接收提示词 → 调用 https://img.matsca.com → 多 Key 健康度调度 / 两层重试 /
逐 Key 冷却 / 故障转移 / 赛马 / 覆盖优先 / 受阻侦测 → 图像落盘 → 增量写 manifest.json。

纯标准库实现，无第三方依赖。
"""

import argparse
import base64
import hashlib
import json
import os
import queue
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
API_BASE = os.environ.get("MATSCA_API_BASE", "https://img.matsca.com")

RETRYABLE_HTTP_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524}
TRANSIENT_ERROR_CODES = {
    "upstream_timeout", "upstream_unreachable", "upstream_session_pool_exhausted",
    "api_agent_queue_full", "upstream_rate_limited", "no_available_account",
    "account_concurrency_exhausted", "upstream_direct_unavailable",
    "upstream_server_error", "upstream_error", "empty_response",
}
KEY_FAILOVER_ERROR_CODES = {"account_token_invalid", "image_permission_unavailable"}
COOLDOWN_ERROR_CODES = {
    "upstream_rate_limited", "account_concurrency_exhausted",
    "api_agent_queue_full", "upstream_session_pool_exhausted", "no_available_account",
}
COOLDOWN_STATUSES = {401, 429, 503}

# 容量类错误归类（受阻侦测）
CAP_5XX_STATUSES = {500, 502, 503, 504, 520, 522, 524}
CAP_5XX_CODES = {"upstream_server_error", "upstream_error", "upstream_timeout",
                 "upstream_unreachable", "upstream_direct_unavailable"}
CAP_429_CODES = {"upstream_rate_limited", "account_concurrency_exhausted",
                 "api_agent_queue_full", "upstream_session_pool_exhausted", "no_available_account"}

# 并发/重试/冷却常量
QUEUE_CONCURRENCY_PER_KEY = 2
IMAGE_REQUEST_CONCURRENCY_PER_KEY = 2
GLOBAL_IMAGE_REQUEST_CONCURRENCY = 6
MAX_TRANSIENT_TASK_RETRIES = 4
TASK_RETRY_BASE_MS = 5000
TASK_RETRY_MAX_MS = 120000
KEY_COOLDOWN_BASE_MS = 6000
KEY_COOLDOWN_MAX_MS = 120000
HTTP_RETRIES_IMAGE = 2
RETRY_AFTER_CAP_MS = 120000
SCHEDULER_TICK_S = 1.0
IMAGE_TIMEOUT_S = 600
PING_TIMEOUT_S = 8
BLOCK_NO_PROGRESS_MS = 90000
CAP_WINDOW_MS = 120000

_PARAM_KEYS = ("quality", "moderation", "background", "style",
               "output_format", "output_compression", "input_fidelity")


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def _now_ms():
    return int(time.time() * 1000)


def _utc_ts():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def log(msg):
    sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    sys.stderr.flush()


# --------------------------------------------------------------------------- #
# 错误封装 / 解析
# --------------------------------------------------------------------------- #
class RequestError(Exception):
    def __init__(self, message, status=0, code="", raw="", retry_after_ms=0):
        super().__init__(message)
        self.status = int(status or 0)
        self.code = code or ""
        self.raw = raw or ""
        self.retry_after_ms = max(0, int(retry_after_ms or 0))


def _extract_error_code(payload):
    if not isinstance(payload, dict):
        return ""
    if isinstance(payload.get("code"), str):
        return payload["code"]
    for key in ("detail", "error"):
        node = payload.get(key)
        if isinstance(node, dict) and isinstance(node.get("code"), str):
            return node["code"]
    return ""


def _extract_error_message(payload, fallback):
    if not isinstance(payload, dict):
        return fallback
    cands = []
    detail = payload.get("detail")
    error = payload.get("error")
    if isinstance(detail, str):
        cands.append(detail)
    if isinstance(error, str):
        cands.append(error)
    if isinstance(payload.get("message"), str):
        cands.append(payload["message"])
    if isinstance(detail, dict):
        if isinstance(detail.get("error"), str):
            cands.append(detail["error"])
        if isinstance(detail.get("error"), dict) and isinstance(detail["error"].get("message"), str):
            cands.append(detail["error"]["message"])
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        cands.append(error["message"])
    for c in cands:
        if str(c).strip():
            return str(c)
    return fallback


def _retry_after_ms_from_headers(headers):
    if not headers:
        return 0
    val = headers.get("Retry-After")
    if not val:
        return 0
    try:
        secs = float(val)
        if secs >= 0:
            return min(RETRY_AFTER_CAP_MS, int(secs * 1000))
    except (TypeError, ValueError):
        pass
    return 0


def _parse_json(raw):
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        text = raw.decode("utf-8", "replace")
        text = re.sub(r"<[^>]+>", " ", text)        # 去 HTML 标签
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > 200:
            text = text[:200] + "…"
        return {"message": text or "非 JSON 响应"}


# --------------------------------------------------------------------------- #
# HTTP 层
# --------------------------------------------------------------------------- #
def _http(method, path_or_url, headers=None, body=None, timeout=None,
          retries=2, retry_delay_ms=1200, expect_json=True):
    url = path_or_url if path_or_url.startswith("http") else (API_BASE + path_or_url)
    timeout = timeout or IMAGE_TIMEOUT_S
    hdrs = dict(headers or {})
    data = None
    if body is not None:
        if isinstance(body, (bytes, bytearray)):
            data = bytes(body)
        else:
            data = json.dumps(body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")

    last_exc = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                status = resp.getcode()
                return status, dict(resp.headers), raw
        except urllib.error.HTTPError as e:
            raw = e.read()
            status = e.code
            headers_map = dict(e.headers or {})
            if attempt < retries and status in RETRYABLE_HTTP_STATUSES:
                delay = _retry_after_ms_from_headers(headers_map)
                if not delay:
                    base = max(10000, retry_delay_ms) if status == 429 else retry_delay_ms * (2 ** attempt)
                    delay = min(30000, int(base + random.random() * 600))
                time.sleep(delay / 1000.0)
                continue
            return status, headers_map, raw
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_exc = e
            if attempt < retries:
                time.sleep((retry_delay_ms * (2 ** attempt)) / 1000.0)
                continue
            raise RequestError("网络请求失败：%s" % e, status=0, code="network_error")
    raise RequestError("网络请求失败：%s" % last_exc, status=0, code="network_error")


def api_json(method, path, key=None, body=None, timeout=None, retries=2,
             retry_delay_ms=1200, extra_headers=None):
    headers = dict(extra_headers or {})
    if key:
        headers["Authorization"] = "Bearer %s" % key
    status, resp_headers, raw = _http(method, path, headers=headers, body=body,
                                      timeout=timeout, retries=retries,
                                      retry_delay_ms=retry_delay_ms)
    payload = _parse_json(raw)
    if not (200 <= status < 300):
        raise RequestError(
            _extract_error_message(payload, "请求失败 (%d)" % status),
            status=status,
            code=_extract_error_code(payload),
            raw=_extract_error_message(payload, ""),
            retry_after_ms=_retry_after_ms_from_headers(resp_headers),
        )
    return payload


# --------------------------------------------------------------------------- #
# multipart / 图片处理
# --------------------------------------------------------------------------- #
def _multipart(fields, files):
    boundary = "----matsca%d" % _now_ms()
    out = bytearray()
    for name, value in fields.items():
        if value is None:
            continue
        out += ("--%s\r\n" % boundary).encode()
        out += ('Content-Disposition: form-data; name="%s"\r\n\r\n' % name).encode()
        out += ("%s\r\n" % value).encode()
    for name, (filename, content) in files.items():
        out += ("--%s\r\n" % boundary).encode()
        out += ('Content-Disposition: form-data; name="%s"; filename="%s"\r\n' % (name, filename)).encode()
        out += b"Content-Type: application/octet-stream\r\n\r\n"
        out += content + b"\r\n"
    out += ("--%s--\r\n" % boundary).encode()
    return bytes(out), "multipart/form-data; boundary=%s" % boundary


def detect_image_ext(data, fmt_hint=""):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    fmt = (fmt_hint or "").lower().strip().replace("jpeg", "jpg")
    return fmt if fmt in {"png", "jpg", "webp"} else "png"


def _download_image(url, timeout=IMAGE_TIMEOUT_S, proxy=None):
    if url.startswith("data:"):
        header, _, payload = url.partition(",")
        if ";base64" not in header or not payload:
            raise RequestError("不支持的 data URI 图片", code="bad_data_uri")
        return base64.b64decode(payload)
    handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {})
    opener = urllib.request.build_opener(handler)
    req = urllib.request.Request(url, headers={"User-Agent": "matsca-image-gen/1.0"})
    with opener.open(req, timeout=timeout) as r:
        return r.read()


def _extract_images(payload, timeout, proxy=None):
    out = []
    data = payload.get("data") if isinstance(payload, dict) else None
    for item in (data or []):
        if not isinstance(item, dict):
            continue
        hint = item.get("output_format") or ""
        if item.get("b64_json"):
            raw = base64.b64decode(item["b64_json"])
        elif item.get("url"):
            raw = _download_image(item["url"], timeout=timeout, proxy=proxy)
        else:
            continue
        out.append({"data": raw, "format": detect_image_ext(raw, hint)})
    return out


def _safe_name(text, max_len=80):
    s = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff._-]+", "-", (text or "").strip()).strip("-._")
    if not s:
        s = hashlib.sha1(str(uuid.uuid4()).encode("ascii")).hexdigest()[:12]
    return s[:max_len]


# --------------------------------------------------------------------------- #
# 端点封装
# --------------------------------------------------------------------------- #
def ping_server(key):
    return api_json("GET", "/v1/ping", key=key, timeout=PING_TIMEOUT_S, retries=0)


def generate_image(key, payload, timeout=IMAGE_TIMEOUT_S):
    return api_json("POST", "/v1/images/generations", key=key, body=payload,
                    timeout=timeout, retries=HTTP_RETRIES_IMAGE, retry_delay_ms=1200)


def edit_image(key, fields, files, timeout=IMAGE_TIMEOUT_S):
    body, content_type = _multipart(fields, files)
    return api_json("POST", "/v1/images/edits", key=key,
                    body=body, timeout=timeout, retries=HTTP_RETRIES_IMAGE,
                    retry_delay_ms=1200, extra_headers={"Content-Type": content_type})


def variation_image(key, fields, files, timeout=IMAGE_TIMEOUT_S):
    body, content_type = _multipart(fields, files)
    return api_json("POST", "/v1/images/variations", key=key,
                    body=body, timeout=timeout, retries=HTTP_RETRIES_IMAGE,
                    retry_delay_ms=1200, extra_headers={"Content-Type": content_type})


# --------------------------------------------------------------------------- #
# 凭据解析 / 自愈 / 回写
# --------------------------------------------------------------------------- #
def _read_secrets_file(path):
    keys = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() in ("MATSCA_API_KEY", "MATSCA_API_KEYS"):
                    keys += [x.strip() for x in v.strip().strip('"').split(",") if x.strip()]
    except OSError:
        pass
    return keys


def _candidate_secrets_files(explicit):
    cands = []
    if explicit:
        cands.append(explicit)
    if os.environ.get("MATSCA_SECRETS_FILE"):
        cands.append(os.environ["MATSCA_SECRETS_FILE"])
    cands += ["secrets.env", os.path.expanduser("~/720_Agents/secrets.env"),
              os.path.expanduser("~/secrets.env")]
    seen, out = set(), []
    for c in cands:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _read_dev_creds(path):
    email = pw = ""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"')
                if k == "MATSCA_DEV_EMAIL":
                    email = v
                elif k == "MATSCA_DEV_PASSWORD":
                    pw = v
    except OSError:
        return None
    return (email, pw) if email and pw else None


def save_keys_to_secrets(path, raw_keys):
    line = "MATSCA_API_KEYS=" + ",".join(raw_keys)
    kept, replaced = [], False
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for raw in f.read().splitlines():
                if raw.strip().split("=", 1)[0].strip() in ("MATSCA_API_KEYS", "MATSCA_API_KEY"):
                    if not replaced:
                        kept.append(line)
                        replaced = True
                    continue
                kept.append(raw)
    if not replaced:
        kept.append("# 由 matsca-image-gen 自动回写（dev 登录 reveal 的新鲜 Key）")
        kept.append(line)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(kept) + "\n")
    try:
        os.chmod(tmp, 0o600)  # 尽量收紧权限（Windows 上无效，忽略）
    except OSError:
        pass
    os.replace(tmp, path)
    log("已把 %d 把新鲜 Key 回写本地 secrets：%s" % (len(raw_keys), path))


def _reveal_keys_via_token(token):
    auth = {"Authorization": "Bearer %s" % token}
    me = api_json("GET", "/api/dev/me", extra_headers=auth)
    keys = me.get("keys") if isinstance(me, dict) else None
    out = []
    for k in (keys or []):
        kid = str(k.get("id") or "")
        relay = bool(k.get("relay_mode"))
        native = bool(k.get("native_mode"))
        if not relay and not native:
            try:
                toggled = api_json("POST", "/api/dev/keys/%s/relay" % kid,
                                   extra_headers=auth, body={})
                relay = bool(toggled.get("relay_mode", True))
                native = bool(toggled.get("native_mode", False))
            except RequestError as e:
                log("开启 relay 失败 key=%s: %s" % (kid, e))
        if not relay and not native:
            continue
        try:
            revealed = api_json("GET", "/api/dev/keys/%s/reveal" % kid, extra_headers=auth)
            raw = str(revealed.get("key") or "")
            if raw:
                out.append((kid, raw))
        except RequestError as e:
            log("reveal 失败 key=%s: %s" % (kid, e))
    return out


def _resolve_dev_creds(args):
    token = args.dev_token or os.environ.get("MATSCA_DEV_TOKEN")
    email = args.email or os.environ.get("MATSCA_DEV_EMAIL")
    password = args.password or os.environ.get("MATSCA_DEV_PASSWORD")
    sf_used = None
    if not token and not (email and password):
        for sf in _candidate_secrets_files(args.secrets_file):
            creds = _read_dev_creds(sf)
            if creds:
                email, password = email or creds[0], password or creds[1]
                sf_used = sf
                break
    return token, email, password, sf_used


def _reveal_via_creds(token, email, password):
    if not token and email and password:
        payload = api_json("POST", "/api/dev/login", retries=4,
                           body={"email": email, "password": password})
        token = str(payload.get("token") or payload.get("dev_token") or "")
    return _reveal_keys_via_token(token) if token else []


def _any_key_healthy(keys):
    ok = {}

    def _probe(kid, raw):
        try:
            payload = ping_server(raw)
            auth = payload.get("auth") if isinstance(payload, dict) else {}
            ok[kid] = not bool((auth or {}).get("banned"))
        except RequestError:
            ok[kid] = False
    threads = [threading.Thread(target=_probe, args=(kid, raw), daemon=True)
               for kid, raw in keys]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return any(ok.values())


def resolve_keys(args):
    args._keys_from_reveal = False
    args._secrets_target = None
    raw_keys = []
    if args.keys:
        raw_keys = [k.strip() for k in args.keys.split(",") if k.strip()]
    if not raw_keys:
        env_keys = os.environ.get("MATSCA_API_KEYS")
        if env_keys:
            raw_keys = [k.strip() for k in env_keys.split(",") if k.strip()]
            if raw_keys:
                log("从环境变量 MATSCA_API_KEYS 读到 %d 把 Key" % len(raw_keys))
    if not raw_keys:
        for sf in _candidate_secrets_files(args.secrets_file):
            raw_keys = _read_secrets_file(sf)
            if raw_keys:
                log("从 secrets 文件读到 %d 把 Key：%s" % (len(raw_keys), sf))
                args._secrets_target = sf
                break

    if raw_keys:
        # 去重 + 生成 key_id
        seen, out = set(), []
        for i, k in enumerate(raw_keys):
            if k in seen:
                continue
            seen.add(k)
            out.append(("key%d-%s" % (i + 1, k[-4:]), k))
        # 静态 Key 可能已过期；有 dev 账号兜底时先验活
        token, email, password, sf = _resolve_dev_creds(args)
        if token or (email and password):
            if _any_key_healthy(out):
                return out
            log("静态 Key 全部失效，改用 dev 账号自动登录 reveal 新鲜 Key")
            try:
                revealed = _reveal_via_creds(token, email, password)
            except RequestError as e:
                log("dev 登录/reveal 失败（%s）；暂用原静态 Key 继续，由调度层重试/受阻侦测兜底" % e)
                revealed = []
            if revealed:
                args._keys_from_reveal = True
                if not args._secrets_target:
                    args._secrets_target = sf
                return revealed
            log("dev reveal 未取到新鲜 Key，沿用原静态 Key（可能都已失效）")
        return out

    # 没有任何静态 Key：纯靠 dev 登录 reveal
    token, email, password, sf = _resolve_dev_creds(args)
    if sf:
        log("从 secrets 文件读到 dev 账号：%s" % sf)
        args._secrets_target = sf
    try:
        revealed = _reveal_via_creds(token, email, password)
    except RequestError as e:
        log("dev 登录/reveal 失败：%s" % e)
        revealed = []
    if revealed:
        args._keys_from_reveal = True
    return revealed


# --------------------------------------------------------------------------- #
# 任务模型
# --------------------------------------------------------------------------- #
class Task:
    def __init__(self, index, name, prompt, n, size, model, aspect=None,
                 edit_path=None, mask_path=None, var_path=None, params=None):
        self.index = index
        self.name = name
        self.prompt = prompt
        raw_n = int(n)
        self.n = max(1, min(4, raw_n))       # 钳制到 1~4
        if raw_n != self.n:
            log("注意 [%s]：每内容数量 n=%d 越界，已钳制到 %d（有效范围 1~4）" % (name, raw_n, self.n))
        self.need = self.n                    # 想要的总图数
        self.size = size
        self.model = model
        self.aspect = aspect
        self.edit_path = edit_path
        self.mask_path = mask_path            # 改图蒙版（配合 edit_path）
        self.var_path = var_path              # 图生图/变体原图
        self.params = params or {}            # 透传参数白名单
        # runtime
        self.status = "waiting"               # waiting/running/completed/failed
        self.preferred_key = None             # 钉死的首选 key_id
        self.key_id = None
        self.retry_count = 0
        self.retry_after_ms = 0               # 早于此时间不调度
        self.error = ""
        self.results = []                     # [{path,bytes,format,role}]，0=主图
        # 赛马/覆盖优先运行态
        self.got = 0                          # 已落盘图数
        self.inflight = 0                     # 该内容当前在飞请求数
        self.inflight_images = 0              # 在飞请求预期产出的图数
        self.issued = 0                       # 累计派发请求数
        self.exhausted = False                # 不可重试/重试耗尽


class KeyHealth:
    def __init__(self, key_id, raw):
        self.key_id = key_id
        self.raw = raw
        self.cooldown_until_ms = 0
        self.error_streak = 0
        self.running = 0                      # 该 key 上 running 的任务数


# --------------------------------------------------------------------------- #
# 提示词比例注入
# --------------------------------------------------------------------------- #
def _prompt_for_upstream(prompt, size, aspect):
    if aspect and aspect != "auto" and (not size or size == "auto"):
        return "%s\n（画面比例：%s）" % (prompt, aspect)
    return prompt


def _cap_kind(err):
    s = getattr(err, "status", 0)
    c = getattr(err, "code", "")
    if s in CAP_5XX_STATUSES or c in CAP_5XX_CODES:
        return "5xx"
    if s == 429 or c in CAP_429_CODES:
        return "429"
    return None


# --------------------------------------------------------------------------- #
# 调度器
# --------------------------------------------------------------------------- #
class Scheduler:
    def __init__(self, tasks, keyhealths, outdir, timeout, preflight_ping=False,
                 proxy=None, race=False, coverage_first=False,
                 block_after_ms=BLOCK_NO_PROGRESS_MS, give_up_after_ms=0, ping_refine=True,
                 date_suffix=True):
        self.tasks = tasks
        self.keys = {kh.key_id: kh for kh in keyhealths}
        self.key_order = [kh.key_id for kh in keyhealths]
        self.outdir = outdir
        self.timeout = timeout
        self.preflight_ping = preflight_ping
        self.proxy = proxy
        self.race = race
        self.coverage_first = coverage_first
        self.block_after_ms = block_after_ms
        self.give_up_after_ms = give_up_after_ms
        self.ping_refine = ping_refine
        self.date_suffix = date_suffix
        self.date = time.strftime("%Y%m%d")
        self.lock = threading.Lock()
        self.events = queue.Queue()
        self.global_inflight = 0
        self.per_key_inflight = {kid: 0 for kid in self.keys}
        # 受阻侦测运行态
        self.start_ms = _now_ms()
        self.last_progress_ms = self.start_ms
        self.recent_caps = []            # [(ts_ms, '5xx'|'429')]
        self.blocked = False
        self.block_reason = ""
        self.blocked_since_ms = 0
        self.gave_up = False

    # ---- 冷却 ---- #
    def _cooldown_remaining(self, kid):
        kh = self.keys[kid]
        rem = kh.cooldown_until_ms - _now_ms()
        if rem <= 0:
            if kh.cooldown_until_ms:
                kh.cooldown_until_ms = 0
            return 0
        return rem

    def _clear_cooldown(self, kid):
        kh = self.keys[kid]
        kh.cooldown_until_ms = 0
        kh.error_streak = 0

    # ---- 健康分 / Key 选择 ---- #
    def _key_score(self, kid, preferred):
        kh = self.keys[kid]
        running = kh.running
        inflight = self.per_key_inflight.get(kid, 0)
        return (running * 1000 + inflight * 160 + kh.error_streak * 90
                - (120 if kid == preferred else 0))

    def _runnable_key_for(self, task):
        candidates = []
        for kid in self.key_order:
            kh = self.keys[kid]
            if self._cooldown_remaining(kid) > 0:
                continue
            if kh.running >= QUEUE_CONCURRENCY_PER_KEY:
                continue
            if self.global_inflight >= GLOBAL_IMAGE_REQUEST_CONCURRENCY:
                continue
            candidates.append(kid)
        if not candidates:
            return None
        pref = task.preferred_key
        if pref in candidates:
            return pref
        candidates.sort(key=lambda k: self._key_score(k, pref))
        return candidates[0]

    def _alternate_key(self, kid):
        others = [k for k in self.key_order if k != kid]
        if not others:
            return None
        free = [k for k in others if self._cooldown_remaining(k) <= 0]
        pool = free or others
        pool.sort(key=lambda k: self._key_score(k, None))
        return pool[0]

    # ---- 派发 ---- #
    def _remaining(self, task):
        return task.need - task.got - task.inflight_images

    def _next_demand_task(self, now):
        cands = []
        for t in self.tasks:
            if t.status in ("completed", "failed") or t.exhausted:
                continue
            if t.retry_after_ms and t.retry_after_ms > now:
                continue
            if self._remaining(t) <= 0:
                continue
            cands.append(t)
        if not cands:
            return None
        if self.coverage_first:
            cands.sort(key=lambda t: (1 if (t.got + t.inflight_images) >= 1 else 0, t.index))
        else:
            cands.sort(key=lambda t: t.index)
        return cands[0]

    def _dispatch_locked(self):
        now = _now_ms()
        while self.global_inflight < GLOBAL_IMAGE_REQUEST_CONCURRENCY:
            task = self._next_demand_task(now)
            if task is None:
                break
            kid = self._runnable_key_for(task)
            if not kid:
                break
            remaining = self._remaining(task)
            n = 1 if self.race else remaining
            if task.preferred_key is None:
                task.preferred_key = kid
            task.key_id = kid
            task.status = "running"
            task.inflight += 1
            task.inflight_images += n
            task.issued += 1
            self.keys[kid].running += 1
            self.per_key_inflight[kid] += 1
            self.global_inflight += 1
            threading.Thread(target=self._worker, args=(task, kid, n), daemon=True).start()

    # ---- worker ---- #
    def _worker(self, task, kid, n):
        kh = self.keys[kid]
        try:
            up_prompt = _prompt_for_upstream(task.prompt, task.size, task.aspect)
            if task.var_path:
                with open(task.var_path, "rb") as f:
                    img = f.read()
                fields = {"n": str(n), "response_format": "b64_json", "model": task.model}
                if up_prompt:
                    fields["prompt"] = up_prompt
                for k, v in task.params.items():
                    fields[k] = str(v)
                payload = variation_image(kh.raw, fields,
                                          {"image": (os.path.basename(task.var_path), img)},
                                          timeout=self.timeout)
            elif task.edit_path:
                with open(task.edit_path, "rb") as f:
                    img = f.read()
                fields = {"model": task.model, "size": task.size, "n": str(n),
                          "response_format": "b64_json",
                          "prompt": up_prompt or "基于已上传图片生成自然一致的图片"}
                for k, v in task.params.items():
                    fields[k] = str(v)
                files = {"image": (os.path.basename(task.edit_path), img)}
                if task.mask_path:
                    with open(task.mask_path, "rb") as mf:
                        files["mask"] = (os.path.basename(task.mask_path), mf.read())
                payload = edit_image(kh.raw, fields, files, timeout=self.timeout)
            else:
                body = {"model": task.model, "prompt": up_prompt, "n": n,
                        "size": task.size, "response_format": "b64_json"}
                body.update(task.params)
                payload = generate_image(kh.raw, body, timeout=self.timeout)
            images = _extract_images(payload, self.timeout, proxy=self.proxy)
            if not images:
                raise RequestError("无结果返回（疑空返回/服务异常）", code="empty_response")
            self.events.put(("ok", task, kid, n, images))
        except RequestError as e:
            self.events.put(("err", task, kid, n, e))
        except Exception as e:
            self.events.put(("err", task, kid, n, RequestError(str(e), code="worker_error")))

    # ---- 落盘 ---- #
    def _result_path(self, task, position, ext):
        base = _safe_name(task.name)
        if self.date_suffix:
            base = "%s_%s" % (base, self.date)
        if position == 0:
            fname = "%s.%s" % (base, ext)
        else:
            fname = "%s（备%d）.%s" % (base, position, ext)
        return os.path.join(self.outdir, fname)

    def _save_results(self, task, images):
        saved = 0
        for im in images:
            position = len(task.results)
            ext = im["format"]
            path = self._result_path(task, position, ext)
            role = "primary" if position == 0 else "backup"
            try:
                with open(path, "wb") as f:
                    f.write(im["data"])
                task.results.append({"path": path, "bytes": os.path.getsize(path),
                                     "format": ext, "role": role})
                saved += 1
            except OSError as e:
                log("保存失败 %s: %s" % (os.path.basename(path), e))
        return saved

    # ---- 事件处理 ---- #
    def _handle_event(self, ev):
        kind, task, kid, n, data = ev
        with self.lock:
            now = _now_ms()
            self.keys[kid].running = max(0, self.keys[kid].running - 1)
            self.per_key_inflight[kid] = max(0, self.per_key_inflight[kid] - 1)
            self.global_inflight = max(0, self.global_inflight - 1)
            task.inflight = max(0, task.inflight - 1)
            task.inflight_images = max(0, task.inflight_images - n)
            if kind == "ok":
                self._clear_cooldown(kid)
                saved = self._save_results(task, data)
                task.got += saved
                if saved > 0:
                    self.last_progress_ms = now
                    self.recent_caps = []
                    if self.blocked:
                        self.blocked = False
                        self.block_reason = ""
                        log(">>> IMAGE_GEN_RECOVERED <<<")
                log("出图 [%s] via %s：+%d 张（累计 %d/%d）" % (
                    task.name, kid, saved, task.got, task.need))
            else:
                kcap = _cap_kind(data)
                if kcap:
                    self.recent_caps.append((now, kcap))
                self._on_error(task, kid, data)
            self._maybe_finalize(task)
            try:
                write_manifest(self.outdir, self.tasks, self._block_status_locked())
            except OSError as e:
                log("增量写 manifest 失败（忽略）：%s" % e)

    def _maybe_finalize(self, task):
        if task.status == "completed":
            return
        if task.got >= task.need:
            task.status = "completed"
            return
        if task.got >= 1:
            task.status = "completed" if (task.exhausted and task.inflight == 0) else "waiting"
            return
        if task.exhausted and task.inflight == 0:
            if task.status != "failed":
                task.status = "failed"
                log("失败 [%s]：%s" % (task.name, task.error or "无图片返回"))
        else:
            task.status = "waiting"

    def _on_error(self, task, kid, err):
        code = err.code
        status = err.status
        transient = (status in RETRYABLE_HTTP_STATUSES or code in TRANSIENT_ERROR_CODES)
        key_specific = (code in KEY_FAILOVER_ERROR_CODES or status == 401)
        task.error = str(err)
        if not (transient or key_specific) or task.retry_count >= MAX_TRANSIENT_TASK_RETRIES:
            task.exhausted = True
            log("不再重试 [%s] via %s：%s (%s/%s)" % (task.name, kid, err, status, code))
            return

        cooldown = self._apply_cooldown(kid, err)
        task.retry_count += 1
        task.status = "waiting"

        alt = self._alternate_key(kid) if key_specific else None
        if alt is not None:
            task.preferred_key = alt
            task.retry_after_ms = _now_ms()  # 立即转移
            log("故障转移 [%s]：%s (%s/%s) → 改用 %s" % (task.name, kid, status, code, alt))
            return

        base = TASK_RETRY_BASE_MS * (2 ** min(5, task.retry_count))
        retry_delay = err.retry_after_ms or min(TASK_RETRY_MAX_MS, int(base + random.random() * 1800))
        task.retry_after_ms = _now_ms() + max(retry_delay, cooldown)
        log("临时失败 [%s] via %s (%s/%s)，第 %d 次重试将在 %.1fs 后" % (
            task.name, kid, status, code, task.retry_count,
            (task.retry_after_ms - _now_ms()) / 1000.0))

    def _apply_cooldown(self, kid, err):
        kh = self.keys[kid]
        status = getattr(err, "status", 0)
        code = getattr(err, "code", "")
        should = (status in COOLDOWN_STATUSES or code in COOLDOWN_ERROR_CODES
                  or code in KEY_FAILOVER_ERROR_CODES)
        if not should:
            return 0
        now = _now_ms()
        active = kh.cooldown_until_ms > now
        kh.error_streak = max(1, kh.error_streak) if active else kh.error_streak + 1
        retry_after = getattr(err, "retry_after_ms", 0)
        backoff = retry_after or KEY_COOLDOWN_BASE_MS * (2 ** min(5, kh.error_streak - 1))
        delay = min(KEY_COOLDOWN_MAX_MS, int(backoff + random.random() * 2000))
        kh.cooldown_until_ms = max(kh.cooldown_until_ms, now + delay)
        return max(0, kh.cooldown_until_ms - now)

    # ---- 受阻侦测 ---- #
    def _any_key_banned(self):
        banned = {}

        def _probe(kid, kh):
            try:
                payload = ping_server(kh.raw)
                auth = payload.get("auth") if isinstance(payload, dict) else None
                if isinstance(auth, dict) and auth.get("banned"):
                    log("受阻 ping：Key %s 处于封禁（ban_remaining≈%ss）" % (
                        kid, int(auth.get("ban_remaining_seconds") or 0)))
                    banned[kid] = True
            except RequestError as e:
                log("受阻 ping 失败 key=%s: %s（忽略）" % (kid, e))
        threads = [threading.Thread(target=_probe, args=(kid, kh), daemon=True)
                   for kid, kh in self.keys.items()]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return any(banned.values())

    def _check_blocked(self):
        now = _now_ms()
        with self.lock:
            pending = any(t.status not in ("completed", "failed") for t in self.tasks)
            if not pending:
                self.blocked = False
                return
            self.recent_caps = [(ts, k) for ts, k in self.recent_caps if now - ts <= CAP_WINDOW_MS]
            no_progress = now - self.last_progress_ms
            all_cooling = bool(self.keys) and all(
                self._cooldown_remaining(k) > 0 for k in self.keys)
            recent = list(self.recent_caps)
            should = no_progress >= self.block_after_ms and (bool(recent) or all_cooling)
            n5 = sum(1 for _, k in recent if k == "5xx")
            n4 = sum(1 for _, k in recent if k == "429")
            reason = "502_storm" if n5 > n4 else "429_congestion"
            was_blocked = self.blocked
            give_up = (self.give_up_after_ms > 0 and was_blocked
                       and now - self.blocked_since_ms >= self.give_up_after_ms)

        if should and not was_blocked:
            if self.ping_refine and self._any_key_banned():
                reason = "key_banned"
            with self.lock:
                self.blocked = True
                self.block_reason = reason
                self.blocked_since_ms = now
            log(">>> IMAGE_GEN_BLOCKED: %s <<<" % reason)
            try:
                write_manifest(self.outdir, self.tasks, self._block_status())
            except OSError:
                pass

        if give_up:
            with self.lock:
                for t in self.tasks:
                    if t.status not in ("completed", "failed"):
                        t.status = "completed" if t.got >= 1 else "failed"
                        if not t.error:
                            t.error = "blocked_give_up"
                self.gave_up = True
            log(">>> IMAGE_GEN_GAVE_UP: 受阻持续超过 %ds，提前收尾 <<<" % int(
                self.give_up_after_ms / 1000))
            try:
                write_manifest(self.outdir, self.tasks, self._block_status())
            except OSError:
                pass

    def _block_status_locked(self):
        return {"blocked": self.blocked, "block_reason": self.block_reason,
                "blocked_since": (_utc_ts() if self.blocked_since_ms else None),
                "no_progress_seconds": int((_now_ms() - self.last_progress_ms) / 1000),
                "gave_up": self.gave_up}

    def _block_status(self):
        with self.lock:
            return self._block_status_locked()

    # ---- 预检 / 刷新 ---- #
    def _preflight(self):
        for kid, kh in self.keys.items():
            try:
                payload = ping_server(kh.raw)
                auth = payload.get("auth") if isinstance(payload, dict) else None
                if isinstance(auth, dict) and auth.get("banned"):
                    rem = int(auth.get("ban_remaining_seconds") or 0)
                    kh.cooldown_until_ms = _now_ms() + max(1000, rem * 1000)
                    log("预检 ping：Key %s 临时受限，约 %ds 后恢复" % (kid, rem))
                else:
                    log("预检 ping：Key %s 可用" % kid)
            except RequestError as e:
                log("预检 ping 失败 key=%s: %s（忽略，继续）" % (kid, e))

    def _progress_snapshot(self):
        with self.lock:
            tasks = tuple((t.got, t.status, t.retry_count) for t in self.tasks)
        return (tasks, self.blocked, self.gave_up)

    def _refresh_manifest(self, last_snapshot):
        if self._progress_snapshot() == last_snapshot:
            return
        try:
            write_manifest(self.outdir, self.tasks, self._block_status())
        except OSError as e:
            log("刷新 manifest 失败（忽略）：%s" % e)

    # ---- 主循环 ---- #
    def run(self):
        os.makedirs(self.outdir, exist_ok=True)
        try:
            write_manifest(self.outdir, self.tasks, self._block_status())
        except OSError as e:
            log("写初始 manifest 失败（忽略）：%s" % e)
        if self.preflight_ping:
            self._preflight()
        last_snapshot = None
        while True:
            with self.lock:
                self._dispatch_locked()
                done = all(t.status in ("completed", "failed") for t in self.tasks)
                inflight = any(t.inflight > 0 for t in self.tasks)
            if done and not inflight:
                break
            self._check_blocked()
            if self.gave_up:
                break
            try:
                ev = self.events.get(timeout=SCHEDULER_TICK_S)
            except queue.Empty:
                self._refresh_manifest(last_snapshot)
                last_snapshot = self._progress_snapshot()
                continue
            self._handle_event(ev)
            self._refresh_manifest(last_snapshot)
            last_snapshot = self._progress_snapshot()


# --------------------------------------------------------------------------- #
# manifest
# --------------------------------------------------------------------------- #
def write_manifest(outdir, tasks, extra=None):
    results, errors, pending = [], [], []
    incomplete = 0
    for t in sorted(tasks, key=lambda x: x.index):
        terminal = t.status in ("completed", "failed")
        if t.results:
            complete = len(t.results) >= t.need
            if terminal and not complete:
                incomplete += 1
            entry = {"name": t.name, "index": t.index, "prompt": t.prompt,
                     "ok": complete, "complete": complete, "key_id": t.key_id, "size": t.size,
                     "n_requested": t.n, "n_got": len(t.results), "saved": list(t.results)}
            if not complete:
                entry["status"] = t.status
                entry["note"] = "部分成功" if terminal else "进行中"
                if t.error:
                    entry["error"] = t.error
            results.append(entry)
        elif t.status == "failed":
            errors.append({"name": t.name, "index": t.index, "prompt": t.prompt,
                           "error": t.error or "无图片返回", "key_id": t.key_id})
        else:
            pending.append({"name": t.name, "index": t.index, "prompt": t.prompt,
                            "status": t.status, "retry_count": t.retry_count,
                            "n_requested": t.n, "n_got": 0, "saved": []})
    manifest = {"created_at": _utc_ts(), "results": results, "errors": errors,
                "pending": pending,
                "ok": not errors and not pending and incomplete == 0,
                "saved_images": sum(len(t.results) for t in tasks),
                "completed": len(results), "failed": len(errors), "pending_count": len(pending)}
    if extra:
        manifest.update(extra)
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, "manifest.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return path, manifest


def _resume_completed(outdir, tasks):
    path = os.path.join(outdir, "manifest.json")
    try:
        with open(path, encoding="utf-8") as f:
            prev = json.load(f)
    except (OSError, ValueError):
        return 0
    done = {}
    for r in (prev.get("results") or []):
        saved = [s for s in (r.get("saved") or []) if s.get("path") and os.path.exists(s["path"])]
        if r.get("ok") and saved:
            done[r.get("name")] = saved
    n = 0
    for t in tasks:
        if t.name in done:
            t.status = "completed"
            t.results = done[t.name]
            t.got = len(done[t.name])
            t.key_id = "(resumed)"
            n += 1
    return n


# --------------------------------------------------------------------------- #
# 参数 / 任务构建
# --------------------------------------------------------------------------- #
def _normalize_param(k, v):
    key = "output_image_format" if k == "output_format" else k
    if k == "output_format":
        v = str(v).lower().replace("jpg", "jpeg")
    return key, v


def _build_params(args):
    out = {}
    for k in _PARAM_KEYS:
        v = getattr(args, k, None)
        if v is None or str(v) == "":
            continue
        key, val = _normalize_param(k, v)
        out[key] = val
    return out


def _params_from_spec(spec):
    out = {}
    for k in _PARAM_KEYS:
        v = spec.get(k)
        if v is None or str(v) == "":
            continue
        key, val = _normalize_param(k, v)
        out[key] = val
    return out


def _load_prompts_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        sys.exit("批量文件无法读取：%s（%s）" % (path, e))
    specs = []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, list):
        for i, item in enumerate(parsed, 1):
            if isinstance(item, str):
                specs.append({"name": "图%d" % i, "prompt": item})
            elif isinstance(item, dict) and (item.get("prompt") or item.get("edit") or item.get("variation")):
                specs.append(dict(item, name=item.get("name") or "图%d" % i))
    else:
        for line in (l.strip() for l in text.splitlines()):
            if line and not line.startswith("#"):
                specs.append({"name": "图%d" % (len(specs) + 1), "prompt": line})
    if not specs:
        sys.exit("批量文件没有可用的 prompt：%s" % path)
    return specs


def _validate_tasks(tasks):
    for t in tasks:
        if t.mask_path and not t.edit_path:
            sys.exit("--mask 需要配合 --edit 使用（蒙版只在改图 /v1/images/edits 时生效）；"
                     "内容 '%s' 给了 mask 但没有 edit。" % t.name)


def _dedupe_names(tasks):
    used = set()
    for t in tasks:
        if t.name not in used:
            used.add(t.name)
            continue
        i = 2
        while "%s-%d" % (t.name, i) in used:
            i += 1
        new = "%s-%d" % (t.name, i)
        log("注意：重名内容 '%s' 自动改名为 '%s'（避免输出互相覆盖）" % (t.name, new))
        t.name = new
        used.add(new)
    return tasks


def build_tasks(args):
    base_params = _build_params(args)
    tasks = []
    if args.prompts_file:
        for i, spec in enumerate(_load_prompts_file(args.prompts_file), 1):
            params = dict(base_params)
            params.update(_params_from_spec(spec))
            tasks.append(Task(
                index=i, name=spec["name"], prompt=spec.get("prompt", ""),
                n=int(spec.get("n", args.n)), size=spec.get("size", args.size),
                model=spec.get("model", args.model), aspect=spec.get("aspect", args.aspect),
                edit_path=spec.get("edit"), mask_path=spec.get("mask"),
                var_path=spec.get("variation"), params=params,
            ))
    else:
        if not args.prompt and not args.edit and not args.variation:
            sys.exit("缺少提示词：给位置参数 prompt，或 --edit 改图，或 --variation 变体，或 --prompts-file 批量")
        tasks.append(Task(
            index=1, name=args.name or "image", prompt=args.prompt or "",
            n=args.n, size=args.size, model=args.model, aspect=args.aspect,
            edit_path=args.edit, mask_path=args.mask, var_path=args.variation,
            params=base_params,
        ))
    _validate_tasks(tasks)
    return _dedupe_names(tasks)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser():
    ap = argparse.ArgumentParser(description="matsca 生图 CLI（官方插件策略无头移植版）",
                                 allow_abbrev=False)  # 禁用前缀缩写：避免 --n 与 --name/--no-* 歧义
    ap.add_argument("prompt", nargs="?", default=None, help="提示词（单内容）")
    ap.add_argument("--prompts-file", dest="prompts_file", default=None,
                    help="批量任务文件：JSON 列表 [{name,prompt,n?,size?,edit?}] 或每行一个 prompt")
    ap.add_argument("--name", default=None, help="文件名（单内容；默认 image）")
    ap.add_argument("--outdir", default=os.path.join("output", "fig"), help="输出目录（默认 output/fig）")
    ap.add_argument("--model", default="gpt-image-2")
    ap.add_argument("--size", default="auto")
    ap.add_argument("--aspect", default="auto", help="比例（size=auto 时写进提示词）：1:1/16:9/9:16/4:3…")
    ap.add_argument("-n", type=int, default=2, help="每内容生成数量（产品化默认 2 = 主图+备1，1~4）")
    ap.add_argument("--edit", default=None, help="改图：原图路径，走 /v1/images/edits")
    ap.add_argument("--mask", default=None, help="改图蒙版路径（配合 --edit；标注要重绘的区域）")
    ap.add_argument("--variation", default=None, help="图生图/变体：原图路径，走 /v1/images/variations（可无 prompt）")
    ap.add_argument("--timeout", type=int, default=IMAGE_TIMEOUT_S, help="单请求墙钟上限（秒，默认 600）")
    ap.add_argument("--proxy", default=None, help="下载 url 图片用的代理（可选，仅 url 模式用得上）")
    # 赛马 / 覆盖优先 —— 默认常开
    ap.add_argument("--no-race", dest="race", action="store_false", default=True)
    ap.add_argument("--no-coverage-first", dest="coverage_first", action="store_false", default=True)
    # 受阻 SOP
    ap.add_argument("--block-after", dest="block_after", type=int, default=90)
    ap.add_argument("--give-up-after", dest="give_up_after", type=int, default=0)
    ap.add_argument("--no-ping-refine", dest="ping_refine", action="store_false", default=True)
    # 产品化命名
    ap.add_argument("--no-date", dest="date_suffix", action="store_false", default=True)
    # 透传参数面（默认都不发）
    ap.add_argument("--quality", default=None, choices=["auto", "low", "medium", "high"])
    ap.add_argument("--moderation", default=None, choices=["auto", "low"])
    ap.add_argument("--background", default=None, choices=["auto", "transparent", "opaque"])
    ap.add_argument("--style", default=None)
    ap.add_argument("--output-format", dest="output_format", default=None, choices=["png", "jpeg", "jpg", "webp"])
    ap.add_argument("--output-compression", dest="output_compression", default=None, type=int)
    ap.add_argument("--input-fidelity", dest="input_fidelity", default=None, choices=["low", "high"])
    # Key 来源
    ap.add_argument("--keys", default=None, help="逗号分隔的裸 API Key")
    ap.add_argument("--secrets-file", dest="secrets_file", default=None)
    ap.add_argument("--dev-token", dest="dev_token", default=None)
    ap.add_argument("--email", default=None)
    ap.add_argument("--password", default=None)
    ap.add_argument("--no-save-keys", dest="save_keys", action="store_false", default=True)
    ap.add_argument("--save-keys-file", dest="save_keys_file", default=None)
    # 健康检查
    ap.add_argument("--preflight-ping", dest="preflight_ping", action="store_true")
    ap.add_argument("--ping-only", dest="ping_only", action="store_true")
    ap.add_argument("--json-out", dest="json_out", default=None)
    ap.add_argument("--resume", action="store_true")
    return ap


def main():
    ap = build_parser()
    args = ap.parse_args()

    keys = resolve_keys(args)
    if not keys:
        sys.exit("没有可用 Key：用 --keys 临时直传，或设环境变量 MATSCA_API_KEYS，"
                 "或在本地 secrets.env 写 MATSCA_API_KEYS= / dev 账号（自动 reveal）")
    log("可用 Key：%d 把（%s）" % (len(keys), ", ".join(kid for kid, _ in keys)))

    if args.save_keys and getattr(args, "_keys_from_reveal", False):
        target = args.save_keys_file or getattr(args, "_secrets_target", None)
        if target:
            try:
                save_keys_to_secrets(target, [raw for _, raw in keys])
            except OSError as e:
                log("回写 secrets 失败（忽略，不影响本次生成）：%s" % e)

    if args.ping_only:
        out = {}
        for kid, raw in keys:
            try:
                payload = ping_server(raw)
                auth = payload.get("auth") if isinstance(payload, dict) else {}
                out[kid] = {"ok": True, "banned": bool((auth or {}).get("banned")),
                            "ban_remaining_seconds": int((auth or {}).get("ban_remaining_seconds") or 0)}
            except RequestError as e:
                out[kid] = {"ok": False, "status": e.status, "code": e.code, "error": str(e)}
        print(json.dumps(out, ensure_ascii=False, indent=2))
        if not any(v.get("ok") for v in out.values()):
            sys.exit(4)
        return

    tasks = build_tasks(args)
    if args.resume:
        resumed = _resume_completed(args.outdir, tasks)
        if resumed:
            log("断点续跑：跳过已完成 %d 个内容（%s）" % (resumed, ", ".join(
                t.name for t in tasks if t.status == "completed")))
    keyhealths = [KeyHealth(kid, raw) for kid, raw in keys]
    total_n = sum(t.n for t in tasks)
    log("生成 %d 个内容共 %d 张；每Key在飞≤%d、全局≤%d；任务重试≤%d；允许 ping。" % (
        len(tasks), total_n, IMAGE_REQUEST_CONCURRENCY_PER_KEY,
        GLOBAL_IMAGE_REQUEST_CONCURRENCY, MAX_TRANSIENT_TASK_RETRIES))

    sched = Scheduler(tasks, keyhealths, args.outdir, args.timeout,
                      preflight_ping=args.preflight_ping, proxy=args.proxy,
                      race=args.race, coverage_first=args.coverage_first,
                      block_after_ms=max(1, args.block_after) * 1000,
                      give_up_after_ms=max(0, args.give_up_after) * 1000,
                      ping_refine=args.ping_refine, date_suffix=args.date_suffix)
    sched.run()

    manifest_path, manifest = write_manifest(args.outdir, tasks, sched._block_status())
    summary = {"ok": manifest["ok"], "saved_images": manifest["saved_images"],
               "failed": manifest["failed"], "manifest": manifest_path,
               "output_dir": os.path.abspath(args.outdir)}
    if getattr(args, "_keys_from_reveal", False) and not getattr(args, "_secrets_target", None):
        summary["refreshed_keys"] = [raw for _, raw in keys]
        log("Key 已刷新但无本地 secrets 文件可回写，已输出到 summary.refreshed_keys 供 agent 同步")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.json_out:
        try:
            d = os.path.dirname(os.path.abspath(args.json_out))
            if d:
                os.makedirs(d, exist_ok=True)
            with open(args.json_out, "w", encoding="utf-8") as f:
                json.dump(dict(manifest, **summary), f, ensure_ascii=False)
        except OSError as e:
            log("写 --json-out 失败：%s" % e)
    if manifest["saved_images"] == 0:
        sys.exit(4)
    if not manifest["ok"]:
        sys.exit(3)


if __name__ == "__main__":
    main()
