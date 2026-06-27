#!/usr/bin/env python3
"""矩岩(matsca)生图助手 —— 以服务商 matscaimg 内核为底,叠加本仓库偏好层。

【内核来自服务商 matscaimg(不再瞎猜服务端行为)】
  · 只走同步端点 /v1/images/generations(改图走 /v1/images/edits)。app/direct/native
    三模式都走同步;彻底弃用异步 /api/image-tasks 那套"提交→轮询→换 id 重发"的幽灵任务
    机制(它建立在"502=每分钟重启 / 重试免费 / 在途作废"等**未经证实的猜测**上)。
  · 错误分类决定要不要重试(照搬服务商规则):
      - 重试:HTTP 408/409/425/429/500/502/503/504/520/522/524,以及
        upstream_*/api_agent_queue_full/no_available_account/account_concurrency_exhausted
        等"服务端/容量类"瞬时错误(这些不算在客户头上)。
      - 不重试:content_policy_violation / invalid_request / trivial_intercept /
        api_key_temporarily_banned / 余额不足 / 无高清权限 / 鉴权失败 等"客户侧"错误。
  · 退避:优先尊重服务端 Retry-After;否则指数退避带 jitter(上限 60s)。固定次数
    (--retries,默认 5)后失败,**不再无限循环、不再固定盲等、不再砍正常请求**。
  · **并发在单进程内统一卡死(线程池 max_workers=并发上限)**——这正是服务商防"撞墙"的
    做法:一次调用内所有请求共用一个池,**永不超过单密钥在途硬顶**,撞了靠 jitter 退避自愈,
    不需要跨进程错峰。批量(多内容)请优先用本脚本的 --prompts-file 一次性交给它,而不是
    起多个进程 `&`(多进程互不知情、瞬时在途会叠加,正是放大撞墙的根因)。

【保留的本仓库偏好层】
  · 三种 Key 级计费模式(--mode app/direct/native)与 secrets.env 凭证读取。
  · 每内容默认 -n 2:并发发 N 个 n=1 请求,**谁先完成谁当主图(先到为主)**,其余存为
    "（备N）"备选 + 写 _index.md 清单。
  · 多内容批量(--prompts-file):所有内容在一个进程的线程池里跑,**Coverage-First**——
    空闲并发永远优先给"还没有任何图的内容",绝不让备图顶掉未覆盖内容的槽。
  · 默认 size 1536x864 / quality high / moderation low / 输出 output/fig。
  · 机读产物(服务商 matscaimg 风格):输出目录写 manifest.json({created_at,results[],errors[]}),
    stdout 打印 {ok,saved_images,failed_prompts,manifest,output_dir} 摘要;可选 --json-out 落整份 manifest。
  · 可观测性:--log-file 带时间戳任务日志 + <log>.heartbeat 心跳(供 status.py 聚合)。

Key 来源(私密仓库,明文):从 720_Agents/secrets.env 按模式读取
        MATSCA_APP_KEY / MATSCA_DIRECT_KEY / MATSCA_NATIVE_KEY;也支持同名进程环境变量覆盖。
        app 模式另需 MATSCA_APP_ID / MATSCA_APP_SECRET。

用法:
  # 单内容(默认主图+备1)
  python gen_image.py "一只橘猫坐在窗台上,清晨柔光" --name 橘猫 -n 2
  python gen_image.py "赛博朋克城市夜景" --size 1536x1024 -o city.png
  # 透明底图标/logo(图生图常用 transparent + png)
  python gen_image.py "极简狐狸 logo,扁平" --name 狐狸logo --background transparent --size 1024x1024
  # 改图 / 图生图(--edit 原图;可选 --mask 蒙版,白色区域将被重绘)
  python gen_image.py "把背景换成星空" --edit input.png --name 星空版 -o out.png
  # 多内容批量(一次卡并发、Coverage-First;最防撞墙) → prompts.json:
  #   [{"name":"蛙卵","prompt":"...","n":2}, {"name":"蝌蚪","prompt":"..."}, ...]
  python gen_image.py --prompts-file prompts.json --outdir output/fig --json-out output/fig/_batch.json
  用 uv 管理环境时:uv run gen_image.py "提示词" --name 描述
"""
import argparse
import atexit
import base64
import concurrent.futures
import hashlib
import http.client
import io
import json
import os
import queue
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

BASE = "https://img.matsca.com"

# ── 重试/退避内核常量(来自服务商 matscaimg)─────────────────────────────────
# 可重试的 HTTP 状态码:连接/排队/限流/服务端 5xx/CDN 类瞬时错误。
RETRY_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524}
# 可重试的业务错误码(服务端/容量类,不算客户头上,服务商对这些会重试)。
RETRYABLE_ERROR_CODES = {
    "upstream_timeout",
    "upstream_unreachable",
    "upstream_session_pool_exhausted",
    "api_agent_queue_full",
    "upstream_rate_limited",
    "no_available_account",
    "account_concurrency_exhausted",
    "image_permission_unavailable",
    "upstream_direct_unavailable",
    "upstream_server_error",
    "upstream_error",
}
# 不可重试的业务错误码(客户侧:内容/参数/琐碎拦截/key封禁/张数超限/无高清权限)。
NON_RETRYABLE_ERROR_CODES = {
    "content_policy_violation",
    "invalid_request_error",
    "invalid_value",
    "invalid_type",
    "image_count_limit_exceeded",
    "high_res_not_enabled",
    "trivial_intercept",
    "api_key_temporarily_banned",
}
# 不可重试的报文关键词(余额/鉴权/登录/高清/内容策略)。
NON_RETRYABLE_MESSAGE_HINTS = (
    "余额不足",
    "quota exhausted",
    "insufficient",
    "invalid api key",
    "密钥无效",
    "请先登录",
    "登录已过期",
    "暂无高清生图权限",
    "high_res_not_enabled",
    "content policy",
    "policy violation",
)

HEARTBEAT_INTERVAL = 15  # 心跳文件刷新间隔(秒)

# ── 可观测性全局态(后台脱离进程时靠日志+心跳被看见)──────────────────────
_LOG_FH = None
_LOG_LOCK = threading.Lock()
_LAST_LINE = ""
_START_TS = time.time()
_TASK_NAME = ""
_TASK_N = 1
_DONE_COUNT = 0
_HB_PATH = None
# 真实在途计数(实际正在发的 API 请求数;批量时即进程内并发占用,供 status.py 聚合)。
_INFLIGHT = 0
_INFLIGHT_LOCK = threading.Lock()

# ── 上游网关 502/503/504 风暴侦测(更迟钝阈值:只在持续撞墙时才报警,避免误报)──
# 502/503/504 = 服务商上游网关层 5xx(nginx),不是你被风控、也不是 account_concurrency_exhausted(池满)。
# 三者都"可重试、不计风控分",但网关风暴症状更重、靠等可能很久,所以要主动早报、建议换渠道,别让用户傻等。
GATEWAY_5XX_CODES = {502, 503, 504, 520, 522, 524}
STORM_5XX_THRESHOLD = 12         # 累计网关 5xx 达此数即判风暴(更迟钝;撞够久才报)
STORM_NO_PROGRESS_SECONDS = 150  # 或:这么久没有任何新图落盘、且仍在撞 5xx → 也判风暴
STORM_RECENT_WINDOW = 90         # 距上次 5xx 在此窗口内才算"仍在撞"(风暴过去就自动解除报警)
_STORM_5XX_TOTAL = 0             # 自启动累计网关 5xx 次数
_STORM_LAST_5XX_TS = 0.0         # 最近一次网关 5xx 时刻
_LAST_DONE_TS = 0.0             # 最近一次成功落盘(新覆盖)时刻;0=还没出过图(从 _START_TS 起算)
_STORM_LOCK = threading.Lock()
_STORM_ANNOUNCED = False        # 是否已打过醒目 stderr 风暴标记(只打一次)
_STORM_EVER = False             # 本次运行是否曾经历过风暴(供收尾 manifest/摘要标记)
_GAVE_UP = False               # 是否因 --storm-give-up-after 熔断提前收尾


