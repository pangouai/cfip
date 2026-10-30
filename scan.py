#!/usr/bin/env python3
"""
scan.py — 优选 IP 抓取与测活

数据源策略（重要）：
  GitHub Actions 的 runner 在境外机房，它测出的延迟与「中国大陆用户的实际体验」
  完全无关（Cloudflare 用 anycast，同一 IP 对境外机房和大陆用户会路由到不同节点）。
  所以延迟数据必须来自「大陆测速点」，而不是 runner 自己测。

  因此：
    主数据源 = 大陆三网实测优选 IP（电信/联通/移动分别给出）
    辅助     = best-cf-ips（境外视角，仅作补充，防止主源全挂）

  runner 这边只做一件事：**测活**（TLS 握手能否成功）。
  存活判定是准确的，延迟数字不可信，所以不用它排序。

流程：
  1. 拉三网 API（电信/联通/移动）
  2. 若某一路为空，用 best-cf-ips 兜底
  3. 对每个 IP 做 TLS 握手测活（并发）
  4. 按运营商分组保留，保证三网都有覆盖
  5. 写出 public/nodes.txt 与 public/stats.json
  6. 同步到 Cloudflare KV 的 ADD.txt

环境变量：
  DOMAIN           TLS 测试用的目标域名（Secret: TEST_DOMAIN）
  CF_API_TOKEN     Cloudflare API Token（Secret）
  CF_ACCOUNT_ID    Cloudflare Account ID（Secret）
  CF_KV_NAMESPACE  KV Namespace ID（Secret）
"""

import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

# ── 配置 ──

# 大陆三网实测优选 IP API（每 10 分钟更新）
# ips 参数尽量要满，脚本会自己按 QUOTA 截取
CARRIER_SOURCES = [
    ("ct",   "电信", "https://cf.090227.xyz/ct?ips=20"),
    ("cu",   "联通", "https://cf.090227.xyz/cu?ips=10"),
    ("cmcc", "移动", "https://cf.090227.xyz/cmcc?ips=10"),
]

# 兜底源（境外视角，仅在某一路运营商拿不到数据时补位）
FALLBACK_SOURCES = [
    "https://raw.githubusercontent.com/LancelotRar/best-cf-ips/main/best-cf-ip-scanned-top50.txt",
]

PORT = 443

# 各运营商保留名额。
# 注意：cf.090227.xyz 的 ips 参数不生效，每路固定返回约 6-8 个，
# 所以电信拿满（6），联通/移动各 6，剩下名额从富余里补。
QUOTA = {
    "ct":    6,   # 电信（接口上限，全要）
    "cu":    6,   # 联通
    "cmcc":  6,   # 移动
}

TRIES = 2              # 每个 IP 测几次
TIMEOUT = 6            # 单次连接超时（秒）
WORKERS = 20           # 并发线程数

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public")

