# 实机验证报告

时间：
- 第一轮：2026-11-13 14:47–15:14 (Asia/Shanghai) / 06:47–07:14 UTC
- **第二轮（push 前复测）：2026-11-16 11:15–11:26 (Asia/Shanghai) / 03:15–03:26 UTC**

环境：Windows 10.0.19045、PowerShell 5.1、`dsh-primary-runtime` Python 3.12、
Node、git 2.54.0 / bash 5.3.9 / gh 2.104.0；网络直连官方 CDN 与 github.com。
原则：**每一条结论都附可复现命令与实测输出，不推断、不补写。**

> **第二轮的重要发现**：feed 已从 `0.2.2-rc.1` 更新为 `0.2.2-rc.3`
> （`releaseDate: 2026-11-13T07:31:42.937Z`），且 CDN 上新增了
> `0.2.2-rc.3`。这个新版本恰好在首轮测试结束约 3 小时后发布 —— 直接验证
> 「feed 只暴露当前灰度版本、必须每轮 gap probe」的设计是对的。
> 详见 [§8 第二轮复测](#8-第二轮复测2026-11-16-push-前)。

---

## 1. 为什么需要这份报告

首版方案把 `nightly.yml` 当成完整版本清单。复核时做了穷举探测，结论是**这个前提是错的**：

- 首轮 `nightly.yml` 只指向 `0.2.2-rc.1`，但 CDN 上还有 `0.2.1-rc.5`、`0.2.2-rc.2`
- 第二轮 feed 已切到 `0.2.2-rc.3`，CDN 上共 9 个版本
- 因此「只盯 feed」会漏版本，方案已按此修正（每轮 bounded gap probe）

---

## 2. 官方源逐字取证

### 2.1 feed 全文（2026-11-13 07:13 UTC 最后一次重读）

```
GET https://download.deepseek.com/dsh-desk/feeds/win-x64/nightly.yml
HTTP/1.1 200
Content-Type: text/yaml
Content-Length: 533
ETag: "1aafc4e01d0088cbc22df5f3c9057613"
Last-Modified: Thu, 12 Nov 2026 10:36:50 GMT
Cache-Control: max-age=0
Accept-Ranges: bytes
Server: tencent-cos
x-cos-hash-crc64ecma: 16007864819975415753
```

```yaml
version: 0.2.2-rc.1
files:
  - url: >-
      https://download.deepseek.com/dsh-desk/bin/win-x64/deepseek-harness-0.2.2-rc.1-win-x64.exe
    size: 293425336
    sha512: >-
      swcK0J6ofGkaAWLRpOes2ca7CWO8vuM5Vp0FRnQ/DG3q+Z+OhvsgLz3hIiSYpRUETY+fEz+Ww86fNa0jiVIifw==
path: >-
  https://download.deepseek.com/dsh-desk/bin/win-x64/deepseek-harness-0.2.2-rc.1-win-x64.exe
sha512: >-
  swcK0J6ofGkaAWLRpOes2ca7CWO8vuM5Vp0FRnQ/DG3q+Z+OhvsgLz3hIiSYpRUETY+fEz+Ww86fNa0jiVIifw==
stagingPercentage: 100
releaseDate: '2026-11-04T09:31:49.677Z'
```

feed 内部一致性：`path == files[0].url`，顶层 `sha512 == files[0].sha512`（脚本强制校验，不一致即终止）。

### 2.2 feed SHA-512（base64 → hex，两条独立链路一致）

```
base64 : swcK0J6ofGkaAWLRpOes2ca7CWO8vuM5Vp0FRnQ/DG3q+Z+OhvsgLz3hIiSYpRUETY+fEz+Ww86fNa0jiVIifw==
decoded: 64 bytes
hex    : b3070ad09ea87c691a0162d1a4e7acd9c6bb0963bcbee339569d0546743f0c6deaf99f8e86fb202f3de1222498a515044d8f9f133f96c3ce9f35ad238952227f
```

- 链路 A：`[Convert]::FromBase64String()` → PowerShell 逐字节 `x2`
- 链路 B：`scripts/watch_dsh.py::normalize_digest()`（Python `base64.b64decode`）
- 两条链路输出逐字符相同。

### 2.3 全量下载 + 独立哈希（不复用脚本任何代码）

```
Invoke-WebRequest -OutFile deepseek-harness-0.2.2-rc.1-win-x64.exe
ACTUAL_SIZE    = 293425336          (== feed size)
Get-FileHash -Algorithm SHA512
ACTUAL_SHA512  = b3070ad09ea87c691a0162d1a4e7acd9c6bb0963bcbee339569d0546743f0c6deaf99f8e86fb202f3de1222498a515044d8f9f133f96c3ce9f35ad238952227f
VERDICT        = PASS (independent chain)
```

脚本自身 `hashlib.sha512` 流式摘要也得到同一值。三方（feed / PowerShell / Python）一致。

### 2.4 第二个独立全量校验：`0.2.2-rc.2`（feed 里没有的版本）

```
Invoke-WebRequest -OutFile deepseek-harness-0.2.2-rc.2-win-x64.exe
ACTUAL_SIZE    = 299659528          (== HEAD Content-Length)  → SIZE_VS_HEAD=PASS
Get-FileHash -Algorithm SHA512
ACTUAL_SHA512  = e6b440915aee2a49b82842c06d3a95f1916b3304a8de81129f35dbf5e6e42563c231ee96b8739141947adee63e06a01ceac05dc81b7049e96c08687493535c24
```

该版本没有 feed 元数据，因此其 SHA-512 是**本地计算的事实**，不是官方声明值；
归档器会把这类版本标为 `source: gap-probe/backfill` 并在 notes 里如实说明校验来源。

### 2.5 ETag 条件请求

```
GET + If-None-Match: "1aafc4e01d0088cbc22df5f3c9057613"  → 304 Not Modified
```
脚本在 304 时回放缓存载荷，保证上次上传失败仍会重试。

### 2.6 所有 channel feed 的真实状态

| URL 尾段 | 状态 |
| --- | --- |
| `nightly.yml` | **200**, 533 bytes, text/yaml |
| `latest.yml` / `stable.yml` / `beta.yml` / `alpha.yml` / `dev.yml` / `rc.yml` / `preview.yml` / `canary.yml` | 404 |
| `changelog.json` / `versions.json` / `index.json` | 404 |
| `feeds/win-x64/`（目录） | 404 |
| `bin/win-x64/`（目录） | 404 |

### 2.7 站点端点

| URL | 状态 |
| --- | --- |
| `https://download.deepseek.com/` | 200, 2211 bytes, text/html（移动端 App 落地页，无桌面版版本信息） |
| `https://www.deepseek.com/` | 200, 115730 bytes, text/html |
| `https://harness.deepseek.com/` | 404 |
| `https://harness.deepseek.com/download` | 404 |

### 2.8 本机安装（用于说明 feed 与本地版本不同）

| 来源 | 值 | 说明 |
| --- | --- | --- |
| `DeepSeek Harness.exe` FileVersion | `0.2.1-rc.3` | 应用版本，**落后于 feed** |
| 同文件 ProductVersion | `0.2.1.0` | 打包时归一化 |
| 同文件 ProductName | `DeepSeek Harness` | |
| 同文件 CompanyName | `GitHub, Inc.` | Electron 默认值 |
| `resources/app/package.json` → version | `0.2.1-rc.3` | |
| 同文件 `dshDesktopUpdateChannel` | `nightly` | |
| `resources/app-update.yml` → url | `https://download.deepseek.com/dsh-desk/feeds/win-x64/` | updater 基址 |
| 同文件 `channel` | `nightly` | 故实际取 `nightly.yml` |
| 安装根 `version` 文件 | `44.7.0` | **Electron/Chromium 版本，不是应用版本** |

`44.7.0` 曾容易被误读成应用版本，此处明确区分。

---

## 3. 穷举探测：feed 不是完整版本清单

对 `https://download.deepseek.com/dsh-desk/bin/win-x64/deepseek-harness-<v>-win-x64.exe`
做了有界穷举 HEAD：`0.0.0`…`0.3.3` 的 `rc.0`…`rc.12` 及无后缀正式版。

**命中的全部 8 个版本**（`HEAD 200` + `Content-Length` + `Last-Modified` + `ETag`）：

| 版本 | 字节数 | Last-Modified (GMT) | ETag | 在 feed 中？ |
| --- | ---: | --- | --- | --- |
| `0.2.0-rc.1` | 288472536 | Mon, 28 Sep 2026 12:57:48 | `"904c93dcc168ec6932cfb6231a6d061f"` | 否 |
| `0.2.0-rc.2` | 289313640 | Tue, 29 Sep 2026 10:36:09 | `"7754925fe3c4542123eca76c0e11076e"` | 否 |
| `0.2.1-rc.1` | 289382752 | Sat, 17 Oct 2026 06:41:30 | `"c3a13bd855855d2d3b5ec7e8a02dae31"` | 否 |
| `0.2.1-rc.2` | 289385456 | Sat, 17 Oct 2026 09:36:06 | `"f8fa3ed15a9519683e16ae8f0810e1c7"` | 否 |
| `0.2.1-rc.3` | 289415960 | Sat, 24 Oct 2026 05:13:01 | `"f72a878df10f7b2297ac3b2f38d296cc"` | 否（本机安装版） |
| `0.2.1-rc.5` | 289408544 | Fri, 30 Oct 2026 02:01:50 | — | **否（新发现）** |
| `0.2.2-rc.1` | 293425336 | Wed, 04 Nov 2026 09:50:18 | `"ecd1d9880f68ec91a63b4936163bfc44"` | **是（feed 当前指向）** |
| `0.2.2-rc.2` | 299659528 | Wed, 11 Nov 2026 11:58:32 | `"d3b3d0a89cabee101f0381bc2f5dd8f5"` | **否（新发现）** |

`0.2.2-rc.2` 完整响应头还包含 `Content-Type: application/vnd.microsoft.portable-executable`、
`x-cos-hash-crc64ecma: 689760880065028319`、`Cache-Control: max-age=86400`，且
`.exe.blockmap` 边车存在（310593 bytes）——确认为正式构建产物，不是残留文件。

**否定证据（404 抽样）**：`0.2.0-rc.0`、`0.2.0-rc.3`、`0.2.1-rc.0`、
`0.2.1-rc.4`、`0.2.1-rc.6`、`0.2.2-rc.0`、`0.2.2-rc.3`、`0.2.2`、
`0.2.3-rc.0`、`0.2.3-rc.1`、`0.2.3`、`0.1.0`、`0.1.9`、`0.3.0-rc.1`、`0.3.0`。

推论（有证据支撑）：版本号**不连续**（`rc.4` 404 而 `rc.5` 存在），所以
不能靠 `+1` 推断版本；必须逐个 HTTP 证明。脚本的 `gap_probe()` 正是这么做的。

---

## 4. 由此修正的设计（相对首版）

| 缺陷 | 修正 |
| --- | --- |
| 只盯 feed，会漏 `0.2.1-rc.5` / `0.2.2-rc.2` | **每轮**执行 bounded gap probe（默认 12 步），仅 `HEAD 200` 才准入 |
| gap probe 原本只在 feed 变化时跑 | 改为每轮跑；feed 不变也可能 CDN 上有新包 |
| YAML 子集解析器漏读 `files[0].size` / `files[0].sha512` | 重写为支持嵌套序列的递归解析；强制交叉校验 `path` vs `files[0].url`、顶层 vs 嵌套 `sha512`，不一致即终止 |
| feed 缺 size/sha512 时会下载未校验文件 | 缺少任一即 `die()`，拒绝下载 |
| 复用本地文件只比大小、不重算哈希 | `verify_file()` 强制流式重算 SHA-512 并比对 |
| 「已归档」只判 tag 存在 | 改为校验远端 `.exe` 资产存在且 `size` 与期望一致；截断上传判为未完成并重试 |
| `versions.json` 被当权威 | 明确为派生索引，日志标注 offline 模式「NOT authoritative」 |
| 版本排序用数字序列，`0.2.1` 会排在 `0.2.1-rc.3` 前 | 改用 semver：预发布排在正式版之前 |
| `--dry-run` 仍写 ETag/缓存 | 改为零写入，已用「目录前后文件数」验证 PASS |
| 并发 push/schedule 可能双跑 | `concurrency.group` 收敛到仓库级，`cancel-in-progress: false` |
| Release 上传后无远端复核 | 上传后 `gh api` 比对远端 asset size 与本地已校验文件，不符即 fail 让下轮重试 |
| 无 CI 自检 | 新增 `scripts/selftest.py`，每次运行前执行（离线单测 + 在线 feed 交叉校验） |

---

## 5. 自检与端到端结果

`python scripts/selftest.py` — **全部通过**：

```
yaml.version / yaml.files_seq / yaml.folded_url / yaml.nested_size
yaml.nested_sha512 / yaml.quoted_date
digest.base64 / digest.hex_passthrough / digest.whitespace / digest.empty_is_none
sort.semver
url.allow / url.allow_cdn / url.reject (http / foreign host / suffix-spoof)
feed.reject_path_url_mismatch
live.feed_version / live.feed_size / live.feed_sha512_hex / live.feed_etag
live.feed_changed / live.etag_304
live.exe_head_200 / live.exe_size_matches_feed
SELFTEST SUMMARY: version=0.2.2-rc.1 size=293425336
  sha512=b3070ad0...227f  exe_http=200 exe_content_length=293425336
```

`python scripts/watch_dsh.py --dry-run --force --backfill backfill.txt --max-new 20 --probe-steps 12`
→ 8 个候选，与穷举结果完全一致；`dry-run: no download, no index/etag/cache writes`，
目录前后均为空 → `DRY_ZERO_WRITE=PASS`。

`python scripts/watch_dsh.py --backfill backfill.txt --max-new 1 ...`（复用已独立校验的
293 MB 文件）→ `reuse+verified ... sha512=b3070ad0...227f`，写出 `versions.json` 与
`.notes.md`，退出码 0。

workflow YAML 用 `js-yaml` 解析通过：`on: schedule/workflow_dispatch/push`、
`permissions: contents: write`、`concurrency` 存在、7 个 step。

---

## 6. 可复现命令

```bash
# feed 全文
curl -i https://download.deepseek.com/dsh-desk/feeds/win-x64/nightly.yml

# 版本存在性（200 = 存在，404 = 不存在）
curl -I https://download.deepseek.com/dsh-desk/bin/win-x64/deepseek-harness-0.2.2-rc.2-win-x64.exe

# base64 → hex
printf '%s' 'swcK0J6ofGkaAWLRpOes2ca7CWO8vuM5Vp0FRnQ/DG3q+Z+OhvsgLz3hIiSYpRUETY+fEz+Ww86fNa0jiVIifw==' \
  | base64 -d | xxd -p -c 128

# 本地校验
sha512sum deepseek-harness-0.2.2-rc.1-win-x64.exe

# 自检 / 干跑
python scripts/selftest.py
python scripts/watch_dsh.py --dry-run --force --backfill backfill.txt --probe-steps 12
```

---

## 7. 尚未做的验证（明确边界）

- `0.2.2-rc.1`、`0.2.2-rc.2`、`0.2.2-rc.3` 已完成全量下载 + 独立 SHA-512；
  其余 6 个历史包只做了 HEAD（存在性 + 长度），**未逐个全量下载算哈希**。
  归档器在真正归档时会对每个包流式计算 SHA-512 并写入 Release notes，
  此处不预先声称。
- GitHub Release 创建/上传、`gh release view --json assets` 远端 size 复核、
  `git push` 回写状态这几步需要真实仓库与 `contents: write` 权限，本机无该环境，
  属于**未实机执行**的 CI 路径，代码与 workflow 已按幂等 + 失败重试设计。
  `gh --jq` 的 jq 表达式已改为静态无变量形式，规避离线无法验证的变量注入。
- gap probe 是有界的（`0.x.y` 已知 bases 的 `rc.0..rc.12` 及下一个 patch 的
  `rc.0..rc.2`）。若官方将来改用全新命名规则（如 `1.0.0-beta.1`），需扩展
  `gap_probe()` 的生成规则；脚本不会静默假装覆盖所有格式。

---

## 8. 第二轮复测（2026-11-16，push 前）

### 8.1 官方源状态已变化（逐字取证）

```
GET https://download.deepseek.com/dsh-desk/feeds/win-x64/nightly.yml
HTTP/1.1 200
Content-Type: application/yaml        ← 首轮是 text/yaml
Content-Length: 533
ETag: "3c7d64d0476bf5891c31d24246a627da"   ← 首轮 1aafc4e0…
Last-Modified: Fri, 13 Nov 2026 15:40:43 GMT
Date: Mon, 16 Nov 2026 03:16:45 GMT
Cache-Control: max-age=0
Server: tencent-cos
```

```yaml
version: 0.2.2-rc.3
files:
  - url: >-
      https://download.deepseek.com/dsh-desk/bin/win-x64/deepseek-harness-0.2.2-rc.3-win-x64.exe
    size: 299595752
    sha512: >-
      GiJf8kx+C7eoiZ0xQSYBcTyh5xmXQl+vh63U6uSm963McK1AkMgB8PydzuekWdUY7hyURvhVMHANM4pn+tCKzQ==
path: >-
  https://download.deepseek.com/dsh-desk/bin/win-x64/deepseek-harness-0.2.2-rc.3-win-x64.exe
sha512: >-
  GiJf8kx+C7eoiZ0xQSYBcTyh5xmXQl+vh63U6uSm963McK1AkMgB8PydzuekWdUY7hyURvhVMHANM4pn+tCKzQ==
stagingPercentage: 100
releaseDate: '2026-11-13T07:31:42.937Z'
```

**Content-Type 变化是一个真实边界**：PowerShell `Invoke-WebRequest` 对
`application/yaml` 返回 `byte[]`（首轮 `text/yaml` 返回 string），直接打印会得到
一串 ASCII 数字。脚本用 `r.read().decode("utf-8")` 读原始字节，与 Content-Type
无关 —— 已实测解析结果完全正确。

### 8.2 `0.2.2-rc.3` 独立全量校验

```
FEED_B64  = GiJf8kx+C7eoiZ0xQSYBcTyh5xmXQl+vh63U6uSm963McK1AkMgB8PydzuekWdUY7hyURvhVMHANM4pn+tCKzQ==
EXPECTED  = 1a225ff24c7e0bb7a8899d31412601713ca1e71997425faf87add4eae4a6f7adcc70ad4090c801f0fc9dcee7a459d518ee1c9446f85530700d338a67fad08acd

Invoke-WebRequest -OutFile deepseek-harness-0.2.2-rc.3-win-x64.exe
ACTUAL_SIZE   = 299595752
Get-FileHash -Algorithm SHA512
ACTUAL_SHA512 = 1a225ff24c7e0bb7a8899d31412601713ca1e71997425faf87add4eae4a6f7adcc70ad4090c801f0fc9dcee7a459d518ee1c9446f85530700d338a67fad08acd
VERDICT       = PASS (independent chain)
```

exe 响应头：`Content-Type: application/vnd.microsoft.portable-executable`、
`ETag: "a00249bd408a1341c8c453294c03c506"`、
`Last-Modified: Fri, 13 Nov 2026 08:08:00 GMT`、
`x-cos-hash-crc64ecma: 6725580478689904086`；`.blockmap` 边车 200 / 311272 B。

同时复测 8 个旧版本全部仍为 200；探测 `0.2.2-rc.4/.5`、`0.2.3*`、
`0.3.0-rc.1`、`0.2.1-rc.6/.7` 全部 404。CDN 上共 **9** 个版本。

### 8.3 完整回归测试结果（全通过）

| 测试 | 结果 |
| --- | --- |
| `py_compile scripts/*.py` | exit 0 |
| `selftest.py`（21 项，含拒绝路径 + 在线 feed 交叉校验） | 全 OK，exit 0；live version `0.2.2-rc.3`、size `299595752`、sha512 `1a225ff2…08acd`、ETag `3c7d64d0…`、304 正常 |
| workflow YAML（js-yaml） | 5 顶层键、3 触发器、cron `*/15`、7 步、`probe_steps` 默认 12、`permissions contents: write` |
| 全部 5 个 `run:` 块 `bash -n` | 全部 exit 0（用 Git Bash 5.3.9 校验，模拟 Linux runner） |
| 全新状态 `--dry-run --force --backfill --probe-steps 12` | 9 个候选，与穷举一致；零文件写入 |
| 真实归档 `--force --max-new 1` | `reuse+verified` 强制重算 SHA-512 = feed 值；写出 `versions.json` 与 `.notes.md`，exit 0 |
| `git init -b main` + 提交 | 3 个 commits，工作区干净 |
| `.gitignore` 生效 | `git check-ignore` 命中 `dist/` 与 `__pycache__/`；11 个跟踪文件全部 < 27 KB |
| `.gitattributes` LF | 全部 11 个跟踪 blob `CR=0` |
| `push.ps1` PowerShell 解析器 | 0 错误 |
| `push.ps1 -WhatIf` 全流程 | exit 0，7 个步骤全部走通，`REMOTES=0`、`STATUS=0` 零副作用 |

### 8.4 push 前修掉的 4 个真实缺陷

1. `gh release create` 用空数组 `"${extra[@]}"` + `set -u` 有旧版 bash 兼容隐患
   → 改为显式 if/else 两分支，不带数组。
2. `gh api --jq ".assets[] | select(.name == \"$(basename "$f")\")"` 嵌套引号脆弱
   → 改为 `gh release view "$v" --json assets --jq '[...] | max'` 静态表达式，
   无 shell 插值、无变量注入。
3. `--jq --arg n "$asset_name"` 不是 gh 支持的用法 → 已移除。
4. `push.ps1` 中 `git remote get-url origin` 在 origin 不存在时向 stderr 输出，
   在 `powershell -File` + `$ErrorActionPreference='Stop'` 下被当作终止错误
   → 改用 `git remote` 列名字 + `git config --get`，WhatIf 第三次跑通。

另外新增 `.gitattributes` 强制 LF，避免 Windows CRLF 让 Linux runner 上的 bash
脚本带 `\r` 报错。

### 8.5 工具链与仓库状态

| 工具 | 版本 | 位置 |
| --- | --- | --- |
| git | `2.54.0.windows.1` | `C:\Users\Administrator\AppData\Local\hermes\git\cmd\git.exe` |
| git-lfs | `3.7.1` | 同工具链（本项目未使用） |
| bash | `5.3.9(1)-release` | `...\hermes\git\bin\bash.exe` |
| gh | `2.104.0` | `C:\Users\Administrator\AppData\Local\hermes\gh\gh.exe`（本次新装，已持久化用户 PATH） |

`git ls-remote https://github.com/git/git.git HEAD` → `7fb6055e678f…`（网络连通）。
`gh auth status` → 未登录（需要用户自己的 GitHub 身份，故未代为 push）。

本地仓库：`main`，3 个 commits，11 个跟踪文本文件，工作区干净，无 remote。
上传流程见 [PUSH.md](PUSH.md)。