def _inflight_inc():
    global _INFLIGHT
    with _INFLIGHT_LOCK:
        _INFLIGHT += 1


def _inflight_dec():
    global _INFLIGHT
    with _INFLIGHT_LOCK:
        _INFLIGHT = max(0, _INFLIGHT - 1)


def _note_gateway_error(exc):
    """worker 线程:每次"决定重试"时调用,若是上游网关 5xx 就累计一次(用于风暴侦测)。"""
    global _STORM_5XX_TOTAL, _STORM_LAST_5XX_TS
    is_5xx = isinstance(exc, HttpJsonError) and exc.status in GATEWAY_5XX_CODES
    if not is_5xx and isinstance(exc, MatscaError):
        m = str(exc).lower()
        if "bad gateway" in m or "gateway time" in m or any(str(c) in m for c in (502, 503, 504)):
            is_5xx = True
    if is_5xx:
        with _STORM_LOCK:
            _STORM_5XX_TOTAL += 1
            _STORM_LAST_5XX_TS = time.time()


def _storm_state():
    """计算当前是否处于上游网关风暴(更迟钝:必须"仍在撞 5xx" + (累计够多 或 长时间零覆盖))。
    返回 (active, total_5xx, no_progress_s)。"""
    now = time.time()
    with _STORM_LOCK:
        total = _STORM_5XX_TOTAL
        last_5xx = _STORM_LAST_5XX_TS
    no_progress_s = now - (_LAST_DONE_TS or _START_TS)
    recent = bool(last_5xx) and (now - last_5xx) <= STORM_RECENT_WINDOW
    active = recent and (total >= STORM_5XX_THRESHOLD or no_progress_s >= STORM_NO_PROGRESS_SECONDS)
    return active, total, round(no_progress_s, 1)


def _announce_storm(total, no_progress_s):
    """风暴首次确认时,打一条醒目 stderr 标记(只打一次)。Agent 轮询心跳命中 upstream_storm 时也据此提醒用户。"""
    global _STORM_ANNOUNCED, _STORM_EVER
    with _STORM_LOCK:
        _STORM_EVER = True
        if _STORM_ANNOUNCED:
            return
        _STORM_ANNOUNCED = True
    sys.stderr.write(
        "\n>>> UPSTREAM_502_STORM 上游网关风暴 (累计 %d 次 5xx, 已 %.0fs 无新图) <<<\n"
        "    这是服务商上游网关层 502/503/504,**不是**你被风控、也不是 account_concurrency_exhausted(池满)。\n"
        "    工具正在有界退避自愈,不硬刷、不抬风控分;但靠等可能很久。\n"
        "    建议你同时去找别的渠道 / 晚点再来,别傻等。已出的图不会丢。\n\n"
        % (total, no_progress_s)
    )
    sys.stderr.flush()


def _set_gave_up():
    global _GAVE_UP
    _GAVE_UP = True


def _storm_summary():
    """收尾元数据:本次是否经历过上游网关风暴 + 累计 5xx + 是否熔断收尾(写进 manifest/stdout 摘要)。"""
    return {"upstream_storm": _STORM_EVER, "storm_5xx": _STORM_5XX_TOTAL, "gave_up": _GAVE_UP}


class MatscaError(RuntimeError):
    pass


class NeedLoginError(MatscaError):
    pass


class HttpJsonError(MatscaError):
    """携带 HTTP 状态码 + 报文 + 响应头(用于读取 Retry-After)的错误。"""

    def __init__(self, status, payload, headers=None):
        self.status = status
        self.payload = payload
        self.headers = headers or {}
        super().__init__(extract_error_message(payload) or ("HTTP %s" % status))

    @property
    def code(self):
        return extract_error_code(self.payload)


