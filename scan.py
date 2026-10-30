#!/usr/bin/env python3
"""
scan.py — 优选 IP 抓取与测活

流程：
  1. 从多个 GitHub 自动更新源拉取候选 IP
  2. 对每个 IP 做真实 TLS 握手测试（针对 TEST_DOMAIN）
  3. 按延迟排序，保留最快的一批
  4. 写出 public/nodes.txt 与 public/stats.json

环境变量：
  DOMAIN   用于 TLS 测试的目标域名（GitHub Secret: TEST_DOMAIN）
"""

import json
import os
import socket
import ssl
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

# ── 配置 ──
SOURCES = [
    # 每 1-2 小时自动更新的 CF 优选 IP 列表
    "https://raw.githubusercontent.com/LancelotRar/best-cf-ips/main/best-cf-ip-scanned-top50.txt",
    "https://raw.githubusercontent.com/LancelotRar/best-cf-ips/main/best-cf-ip-scanned-top200.txt",
]

PORT = 443
KEEP = 24          # 最终保留多少个 IP
TRIES = 3          # 每个 IP 测几次
TIMEOUT = 6        # 单次连接超时（秒）
WORKERS = 24       # 并发线程数

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public")


def log(msg):
    print(msg, flush=True)


def fetch(url):
    """拉取一个源，返回 IP 列表"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=25) as r:
            text = r.read().decode("utf-8", "ignore")
    except Exception as e:
        log(f"  [跳过] {url.split('/')[-1]}: {type(e).__name__}")
        return []

    ips = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # 支持 ip:port#备注 或 ip#备注
        head = line.split("#")[0].strip()
        ip = head.split(":")[0].strip()
        if ip and ip[0].isdigit():
            ips.append(ip)
    log(f"  [拉取] {url.split('/')[-1]}: {len(ips)} 个")
    return ips


def test_tls(ip, sni):
    """测试到 ip:443 的 TLS 握手，返回最快毫秒数或 None"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    best = None
    for _ in range(TRIES):
        try:
            t0 = time.time()
            sock = socket.create_connection((ip, PORT), timeout=TIMEOUT)
            ssock = ctx.wrap_socket(sock, server_hostname=sni)
            ms = int((time.time() - t0) * 1000)
            ssock.close()
            if best is None or ms < best:
                best = ms
        except Exception:
            pass
    return best


def upload_kv(lines):
    """把 IP 列表写入 Cloudflare KV 的 ADD.txt"""
    token = os.environ.get("CF_API_TOKEN", "").strip()
    acc = os.environ.get("CF_ACCOUNT_ID", "").strip()
    ns = os.environ.get("CF_KV_NAMESPACE", "").strip()

    if not (token and acc and ns):
        log("跳过 KV 写入（未配置 CF_API_TOKEN / CF_ACCOUNT_ID / CF_KV_NAMESPACE）")
        return False

    body = "\n".join(lines).encode("utf-8")
    url = (
        f"https://api.cloudflare.com/client/v4/accounts/{acc}"
        f"/storage/kv/namespaces/{ns}/values/ADD.txt"
    )
    req = urllib.request.Request(
        url,
        data=body,
        method="PUT",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "text/plain",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            raw = r.read().decode("utf-8", "ignore")
        ok = '"success":true' in raw.replace(" ", "")
        log("KV 写入: " + ("成功" if ok else "失败 " + raw[:200]))
        return ok
    except Exception as e:
        log(f"KV 写入失败: {type(e).__name__}: {str(e)[:150]}")
        return False


def main():
    domain = os.environ.get("DOMAIN", "").strip()
    if not domain:
        log("错误：未设置 DOMAIN 环境变量")
        log("请在仓库 Settings → Secrets → Actions 添加 TEST_DOMAIN")
        sys.exit(1)

    log(f"目标域名: {domain}")
    log("")

    # 1. 收集候选
    log("=== 1. 拉取候选 IP ===")
    candidates = set()
    for url in SOURCES:
        for ip in fetch(url):
            candidates.add(ip)
    candidates = list(candidates)
    log(f"合计去重: {len(candidates)} 个")
    log("")

    if not candidates:
        log("错误：没有拿到任何候选 IP")
        sys.exit(1)

    # 2. 测活
    log(f"=== 2. 测活（并发 {WORKERS}，每 IP {TRIES} 次）===")
    results = []
    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(test_tls, ip, domain): ip for ip in candidates}
        for fut in as_completed(futures):
            ip = futures[fut]
            done += 1
            try:
                ms = fut.result()
            except Exception:
                ms = None
            if ms is not None:
                results.append((ip, ms))
            if done % 20 == 0:
                log(f"  进度 {done}/{len(candidates)}，可用 {len(results)}")

    results.sort(key=lambda x: x[1])
    log(f"可用: {len(results)}/{len(candidates)}")
    log("")

    if not results:
        log("错误：测活后没有可用 IP，保留上次结果")
        sys.exit(1)

    # 3. 写出
    top = results[:KEEP]
    os.makedirs(OUT_DIR, exist_ok=True)

    nodes_path = os.path.join(OUT_DIR, "nodes.txt")
    with open(nodes_path, "w", encoding="utf-8", newline="\n") as f:
        for ip, ms in top:
            f.write(f"{ip}:{PORT}#{ms}ms\n")

    stats = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "domain": domain,
        "candidates": len(candidates),
        "alive": len(results),
        "kept": len(top),
        "fastest_ms": top[0][1] if top else None,
        "slowest_ms": top[-1][1] if top else None,
        "ips": [{"ip": ip, "ms": ms} for ip, ms in top],
    }
    with open(os.path.join(OUT_DIR, "stats.json"), "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)

    log("=== 3. 结果 ===")
    log(f"保留 {len(top)} 个，最快 {top[0][1]}ms，最慢 {top[-1][1]}ms")
    for ip, ms in top[:8]:
        log(f"  {ip:<18} {ms}ms")
    log("")
    log(f"已写入 {nodes_path}")

    # 4. 同步到 Cloudflare KV（edgetunnel 的 ADD.txt）
    log("")
    log("=== 4. 同步到 KV ===")
    kv_lines = [f"{ip}:{PORT}" for ip, _ in top]
    upload_kv(kv_lines)


if __name__ == "__main__":
    main()
