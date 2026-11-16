# 部署与推送

本文只说明如何把本仓库推到 GitHub 并让它跑起来。

## 前置

| 工具 | 说明 |
| --- | --- |
| git | 用于本地提交 |
| gh | 用于建仓、触发 workflow、查看 Release |
| python | 仅在需要用 `push_rest.py` 时使用（标准库即可） |

GitHub token 从 git 凭据管理器读取，不写入磁盘、不进仓库：

```powershell
# 首次需要登录，之后凭据由管理器缓存
gh auth login
gh auth status
```

## 推送

### 方式一：常规 `git push`

```powershell
cd <repo dir>
git add -A; git commit -m "message"
powershell -ExecutionPolicy Bypass -File .\push.ps1 -ProjectDir . -RepoName dsh-harness-archiver
```

`push.ps1` 会检查工作区与换行符、必要时创建仓库、把 origin 规范为
canonical URL，然后 `git push`。支持 `-WhatIf` 干跑与 `-NoCreateRepo`。

### 方式二：`push_rest.py`

当 `git push` 不可用时（例如所在网络把 github.com 重写到不转发认证的代理），
改用 Git Data API 推送。它走 `api.github.com`，与 git 传输无关：

```powershell
cd <repo dir>
python push_rest.py --owner <你的账号> --repo dsh-harness-archiver
```

流程：读本地对象 → 上传 blob/tree/commit → 更新分支引用 → 校验远端 tree 与
`git rev-parse main^{tree}` 相等。已同步过时会直接提示无需推送，只追加新提交。
以 **tree 相等**作为验收判据（commit SHA 会因平台统一时区而不同）。

## 首次运行

仓库需要在 `Settings → Actions → General → Workflow permissions` 里选择
**Read and write permissions**，否则无法创建 Release。

```powershell
gh workflow run "Archive DeepSeek Harness releases" -f max_new=20 -f probe_steps=12
gh run list --workflow "Archive DeepSeek Harness releases" --limit 3
```

`max_new=20` 用于一次性补齐已存在的历史版本。

## 验收

```powershell
gh release list --limit 50
gh release view 0.2.2-rc.3 --json assets --jq '.assets[] | "\(.name) \(.size)"'
gh run download --name versions-index
```
