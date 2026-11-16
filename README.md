# DeepSeek Harness 全版本归档器

自动监测 DeepSeek Harness 官方更新源与 CDN，一旦发现新的安装包就下载、校验、
并永久保存为 GitHub Release。**每个版本各占一个独立 Release，永不覆盖、永不过期**
（Actions artifacts 默认 90 天过期，300 MB 二进制也不适合 commit 进 git）。

> **完整实机取证见 [VERIFICATION.md](VERIFICATION.md)**（含 2026-11-16 push 前复测）。
> **上传与首次运行见 [PUSH.md](PUSH.md)**，一键脚本 `push.ps1`。
> 本页只保留结论与用法。

## 官方源（实测 2026-11-16 03:16 UTC 最后一次重读）

本机 `resources/app-update.yml`：

```yaml
provider: generic
url: https://download.deepseek.com/dsh-desk/feeds/win-x64/
channel: nightly
```

| 项 | 实测值 |
| --- | --- |
| 唯一有效 feed | `https://download.deepseek.com/dsh-desk/feeds/win-x64/nightly.yml`（200，533 B；首轮 `text/yaml`，**复测已变为 `application/yaml`**） |
| 其他 channel | `latest.yml` / `stable.yml` / `beta.yml` / `alpha.yml` / `dev.yml` / `rc.yml` / `preview.yml` / `canary.yml` 全部 404 |
| 目录与清单 | `feeds/win-x64/`、`bin/win-x64/`、`changelog.json`、`versions.json`、`index.json` 全部 404 |
| 安装包模板 | `https://download.deepseek.com/dsh-desk/bin/win-x64/deepseek-harness-<version>-win-x64.exe` |
| **feed 当前指向** | **`0.2.2-rc.3`**，299,595,752 字节，`releaseDate: 2026-11-13T07:31:42.937Z` |
| feed SHA-512 (hex) | `1a225ff24c7e0bb7a8899d31412601713ca1e71997425faf87add4eae4a6f7adcc70ad4090c801f0fc9dcee7a459d518ee1c9446f85530700d338a67fad08acd` |
| ETag | `"3c7d64d0476bf5891c31d24246a627da"` → `If-None-Match` 返回 304（首轮为 `1aafc4e0…`） |
| 传输 | `Accept-Ranges: bytes`、`Cache-Control: max-age=0`、`Server: tencent-cos`；有 `.exe.blockmap` 边车 |
| 官网 | `https://download.deepseek.com/` 200（移动端 App 落地页）；`harness.deepseek.com`、`/download` 404 |
| 本机应用版本 | `DeepSeek Harness.exe` FileVersion `0.2.1-rc.3`（落后于 feed）；安装根 `version` 文件 `44.7.0` 是 **Electron 版本，不是应用版本** |

> Content-Type 变化让 PowerShell 返回 `byte[]` 而非 string；脚本按原始字节
> `r.read().decode()` 读取，不受影响，已实测解析正确。

### 关键事实：feed 不是完整版本清单

对 CDN 做有界穷举（0.0.0…0.3.3 的 rc.0…rc.12 + 正式版），2026-11-16 复测命中
**9** 个，其中只有 1 个在 feed 里。`0.2.2-rc.3` 是首轮测试结束约 3 小时后新发布的
版本 —— 直接印证了每轮 gap probe 的必要性：

| 版本 | 字节数 | 在 feed 中 |
| --- | ---: | --- |
| `0.2.0-rc.1` | 288472536 | 否 |
| `0.2.0-rc.2` | 289313640 | 否 |
| `0.2.1-rc.1` | 289382752 | 否 |
| `0.2.1-rc.2` | 289385456 | 否 |
| `0.2.1-rc.3` | 289415960 | 否 |
| `0.2.1-rc.5` | 289408544 | 否（rc.4 是 404，版本不连续） |
| `0.2.2-rc.1` | 293425336 | 否（首轮曾是当前 feed 版本） |
| `0.2.2-rc.2` | 299659528 | 否 |
| `0.2.2-rc.3` | 299595752 | **是（当前）** |

所以归档器**每轮**都做 bounded gap probe，且只承认真实 `HEAD 200` 的版本，
绝不靠数字递推「推断」版本存在。

## 文件

| 文件 | 作用 |
| --- | --- |
| `.github/workflows/archive-dsh.yml` | 定时 + 手动触发的 Action，含发布后远端 size 复核 |
| `scripts/watch_dsh.py` | 轮询 / 解析 / 探测 / 下载 / 校验 / 状态维护，纯标准库 |
| `scripts/selftest.py` | CI 每轮先跑：离线单测 + 在线 feed 交叉校验（含拒绝路径） |
| `backfill.txt` | 已实测存在的 9 个版本清单（每行仍会重新 HEAD 证明） |
| `PUSH.md` | 上传 GitHub 的完整流程、验收清单、失败定位 |
| `push.ps1` | 一键检查 / 建仓库 / 推送脚本（支持 `-WhatIf`） |
| `VERIFICATION.md` | 实机验证报告（含 2026-11-16 复测）、命令、否定证据、未验证边界 |
| `.gitattributes` | 强制所有文本文件 LF（Linux runner 安全） |
| `.gitignore` | 挡住 300 MB exe 与所有测试临时目录 |
| `versions.json` | 派生索引，非权威；每次运行重写 |
| `.feed-etags.json`、`.feed-cache.json` | ETag 与上次 feed 载荷，用于 304 与失败重试 |