# ── 错误报文解析(来自服务商 matscaimg)────────────────────────────────────
def extract_error_value(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "; ".join(extract_error_value(item) for item in value if item)
    if isinstance(value, dict):
        if isinstance(value.get("message"), str):
            return value["message"]
        return extract_error_value(value.get("error") or value.get("detail"))
    return ""


def extract_error_message(payload):
    if not isinstance(payload, dict):
        return extract_error_value(payload)
    return (
        extract_error_value(payload.get("detail"))
        or extract_error_value(payload.get("error"))
        or extract_error_value(payload.get("message"))
    )


def extract_error_code(value):
    if not isinstance(value, dict):
        return ""
    code = value.get("code")
    if isinstance(code, str):
        return code
    return extract_error_code(value.get("detail")) or extract_error_code(value.get("error"))


def should_retry_error(exc):
    """决定一个异常是否值得重试。客户侧错误一律 False;服务端/容量类 True。"""
    if isinstance(exc, HttpJsonError):
        message = str(exc).lower()
        code = exc.code
        if code in RETRYABLE_ERROR_CODES:
            return True
        if code in NON_RETRYABLE_ERROR_CODES:
            return False
        if any(hint.lower() in message for hint in NON_RETRYABLE_MESSAGE_HINTS):
            return False
        return exc.status in RETRY_STATUS_CODES
    if isinstance(exc, NeedLoginError):
        return False
    # 纯网络层异常(连接重置/超时/DNS 等):瞬时,值得重试。
    return True


def retry_delay(attempt, headers=None):
    """退避秒数:优先 Retry-After,否则指数退避带 jitter(上限 60s)。"""
    retry_after = ""
    for key, value in (headers or {}).items():
        if key.lower() == "retry-after":
            retry_after = value
            break
    if retry_after:
        try:
            return min(60.0, max(0.0, float(retry_after)))
        except ValueError:
            pass
    base = min(60.0, 1.5 * (2 ** attempt))
    return base + random.uniform(0, base * 0.25)


def with_retries(fn, attempts, label, on_retry=None, abort_event=None):
    """对 fn 做固定次数重试(只重试 should_retry_error 为真的错误)。
    on_retry(exc):每次"决定重试"时回调一次(调度层据此侦测连续上游池满→自动降级)。
    abort_event:可选熔断信号(--storm-give-up-after);一旦置位,放弃后续重试并打断退避等待立刻抛出。"""
    last_exc = None
    for attempt in range(max(1, attempts)):
        if abort_event is not None and abort_event.is_set():
            raise MatscaError("%s storm-give-up 熔断:停止重试" % label)
        try:
            return fn()
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt >= attempts - 1 or not should_retry_error(exc):
                raise
            # 风暴侦测:无论是否池满,先记一笔网关 5xx(若是)
            _note_gateway_error(exc)
            if on_retry is not None:
                try:
                    on_retry(exc)
                except Exception:  # noqa: BLE001
                    pass
            headers = exc.headers if isinstance(exc, HttpJsonError) else {}
            delay = retry_delay(attempt, headers)
            sys.stderr.write(
                "[%s] 第%d次失败(%s);%.1fs 后重试(不探活/不 ping)\n"
                % (label, attempt + 1, exc, delay)
            )
            sys.stderr.flush()
            # 退避等待:开了熔断则用可打断的 wait(置位即立刻放弃),否则普通 sleep
            if abort_event is not None:
                if abort_event.wait(delay):
                    raise MatscaError("%s storm-give-up 熔断:停止重试" % label)
            else:
                time.sleep(delay)
    raise last_exc or MatscaError("%s failed" % label)


# ── 可观测性:任务日志(tee stderr,每行带时间戳)+ 心跳文件 ────────────────
class _Tee:
    """把写往 stderr 的内容同时落一份到日志文件(每行前缀 [HH:MM:SS])。"""

    def __init__(self, real):
        self._real = real
        self._at_line_start = True

    def write(self, s):
        global _LAST_LINE
        try:
            self._real.write(s)
        except Exception:  # noqa: BLE001
            pass
        if _LOG_FH is not None and s:
            with _LOG_LOCK:
                try:
                    for ch in s:
                        if self._at_line_start:
                            _LOG_FH.write(time.strftime("[%H:%M:%S] "))
                            self._at_line_start = False
                        _LOG_FH.write(ch)
                        if ch == "\n":
                            self._at_line_start = True
                    _LOG_FH.flush()
                except Exception:  # noqa: BLE001
                    pass
        line = s.strip()
        if line:
            _LAST_LINE = line
        return len(s)

    def flush(self):
        try:
            self._real.flush()
        except Exception:  # noqa: BLE001
            pass


def _heartbeat_payload(alive):
    if alive:
        storm, s5xx, no_progress_s = _storm_state()
        if storm:
            _announce_storm(s5xx, no_progress_s)  # 心跳线程兜底:确保风暴一定被打标一次
    else:
        storm, s5xx, no_progress_s = _STORM_EVER, _STORM_5XX_TOTAL, 0.0
    return {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_s": round(time.time() - _START_TS, 1),
        "alive": alive,
        "name": _TASK_NAME,
        "n": _TASK_N,
        "done": _DONE_COUNT,
        # 在途 = 真实正在发的 API 请求数(批量时即进程内并发占用);终态写 0。
        "inflight": (_INFLIGHT if alive else 0),
        # 上游网关风暴:命中即 true(Agent 必须轮询并据此第一时间提醒用户、建议换渠道,别等全部重试跑完)。
        "upstream_storm": storm,
        "storm_5xx": s5xx,
        "storm_no_progress_s": no_progress_s,
        "last": _LAST_LINE,
    }


def _heartbeat_loop(path):
    """守护线程:每 HEARTBEAT_INTERVAL 秒覆写心跳文件。mtime 不再变=进程已死/卡死。"""
    while True:
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(_heartbeat_payload(True), f, ensure_ascii=False)
        except OSError:
            pass
        time.sleep(HEARTBEAT_INTERVAL)


def _final_heartbeat():
    """进程退出时写一次终态心跳(alive=False),让状态聚合器立刻判定本任务已结束。"""
    if not _HB_PATH:
        return
    try:
        with open(_HB_PATH, "w", encoding="utf-8") as f:
            json.dump(_heartbeat_payload(False), f, ensure_ascii=False)
    except OSError:
        pass


def _start_observability(args, task_name, task_n):
    """按 --log-file(或从 --json-out 派生)开启任务日志 + 心跳文件。任何文件错误静默降级。"""
    global _LOG_FH, _START_TS, _TASK_NAME, _TASK_N, _HB_PATH, _LAST_DONE_TS
    _START_TS = time.time()
    _LAST_DONE_TS = 0.0
    _TASK_NAME = task_name or "image"
    _TASK_N = max(1, task_n or 1)
    log_path = getattr(args, "log_file", None)
    if not log_path and getattr(args, "json_out", None):
        log_path = os.path.splitext(args.json_out)[0] + ".log"
    if not log_path:
        return
    try:
        d = os.path.dirname(os.path.abspath(log_path))
        if d:
            os.makedirs(d, exist_ok=True)
        _LOG_FH = open(log_path, "a", encoding="utf-8")  # noqa: SIM115
        sys.stderr = _Tee(sys.stderr)
    except OSError:
        _LOG_FH = None
        return
    hb_path = os.path.splitext(log_path)[0] + ".heartbeat"
    _HB_PATH = hb_path
    atexit.register(_final_heartbeat)
    threading.Thread(target=_heartbeat_loop, args=(hb_path,), daemon=True).start()
    sys.stderr.write(
        "任务日志: %s ; 心跳: %s(每 %ds 刷新)\n" % (log_path, hb_path, HEARTBEAT_INTERVAL)
    )
    sys.stderr.flush()


# ── 凭证与模式(本仓库偏好层)──────────────────────────────────────────────
SECRETS_ABS = r"D:\700_Resources\720_Agents\secrets.env"

MODE_KEY_ENV = {
    "app": "MATSCA_APP_KEY",
    "direct": "MATSCA_DIRECT_KEY",
    "native": "MATSCA_NATIVE_KEY",
}
MODE_MULT = {"app": "1x", "direct": "x2", "native": "x3"}
# 数值倍率(计费透明 by_mode/total_credits 用):每张图消耗的积分倍数。
MODE_COST = {"app": 1, "direct": 2, "native": 3}

# 注:模式不再用全局变量保存。双模式同进程并发时,header 按"每个请求单元"自带的 mode 现算
# (见 _base_headers(key, mode)),绝不串味。


def _secrets_paths():
    here = os.path.dirname(os.path.abspath(__file__))
    return (os.path.join(here, "..", "..", "..", "secrets.env"), SECRETS_ABS)


def _read_env_value(path, name):
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == name:
                    return v.strip().strip('"').strip("'")
    except OSError:
        return None
    return None


def _lookup_env(name):
    v = os.environ.get(name)
    if v:
        return v
    for path in _secrets_paths():
        v = _read_env_value(path, name)
        if v:
            return v
    return None


def get_key_for_mode(mode):
    env_name = MODE_KEY_ENV[mode]
    key = _lookup_env(env_name)
    if key:
        return key
    sys.exit("缺少 %s:请在 720_Agents\\secrets.env 写入 %s=sk-..." % (env_name, env_name))


_APP_HEADERS_CACHE = None


def get_app_headers():
    """应用模式凭证(X-App-ID/X-App-Secret);缺失返回空 dict。结果缓存一次。"""
    global _APP_HEADERS_CACHE
    if _APP_HEADERS_CACHE is not None:
        return _APP_HEADERS_CACHE
    aid = os.environ.get("MATSCA_APP_ID")
    asec = os.environ.get("MATSCA_APP_SECRET")
    if not (aid and asec):
        for path in _secrets_paths():
            aid = aid or _read_env_value(path, "MATSCA_APP_ID")
            asec = asec or _read_env_value(path, "MATSCA_APP_SECRET")
    _APP_HEADERS_CACHE = {"X-App-ID": aid, "X-App-Secret": asec} if (aid and asec) else {}
    return _APP_HEADERS_CACHE


def resolve_mode(arg_mode):
    if arg_mode != "auto":
        return arg_mode
    return "app" if get_app_headers() else "direct"


# ── HTTP 请求层(http.client:保大小写敏感的 X-App-ID;并回传状态/头/报文)──────
def _base_headers(key, mode):
    """所有请求公共头:Bearer 鉴权 + (仅 app 模式)大小写敏感的 App 头。
    mode 按"每个请求单元"传入(双模式同进程并发时各请求各算 header,绝不串味)。"""
    headers = {"Authorization": "Bearer " + key, "Accept": "application/json"}
    if mode == "app":
        headers.update(get_app_headers())
    return headers


def _send(method, path, headers, data, timeout):
    """底层单次发送(http.client)。成功返回 dict;>=400 抛 HttpJsonError;网络层错误抛 MatscaError。
    用 http.client 直发:urllib 会把头名 title() 化(X-App-ID -> X-App-Id),而服务端对
    X-App-ID 大小写敏感,会误报"缺少应用凭证"。这里原样发送。**绝不探活/ping**。"""
    parts = urllib.parse.urlsplit(BASE)
    https = parts.scheme == "https"
    conn_cls = http.client.HTTPSConnection if https else http.client.HTTPConnection
    conn = conn_cls(parts.hostname, parts.port or (443 if https else 80), timeout=timeout)
    try:
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        resp_headers = dict(resp.getheaders())
        text = raw.decode("utf-8", errors="replace")
        try:
            payload_out = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError:
            payload_out = {"error": text}
        if resp.status >= 400:
            raise HttpJsonError(resp.status, payload_out, resp_headers)
        return payload_out
    except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
        raise MatscaError("连接错误/超时 %s" % e)
    finally:
        conn.close()


def _request(method, path, key, mode, payload=None, timeout=600):
    """发一次 JSON 请求(文生图)。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = _base_headers(key, mode)
    if data is not None:
        headers["Content-Type"] = "application/json"
    return _send(method, path, headers, data, timeout)


def _request_multipart(path, key, mode, fields, files, timeout=600):
    """发一次 multipart/form-data 请求(图生图/改图 /v1/images/edits)。
    fields: [(name, value_str), ...];files: [(name, (filename, bytes, content_type)), ...]。"""
    boundary = "----matsca%s" % uuid.uuid4().hex
    buf = io.BytesIO()
    for name, value in fields:
        buf.write(("--%s\r\n" % boundary).encode("utf-8"))
        buf.write(('Content-Disposition: form-data; name="%s"\r\n\r\n' % name).encode("utf-8"))
        buf.write(("%s\r\n" % value).encode("utf-8"))
    for name, (filename, blob, ctype) in files:
        buf.write(("--%s\r\n" % boundary).encode("utf-8"))
        buf.write(
            ('Content-Disposition: form-data; name="%s"; filename="%s"\r\n' % (name, filename)).encode("utf-8")
        )
        buf.write(("Content-Type: %s\r\n\r\n" % ctype).encode("utf-8"))
        buf.write(blob)
        buf.write(b"\r\n")
    buf.write(("--%s--\r\n" % boundary).encode("utf-8"))
    data = buf.getvalue()
    headers = _base_headers(key, mode)
    headers["Content-Type"] = "multipart/form-data; boundary=%s" % boundary
    return _send("POST", path, headers, data, timeout)


# ── 取图/落盘 ──────────────────────────────────────────────────────────────
def extract_b64(body):
    out = []
    for x in (body.get("data") or []) if isinstance(body, dict) else []:
        if isinstance(x, dict) and x.get("b64_json"):
            out.append(x["b64_json"])
    return out


def extract_url(body):
    out = []
    for x in (body.get("data") or []) if isinstance(body, dict) else []:
        if isinstance(x, dict) and x.get("url"):
            out.append(x["url"])
    return out


def detect_image_ext(data, output_format=""):
    """按图片 magic bytes 判定扩展名;判不出时回退到 output_format,再回退 png(来自服务商)。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    fmt = (output_format or "").lower().strip().replace("jpeg", "jpg")
    return fmt if fmt in {"png", "jpg", "webp"} else "png"


def _download(url, proxy=None, timeout=600):
    """下载 url 模式返回的图片字节。来自服务商 download_url:
    ① 处理内联 data: URI(服务端有时不回网址而直接塞 base64);
    ② 带 User-Agent(urllib 默认 UA 易被 CDN/WAF 403)。"""
    if url.startswith("data:"):
        header, _, payload = url.partition(",")
        if ";base64" not in header or not payload:
            raise MatscaError("不支持的 data URI 图片格式")
        return base64.b64decode(payload)
    handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {})
    opener = urllib.request.build_opener(handler)
    req = urllib.request.Request(url, headers={"User-Agent": "image-gen/1.0"})
    with opener.open(req, timeout=timeout) as r:
        return r.read()