IP_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3})(?::(\d+))?(?:#(.*))?$")


def log(msg):
    print(msg, flush=True)


def http_get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def parse_ips(text):
    """从 ip#备注 / ip:port#备注 文本中提取 (ip, note)"""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = IP_RE.match(line)
        if m:
            out.append((m.group(1), (m.group(3) or "").strip()))
    return out


def fetch_carriers():
    """拉三网 API，返回 {carrier_key: [(ip, note), ...]}"""
    result = {}
    for key, label, url in CARRIER_SOURCES:
        try:
            text = http_get(url)
            pairs = parse_ips(text)
            result[key] = pairs
            log(f"  [拉取] {label}: {len(pairs)} 个")
        except Exception as e:
            log(f"  [失败] {label}: {type(e).__name__}")
            result[key] = []
    return result


def fetch_fallback():
    """兜底源"""
    pairs = []
    for url in FALLBACK_SOURCES:
        try:
            pairs += parse_ips(http_get(url))
            log(f"  [兜底] {url.split('/')[-1]}: {len(pairs)} 个累计")
        except Exception as e:
            log(f"  [兜底失败] {type(e).__name__}")
    return pairs


def test_tls(ip, sni):
    """TLS 握手测活。返回最快毫秒数，失败返回 None。

    注意：这个延迟是「runner → CF 边缘」的，不代表大陆用户体验，
    仅用于判断 IP 是否存活。
    """
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

    # ── 1. 拉三网数据 ──
    log("=== 1. 拉取三网优选 IP（大陆实测）===")
    carriers = fetch_carriers()

    total = sum(len(v) for v in carriers.values())
    if total == 0:
        log("")
        log("三网 API 全部失败，启用兜底源")
        fb = fetch_fallback()
        # 兜底时无法区分运营商，平均分给三路
        for i, (ip, note) in enumerate(fb):
            carriers[("ct", "cu", "cmcc")[i % 3]].append((ip, note))
        total = sum(len(v) for v in carriers.values())
        if total == 0:
            log("错误：没有任何候选 IP")
            sys.exit(1)

    # 某一路为空时用兜底补齐
    for key, label, _ in CARRIER_SOURCES:
        if not carriers.get(key):
            log(f"  {label} 为空，用兜底源补位")
            fb = fetch_fallback()
            carriers[key] = fb

    log("")

    # ── 2. 去重（同一 IP 可能出现在多个运营商列表里，保留首个归属）──
    seen = {}
    for key, _, _ in CARRIER_SOURCES:
        for ip, note in carriers.get(key, []):
            if ip not in seen:
                seen[ip] = (key, note)

    candidates = list(seen.keys())
    log(f"去重后候选: {len(candidates)} 个")
    log("")

    # ── 3. 测活 ──
    log(f"=== 2. 测活（并发 {WORKERS}，每 IP {TRIES} 次）===")
    alive = {}
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
                alive[ip] = ms
            if done % 10 == 0 or done == len(candidates):
                log(f"  进度 {done}/{len(candidates)}，存活 {len(alive)}")

    log(f"存活: {len(alive)}/{len(candidates)}")
    log("")

    if not alive:
        log("错误：测活后没有可用 IP，保留上次结果")
        sys.exit(1)

    # ── 4. 按运营商分组保留 ──
    # 目标：电信优先（QUOTA 里配的份额），某家不够时从其他家富余里补
    log("=== 3. 按运营商分组 ===")
    grouped = {key: [] for key, _, _ in CARRIER_SOURCES}
    for ip, ms in alive.items():
        key = seen[ip][0]
        if key in grouped:
            grouped[key].append((ip, ms))

    # 组内按握手时间排（虽不代表大陆延迟，但能筛掉质量差的）
    for key in grouped:
        grouped[key].sort(key=lambda x: x[1])

    picked = []
    used = set()

    # 第一轮：按 QUOTA 分配
    for key, label, _ in CARRIER_SOURCES:
        quota = QUOTA.get(key, 8)
        take = grouped[key][:quota]
        for ip, ms in take:
            picked.append((ip, ms, key))
            used.add(ip)
        lack = quota - len(take)
        log(f"  {label}: {len(take)}/{quota} 个" + (f"（缺 {lack}）" if lack > 0 else ""))

    # 第二轮：不足的部分从剩余存活 IP 补（电信优先补）
    target = sum(QUOTA.values())
    if len(picked) < target:
        # 排序时让电信的排前面，保证补位也优先电信
        order = {"ct": 0, "cu": 1, "cmcc": 2}
        rest = sorted(
            [(ip, ms, seen[ip][0]) for ip, ms in alive.items() if ip not in used],
            key=lambda x: (order.get(x[2], 9), x[1]),
        )
        added = 0
        for ip, ms, key in rest:
            if len(picked) >= target:
                break
            picked.append((ip, ms, key))
            used.add(ip)
            added += 1
        if added:
            log(f"  从富余补位: +{added} 个")

    # 最终按运营商排一下，让电信节点排在订阅前面
    order = {"ct": 0, "cu": 1, "cmcc": 2}
    picked.sort(key=lambda x: (order.get(x[2], 9), x[1]))

    log(f"  合计: {len(picked)} 个")
    log("")

    # ── 5. 写出 ──
    os.makedirs(OUT_DIR, exist_ok=True)

    notes = {key: label for key, label, _ in CARRIER_SOURCES}

    nodes_path = os.path.join(OUT_DIR, "nodes.txt")
    with open(nodes_path, "w", encoding="utf-8", newline="\n") as f:
        for ip, ms, key in picked:
            f.write(f"{ip}:{PORT}#{notes.get(key, key)}\n")

    stats = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "domain": domain,
        "source": "cf.090227.xyz 三网实测",
        "candidates": len(candidates),
        "alive": len(alive),
        "kept": len(picked),
        "by_carrier": {notes.get(k, k): len([1 for _, _, c in picked if c == k])
                       for k, _, _ in CARRIER_SOURCES},
        "ips": [{"ip": ip, "ms": ms, "carrier": notes.get(key, key)}
                for ip, ms, key in picked],
        "note": "ms 为 GitHub runner 测得的握手耗时，仅表示存活，不代表中国大陆访问延迟",
    }
    with open(os.path.join(OUT_DIR, "stats.json"), "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)

    log("=== 4. 结果 ===")
    log(f"保留 {len(picked)} 个")
    for ip, ms, key in picked:
        log(f"  {ip:<18} {notes.get(key, key)}  {ms}ms")
    log("")
    log(f"已写入 {nodes_path}")

    # ── 6. 同步 KV ──
    log("")
    log("=== 5. 同步到 KV ===")
    kv_lines = [f"{ip}:{PORT}" for ip, _, _ in picked]
    upload_kv(kv_lines)


if __name__ == "__main__":
    main()
