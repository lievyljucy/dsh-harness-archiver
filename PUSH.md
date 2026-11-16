# 上传到 GitHub 并跑起来的完整流程

本地仓库已初始化并做了初始提交（`main` 分支，工作区干净，9 个文件共约 60 KB，
**无任何 exe 二进制**）。剩下的只差「推到你的 GitHub 仓库」。

> Git 2.54.0 与 gh 2.104.0 已装好并加入用户 PATH；GitHub 网络连通性已实测
> （`git ls-remote https://github.com/git/git.git HEAD` 返回 `7fb6055e…`）。
> `gh auth status` 当前为未登录 —— 需要你自己的 GitHub 账号完成一次登录。

---

## 环境清单（已实测）

| 工具 | 版本 | 位置 |
| --- | --- | --- |
| git | `2.54.0.windows.1` | `C:\Users\Administrator\AppData\Local\hermes\git\cmd\git.exe` |
| git-lfs | `3.7.1` | 同上工具链（本项目不需要 LFS） |
| bash | `5.3.9(1)-release` | `...\hermes\git\bin\bash.exe` |
| gh | `2.104.0` | `C:\Users\Administrator\AppData\Local\hermes\gh\gh.exe` |
| `user.name` | `lievyljucy` | 全局 git config |
| `user.email` | `352022394+lievyljucy@users.noreply.github.com` | 全局 git config |

用户 PATH 已持久化包含 `hermes\git\cmd`、`hermes\git\bin`、`hermes\git\usr\bin`、
`hermes\gh`。**新开一个 PowerShell / 终端窗口** 后 `git`、`gh`、`bash` 均可直接调用。

---

## 方式 A：一键脚本（推荐）

在本目录打开 **新的 PowerShell 窗口**，执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\push.ps1 -RepoName dsh-harness-archiver
```

脚本会按顺序做：检查工具 → 检查提交干净 → `gh auth status`（未登录则提示先登录）
→ `gh repo create` → `git remote add origin` → `git push -u origin main`
→ 提示开启 Workflow 权限 → 打印首次运行命令。

可选参数：

```powershell
# 推到已存在的仓库（不新建）
.\push.ps1 -RepoOwner lievyljucy -RepoName dsh-harness-archiver -NoCreateRepo

# 建私有仓库
.\push.ps1 -RepoName dsh-harness-archiver -Private

# 只检查不执行
.\push.ps1 -RepoName dsh-harness-archiver -WhatIf
```

---

## 方式 B：手动逐条命令

### 1. 登录 GitHub（一次性）

```powershell
gh auth login
```

交互式选择：`GitHub.com` → `HTTPS` → `Login with a web browser` → 按提示在浏览器
粘贴 one-time code。需要 `repo` 权限即可；`workflow` scope 只在你要用 gh 改 workflow
文件时需要（本流程不用）。

验证：

```powershell
gh auth status
git config --get user.name
```

### 2. 创建远程仓库并推送

```powershell
cd C:\Users\Administrator\Documents\deepseek-harness\default-workspace\dsh-harness-archiver

gh repo create dsh-harness-archiver --public --source=. --remote=origin --push
```

或者拆开写，便于看清每一步：

```powershell
gh repo create dsh-harness-archiver --public --confirm
git remote add origin https://github.com/<OWNER>/dsh-harness-archiver.git
git push -u origin main
```

推送完成后确认：

```powershell
git remote -v
git log -1 --oneline
gh repo view --web     # 浏览器打开仓库
```

### 3. 打开 Workflow 写权限（**关键，不做会失败**）

推送后到仓库页面：

```
Settings → Actions → General → Workflow permissions
    → 选 "Read and write permissions" → Save
```

原因：workflow 里 `permissions: contents: write`，需要仓库允许 `GITHUB_TOKEN`
创建 Release 并回写 `versions.json`。默认只读时，`gh release create` 与
`git push` 会 403。

### 4. 首次运行（补齐 9 个版本）

```powershell
gh workflow run "Archive DeepSeek Harness releases" `
  -f max_new=20 `
  -f probe_steps=12
```

或在页面操作：**Actions → Archive DeepSeek Harness releases → Run workflow**
把 `max_new` 填 `20`，`probe_steps` 保持 `12`。

首次会下载并归档当前已实测存在的 9 个版本（约 2.6 GB），
runner 上耗时约几分钟到十几分钟。`timeout-minutes: 60` 足够。

跟踪：

```powershell
gh run list --workflow "Archive DeepSeek Harness releases" --limit 5
gh run watch                 # 实时跟随最新一次
gh release list              # 应出现 9 个 Release
```

### 5. 之后自动运行

`schedule: */15 * * * *` 每 15 分钟轮询。每轮：

- `selftest.py` 自检
- ETag 条件请求（304 零流量）
- 每轮 bounded gap probe（feed 只暴露当前灰度版，实测已漏过 `0.2.1-rc.5`、
  `0.2.2-rc.2`、`0.2.2-rc.3`）
- 新版本才下载 → 校验 → `gh release create/upload --clobber` → 远端 size 复核

> 仓库连续 60 天无活动后 GitHub 会暂停 schedule；用 `workflow_dispatch` 手动
> 跑一次即可唤醒。

---

## 首次运行结果验收清单

```powershell
# Release 数量（应为 9 或更多，若期间官方又发新版）
gh release list --limit 50

# 单个 Release 资产大小
gh release view 0.2.2-rc.3 --json assets --jq '.assets[].name + " " + (.assets[].size|tostring)'

# 索引已回写
gh api repos/<OWNER>/dsh-harness-archiver/contents/versions.json --jq '.size'

# 最新一次运行结论
gh run list --workflow "Archive DeepSeek Harness releases" --limit 1
```

对照 [VERIFICATION.md](VERIFICATION.md) 的哈希值核对 `0.2.2-rc.3`：

```
299595752 bytes
1a225ff24c7e0bb7a8899d31412601713ca1e71997425faf87add4eae4a6f7adcc70ad4090c801f0fc9dcee7a459d518ee1c9446f85530700d338a67fad08acd
```

下载验证：

```powershell
gh release download 0.2.2-rc.3 --pattern '*.exe' --dir .\dl
(Get-FileHash .\dl\deepseek-harness-0.2.2-rc.3-win-x64.exe -Algorithm SHA512).Hash.ToLower()
```

---

## 常见失败与定位

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `gh: Not logged in` | 未登录 | `gh auth login` |
| `Resource not accessible by integration` (403) | Workflow 权限只读 | 见上面第 3 步 |
| `remote: Repository not found` | 仓库名/owner 写错或私有仓库无权限 | `gh repo view` 确认；`gh auth refresh -s repo` |
| `git push` 提示 `GH001: Large files` | 误把 exe 加进 git | `.gitignore` 已挡；`git rm --cached <file>` |
| workflow 里 `python: command not found` | `setup-python` 步骤被跳过 | 检查 Actions 是否启用 |
| `bash: line N: $'\r': command not found` | CRLF | `.gitattributes` 已强制 LF（已验证暂存 blob CR=0） |
| 首次只归档了 2 个版本 | `max_new` 用了默认 2 | 再跑一次 `-f max_new=20`，幂等会补齐 |
| Release 建了但没 exe | 上传中断 | 下一轮自动重试（`--clobber` + 远端 size 复核） |

---

## 为什么没有替你直接 push

`gh auth status` 显示未登录，创建仓库和推送必须用你自己的 GitHub 身份。
本地提交、工具链、`.gitignore`、LF、脚本语法、workflow YAML 都已就绪并实测，
你只需完成一次 `gh auth login` 然后执行方式 A 的一条命令。