def _safe_name(text, max_len=80):
    """把任意描述清洗成可安全落盘的文件名片段(保留中文;非法字符→-;截断;空则哈希兜底)。来自服务商 compact_slug。"""
    s = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff._-]+", "-", (text or "").strip()).strip("-._")
    if not s:
        s = hashlib.sha1(str(uuid.uuid4()).encode("ascii")).hexdigest()[:12]
    return s[:max_len]


def _save_bytes(data, path):
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return os.path.abspath(path)


# ── 任务对象:一个"内容"= 一个 Job(要 need 张,先到为主分主图/备图)─────────────
class Job:
    def __init__(self, name, prompt, need, params, response_format,
                 out=None, outdir=None, date=None,
                 edit_image=None, edit_name=None, mask=None, mask_name=None,
                 variation_image=None, variation_name=None, index=0, single=False, mode=None):
        self.name = name
        self.prompt = prompt
        self.need = max(1, need)
        self.params = params  # 仅含非空的 model/size/quality/moderation/background/style/...
        self.response_format = response_format
        self.single = bool(single)  # True=单请求 n=N(占1槽、无早交付);False=赛马 N×n=1(占N槽、先到为主)
        self.mode = mode  # None=调度器按双模式策略自选;显式值=本内容钉死该模式(逐项可不同)
        self.out = out
        self.outdir = outdir or os.path.join("output", "fig")
        self.date = date
        self.edit_image = edit_image
        self.edit_name = edit_name
        self.mask = mask
        self.mask_name = mask_name
        self.variation_image = variation_image
        self.variation_name = variation_name
        self.index = index  # 1-based job_index(写进服务商风格 manifest)
        # 运行态
        self.primary = None
        self.primary_mode = None  # 主图实际走的哪把 key/模式(写进 results[].mode)
        self.alts = []
        self.altc = 1
        self.got = 0
        self.saved = []        # [{"path","bytes","role","mode"}]，服务商 manifest 的 saved 列表
        self.last_error = None  # 该内容最后一次失败原因(全失败时写进 manifest.errors)
        self.created = None     # 服务端响应里的 created 时间戳(若有)
        # 事件驱动调度运行态
        self.inflight_count = 0   # 该内容当前在飞的请求数
        self.inflight_images = 0  # 该内容在飞请求预期产出的图数(∑ 各请求的 n)
        self.boosters = 0         # 已为"顽固难产主图"额外加的赛马路数
        self.issued = 0           # 调度层累计派发的请求次数(防全失败时无限重发)

    def primary_path(self, ext):
        if self.out:
            return self.out
        return os.path.join(self.outdir, "%s_%s.%s" % (_safe_name(self.name), self.date, ext))

    def alt_path(self, k, ext):
        if self.out:
            d = os.path.dirname(os.path.abspath(self.out)) or "."
            stem = os.path.splitext(os.path.basename(self.out))[0]
            oext = os.path.splitext(self.out)[1] or ("." + ext)
            return os.path.join(d, "%s（备%d）%s" % (stem, k, oext))
        return os.path.join(self.outdir, "%s（备%d）_%s.%s" % (_safe_name(self.name), k, self.date, ext))


def generate_images(job, key, mode, n, retries, timeout, proxy=None, on_retry=None, abort_event=None):
    """同步生成 n 张图(一个请求 n=n),带重试,返回图片字节列表(len=实际 n_got≤n)。
    文生图走 JSON;改图走 /v1/images/edits;变体走 /v1/images/variations(三端点都接受 n)。
    response_format 按本请求 mode 现算(native→url 下载,其余→b64)。失败抛异常(由调度层隔离)。"""
    n = max(1, int(n))
    rf = "url" if mode == "native" else "b64_json"

    def call_api():
        if job.variation_image is not None:
            # 图生图/变体:走 /v1/images/variations(multipart;变体不需要 prompt,有则带上)
            fields = [("n", str(n)), ("response_format", rf)]
            if job.prompt:
                fields.append(("prompt", job.prompt))
            for k, v in job.params.items():
                fields.append((k, str(v)))
            files = [("image", (job.variation_name or "image.png", job.variation_image, "application/octet-stream"))]
            body = _request_multipart("/v1/images/variations", key, mode, fields, files, timeout=timeout)
        elif job.edit_image is not None:
            fields = [("prompt", job.prompt), ("n", str(n)), ("response_format", rf)]
            for k, v in job.params.items():
                fields.append((k, str(v)))
            files = [("image", (job.edit_name or "image.png", job.edit_image, "application/octet-stream"))]
            if job.mask is not None:
                files.append(("mask", (job.mask_name or "mask.png", job.mask, "application/octet-stream")))
            body = _request_multipart("/v1/images/edits", key, mode, fields, files, timeout=timeout)
        else:
            payload = {"prompt": job.prompt, "n": n, "response_format": rf}
            payload.update(job.params)
            body = _request("POST", "/v1/images/generations", key, mode, payload, timeout=timeout)
        if isinstance(body, dict) and body.get("error"):
            raise HttpJsonError(400, body)
        # 取图不钉死 mode:逐 item 看返回里实际有什么(b64 优先,否则 url)。
        # 来自服务商 generate_one——服务端拥堵/降级时可能回与请求 response_format 不同的格式,
        # 钉死 mode 会把已出的图误判成"无结果返回"而失败。
        vals = []
        for x in (body.get("data") or []) if isinstance(body, dict) else []:
            if not isinstance(x, dict):
                continue
            if x.get("b64_json"):
                vals.append(("b64", x["b64_json"]))
            elif x.get("url"):
                vals.append(("url", x["url"]))
        if not vals:
            raise MatscaError("无结果返回(疑流式空返回/服务异常)")
        return vals

    items = with_retries(call_api, retries, job.name, on_retry=on_retry, abort_event=abort_event)
    out = []
    for kind, value in items:
        out.append(_download(value, proxy=proxy, timeout=timeout) if kind == "url" else base64.b64decode(value))
    return out


