#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线测试：monkeypatch 网络层，不联网验证调度/重试/赛马/受阻/manifest/回传策略。

按依赖顺序串行跑，任一 assert 失败即终止。运行：python offline_test.py
"""

import base64
import json
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gen_image as G
import make_preview as MP
import auto_deliver as AD

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nFAKE").decode()
JPG_BYTES = b"\xff\xd8\xff\xe0FAKEJPG"
WEBP_BYTES = b"RIFF\x00\x00\x00\x00WEBPVP8 "


def _ok_payload(n):
    return {"data": [{"b64_json": PNG, "output_format": "png"} for _ in range(n)]}


def run_sched(tasks, keys, **kw):
    khs = [G.KeyHealth(kid, raw) for kid, raw in keys]
    outdir = tempfile.mkdtemp()
    s = G.Scheduler(tasks, khs, outdir, timeout=5, **kw)
    s.run()
    return s, outdir


def _task(index=1, name=None, prompt="p", n=1, **kw):
    return G.Task(index=index, name=name or ("图%d" % index), prompt=prompt, n=n,
                  size=kw.pop("size", "auto"), model=kw.pop("model", "gpt-image-2"), **kw)


class _Patch:
    """临时替换 gen_image 模块级函数，退出时恢复。"""
    def __init__(self, **subs):
        self.subs = subs
        self.saved = {}

    def __enter__(self):
        for name, fn in self.subs.items():
            self.saved[name] = getattr(G, name)
            setattr(G, name, fn)
        return self

    def __exit__(self, *a):
        for name, fn in self.saved.items():
            setattr(G, name, fn)


# --------------------------------------------------------------------------- #
# Phase 0 — 核心策略
# --------------------------------------------------------------------------- #
def test_concurrency_caps():
    cur = {"g": 0, "max_g": 0, "per": {}, "max_per": {}}
    lock = threading.Lock()

    def fake_gen(key, payload, timeout=600):
        kid = key  # raw==kid in this test
        with lock:
            cur["g"] += 1
            cur["max_g"] = max(cur["max_g"], cur["g"])
            cur["per"][kid] = cur["per"].get(kid, 0) + 1
            cur["max_per"][kid] = max(cur["max_per"].get(kid, 0), cur["per"][kid])
        time.sleep(0.2)
        with lock:
            cur["g"] -= 1
            cur["per"][kid] -= 1
        return _ok_payload(payload["n"])

    tasks = [_task(index=i, name="图%d" % i, n=2) for i in range(1, 9)]
    keys = [("k1", "k1"), ("k2", "k2"), ("k3", "k3")]
    with _Patch(generate_image=fake_gen):
        s, outdir = run_sched(tasks, keys, race=True, coverage_first=True)
    assert cur["max_g"] <= G.GLOBAL_IMAGE_REQUEST_CONCURRENCY, cur["max_g"]
    for kid, m in cur["max_per"].items():
        assert m <= G.QUEUE_CONCURRENCY_PER_KEY, (kid, m)
    assert all(t.status == "completed" for t in tasks)
    print("ok test_concurrency_caps (max_g=%d)" % cur["max_g"])


def test_single_call_n():
    seen = {}

    def fake_gen(key, payload, timeout=600):
        seen["n"] = payload["n"]
        return _ok_payload(payload["n"])

    t = _task(n=3)
    with _Patch(generate_image=fake_gen):
        s, outdir = run_sched([t], [("k1", "k1")], race=False, coverage_first=False)
    assert seen["n"] == 3, seen
    assert t.got == 3 and t.status == "completed"
    print("ok test_single_call_n")


def test_transient_retry_then_success():
    calls = {"n": 0}

    def fake_gen(key, payload, timeout=600):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise G.RequestError("busy", status=503, code="upstream_rate_limited",
                                 retry_after_ms=10)
        return _ok_payload(payload["n"])

    t = _task(n=1)
    with _Patch(generate_image=fake_gen):
        s, outdir = run_sched([t], [("k1", "k1")], race=True, coverage_first=True)
    assert t.status == "completed" and t.got == 1, (t.status, t.got)
    assert t.retry_count >= 1
    print("ok test_transient_retry_then_success (calls=%d)" % calls["n"])


def test_content_policy_no_retry():
    calls = {"n": 0}

    def fake_gen(key, payload, timeout=600):
        calls["n"] += 1
        raise G.RequestError("blocked", status=400, code="content_policy_violation")

    t = _task(n=1)
    with _Patch(generate_image=fake_gen):
        s, outdir = run_sched([t], [("k1", "k1")], race=True, coverage_first=True)
    assert t.status == "failed", t.status
    assert calls["n"] == 1, calls  # 不重试
    print("ok test_content_policy_no_retry")


def test_failover_on_401():
    used = []

    def fake_gen(key, payload, timeout=600):
        used.append(key)
        if key == "kbad":
            raise G.RequestError("unauthorized", status=401, code="account_token_invalid")
        return _ok_payload(payload["n"])

    t = _task(n=1)
    # 强制首选 kbad
    t.preferred_key = "kbad"
    with _Patch(generate_image=fake_gen):
        s, outdir = run_sched([t], [("kbad", "kbad"), ("kgood", "kgood")],
                              race=True, coverage_first=True)
    assert t.status == "completed" and t.got == 1, (t.status, t.got)
    assert "kgood" in used, used
    print("ok test_failover_on_401 (used=%s)" % used)


def test_preflight_ping_banned():
    def fake_ping(key):
        if key == "kban":
            return {"auth": {"banned": True, "ban_remaining_seconds": 30}}
        return {"auth": {"banned": False}}

    def fake_gen(key, payload, timeout=600):
        return _ok_payload(payload["n"])

    t = _task(n=1)
    khs = [G.KeyHealth("kban", "kban"), G.KeyHealth("kok", "kok")]
    outdir = tempfile.mkdtemp()
    with _Patch(ping_server=fake_ping, generate_image=fake_gen):
        s = G.Scheduler([t], khs, outdir, timeout=5, preflight_ping=True,
                        race=True, coverage_first=True)
        # 仅测预检对 banned 上冷却
        s._preflight()
        assert s.keys["kban"].cooldown_until_ms > G._now_ms(), "banned key 应被上冷却"
        assert s.keys["kok"].cooldown_until_ms == 0
    print("ok test_preflight_ping_banned")


# --------------------------------------------------------------------------- #
# Phase 1 — 取图/命名/参数/端点
# --------------------------------------------------------------------------- #
def test_fetch_url_and_datauri_and_ext():
    datauri = "data:image/png;base64," + base64.b64encode(JPG_BYTES).decode()
    real_dl = G._download_image

    def fake_dl(url, timeout=600, proxy=None):
        if url.startswith("data:"):
            return real_dl(url, timeout=timeout, proxy=proxy)  # data: URI 走真实解码
        return WEBP_BYTES                                       # http(s) url 返回 webp 字节

    with _Patch(_download_image=fake_dl):
        payload = {"data": [
            {"url": "http://x/y.png", "output_format": "png"},
            {"url": datauri},
        ]}
        imgs = G._extract_images(payload, timeout=5)
    assert imgs[0]["format"] == "webp", imgs[0]["format"]   # magic bytes 胜过 hint
    assert imgs[1]["format"] == "jpg", imgs[1]["format"]    # data: URI 解出 JPG 头
    # jpeg→jpg 归一
    assert G.detect_image_ext(b"\x00\x00", "jpeg") == "jpg"
    print("ok test_fetch_url_and_datauri_and_ext")


def test_filename_sanitize():
    assert G._safe_name("a/b:c*d?山水") == "a-b-c-d-山水", G._safe_name("a/b:c*d?山水")
    assert G._safe_name("") and len(G._safe_name("")) == 12
    print("ok test_filename_sanitize")


def test_params_passthrough():
    seen = {}

    def fake_gen(key, payload, timeout=600):
        seen.update(payload)
        return _ok_payload(payload["n"])

    class A:
        pass
    a = A()
    for k in G._PARAM_KEYS:
        setattr(a, k, None)
    a.output_format = "jpg"
    a.quality = "high"
    params = G._build_params(a)
    assert params["output_image_format"] == "jpeg", params
    assert params["quality"] == "high"
    t = _task(n=1)
    t.params = params
    with _Patch(generate_image=fake_gen):
        s, outdir = run_sched([t], [("k1", "k1")], race=True, coverage_first=True)
    assert seen.get("output_image_format") == "jpeg", seen
    print("ok test_params_passthrough")


def test_variation_endpoint():
    hit = {"var": False, "gen": False}

    def fake_var(key, fields, files, timeout=600):
        hit["var"] = True
        assert "image" in files
        return _ok_payload(int(fields["n"]))

    def fake_gen(key, payload, timeout=600):
        hit["gen"] = True
        return _ok_payload(payload["n"])

    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.write(JPG_BYTES)
    tmp.close()
    t = _task(n=1, prompt="", var_path=tmp.name)
    with _Patch(variation_image=fake_var, generate_image=fake_gen):
        s, outdir = run_sched([t], [("k1", "k1")], race=True, coverage_first=True)
    assert hit["var"] and not hit["gen"], hit
    print("ok test_variation_endpoint")


def test_edit_with_mask():
    got = {}

    def fake_edit(key, fields, files, timeout=600):
        got["files"] = set(files.keys())
        return _ok_payload(int(fields["n"]))

    img = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    img.write(JPG_BYTES)
    img.close()
    mask = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    mask.write(JPG_BYTES)
    mask.close()
    t = _task(n=1, prompt="edit", edit_path=img.name, mask_path=mask.name)
    with _Patch(edit_image=fake_edit):
        s, outdir = run_sched([t], [("k1", "k1")], race=True, coverage_first=True)
    assert got["files"] == {"image", "mask"}, got
    print("ok test_edit_with_mask")


# --------------------------------------------------------------------------- #
# Phase 2 — 赛马 + 覆盖优先
# --------------------------------------------------------------------------- #
def test_race_splits_into_n1():
    ns = []
    lock = threading.Lock()

    def fake_gen(key, payload, timeout=600):
        with lock:
            ns.append(payload["n"])
        time.sleep(0.05)
        return _ok_payload(payload["n"])

    t = _task(n=3)
    with _Patch(generate_image=fake_gen):
        s, outdir = run_sched([t], [("k1", "k1"), ("k2", "k2")],
                              race=True, coverage_first=True)
    assert all(n == 1 for n in ns), ns
    assert t.got == 3 and t.status == "completed"
    print("ok test_race_splits_into_n1 (issued=%d)" % len(ns))


def test_coverage_first_spreads_primaries():
    order = []
    lock = threading.Lock()

    def fake_gen(key, payload, timeout=600):
        with lock:
            order.append(payload["prompt"])
        time.sleep(0.05)
        return _ok_payload(payload["n"])

    t1 = _task(index=1, name="A", prompt="A", n=2)
    t2 = _task(index=2, name="B", prompt="B", n=2)
    with _Patch(generate_image=fake_gen):
        s, outdir = run_sched([t1, t2], [("k1", "k1")], race=True, coverage_first=True)
    # 单 Key 串行：覆盖优先应先各出一张（A,B,...）而不是 A,A,B,B
    assert order[:2] == ["A", "B"] or order[:2] == ["B", "A"], order
    assert t1.got == 2 and t2.got == 2
    print("ok test_coverage_first_spreads_primaries (order=%s)" % order)


def test_partial_success_keeps_primary():
    calls = {"n": 0}
    lock = threading.Lock()

    def fake_gen(key, payload, timeout=600):
        with lock:
            calls["n"] += 1
            c = calls["n"]
        if c == 1:
            return _ok_payload(1)  # 主图成功
        raise G.RequestError("policy", status=400, code="content_policy_violation")

    t = _task(n=3)
    with _Patch(generate_image=fake_gen):
        s, outdir = run_sched([t], [("k1", "k1")], race=True, coverage_first=True)
    assert t.got >= 1, t.got
    assert t.status == "completed", t.status  # 已出≥1 张绝不 failed
    assert len(t.results) >= 1 and t.results[0]["role"] == "primary"
    print("ok test_partial_success_keeps_primary (got=%d)" % t.got)


# --------------------------------------------------------------------------- #
# Phase 3 — 受阻 SOP
# --------------------------------------------------------------------------- #
def test_blocked_detection_and_giveup():
    def fake_gen(key, payload, timeout=600):
        raise G.RequestError("bad gateway", status=502, code="upstream_server_error")

    def fake_ping(key):
        return {"auth": {"banned": False}}

    t = _task(n=1)
    khs = [G.KeyHealth("k1", "k1")]
    outdir = tempfile.mkdtemp()
    with _Patch(generate_image=fake_gen, ping_server=fake_ping):
        s = G.Scheduler([t], khs, outdir, timeout=5, race=True, coverage_first=True,
                        block_after_ms=200, give_up_after_ms=400, ping_refine=True)
        s.run()
    assert s.blocked or s.gave_up, (s.blocked, s.gave_up)
    assert s.block_reason in ("502_storm", "key_banned"), s.block_reason
    assert s.gave_up, "开了 give-up 应提前收尾"
    assert t.status in ("failed", "completed")
    mani = json.load(open(os.path.join(outdir, "manifest.json"), encoding="utf-8"))
    assert mani["gave_up"] is True, mani.get("gave_up")
    print("ok test_blocked_detection_and_giveup (reason=%s)" % s.block_reason)


def test_blocked_ping_distinguishes_ban():
    def fake_gen(key, payload, timeout=600):
        raise G.RequestError("rate", status=429, code="upstream_rate_limited")

    def fake_ping(key):
        return {"auth": {"banned": True, "ban_remaining_seconds": 60}}

    t = _task(n=1)
    khs = [G.KeyHealth("k1", "k1")]
    outdir = tempfile.mkdtemp()
    with _Patch(generate_image=fake_gen, ping_server=fake_ping):
        s = G.Scheduler([t], khs, outdir, timeout=5, race=True, coverage_first=True,
                        block_after_ms=200, give_up_after_ms=600, ping_refine=True)
        s.run()
    assert s.block_reason == "key_banned", s.block_reason
    print("ok test_blocked_ping_distinguishes_ban")


# --------------------------------------------------------------------------- #
# Phase 4 — 健壮性修复回归
# --------------------------------------------------------------------------- #
def test_missing_prompts_file_clean_exit():
    try:
        G._load_prompts_file("/nope/does-not-exist-123.json")
    except SystemExit as e:
        assert "无法读取" in str(e), str(e)
        print("ok test_missing_prompts_file_clean_exit")
        return
    raise AssertionError("应当 sys.exit")


def test_dedupe_duplicate_names():
    ts = [_task(index=1, name="同名"), _task(index=2, name="同名"), _task(index=3, name="同名")]
    G._dedupe_names(ts)
    names = [t.name for t in ts]
    assert len(set(names)) == 3, names
    assert names[0] == "同名"
    print("ok test_dedupe_duplicate_names (%s)" % names)


def test_mask_without_edit_errors():
    t = _task(n=1, mask_path="/x/mask.png")  # 没 edit_path
    try:
        G._validate_tasks([t])
    except SystemExit as e:
        assert "mask" in str(e).lower()
        print("ok test_mask_without_edit_errors")
        return
    raise AssertionError("应当 sys.exit")


def test_n_out_of_range_clamped():
    assert _task(n=9).n == 4
    assert _task(n=0).n == 1
    assert _task(n=-3).n == 1
    print("ok test_n_out_of_range_clamped")


def test_parse_json_html_truncated():
    html = b"<html><body>" + b"x" * 500 + b"</body></html>"
    out = G._parse_json(html)
    assert "message" in out
    assert "\n" not in out["message"] and len(out["message"]) <= 201, len(out["message"])
    print("ok test_parse_json_html_truncated")


def test_initial_manifest_written_before_first_event():
    started = threading.Event()

    def fake_gen(key, payload, timeout=600):
        started.set()
        time.sleep(0.3)
        return _ok_payload(payload["n"])

    t = _task(n=1)
    khs = [G.KeyHealth("k1", "k1")]
    outdir = tempfile.mkdtemp()
    with _Patch(generate_image=fake_gen):
        s = G.Scheduler([t], khs, outdir, timeout=5, race=True, coverage_first=True)
        th = threading.Thread(target=s.run, daemon=True)
        th.start()
        started.wait(2)
        time.sleep(0.05)
        mani = json.load(open(os.path.join(outdir, "manifest.json"), encoding="utf-8"))
        assert mani["pending_count"] == 1, mani
        th.join(5)
    print("ok test_initial_manifest_written_before_first_event")


def test_save_keys_to_secrets_roundtrip():
    f = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False, encoding="utf-8")
    f.write("# 注释\nMATSCA_DEV_EMAIL=a@b.com\nMATSCA_API_KEYS=old1,old2\nMATSCA_DEV_PASSWORD=pw\n")
    f.close()
    G.save_keys_to_secrets(f.name, ["new1", "new2", "new3"])
    txt = open(f.name, encoding="utf-8").read()
    assert "MATSCA_API_KEYS=new1,new2,new3" in txt, txt
    assert "MATSCA_DEV_EMAIL=a@b.com" in txt
    assert "MATSCA_DEV_PASSWORD=pw" in txt
    assert txt.count("MATSCA_API_KEYS=") == 1, txt
    print("ok test_save_keys_to_secrets_roundtrip")


def test_stale_static_keys_self_heal():
    f = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False, encoding="utf-8")
    f.write("MATSCA_API_KEYS=stale1,stale2\nMATSCA_DEV_EMAIL=a@b.com\nMATSCA_DEV_PASSWORD=pw\n")
    f.close()

    def fake_healthy(keys):
        return False  # 静态 Key 全失效

    def fake_reveal(token, email, password):
        return [("kfresh", "freshkey")]

    class A:
        keys = None
        secrets_file = f.name
        dev_token = None
        email = None
        password = None
    a = A()
    with _Patch(_any_key_healthy=fake_healthy, _reveal_via_creds=fake_reveal):
        out = G.resolve_keys(a)
    assert out == [("kfresh", "freshkey")], out
    assert a._keys_from_reveal is True
    print("ok test_stale_static_keys_self_heal")


def test_healthy_static_keys_no_reveal():
    f = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False, encoding="utf-8")
    f.write("MATSCA_API_KEYS=good1,good2\nMATSCA_DEV_EMAIL=a@b.com\nMATSCA_DEV_PASSWORD=pw\n")
    f.close()
    revealed = {"called": False}

    def fake_healthy(keys):
        return True

    def fake_reveal(token, email, password):
        revealed["called"] = True
        return [("x", "y")]

    class A:
        keys = None
        secrets_file = f.name
        dev_token = None
        email = None
        password = None
    a = A()
    with _Patch(_any_key_healthy=fake_healthy, _reveal_via_creds=fake_reveal):
        out = G.resolve_keys(a)
    assert not revealed["called"], "健康 Key 不应触发 reveal"
    assert [kid for kid, _ in out] == ["key1-ood1", "key2-ood2"], out
    print("ok test_healthy_static_keys_no_reveal")


# --------------------------------------------------------------------------- #
# Phase 5 — 默认常开 + manifest 实时
# --------------------------------------------------------------------------- #
def test_default_race_and_coverage_on():
    ap = G.build_parser()
    args = ap.parse_args(["hello"])
    assert args.race is True and args.coverage_first is True
    assert args.n == 2 and args.size == "auto" and args.model == "gpt-image-2"
    print("ok test_default_race_and_coverage_on")


def test_manifest_live_partial_progress():
    gate = threading.Event()
    calls = {"n": 0}
    lock = threading.Lock()

    def fake_gen(key, payload, timeout=600):
        with lock:
            calls["n"] += 1
            c = calls["n"]
        if c == 1:
            return _ok_payload(1)   # 第一张立刻出
        gate.wait(3)                # 其余阻塞，制造"部分进展"窗口
        return _ok_payload(1)

    t = _task(n=3)
    khs = [G.KeyHealth("k1", "k1"), G.KeyHealth("k2", "k2"), G.KeyHealth("k3", "k3")]
    outdir = tempfile.mkdtemp()
    with _Patch(generate_image=fake_gen):
        s = G.Scheduler([t], khs, outdir, timeout=5, race=True, coverage_first=True)
        th = threading.Thread(target=s.run, daemon=True)
        th.start()
        ok = False
        for _ in range(60):
            time.sleep(0.1)
            try:
                mani = json.load(open(os.path.join(outdir, "manifest.json"), encoding="utf-8"))
            except (OSError, ValueError):
                continue
            # 中途应能读到：已出 1 张、该内容 result 标 complete=False、n_got<need
            if mani["saved_images"] >= 1 and mani["results"]:
                r = mani["results"][0]
                if r["complete"] is False and r["n_got"] < 3:
                    ok = True
                    break
        gate.set()
        th.join(5)
    assert ok, "应能在中途读到部分进展（saved>=1, 该内容 complete=False）"
    print("ok test_manifest_live_partial_progress")


def test_race_late_success_recovers_failed_task():
    # 模拟：先到的请求失败把任务标记为 exhausted/failed，晚到的成功翻回 completed
    t = _task(n=1)
    khs = [G.KeyHealth("k1", "k1")]
    outdir = tempfile.mkdtemp()
    s = G.Scheduler([t], khs, outdir, timeout=5, race=True, coverage_first=True)
    # 手工灌入两条在飞
    for _ in range(2):
        t.inflight += 1
        t.inflight_images += 1
        s.global_inflight += 1
        s.keys["k1"].running += 1
        s.per_key_inflight["k1"] += 1
    # 第一条：致命错误 → exhausted（但还有一条在飞，不应立刻 failed）
    err = G.RequestError("policy", status=400, code="content_policy_violation")
    s._handle_event(("err", t, "k1", 1, err))
    assert t.exhausted is True
    # 第二条：成功 → 翻回 completed
    s._handle_event(("ok", t, "k1", 1, [{"data": JPG_BYTES, "format": "jpg"}]))
    assert t.got == 1 and t.status == "completed", (t.got, t.status)
    print("ok test_race_late_success_recovers_failed_task")


def test_manifest_never_hides_disk_images():
    t = _task(n=3)
    t.status = "failed"
    t.error = "some error"
    t.results = [{"path": "/tmp/a.png", "bytes": 10, "format": "png", "role": "primary"}]
    outdir = tempfile.mkdtemp()
    _, mani = G.write_manifest(outdir, [t])
    assert len(mani["results"]) == 1, mani
    assert mani["results"][0]["n_got"] == 1
    assert mani["errors"] == [], mani["errors"]  # 已落盘的图不进 errors
    print("ok test_manifest_never_hides_disk_images")


# --------------------------------------------------------------------------- #
# Phase 6 — 回传预览 + 看护
# --------------------------------------------------------------------------- #
def test_preview_target_size():
    assert MP.target_size(1000, 500, 900) == (900, 450)
    assert MP.target_size(500, 1000, 900) == (450, 900)
    assert MP.target_size(800, 600, 900) == (800, 600)  # 不放大
    print("ok test_preview_target_size")


def test_auto_deliver_collect_and_new():
    mani = {
        "results": [{"name": "A", "saved": [
            {"path": "/o/A.png", "role": "primary"},
            {"path": "/o/A1.png", "role": "backup"}]}],
        "pending": [{"name": "B", "saved": [{"path": "/o/B.png", "role": "primary"}]}],
    }
    allp = [it["path"] for it in AD.collect_saved(mani)]
    assert allp == ["/o/A.png", "/o/A1.png", "/o/B.png"], allp
    nw = [it["path"] for it in AD.new_items(mani, {"/o/A.png"})]
    assert nw == ["/o/A1.png", "/o/B.png"], nw
    print("ok test_auto_deliver_collect_and_new")


def test_auto_deliver_finish_conditions():
    assert AD.is_finished({"ok": True}, True, False, False)[0] is True
    assert AD.is_finished({"ok": False}, False, False, False)[0] is True
    assert AD.is_finished({"ok": False}, True, True, False)[0] is True
    assert AD.is_finished({"ok": False}, True, False, True)[0] is True
    assert AD.is_finished({"ok": False}, True, False, False)[0] is False
    print("ok test_auto_deliver_finish_conditions")


def test_auto_deliver_run_loop():
    outdir = tempfile.mkdtemp()
    mani = {"ok": True, "results": [{"name": "A", "saved": [
        {"path": os.path.join(outdir, "A.png"), "role": "primary"}]}], "pending": [], "errors": []}
    open(os.path.join(outdir, "A.png"), "wb").write(JPG_BYTES)
    json.dump(mani, open(os.path.join(outdir, "manifest.json"), "w", encoding="utf-8"))
    uploaded = []

    def fake_upload(tunnel, token, local, remote, retries=3):
        uploaded.append(remote)
        return True

    def fake_preview(src, dst, max_px=900, quality=82):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        open(dst, "wb").write(JPG_BYTES)
        return dst

    class A:
        pass
    a = A()
    a.outdir = outdir
    a.remote_dir = "D:\\dst"
    a.tunnel = "http://t/api/exec"
    a.token = "Bearer x"
    a.gen_pid = None
    a.max_px = 900
    a.quality = 82
    a.poll_sec = 1
    a.max_idle_min = 0
    a.deadline_min = 90
    saved_up = AD.upload
    saved_pv = MP.make_preview
    AD.upload = fake_upload
    MP.make_preview = fake_preview
    try:
        rc = AD.run(a)
    finally:
        AD.upload = saved_up
        MP.make_preview = saved_pv
    assert rc == 0, rc
    assert len(uploaded) == 1 and uploaded[0].endswith("A.jpg"), uploaded
    print("ok test_auto_deliver_run_loop")


def test_make_preview_compresses():
    try:
        from PIL import Image
    except ImportError:
        print("skip test_make_preview_compresses (无 Pillow)")
        return
    src = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    src.close()
    im = Image.new("RGB", (2000, 1500), (123, 200, 50))
    im.save(src.name, "PNG")
    dst = src.name + ".preview.jpg"
    MP.make_preview(src.name, dst, max_px=900, quality=82)
    w, h = Image.open(dst).size
    assert max(w, h) == 900, (w, h)
    assert os.path.getsize(dst) < os.path.getsize(src.name), "预览应更小"
    print("ok test_make_preview_compresses (%dx%d)" % (w, h))


if __name__ == "__main__":
    # Phase 0
    test_concurrency_caps()
    test_single_call_n()
    test_transient_retry_then_success()
    test_content_policy_no_retry()
    test_failover_on_401()
    test_preflight_ping_banned()
    # Phase 1
    test_fetch_url_and_datauri_and_ext()
    test_filename_sanitize()
    test_params_passthrough()
    test_variation_endpoint()
    test_edit_with_mask()
    # Phase 2
    test_race_splits_into_n1()
    test_coverage_first_spreads_primaries()
    test_partial_success_keeps_primary()
    # Phase 3
    test_blocked_detection_and_giveup()
    test_blocked_ping_distinguishes_ban()
    # Phase 4
    test_missing_prompts_file_clean_exit()
    test_dedupe_duplicate_names()
    test_mask_without_edit_errors()
    test_n_out_of_range_clamped()
    test_parse_json_html_truncated()
    test_initial_manifest_written_before_first_event()
    test_save_keys_to_secrets_roundtrip()
    test_stale_static_keys_self_heal()
    test_healthy_static_keys_no_reveal()
    # Phase 5
    test_default_race_and_coverage_on()
    test_manifest_live_partial_progress()
    test_race_late_success_recovers_failed_task()
    test_manifest_never_hides_disk_images()
    # Phase 6
    test_preview_target_size()
    test_auto_deliver_collect_and_new()
    test_auto_deliver_finish_conditions()
    test_auto_deliver_run_loop()
    test_make_preview_compresses()
    print("\nALL OFFLINE TESTS PASSED")