## 部署（3 步，详见 [PUSH.md](PUSH.md)）

1. **新开终端**执行 `powershell -ExecutionPolicy Bypass -File .\push.ps1 -RepoName dsh-harness-archiver`
   （首次需先 `gh auth login`）。本地仓库已初始化、已提交、工作区干净。
2. `Settings → Actions → General → Workflow permissions` 选 **Read and write
   permissions**（`contents: write` 用于建 Release 和提交索引）。
3. 首次运行 `gh workflow run "Archive DeepSeek Harness releases" -f max_new=20 -f probe_steps=12`
   把 **9** 个版本一次性补齐。之后 `*/15 * * * *` 自动轮询。

> Schedule 在仓库长期无活动 60 天后会暂停；`workflow_dispatch` 随时可手动唤醒。
> cron 可改稳妥些（如 `*/30 * * * *`），GitHub 对高频 schedule 会排队，但每轮幂等。

## 运行逻辑

```
每 15 分钟（或手动）
  │
  ├─ selftest.py：解析器 / base64 / URL 白名单 / 拒绝路径 / 在线 feed 交叉校验
  │
  ├─ GET nightly.yml (If-None-Match: <ETag>)
  │    304 → 回放缓存载荷（仍继续比对 Releases，可重试上次失败的上传）
  │    200 → 强制校验 path == files[0].url、顶层 sha512 == files[0].sha512
  │          缺 size 或 sha512 直接终止，绝不下载未校验文件
  │
  ├─ gh api releases → tag 且 .exe 资产 size 与期望一致 = 已完整归档
  │    只有 tag 没有 exe / size 不符 → 判为未完成，本轮重传
  │
  ├─ 候选 = feed 版本（最优先）
  │       + backfill.txt 中 HEAD 200 的版本
  │       + gap_probe 枚举中 HEAD 200 的版本（每轮都跑，默认 rc.0..rc.12）
  │      − 已完整归档
  │
  ├─ 逐个：已存在则强制重算 SHA-512，不符就重下；否则流式下载
  │    校验 字节数 + SHA-512；feed 版本与官方摘要比对，
  │    历史/gap 版本摘要本地计算并在 notes 标明来源
  │
  ├─ gh release create <version>（rc 标 prerelease）+ upload --clobber
  ├─ gh release view <version> --json assets --jq '[...] | max'
  │    静态 jq 表达式复核远端 asset size == 本地已校验 size，不符即 fail 让下轮重试
  │
  └─ 提交 versions.json / ETag / 缓存，[skip ci] 防自触发
```

## 关键设计点

- **不漏版本**：feed 只暴露当前灰度版本，故每轮做有界 CDN 枚举；所有候选必须
  `HEAD 200` 才准入。版本号不连续（实测 `rc.4` 404、`rc.5` 200），不做递推假设。
- **不虚构元数据**：feed 里有 `size`/`sha512` 就严格比对；没有就只记录「本地计算
  的 SHA-512」，绝不声称是官方值。notes 里逐版本写明校验来源。
- **不丢版本**：判据是远端 Release 的 exe 资产存在且 size 匹配，不是「处理过」。
- **不重复下载**：ETag 304 + Releases 集合双过滤；`--dry-run` 零写入（已实测）。
- **失败即重试**：上传中途挂掉 / 摘要不符 / 网络错误，下一轮自动重做。
- **域名白名单**：只接受 `https://download.deepseek.com` 与
  `https://cdn.deepseek.com`；http、外域、后缀伪装域名一律终止（selftest 覆盖）。
- **并发串行化**：`concurrency.group` 按仓库收敛，`cancel-in-progress: false`，
  避免 300 MB 上传被新轮询打断。
- **零第三方依赖**：不用 PyYAML / requests；CI 自带 Python 3.11 + `gh` 即可。

## 手动参数

| 输入 | 默认 | 说明 |
| --- | --- | --- |
| `feeds` | nightly.yml | 逗号或空格分隔多个 feed URL |
| `max_new` | `2` | 单轮最多归档几个（首次补档填 `20`） |
| `probe_steps` | `12` | 有界 CDN 枚举步数，0 = 关闭（**不建议**） |
| `dry_run` | false | 只探测，不下载、不写任何状态文件 |
| `force` | false | 忽略 ETag 强制重取 feed |
| `no_backfill` | false | 跳过历史清单 |

本地调试：

```bash
python scripts/selftest.py
python scripts/watch_dsh.py --dry-run --force --backfill backfill.txt --probe-steps 12
python scripts/watch_dsh.py --feed https://download.deepseek.com/dsh-desk/feeds/win-x64/nightly.yml
```

## 取用某个版本

```bash
gh release download 0.2.2-rc.3 --repo <owner>/<repo> --pattern '*.exe'
```

## 已知边界

- `0.2.2-rc.1`、`0.2.2-rc.2`、`0.2.2-rc.3` 已完成全量下载 + 独立 SHA-512；
  其余 6 个版本只做了 HEAD 存在性证明，未逐个全量下载算哈希；归档器运行时会做。
- GitHub Release 创建/上传、`gh release view --json assets` 远端复核、
  `git push` 回写需要真实仓库与 `contents: write`，本机无该环境，属未实机执行的
  CI 路径。`gh auth status` 未登录，故未代为 push（见 [PUSH.md](PUSH.md)）。
- gap probe 有界；若官方改用全新命名规则（如 `1.0.0-beta.1`），需扩展
  `gap_probe()` 生成规则。脚本不会静默假装覆盖所有格式。
