/**
 * functions/sub.js — 订阅响应器
 *
 * edgetunnel 的 sub:// 机制会请求：
 *   https://<你的域名>/sub?host=<伪装域名>&uuid=<UUID>
 * 并期望拿到 base64 编码的 vless:// 列表。
 *
 * 本函数：
 *   1. 读取同站点 public/nodes.txt（由 GitHub Actions 定时更新）
 *   2. 把每个 IP:PORT 套成完整的 vless:// 链接
 *   3. base64 编码后返回
 *
 * 关键：返回的节点必须包含 host 和 uuid，edgetunnel 靠这两个值识别出「优选IP行」。
 */

const PORT_FALLBACK = 443;

function b64encode(str) {
  // Cloudflare Workers 有 btoa，但只接受 latin1；先转 UTF-8
  const bytes = new TextEncoder().encode(str);
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin);
}

export async function onRequestGet(context) {
  const { request, env } = context;
  const url = new URL(request.url);

  const host = url.searchParams.get("host") || "";
  const uuid = url.searchParams.get("uuid") || "";

  if (!host || !uuid) {
    return new Response("missing host or uuid", { status: 400 });
  }

  // 读取节点列表：优先从同源静态文件取，避免跨域
  let raw = "";
  try {
    const nodesUrl = new URL("/nodes.txt", url.origin).toString();
    const resp = await fetch(nodesUrl, { cf: { cacheTtl: 60 } });
    if (resp.ok) raw = await resp.text();
  } catch (e) {
    // 忽略，下面会返回空
  }

  if (!raw.trim()) {
    return new Response("", {
      status: 200,
      headers: { "Content-Type": "text/plain;charset=utf-8" },
    });
  }

  const lines = [];
  for (const line of raw.split(/\r?\n/)) {
    const t = line.trim();
    if (!t || t.startsWith("#")) continue;

    // 支持 ip:port#备注 或 ip#备注
    const [addrPart, ...noteParts] = t.split("#");
    const addr = addrPart.trim();
    if (!addr) continue;

    const note = noteParts.join("#").trim();
    const hostPort = addr.includes(":") ? addr : `${addr}:${PORT_FALLBACK}`;

    // 构造 vless:// 链接
    // 注意：edgetunnel 会检查链接里是否同时含 uuid 和 host，
    //       以此判定「这是优选IP行」而非「其他节点」
    const params = new URLSearchParams({
      encryption: "none",
      security: "tls",
      type: "ws",
      host: host,
      sni: host,
      fp: "chrome",
      path: "/?ed=2560",
    });

    const label = note ? `${note}` : host;
    lines.push(`vless://${uuid}@${hostPort}?${params.toString()}#${encodeURIComponent(label)}`);
  }

  const body = b64encode(lines.join("\n"));

  return new Response(body, {
    status: 200,
    headers: {
      "Content-Type": "text/plain;charset=utf-8",
      "Cache-Control": "no-store",
      "Access-Control-Allow-Origin": "*",
    },
  });
}

// 非 GET 请求统一拒绝
export async function onRequest(context) {
  return context.request.method === "GET"
    ? onRequestGet(context)
    : new Response("Method Not Allowed", { status: 405 });
}