# ── 多内容编排:事件驱动 Coverage-First 自适应调度 ───────────────────────────────
# 设计依据:output/双模式+赛马-合并方案.md、output/自适应调度思路-分析.md、output/实测2-n占槽结论.md
#  · 双模式:每把 key 一个独立在途池(app≤3 / direct≤4 / native≤3),各自永不超单密钥硬顶。
#  · 赛马/单请求:一个内容的 need 张,赛马=N 个并行 n=1(先到为主、早交付);单请求=1 个 n=N(省槽、无早交付)。
#  · Coverage-First:开局每个内容先抢 1 张主图;顽固难产的图加一路并行赛马;全部主图就绪后才补备图。
#  · 自动降级(实测2 结论:n=N 单请求占 ~N 个上游槽):连续撞上游池满→后续请求一律降到 n=1(只抢 1 个槽)。
SINGLE_CALL_MAX_N = 4    # 单请求 n 的安全上限(实测 n≥6 易 502/超时);超出部分留给备图阶段补
STALL_SECONDS = 50       # 一张主图在飞超过此秒数视为"顽固难产",可加一路并行赛马抢覆盖
MAX_BOOSTERS = 1         # 每个内容最多额外加几路覆盖赛马(防止无限加压)
DOWNGRADE_THRESHOLD = 3  # 连续撞到 N 次"上游池满"即降级:后续请求一律 n=1
CHECK_INTERVAL = 5       # 协调循环最长等待(秒);超时即重新评估(用于侦测 stall 后加 booster)


class Scheduler:
    """事件驱动的 Coverage-First 调度器:按 mode 分池、赛马/单请求、池满自动降级。"""

    def __init__(self, jobs, key_by_mode, caps, dispatch, modes,
                 retries, timeout, proxy, output_format, storm_give_up_after=None):
        self.jobs = sorted(jobs, key=lambda j: j.index)
        self.key_by_mode = key_by_mode
        self.caps = dict(caps)               # {mode: 在途上限}
        self.dispatch = dispatch             # 'cost' | 'throughput'
        self.modes = list(modes)             # 已按成本升序
        self.retries = retries
        self.timeout = timeout
        self.proxy = proxy
        self.output_format = output_format
        self.used = {m: 0 for m in self.modes}   # 各 mode 当前在途
        self.start_at = {}                       # future -> (unit, t0)
        self.q = queue.Queue()                   # 完成事件队列(放 future)
        self.dg_lock = threading.Lock()
        self.consec_pool_full = 0
        self.downgraded = False
        # 可选熔断(--storm-give-up-after):风暴持续超此秒数→置 abort_event、提前收尾返回部分结果
        self.storm_give_up_after = storm_give_up_after
        self.storm_since = None
        self.gave_up = False
        self.abort_event = threading.Event()

    # — 池满信号(worker 线程在重试回调里调用):累计连续次数,达阈值即降级 —
    def _on_pool_full(self, exc):
        if isinstance(exc, HttpJsonError) and exc.code == "account_concurrency_exhausted":
            with self.dg_lock:
                self.consec_pool_full += 1
                if not self.downgraded and self.consec_pool_full >= DOWNGRADE_THRESHOLD:
                    self.downgraded = True
                    sys.stderr.write(
                        "[降级] 连续 %d 次上游池满(account_concurrency_exhausted):"
                        "后续请求一律降到 n=1(每请求只抢 1 个上游槽),覆盖优先。\n"
                        % self.consec_pool_full
                    )
                    sys.stderr.flush()

    def _is_downgraded(self):
        with self.dg_lock:
            return self.downgraded

    def _free_mode(self, allowed=None):
        """按分配策略在 allowed(默认全部 modes)中挑一个有空位的 mode;无空位返回 None。"""
        pool = allowed if allowed else self.modes
        avail = [m for m in pool if m in self.caps and self.used[m] < self.caps[m]]
        if not avail:
            return None
        if self.dispatch == "throughput":
            # 吞吐优先:剩余绝对空位最多;并列再按成本(便宜优先)
            return max(avail, key=lambda m: (self.caps[m] - self.used[m], -MODE_COST[m]))
        # 成本优先:最便宜且有空位(self.modes 已按成本升序)
        return min(avail, key=lambda m: MODE_COST[m])

    def _mode_for(self, j):
        """本内容可用的模式:钉死了就只试那把,否则按策略在所有 modes 中挑有空位的。"""
        allowed = [j.mode] if j.mode else None
        return self._free_mode(allowed)

    def _demand(self, j):
        return j.need - j.got - j.inflight_images

    def _can_issue(self, j):
        """限制单个内容调度层累计请求数,避免全失败时无限重发。"""
        return j.issued < j.need + MAX_BOOSTERS + 2

    def _open_n(self, j):
        """开局/覆盖请求的 n:降级→1;单请求→剩余需求(≤上限);赛马→1。"""
        if self._is_downgraded():
            return 1
        if j.single:
            return max(1, min(self._demand(j), SINGLE_CALL_MAX_N))
        return 1

    def _pick_unit(self):
        """事件驱动挑下一个该发的 (job, n, mode);没有可发返回 None。"""
        if self._free_mode() is None:
            return None  # 所有池都满,无法发
        # 优先级1:还没主图、当前没有在飞 → 开局抢覆盖
        for j in self.jobs:
            if j.got == 0 and j.inflight_count == 0 and self._demand(j) > 0 and self._can_issue(j):
                m = self._mode_for(j)
                if m is not None:
                    return (j, self._open_n(j), m)
        # 优先级2(顽固图多路赛马):还没主图、在飞那次已 stall → 再加一路 n=1
        now = time.time()
        for j in self.jobs:
            if (j.got == 0 and j.inflight_count > 0 and j.boosters < MAX_BOOSTERS
                    and self._demand(j) > 0 and self._can_issue(j)):
                oldest = min((t0 for (u, t0) in self.start_at.values() if u[0] is j), default=now)
                if now - oldest >= STALL_SECONDS:
                    m = self._mode_for(j)
                    if m is not None:
                        j.boosters += 1
                        return (j, 1, m)
        # 优先级3:只有所有内容都拿到主图后,才发备图(赛马 n=1)
        if all(j.got > 0 for j in self.jobs):
            for j in self.jobs:
                if self._demand(j) > 0 and self._can_issue(j):
                    m = self._mode_for(j)
                    if m is not None:
                        return (j, 1, m)
        return None

    def _submit(self, ex, unit):
        job, n, mode = unit
        self.used[mode] += 1
        job.inflight_count += 1
        job.inflight_images += n
        job.issued += 1
        _inflight_inc()
        fut = ex.submit(generate_images, job, self.key_by_mode[mode], mode, n,
                        self.retries, self.timeout, self.proxy, self._on_pool_full,
                        self.abort_event)
        self.start_at[fut] = (unit, time.time())
        fut.add_done_callback(self.q.put)

    def _handle(self, fut):
        global _DONE_COUNT, _LAST_DONE_TS
        unit, _t0 = self.start_at.pop(fut)
        job, n, mode = unit
        self.used[mode] -= 1
        job.inflight_count -= 1
        job.inflight_images -= n
        _inflight_dec()
        try:
            images = fut.result()
        except KeyboardInterrupt:
            raise
        except (Exception, SystemExit) as e:  # noqa: BLE001
            job.last_error = str(e)
            sys.stderr.write("[%s] 一个出图请求失败,已跳过(不连累其余/主图): %s\n" % (job.name, e))
            sys.stderr.flush()
            return
        with self.dg_lock:
            self.consec_pool_full = 0  # 成功一次→清空连续池满计数
        for data in images:
            if job.got >= job.need:
                break  # 单请求可能多返;不超过 need
            ext = detect_image_ext(data, self.output_format)
            if job.primary is None:
                job.primary = _save_bytes(data, job.primary_path(ext))
                job.primary_mode = mode
                job.saved.append({"path": job.primary, "bytes": len(data), "role": "primary", "mode": mode})
                print("[%s] 主图(可直接使用,已就绪): %s" % (job.name, job.primary))
            else:
                p = _save_bytes(data, job.alt_path(job.altc, ext))
                job.altc += 1
                job.alts.append(p)
                job.saved.append({"path": p, "bytes": len(data), "role": "alt", "mode": mode})
                print("[%s] 备选已就绪: %s" % (job.name, p))
            job.got += 1
            _DONE_COUNT += 1
            _LAST_DONE_TS = time.time()  # 标记有新覆盖→风暴"零覆盖"计时清零
        sys.stdout.flush()

    def _storm_watch(self):
        """侦测上游网关风暴:命中即打醒目标记;若开了 --storm-give-up-after 且风暴持续超时→熔断收尾。
        返回 True 表示应立刻停止派发、提前收尾(返回已出的图 + 缺图清单)。"""
        active, total, no_progress_s = _storm_state()
        if not active:
            self.storm_since = None  # 风暴缓解→重置计时(再撞重新计)
            return False
        _announce_storm(total, no_progress_s)
        if self.storm_since is None:
            self.storm_since = time.time()
        if (self.storm_give_up_after is not None
                and time.time() - self.storm_since >= self.storm_give_up_after
                and not self.gave_up):
            self.gave_up = True
            _set_gave_up()
            self.abort_event.set()
            sys.stderr.write(
                ">>> storm-give-up:上游风暴已持续超过 %ds,提前收尾 —— 停止重试、返回已出的图 + 缺图清单"
                "(不再耗尽 --retries 预算)。<<<\n" % self.storm_give_up_after
            )
            sys.stderr.flush()
            return True
        return False

    def run(self):
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, sum(self.caps.values())))
        try:
            while True:
                # 尽量把有空位的 mode 填满
                while True:
                    unit = self._pick_unit()
                    if unit is None:
                        break
                    self._submit(ex, unit)
                # 侦测上游网关风暴:打醒目标记;命中可选熔断则收尾退出
                if self._storm_watch():
                    break
                # 无在飞 → 若也没有可发的就收工,否则回到顶部继续发
                if sum(self.used.values()) == 0:
                    if self._pick_unit() is None:
                        break
                    continue
                # 等一个完成事件(带超时,以便侦测 stall/风暴后回到上面重新评估)
                try:
                    fut = self.q.get(timeout=CHECK_INTERVAL)
                except queue.Empty:
                    continue
                self._handle(fut)
        finally:
            # 熔断时已 set abort_event,在飞请求会在退避点尽快抛出;正常收尾时本就无在飞。
            ex.shutdown(wait=True)
        # 收尾:把已完成但未处理的结果补登记(熔断瞬间可能有刚成功、还没 handle 的图,别丢)
        while True:
            try:
                self._handle(self.q.get_nowait())
            except queue.Empty:
                break


