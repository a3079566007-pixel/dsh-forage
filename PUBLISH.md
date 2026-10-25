# 发布说明

## 当前状态（已发布）

- 仓库：https://github.com/a3079566007-pixel/dsh-forage （public，默认分支 main）
- 版本：v0.1.0（tag 已推），Release：https://github.com/a3079566007-pixel/dsh-forage/releases/tag/v0.1.0
- 话题：dsh / dsh-plugin / deepseek-harness / bilibili / video / knowledge-base / whisper / ocr

## 本机发布方式（已固化，无需重复登录）

| 项 | 值 |
|---|---|
| GitHub 账号 | a3079566007-pixel（gh 已登录，token 存于 Windows 凭据管理器 keyring） |
| 推送通道 | SSH over 443：`ssh://git@ssh.github.com:443/a3079566007-pixel/dsh-forage.git` |
| 私钥 | `~/.ssh/dsh_forage_ed25519`（仓库 Deploy key，读写） |
| git 凭据助手 | `credential.https://github.com.helper = gh auth git-credential` |
| 代理 | 全局 `http.https://github.com.proxy = http://127.0.0.1:12450`（仅 HTTPS 走；SSH 推送不需要） |
| 凭据备份 | `~/.dsh/backups/gh-hosts.yml`（token 本体在 keyring） |

以后推送：

```powershell
$git = "$env:USERPROFILE\.dsh\tools\mingit\cmd\git.exe"
& $git -C "$env:USERPROFILE\.dsh\repos\dsh-forage" push origin main --tags
```

## 备选：GitHub 网页 / PAT

- 网页建仓并加 Deploy key（Settings → Deploy keys，勾 Allow write）；
- 或使用 PAT：`gh auth login --with-token`，或 `git credential approve`。

## 发布检查清单

- [x] README / LICENSE / CHANGELOG / .gitignore
- [x] 包元数据（repository / homepage / bugs / keywords）
- [x] 默认分支 main
- [x] tag v0.1.0 + Release notes
- [x] topics
- [ ] npm 发布（可选，需要 npm 账号）
