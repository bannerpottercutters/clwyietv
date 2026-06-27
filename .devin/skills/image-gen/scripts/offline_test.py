"""离线逻辑测试:monkeypatch 掉网络层,验证 双模式分池/Coverage-First/赛马 vs 单请求/自动降级。
不发任何真实请求、不花额度。"""
import os, sys, time, threading, base64

os.environ.setdefault("MATSCA_APP_KEY", "appkey")
os.environ.setdefault("MATSCA_DIRECT_KEY", "directkey")
os.environ.setdefault("MATSCA_NATIVE_KEY", "nativekey")
os.environ.setdefault("MATSCA_APP_ID", "aid")
os.environ.setdefault("MATSCA_APP_SECRET", "asec")

import gen_image as G

G.retry_delay = lambda attempt, headers=None: 0.01  # 测试加速:不真的退避数秒

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)

# ---- 记录每个请求实测的并发(用于验证分池上限) ----
peak_used = {}
cur_used = {}
lock = threading.Lock()


def make_fake(scenario):
    """返回替换 generate_images 的假函数。scenario 决定行为。"""
    state = {"calls": 0}

    def fake(job, key, mode, n, retries, timeout, proxy=None, on_retry=None, abort_event=None):
        with lock:
            cur_used[mode] = cur_used.get(mode, 0) + 1
            peak_used[mode] = max(peak_used.get(mode, 0), cur_used[mode])
            state["calls"] += 1
            c = state["calls"]

        def call_api():
            # 模拟耗时(让并发真正叠加)
            delay = 0.15
            if scenario == "stall" and job.name == "图3" and c <= 1:
                delay = 1.2  # 图3 第一次很慢 → 触发 booster
            time.sleep(delay)
            if scenario == "downgrade" and n > 1:
                # n>1 一律报上游池满(可重试)→ 经 with_retries 触发 on_retry → 累计降级;降级后 n=1 放行
                raise G.HttpJsonError(429, {"error": {"code": "account_concurrency_exhausted",
                                                       "message": "并发请求过多"}})
            if scenario == "storm502":
                # 上游网关层 5xx:可重试,但持续撞→应判风暴(写 upstream_storm)
                raise G.HttpJsonError(502, {"error": {"message": "Bad Gateway"}})
            if scenario == "storm_partial" and job.name != "图1":
                # 图1 正常出,图2/图3 网关 502 风暴 → 熔断后应返回部分结果(图1)+ 缺图清单
                raise G.HttpJsonError(502, {"error": {"message": "Bad Gateway"}})
            return [PNG for _ in range(n)]

        try:
            # 走真实 with_retries:复刻生产里 on_retry 回调被触发的路径
            return G.with_retries(call_api, retries, job.name, on_retry=on_retry, abort_event=abort_event)
        finally:
            with lock:
                cur_used[mode] -= 1
    return fake


class Args:
    def __init__(self, **kw):
        d = dict(prompt=None, prompts_file=None, out=None, name=None,
                 outdir="output/fig", date="20260626", model="gpt-image-2",
                 size="1024x1024", quality="high", n=2, edit=None, mask=None,
                 variation=None, background=None, style=None, output_format=None,
                 output_compression=None, input_fidelity=None, concurrency=0,
                 retries=4, timeout=600, moderation="low", mode="auto", modes=None,
                 dispatch="cost", single_call=False, proxy=None, json_out=None, log_file=None)
        d.update(kw)
        self.__dict__.update(d)


def reset_storm():
    """清空风暴侦测的进程级累计状态,使每个风暴用例独立。"""
    G._STORM_5XX_TOTAL = 0
    G._STORM_LAST_5XX_TS = 0.0
    G._STORM_ANNOUNCED = False
    G._STORM_EVER = False
    G._GAVE_UP = False
    G._START_TS = time.time()
    G._LAST_DONE_TS = 0.0


def run_case(title, args, modes, caps, expect, storm_give_up_after=None, reset_storm_first=False):
    global peak_used, cur_used
    peak_used = {}; cur_used = {}
    G._DONE_COUNT = 0
    if reset_storm_first:
        reset_storm()
    jobs = G.build_jobs(args, modes, "b64_json", "20260626")
    sch = G.Scheduler(jobs, {m: "k_"+m for m in modes}, caps, args.dispatch, modes,
                      args.retries, args.timeout, None, None, storm_give_up_after)
    sch.run()
    got = {j.name: (j.got, j.primary_mode, [s["mode"] for s in j.saved]) for j in jobs}
    print("\n=== %s ===" % title)
    for j in jobs:
        print("  %s: got=%d need=%d primary_mode=%s saved_modes=%s single=%s"
              % (j.name, j.got, j.need, j.primary_mode, [s["mode"] for s in j.saved], j.single))
    print("  peak_used per mode:", peak_used)
    total, by_mode = G._compute_billing(jobs)
    print("  billing total=%d by_mode=%s downgraded=%s" % (total, by_mode, sch.downgraded))
    expect(jobs, sch)
    print("  [PASS]")


# 用临时输出目录,避免污染
import tempfile
tmp = tempfile.mkdtemp()

# 用 prompts-file 构造 3 内容批量
import json
pf = os.path.join(tmp, "batch.json")
with open(pf, "w", encoding="utf-8") as f:
    json.dump([{"name": "图1", "prompt": "a"}, {"name": "图2", "prompt": "b"},
               {"name": "图3", "prompt": "c"}], f)


def expect_all_covered(jobs, sch):
    assert all(j.got >= j.need for j in jobs), "应全部出齐 need 张"
    assert all(j.primary is not None for j in jobs), "应都有主图"