def run_jobs(jobs, key_by_mode, caps, dispatch, modes, retries, timeout, proxy, output_format,
             storm_give_up_after=None):
    """入口:构造并运行事件驱动调度器。"""
    Scheduler(jobs, key_by_mode, caps, dispatch, modes,
              retries, timeout, proxy, output_format, storm_give_up_after).run()


# ── 清单 + 机读产物(服务商 matscaimg manifest.json 风格)────────────────────────
def _utc_ts():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _write_index_md(out_dir, jobs_with_alts):
    """人读的备选图清单 _index.md(供需要换主图时手动挑;与机读 manifest.json 互不影响)。"""
    idx = os.path.join(out_dir, "_index.md")
    new = not os.path.exists(idx)
    with open(idx, "a", encoding="utf-8") as f:
        if new:
            f.write(
                "# 备选图清单\n\n"
                "本目录存放多生成的备选图。需要替换主图时，从下面挑一张手动替换即可。\n\n"
            )
        for job in jobs_with_alts:
            f.write("## %s — %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), os.path.basename(job.primary)))
            f.write("- 名称: %s\n" % job.name)
            f.write("- 提示词: %s\n" % job.prompt)
            f.write("- 尺寸/质量: %s / %s\n" % (job.params.get("size", "auto"), job.params.get("quality", "auto")))
            f.write("- 主图(已用): %s\n" % job.primary)
            f.write("- 备选:\n")
            for p in job.alts:
                f.write("  - %s\n" % p)
            f.write("\n")
    return idx


def _job_result(job):
    """服务商 manifest 风格的单条 result(在服务商 saved[] 基础上叠我们的 primary/alts 语义)。"""
    return {
        "name": job.name,
        "job_index": job.index,
        "prompt": job.prompt,
        "ok": job.primary is not None,
        "primary": job.primary,          # 先到为主:已就绪可直接用的主图
        "alts": job.alts,                # 备图(可换)
        "n_requested": job.need,
        "n_got": job.got,
        "saved": job.saved,              # 服务商风格:[{path,bytes,role,mode}]
        "size": job.params.get("size"),
        "mode": job.primary_mode,        # 主图实际走的模式(双模式时逐图可不同)
        "single": job.single,
        "created": job.created,
    }


def write_manifest(out_dir, results, errors, total_credits=0, by_mode=None, storm=None):
    """服务商风格:把 {created_at, results[], errors[], total_credits, by_mode, 风暴元数据} 写到 out_dir/manifest.json。"""
    manifest = {"created_at": _utc_ts(), "results": results, "errors": errors,
                "total_credits": total_credits, "by_mode": by_mode or {}}
    if storm:
        manifest.update(storm)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "manifest.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return path, manifest


def _compute_billing(jobs):
    """按模式聚合计费透明:total_credits + by_mode{mode:{images,credits}}。依据各 saved 条目的 mode。"""
    by_mode = {}
    total = 0
    for j in jobs:
        for s in j.saved:
            m = s.get("mode") or "app"
            cost = MODE_COST.get(m, 1)
            slot = by_mode.setdefault(m, {"images": 0, "credits": 0})
            slot["images"] += 1
            slot["credits"] += cost
            total += cost
    return total, by_mode


def _emit_result(args, date, jobs, manifest_dir):
    """服务商 matscaimg 风格机读产物:
      ① out_dir/manifest.json —— {created_at, results[], errors[], total_credits, by_mode}
      ② stdout —— {ok, saved_images, failed_prompts, total_credits, by_mode, manifest, output_dir} 摘要 JSON
      ③ --json-out(可选)—— 写整份 manifest(后台轮询用,有 ok/results/errors)
    """
    results = [_job_result(j) for j in jobs if j.primary is not None]
    errors = [
        {"job_index": j.index, "name": j.name, "prompt": j.prompt,
         "error": j.last_error or "无图片返回"}
        for j in jobs if j.primary is None
    ]
    results.sort(key=lambda r: int(r.get("job_index") or 0))
    errors.sort(key=lambda e: int(e.get("job_index") or 0))

    total_credits, by_mode = _compute_billing(jobs)
    storm = _storm_summary()
    manifest_path, manifest = write_manifest(manifest_dir, results, errors, total_credits, by_mode, storm)
    summary = {
        "ok": not errors,
        "saved_images": sum(len(j.saved) for j in jobs),
        "failed_prompts": len(errors),
        "total_credits": total_credits,
        "by_mode": by_mode,
        # 上游网关风暴元数据:本次是否撞过风暴 / 累计 5xx / 是否熔断收尾
        "upstream_storm": storm["upstream_storm"],
        "storm_5xx": storm["storm_5xx"],
        "gave_up": storm["gave_up"],
        "manifest": manifest_path,
        "output_dir": manifest_dir,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    sys.stdout.flush()

    if getattr(args, "json_out", None):
        # 后台 fire-and-forget 轮询目标:写整份 manifest + 顶层摘要(含 ok),供编排层判完成
        out = dict(manifest)
        out.update(summary)
        try:
            d = os.path.dirname(os.path.abspath(args.json_out))
            if d:
                os.makedirs(d, exist_ok=True)
            with open(args.json_out, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False)
        except OSError as e:
            sys.stderr.write("写 --json-out 失败: %s\n" % e)
            sys.stderr.flush()
    return summary


# ── 入参 → Job 列表 ──────────────────────────────────────────────────────────
def _build_params(args, mode, overrides=None):
    """构造发给服务端的参数白名单(只放非空字段;来自服务商 image_payload 白名单)。"""
    overrides = overrides or {}
    src = {
        "model": args.model,
        "size": overrides.get("size", args.size),
        "quality": overrides.get("quality", args.quality),
        "moderation": overrides.get("moderation", args.moderation),
        "background": overrides.get("background", args.background),
        "style": overrides.get("style", args.style),
        "output_image_format": overrides.get("output_format", args.output_format),
        "output_compression": overrides.get("output_compression", args.output_compression),
        "input_fidelity": overrides.get("input_fidelity", args.input_fidelity),
    }
    params = {}
    for k, v in src.items():
        if v is None or str(v) == "":
            continue
        if k == "output_image_format":
            v = str(v).lower().replace("jpg", "jpeg")
        params[k] = v
    return params


def _read_image(path):
    with open(path, "rb") as f:
        return f.read(), os.path.basename(path)


def _load_prompts_file(path):
    """读批量任务文件:JSON 列表(对象或字符串)或纯文本(每行一个 prompt)。返回 spec 列表。"""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    specs = []
    parsed = None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, list):
        for i, item in enumerate(parsed, start=1):
            if isinstance(item, str):
                specs.append({"name": "图%d" % i, "prompt": item})
            elif isinstance(item, dict) and (item.get("prompt") or item.get("variation") or item.get("edit")):
                # 文生图需 prompt;图生图(variation)/改图(edit)允许无 prompt
                specs.append(dict(item, name=item.get("name") or "图%d" % i))
    else:
        for line in (l.strip() for l in text.splitlines()):
            if line and not line.startswith("#"):
                specs.append({"name": "图%d" % (len(specs) + 1), "prompt": line})
    if not specs:
        sys.exit("批量文件没有可用的 prompt:%s" % path)
    return specs


def _item_mode(spec, modes):
    """解析批量项的 mode 钉死值:必须落在本次可用 modes 内,否则报错。None=交给调度器自选。"""
    m = spec.get("mode")
    if not m:
        return None
    if m not in modes:
        sys.exit("批量项 %s 指定 mode=%s,但本次 --modes 只有 %s" % (spec.get("name"), m, ",".join(modes)))
    return m


def build_jobs(args, modes, response_format, date):
    """根据 --prompts-file(批量)或位置 prompt(单内容)构造 Job 列表。
    modes=本次可用模式列表(已按成本升序);per-job mode/single 可在批量项内逐项覆盖。"""
    pmode = modes[0]  # 构造 params 用任一模式即可(参数白名单与模式无关)
    jobs = []
    if args.prompts_file:
        if args.out:
            sys.stderr.write("批量模式忽略 -o(用 --outdir + 各内容名命名)\n")
        for spec in _load_prompts_file(args.prompts_file):
            params = _build_params(args, pmode, overrides=spec)
            edit_image = edit_name = mask = mask_name = var_image = var_name = None
            if spec.get("variation"):
                var_image, var_name = _read_image(spec["variation"])
            if spec.get("edit"):
                edit_image, edit_name = _read_image(spec["edit"])
            if spec.get("mask"):
                mask, mask_name = _read_image(spec["mask"])
            single = bool(spec.get("single", args.single_call))
            jobs.append(Job(
                name=spec["name"], prompt=spec.get("prompt", ""),
                need=int(spec.get("n", args.n)), params=params, response_format=response_format,
                out=None, outdir=args.outdir, date=date,
                edit_image=edit_image, edit_name=edit_name, mask=mask, mask_name=mask_name,
                variation_image=var_image, variation_name=var_name,
                single=single, mode=_item_mode(spec, modes),
            ))
    else:
        var_image = var_name = None
        if args.variation:
            var_image, var_name = _read_image(args.variation)
        if not args.prompt and var_image is None:
            sys.exit("缺少提示词:给一个位置参数 prompt,或用 --variation 跑图生图,或用 --prompts-file 跑批量")
        params = _build_params(args, pmode)
        edit_image = edit_name = mask = mask_name = None
        if args.edit:
            edit_image, edit_name = _read_image(args.edit)
        if args.mask:
            mask, mask_name = _read_image(args.mask)
        jobs.append(Job(
            name=args.name or "image", prompt=args.prompt or "",
            need=int(args.n), params=params, response_format=response_format,
            out=args.out, outdir=args.outdir, date=date,
            edit_image=edit_image, edit_name=edit_name, mask=mask, mask_name=mask_name,
            variation_image=var_image, variation_name=var_name,
            single=bool(args.single_call), mode=None,
        ))
    for i, j in enumerate(jobs, start=1):
        j.index = i
    return jobs


def main():
    ap = argparse.ArgumentParser(description="matsca 文生图/改图(matscaimg 内核 + 批量/主备偏好层)")
    ap.add_argument("prompt", nargs="?", default=None, help="提示词(单内容);批量用 --prompts-file 时可省略")
    ap.add_argument("--prompts-file", default=None, dest="prompts_file",
                    help="批量任务文件:JSON 列表 [{\"name\",\"prompt\",\"n\"?,\"size\"?,\"edit\"?,\"mask\"?}]、"
                         "JSON 字符串列表、或纯文本每行一个 prompt。所有内容在一个进程的线程池里跑(最防撞墙)。")
    ap.add_argument("-o", "--out", default=None, help="显式输出路径(仅单内容;批量请用 --outdir)")
    ap.add_argument("--name", default=None, help="图片描述(用作文件名);不带 -o 时按 <outdir>/<描述>_<日期>.<ext> 命名")
    ap.add_argument("--outdir", default=os.path.join("output", "fig"), help="默认输出目录(默认 output/fig)")
    ap.add_argument("--date", default=None, help="文件名日期后缀(默认今天 YYYYMMDD)")
    ap.add_argument("--model", default="gpt-image-2")
    ap.add_argument("--size", default="1536x864", help="默认 1536x864(16:9);方图 1024x1024;竖图 1024x1536;auto")
    ap.add_argument("--quality", default="high", choices=["auto", "low", "medium", "high"])
    ap.add_argument("-n", type=int, default=2, help="每内容生成数量(默认 2:第1张主图,其余存为备选;“就一张”用 -n 1)")
    # ── 图生图/改图 ──
    ap.add_argument("--edit", default=None, help="改图:原图路径,走 /v1/images/edits(把 prompt 应用到该图)")
    ap.add_argument("--mask", default=None, help="改图蒙版路径(可选):标注要重绘的区域(配合 --edit)")
    ap.add_argument("--variation", default=None,
                    help="图生图/变体:原图路径,走 /v1/images/variations(无需 prompt,生成该图的变体;"
                         "批量项写 \"variation\":\"图路径\")")
    # ── 服务商支持的可选透传参数(空则不发)──
    ap.add_argument("--background", default=None, choices=["auto", "transparent", "opaque"],
                    help="背景:transparent=透明底(图标/logo 常用,透明 JPEG 会自动转 PNG)")
    ap.add_argument("--style", default=None, choices=["vivid", "natural"], help="风格:vivid 鲜艳 / natural 自然")
    ap.add_argument("--output-format", default=None, dest="output_format", choices=["png", "jpeg", "jpg", "webp"],
                    help="输出图片格式(默认服务端 png;jpg 归一为 jpeg)")
    ap.add_argument("--output-compression", default=None, type=int, dest="output_compression",
                    help="jpeg/webp 压缩 0~100")
    ap.add_argument("--input-fidelity", default=None, dest="input_fidelity", choices=["low", "high"],
                    help="改图时对原图的保真度(仅 --edit 有意义)")
    ap.add_argument(
        "--concurrency", type=int, default=0,
        help="单进程线程池并发上限(所有内容共用)。默认 0=按模式自适应:app→3(单密钥在途硬顶4、留1余量)/"
             "direct→4/native→3;显式给数字则用你的。调高易触发账户级风控限流",
    )
    ap.add_argument(
        "--retries", type=int, default=5,
        help="单张图遇服务端/容量类瞬时错误时的最大重试次数(默认 5;客户侧错误一律不重试)",
    )
    ap.add_argument(
        "--timeout", type=int, default=600,
        help="单次同步请求 HTTP 墙钟上限(秒,默认 600;正常出图本就要几十秒~两三分钟)",
    )
    ap.add_argument(
        "--moderation", default="low", choices=["auto", "low"],
        help="内容安全审查档位(默认 low,减少误拦;被拒不退款且可能罚金、绝不重试)",
    )
    ap.add_argument(
        "--mode", default="auto", choices=["auto", "app", "direct", "native"],
        help="单模式调用(向后兼容):app=同步×1(需App凭证,最划算)/direct=同步×2(仅需Key)/"
             "native=同步url下载×3;auto(默认)=有App凭证则app、否则direct。给了 --modes 则忽略本项。",
    )
    ap.add_argument(
        "--modes", default=None,
        help="双/多模式:逗号列表如 'app,direct'。每把 key 一个独立在途池(app≤3/direct≤4/native≤3),"
             "总槽位=各池相加,扩的是你自己的在途天花板(救不了上游池拥堵)。不给则退回 --mode 单模式。"
             "批量项可写 \"mode\":\"direct\" 钉死某内容走哪把 key。",
    )
    ap.add_argument(
        "--dispatch", default="cost", choices=["cost", "throughput"],
        help="双模式分配策略:cost(默认)=app主、direct溢出(绝大多数图仍1积分、最省)/"
             "throughput=两池拉满、最快(direct 2积分用得多)。",
    )
    ap.add_argument(
        "--single-call", action="store_true", dest="single_call",
        help="赛马开关:默认赛马(-nN 拆 N 个并行 n=1、先到为主、早交付、占 N 槽);"
             "开启则单请求 n=N(占1槽、1请求、全有或全无、无早交付)。批量项可写 \"single\":true/false 逐项覆盖。",
    )
    ap.add_argument(
        "--storm-give-up-after", type=int, default=None, dest="storm_give_up_after",
        help="(可选熔断,默认关)上游网关 502/503/504 风暴持续超过此秒数就提前收尾:停止重试、"
             "返回已出的图 + 缺图清单(manifest.errors[]),不再耗尽 --retries 预算。不给=一直有界退避重试。",
    )
    ap.add_argument(
        "--proxy", default=None,
        help="原生模式下载图片用的 HTTP/SOCKS 代理(默认读环境 MATSCA_PROXY;无则直连下载)",
    )
    ap.add_argument(
        "--json-out", default=None, dest="json_out",
        help="把最终结果摘要(JSON)额外写到此文件,供后台轮询判完成。",
    )
    ap.add_argument(
        "--log-file", default=None, dest="log_file",
        help="把本任务进度日志(每行带时间戳)写到此文件,并在 <log>.heartbeat 写每 ~15s 心跳;"
             "不给且给了 --json-out 时自动派生 <json-out>.log。",
    )
    args = ap.parse_args()

    # ── 解析本次可用模式列表(--modes 优先,否则退回 --mode 单模式),按成本升序 ──
    if args.modes:
        raw = [m.strip() for m in args.modes.split(",") if m.strip()]
        bad = [m for m in raw if m not in MODE_KEY_ENV]
        if bad:
            sys.exit("--modes 含未知模式 %s(只支持 app/direct/native)" % ",".join(bad))
        seen = []
        for m in raw:
            if m not in seen:
                seen.append(m)
        modes = sorted(seen, key=lambda m: MODE_COST[m])
    else:
        modes = [resolve_mode(args.mode)]
    if not modes:
        sys.exit("没有可用模式")
    if "app" in modes and not get_app_headers():
        sys.exit("应用模式需 MATSCA_APP_ID/MATSCA_APP_SECRET(secrets.env);否则把 app 从 --modes 去掉或改 direct/native")

    key_by_mode = {m: get_key_for_mode(m) for m in modes}
    response_format = "b64_json"  # 占位:实际 response_format 在 generate_images 内按各请求 mode 现算
    date = args.date or time.strftime("%Y%m%d")
    jobs = build_jobs(args, modes, response_format, date)

    total_n = sum(j.need for j in jobs)
    label = jobs[0].name if len(jobs) == 1 else "batch(%d):%s" % (len(jobs), ",".join(j.name for j in jobs))
    _start_observability(args, label, total_n)

    sys.stderr.write(
        "计费模式:%s(%s),分配策略 %s,赛马开关 %s\n"
        % (",".join(modes), "/".join(MODE_MULT[m] for m in modes), args.dispatch,
           "单请求(single-call)" if args.single_call else "赛马(默认)")
    )
    sys.stderr.flush()

    # 每把 key 一个独立在途池:auto 贴服务商单密钥在途硬顶留余量(app4取3/direct8取4/native取3);
    # --concurrency>0 则覆盖各池上限(仍受硬顶约束)。总线程=各池相加。
    auto_cap = {"app": 3, "direct": 4, "native": 3}
    hard_cap = {"app": 4, "direct": 8, "native": 8}
    caps = {}
    for m in modes:
        want = args.concurrency if args.concurrency and args.concurrency > 0 else auto_cap[m]
        caps[m] = max(1, min(want, hard_cap[m], total_n))
    proxy = args.proxy or os.environ.get("MATSCA_PROXY")
    if any(j.variation_image for j in jobs):
        endpoint_label = "/v1/images/variations 图生图"
    elif any(j.edit_image for j in jobs):
        endpoint_label = "/v1/images/edits 改图"
    else:
        endpoint_label = "/v1/images/generations"
    sys.stderr.write(
        "%s:%d 个内容共 %d 张,事件驱动 Coverage-First;在途池 %s(各池永不超单密钥在途硬顶);"
        "撞上游池满连续 %d 次自动降 N→1;失败按服务商规则退避重试(最多 %d 次),绝不探活/ping。%s\n"
        % (endpoint_label, len(jobs), total_n,
           "/".join("%s≤%d" % (m, caps[m]) for m in modes), DOWNGRADE_THRESHOLD, args.retries,
           ("" if args.storm_give_up_after is None
            else "上游网关风暴持续超 %ds 将熔断收尾(返回部分结果)。" % args.storm_give_up_after))
    )
    sys.stderr.flush()

    try:
        run_jobs(jobs, key_by_mode, caps, args.dispatch, modes,
                 args.retries, args.timeout, proxy, args.output_format,
                 args.storm_give_up_after)
    except KeyboardInterrupt:
        raise

    # 人读清单:有备选的内容写进各自 outdir 的 _index.md(批量同目录则合并到一份)。
    by_dir = {}
    for j in jobs:
        if j.alts:
            d = os.path.dirname(os.path.abspath(j.primary)) if j.out else j.outdir
            by_dir.setdefault(d, []).append(j)
    for d, js in by_dir.items():
        idx = _write_index_md(d, js)
        print("共 %d 张备选,清单: %s" % (sum(len(j.alts) for j in js), idx))

    # 机读产物(服务商风格):manifest.json 写到本次主输出目录。
    if len(jobs) == 1 and jobs[0].out:
        manifest_dir = os.path.dirname(os.path.abspath(jobs[0].out)) or "."
    else:
        manifest_dir = os.path.abspath(args.outdir)
    summary = _emit_result(args, date, jobs, manifest_dir)

    if not any(j.primary is not None for j in jobs):
        sys.exit("无图片返回")
    if not summary["ok"]:
        # 批量部分失败:主图有但不全。退出码 0(可用),缺哪个见 manifest.json 的 errors[]。
        sys.stderr.write("注意:部分内容未出主图,详见 manifest.json 的 errors[]\n")
        sys.stderr.flush()


if __name__ == "__main__":
    main()
