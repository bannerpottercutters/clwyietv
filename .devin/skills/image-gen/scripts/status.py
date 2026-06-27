#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生图任务状态聚合器 —— 一眼看清「几个在跑/差几张/占几路并发/剩几路」。

背景：经隧道 Start-Process -WindowStyle Hidden 真脱离后，子进程 stdout/stderr 全丢，
调用方只能盲猜。gen_image.py 每个任务带 --json-out 时会自动写
  <json-out>.heartbeat  (每 ~15s 刷新的结构化心跳: name/n/done/inflight/alive/elapsed_s/last)
本脚本扫描某目录下所有 .heartbeat，按「是否还活着」聚合，打印人类可读的总览 + 机器可读 JSON，
让调用方/用户随时有感知，而不是数 OS 进程（一个 uv run 任务=uv.exe+python.exe 两个进程，
会把 N 个任务误读成 2N 个进程）。

判活口径：心跳 alive=False(进程已写终态) 或 心跳文件 mtime 超过 STALE_S 秒没更新 → 判为已结束/卡死。

用法：
  python status.py [扫描目录, 默认当前目录]
        [--concurrency-limit 4]    # 单密钥在途上限（app 4 / direct 8），用于算剩余可用并发
        [--stale 45]               # 心跳超过该秒数没刷新即判定不活
        [--json]                   # 只输出机器可读 JSON（供编排层解析）

注意：服务商按「单密钥在途上限」限流（app 模式 4 / 直连 8），且该额度由**同一密钥下所有
并发运行的 gen_image.py 进程共享**——不是每个进程各有一份。所以多内容编排时，所有在跑任务
占用的并发之和必须 ≤ 该上限。本工具的 concurrency_used 即为该共享占用，默认按 app 的 4 计算
剩余；走直连请用 --concurrency-limit 8。

【优先批量、少起多进程】gen_image.py 现已支持 --prompts-file 把多内容放进**一个进程**的线程池
统一卡并发（永不超在途硬顶、最防撞墙），此时这里通常只看到 1 条心跳、inflight 即该进程真实
在途占用。本工具仍兼容旧的「多进程 fire-and-forget」用法：多条心跳时把各自 inflight 求和，
即为该密钥的共享占用——发新任务前先看 concurrency_free 有没有空位。
"""
import argparse
import glob
import json
import os
import time


def _load(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def collect(scan_dir, stale_s):
    """扫描 scan_dir 下所有 *.heartbeat，返回任务列表。"""
    tasks = []
    for hb in sorted(glob.glob(os.path.join(scan_dir, "*.heartbeat"))):
        d = _load(hb)
        try:
            age = time.time() - os.path.getmtime(hb)
        except OSError:
            age = None
        # 活着 = 心跳没声明终态(alive!=False) 且 文件还在按时刷新(mtime 未老化)
        fresh = (age is not None and age <= stale_s)
        alive = bool(d.get("alive", True)) and fresh
        n = int(d.get("n", 1) or 1)
        done = int(d.get("done", 0) or 0)
        tasks.append({
            "name": d.get("name") or os.path.basename(hb),
            "alive": alive,
            "n": n,
            "done": done,
            "missing": max(0, n - done),
            # 占用的图片并发：只有活着的任务才在占用；用心跳里的 inflight，缺省回退到 n-done
            "inflight": (int(d.get("inflight", max(0, n - done))) if alive else 0),
            "elapsed_s": d.get("elapsed_s"),
            "hb_age_s": None if age is None else round(age, 1),
            "last": d.get("last", ""),
            "heartbeat": hb,
        })
    return tasks


def summarize(tasks, limit):
    running = [t for t in tasks if t["alive"]]
    used = sum(t["inflight"] for t in running)
    return {
        "tasks_total": len(tasks),
        "tasks_running": len(running),
        "tasks_done": len(tasks) - len(running),
        "images_done": sum(t["done"] for t in tasks),
        "images_missing_running": sum(t["missing"] for t in running),
        "concurrency_used": used,
        "concurrency_limit": limit,
        "concurrency_free": max(0, limit - used),
    }


def main():
    ap = argparse.ArgumentParser(description="生图任务状态聚合器（扫描 *.heartbeat）")
    ap.add_argument("scan_dir", nargs="?", default=".", help="扫描目录（默认当前目录），通常是 output/fig")
    ap.add_argument("--concurrency-limit", type=int, default=4,
                    help="单密钥在途上限，同一密钥下所有并发 gen 进程共享（app 4 / direct 8，默认 4）")
    ap.add_argument("--stale", type=int, default=45, help="心跳超过该秒数没刷新即判不活（默认 45）")
    ap.add_argument("--json", action="store_true", dest="as_json", help="只输出机器可读 JSON")
    args = ap.parse_args()

    tasks = collect(args.scan_dir, args.stale)
    summary = summarize(tasks, args.concurrency_limit)

    if args.as_json:
        print(json.dumps({"summary": summary, "tasks": tasks}, ensure_ascii=False))
        return

    s = summary
    print("=== 生图任务状态 @ %s ===" % time.strftime("%H:%M:%S"))
    if not tasks:
        print("(无 .heartbeat：没有带可观测性的任务在跑，或扫错了目录 %s)" % os.path.abspath(args.scan_dir))
    print("在跑 %d 个任务 / 共 %d 个；已出图 %d 张，运行中任务还差 %d 张" % (
        s["tasks_running"], s["tasks_total"], s["images_done"], s["images_missing_running"]))
    print("占用图片并发 %d/%d，剩余可用 %d 路" % (
        s["concurrency_used"], s["concurrency_limit"], s["concurrency_free"]))
    for t in tasks:
        flag = "运行中" if t["alive"] else "已结束"
        el = "" if t["elapsed_s"] is None else "  跑了%.0fs" % t["elapsed_s"]
        hb = "" if t["hb_age_s"] is None else "  心跳%.0fs前" % t["hb_age_s"]
        print("  [%s] %s  出%d/%d 张  占%d 路%s%s" % (
            flag, t["name"], t["done"], t["n"], t["inflight"], el, hb))
        if t["last"]:
            print("        最近: %s" % t["last"])


if __name__ == "__main__":
    main()
