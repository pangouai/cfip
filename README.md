# 部署步骤

整个流程约 10 分钟，全部在网页上点，不用装软件。

---

## 一、创建 GitHub 仓库

1. 打开 https://github.com/new
2. **Repository name** 填 `cfip`（名字随意，记住即可）
3. 选 **Public**（Pages 免费版需要公开仓库）
4. **不要**勾选 "Add a README file"
5. 点 **Create repository**

---

## 二、上传文件

在新仓库页面，点 **uploading an existing file** 链接，然后把下面 5 个文件拖进去：

```
scan.py
functions/sub.js
public/index.html
public/nodes.txt
.github/workflows/update.yml
```

**注意目录结构必须保持**：

```
仓库根目录/
├── scan.py
├── functions/
│   └── sub.js
├── public/
│   ├── index.html
│   └── nodes.txt
└── .github/
    └── workflows/
        └── update.yml
```

GitHub 网页上传**不支持直接建子目录**。有两种办法：

**办法 A（推荐）**：先上传根目录的 `scan.py`，然后：
- 在仓库页点 **Add file → Create new file**
- 文件名输入框里直接打 `functions/sub.js`，斜杠会自动建目录
- 粘贴内容，点 **Commit**

依次建 `public/index.html`、`public/nodes.txt`、`.github/workflows/update.yml`。

**办法 B**：用 GitHub Desktop 或 git 命令行推送整个目录。

---

## 三、添加 Secret

1. 仓库页 → **Settings** → 左侧 **Secrets and variables** → **Actions**
2. 点 **New repository secret**
3. **Name** 填 `TEST_DOMAIN`
4. **Secret** 填你的节点域名，例如：
   ```
   kvman.ccwu.cc
   ```
5. 点 **Add secret**

> 这个域名用于测活：脚本会对每个候选 IP 发起 TLS 握手，SNI 用这个域名。必须是你的 Worker 实际绑定的域名。

---

## 四、启用 Pages

1. 仓库页 → **Settings** → 左侧 **Pages**
2. **Source** 选 **Deploy from a branch**
3. **Branch** 选 `gh-pages`，目录选 `/ (root)`
4. 点 **Save**

> `gh-pages` 分支由工作流自动创建，第一次运行前可能看不到。先做第五步，跑完再回来设置。

---

## 五、手动跑一次

1. 仓库页 → **Actions** 标签
2. 左侧选 **优选IP自动更新**
3. 右侧点 **Run workflow** → 再点 **Run workflow**
4. 等 2-3 分钟，刷新页面看结果

跑完后应该看到：
- `public/nodes.txt` 有 24 行 IP
- `public/stats.json` 生成
- `gh-pages` 分支出现

---

## 六、部署 Cloudflare Pages

这一步提供 `sub://` 需要的动态接口。

1. 打开 https://dash.cloudflare.com/ → **Workers 和 Pages**
2. 点 **创建** → **Pages** → **连接到 Git**
3. 授权 GitHub，选择刚才的 `cfip` 仓库
4. 构建设置全部留空（不需要构建）：
   - **Build command**：留空
   - **Build output directory**：`public`
5. 点 **保存并部署**

部署完成后会得到一个域名，形如：

```
https://cfip-abc.pages.dev
```

**验证**：浏览器打开

```
https://cfip-abc.pages.dev/sub?host=kvman.ccwu.cc&uuid=2a7108f4-b462-4d6d-a5f5-d611aef9c35a
```

应该返回一串 base64 文本。解码后是 `vless://` 开头的节点列表。

---

## 七、接入 edgetunnel

1. 打开 `https://kvman.ccwu.cc/admin`，用密码登录
2. 找到 **优选订阅生成** 区域
3. **优选订阅模式** 选 **自定义订阅**
4. **自定义优选** 输入框填：
   ```
   sub://https://cfip-abc.pages.dev
   ```
   > 注意是 `sub://` 开头，后面跟你的 Pages 域名，**不要带 `/sub` 路径**
5. 点 **保存**

---

## 八、验证

在客户端（v2rayN / Clash）重新拉取订阅：

```
https://kvman.ccwu.cc/sub?token=<你的TOKEN>
```

应该能看到 24 个节点，备注是延迟毫秒数。

---

## 日常维护

| 项目 | 说明 |
| --- | --- |
| 更新频率 | 每 30 分钟自动 |
| 改频率 | 编辑 `.github/workflows/update.yml` 里的 `cron` |
| 看状态 | `https://cfip-abc.pages.dev/` 有可视化面板 |
| 改数量 | 编辑 `scan.py` 里的 `KEEP`（默认 24） |
| 加源 | 编辑 `scan.py` 里的 `SOURCES` 数组 |

---

## 常见问题

**Q：Actions 报 `未设置 DOMAIN 环境变量`**

Secret 名字必须是 `TEST_DOMAIN`，大小写敏感。检查第三步。

**Q：`/sub` 返回 400**

URL 里必须同时有 `host` 和 `uuid` 参数，缺一个就返回 400。这是正常的保护。

**Q：`/sub` 返回空**

说明 `public/nodes.txt` 还没生成。先去 Actions 手动跑一次。

**Q：edgetunnel 后台保存后订阅没变化**

`sub://` 是每次生成订阅时实时拉取的。客户端重新拉一次订阅即可，不需要重新部署 Worker。

**Q：能不能不用 Cloudflare Pages**

可以，但 `sub://` 要求目标能响应带参数的路径，静态托管（GitHub Pages、Vercel 静态）做不到。Cloudflare Pages Functions、Vercel Serverless、Deno Deploy 都行。

**Q：免费额度够吗**

- GitHub Actions：公开仓库无限量
- GitHub Pages：100 GB/月流量
- Cloudflare Pages：无限请求，500 次构建/月

每 30 分钟一次 = 每天 48 次构建，一个月约 1440 次，**超过 500 次限制**。

建议改成每 2 小时（`0 */2 * * *`），一个月约 360 次，安全。
