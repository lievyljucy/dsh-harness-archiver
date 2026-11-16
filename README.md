# DeepSeek Harness 全版本归档器

[![Archive DeepSeek Harness releases](https://github.com/lievyljucy/dsh-harness-archiver/actions/workflows/archive-dsh.yml/badge.svg)](https://github.com/lievyljucy/dsh-harness-archiver/actions/workflows/archive-dsh.yml)

监测 DeepSeek Harness 的官方更新源，把发现的每个 Windows 安装包下载、校验，
并保存为独立的 GitHub Release。一个版本一个 Release，永不覆盖、永不过期。

版本清单只由 Action 维护，因此不在本文抄录。要看归档了什么、有哪些版本，
直接看 [Releases](https://github.com/lievyljucy/dsh-harness-archiver/releases)。

## 监测目标

| 项 | 值 |
| --- | --- |
| 版本清单 | `https://download.deepseek.com/dsh-desk/feeds/win-x64/nightly.yml` |
| 安装包地址 | `https://download.deepseek.com/dsh-desk/bin/win-x64/deepseek-harness-<version>-win-x64.exe` |
| 有效 channel | 仅 `nightly.yml`；`latest.yml` / `stable.yml` / `beta.yml` 等返回 404 |
| 传输能力 | 支持 `Accept-Ranges: bytes` 与 `ETag` 条件请求；安装包带 `.blockmap` 边车 |

清单只暴露当前分发的那一个版本，且版本号不连续，所以归档器除读清单外还会对
CDN 做**有界枚举**，用来发现清单之外的版本；候选是否真实存在一律以 HTTP
`HEAD 200` 为准。

## Action 的行为

每 15 分钟运行一轮，也可手动触发。每轮：

1. 读取官方版本清单，未变更则跳过
2. 有界枚举 CDN，只接受真实存在的版本
3. 与已归档的 Release 比对，跳过已完成的
4. 下载并校验字节数与 SHA-512
5. 每个新版本建一个独立 Release，版本号含 `-` 的标为 prerelease
6. 发布后复核远端资产大小，不符则交给下一轮重试

版本索引以运行产物 `versions-index` 输出。Action 不向仓库提交任何内容，
因此仓库历史里只有仓库所有者的提交。

## 参数

| 输入 | 默认 | 说明 |
| --- | --- | --- |
| `feeds` | `nightly.yml` | 逗号或空格分隔多个清单地址 |
| `max_new` | `2` | 单轮最多归档几个，首次补档可填 `20` |
| `probe_steps` | `12` | CDN 枚举步数，`0` 关闭 |
| `dry_run` | false | 只探测，不下载也不写状态 |
| `force` | false | 忽略 ETag 强制重取清单 |
| `no_backfill` | false | 跳过 `backfill.txt` |

## 目录结构

| 文件 | 作用 |
| --- | --- |
| `.github/workflows/archive-dsh.yml` | 定时 + 手动触发的 Action |
| `scripts/watch_dsh.py` | 读清单、枚举、下载、校验、发布，纯标准库 |
| `scripts/selftest.py` | 运行前自检 |
| `backfill.txt` | 历史版本清单，逐行仍会重新校验存在性 |
| `push.ps1` | 建仓与 `git push`（PowerShell，支持 `-WhatIf`） |
| `push_rest.py` | git 传输不可用时的替代：走 Git Data API 增量同步 |
| `PUSH.md` | 部署与推送说明 |
| `LICENSE` | MIT |

## 取用已归档的版本

```bash
# 列出所有已归档版本
gh release list --repo lievyljucy/dsh-harness-archiver --limit 50

# 下载某个版本（把 <version> 换成 Releases 里的 tag）
gh release download <version> --pattern '*.exe' --dir .
sha512sum deepseek-harness-<version>-win-x64.exe
```

校验用的 SHA-512 就在对应 Release 的 `.notes.md` 里；来自清单的版本，
其摘要与官方值一致，其余版本标注为本地计算值。

## 实现要点

- **字段交叉校验**：清单中 `path` 与 `files[0].url`、顶层与嵌套 `sha512` 必须
  一致；缺少 `size` 或 `sha512` 时终止，不下载未校验文件。
- **域名白名单**：仅接受官方 CDN 的两个域名，明文、外域与后缀伪装一律拒绝。
- **幂等重试**：网络中断、字节数或摘要不符都会重试，重复上传用覆盖方式处理。
- **零第三方依赖**：仅用 Python 标准库与 GitHub 官方 Actions。

## 作者与许可

- 作者：仓库所有者（`lievyljucy`），源码、workflow 与文档均为此项目编写。
- 许可：MIT，见 [LICENSE](LICENSE)；源文件顶部带 `SPDX-License-Identifier: MIT`。
- 第三方组件：Python 标准库；GitHub 官方 Actions
  （`actions/checkout@v4`、`actions/setup-python@v5`、`actions/upload-artifact@v4`）。

## 边界

- 版本清单与安装包由官方提供，本项目只做归档，不修改、不重新签名。
- CDN 枚举是有界的；若官方改用全新的版本命名规则，需扩展枚举生成规则。
- 定时任务在仓库长期无活动后可能被暂停，手动触发一次即可恢复。