def expect_dualmode(jobs, sch):
    expect_all_covered(jobs, sch)
    # cost 策略:app 应被优先用满,direct 作为溢出
    assert peak_used.get("app", 0) <= 3, "app 池不得超 3"
    assert peak_used.get("direct", 0) <= 4, "direct 池不得超 4"
    used_modes = {m for j in jobs for m in [s["mode"] for s in j.saved]}
    assert "app" in used_modes, "cost 策略下 app 必被使用"


def expect_single(jobs, sch):
    expect_all_covered(jobs, sch)
    for j in jobs:
        assert j.single is True


def expect_downgrade(jobs, sch):
    assert sch.downgraded is True, "应触发降级"
    expect_all_covered(jobs, sch)


# Case 1: 单模式 app 赛马(默认)
G.generate_images = make_fake("ok")
run_case("单模式 app 赛马默认(-n2)", Args(prompts_file=pf, outdir=os.path.join(tmp,"c1")),
         ["app"], {"app": 3}, expect_all_covered)

# Case 2: 双模式 app,direct cost 策略
G.generate_images = make_fake("ok")
run_case("双模式 app,direct cost(-n2)", Args(prompts_file=pf, modes="app,direct",
         dispatch="cost", outdir=os.path.join(tmp,"c2")),
         ["app","direct"], {"app": 3, "direct": 4}, expect_dualmode)

# Case 3: 双模式 throughput
G.generate_images = make_fake("ok")
run_case("双模式 app,direct throughput", Args(prompts_file=pf, modes="app,direct",
         dispatch="throughput", outdir=os.path.join(tmp,"c3")),
         ["app","direct"], {"app": 3, "direct": 4}, expect_dualmode)

# Case 4: --single-call
G.generate_images = make_fake("ok")
run_case("单请求 single-call(-n2→n=2)", Args(prompts_file=pf, single_call=True,
         outdir=os.path.join(tmp,"c4")),
         ["app"], {"app": 3}, expect_single)

# Case 5: stall → booster(图3 第一次慢)
G.generate_images = make_fake("stall")
run_case("顽固图 stall 加 booster", Args(prompts_file=pf, n=1,
         outdir=os.path.join(tmp,"c5")),
         ["app"], {"app": 3}, expect_all_covered)

# Case 6: 自动降级(池满→N=1)
G.generate_images = make_fake("downgrade")
run_case("自动降级 account_concurrency_exhausted→N=1", Args(prompts_file=pf, single_call=True, n=3,
         retries=2, outdir=os.path.join(tmp,"c6")),
         ["app","direct"], {"app": 3, "direct": 4}, expect_downgrade)

# Case 7: per-item mode 钉死
G.generate_images = make_fake("ok")
pf2 = os.path.join(tmp, "batch2.json")
with open(pf2, "w", encoding="utf-8") as f:
    json.dump([{"name": "图1", "prompt": "a", "mode": "direct"},
               {"name": "图2", "prompt": "b", "mode": "app"},
               {"name": "图3", "prompt": "c"}], f)
def expect_pin(jobs, sch):
    expect_all_covered(jobs, sch)
    byname = {j.name: j for j in jobs}
    assert all(s["mode"] == "direct" for s in byname["图1"].saved), "图1 应全 direct"
    assert all(s["mode"] == "app" for s in byname["图2"].saved), "图2 应全 app"
run_case("per-item mode 钉死(图1=direct,图2=app)", Args(prompts_file=pf2, modes="app,direct",
         outdir=os.path.join(tmp,"c7")),
         ["app","direct"], {"app": 3, "direct": 4}, expect_pin)

# ── 风暴用例:把阈值降到 3,使持续 502 能快速、确定地判定为风暴(逻辑等价,只是更快触发)──
G.STORM_5XX_THRESHOLD = 3

# Case 8: 502 风暴侦测(全程 502 → upstream_storm 应被置真;issued 上限保证终止不死循环)
G.generate_images = make_fake("storm502")
def expect_storm_detected(jobs, sch):
    assert G._STORM_EVER is True, "持续 502 应判定为上游网关风暴(upstream_storm=true)"
    assert G._storm_summary()["storm_5xx"] >= 3, "应累计到 5xx 计数"
    assert all(j.primary is None for j in jobs), "全 502 时不应有主图"
run_case("502 风暴侦测(全程 502)", Args(prompts_file=pf, n=2, retries=2,
         outdir=os.path.join(tmp,"c8")),
         ["app"], {"app": 3}, expect_storm_detected, reset_storm_first=True)

# Case 9: 熔断 --storm-give-up-after=0(风暴一确认即收尾)→ 图1 出齐 + 图2/图3 缺图清单
G.generate_images = make_fake("storm_partial")
def expect_giveup_partial(jobs, sch):
    assert sch.gave_up is True and G._GAVE_UP is True, "风暴持续应熔断收尾"
    byname = {j.name: j for j in jobs}
    assert byname["图1"].primary is not None, "图1 应出主图(部分结果)"
    assert byname["图2"].primary is None and byname["图3"].primary is None, "图2/图3 应进缺图清单"
run_case("熔断 storm-give-up-after=0(返回部分结果+缺图清单)", Args(prompts_file=pf, n=2, retries=3,
         outdir=os.path.join(tmp,"c9")),
         ["app"], {"app": 3}, expect_giveup_partial,
         storm_give_up_after=0, reset_storm_first=True)

print("\nALL OFFLINE LOGIC TESTS PASSED")
