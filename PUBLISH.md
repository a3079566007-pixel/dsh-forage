# 发布说明

## 当前状态

- 本地仓库：`~/.dsh/repos/dsh-forage`，分支 `main`，首个提交 `b210a31`
- 离线包：`~/.dsh/repos/dsh-forage.bundle`（可 `git clone dsh-forage.bundle dsh-forage`）
- 打包：`~/.dsh/repos/dsh-forage.zip`

## 发布到 GitHub

本机实测 `github.com:443` 直连超时，且未配置任何 GitHub / Gitee 凭据，因此**推送需要你提供凭据或代理**。

1. 先在网页建空仓库（不要勾选 Initialize README）：
   https://github.com/new?name=dsh-forage
2. 在本机执行（PAT 代替密码）：

```powershell
$git = "$env:USERPROFILE\.dsh\tools\mingit\cmd\git.exe"
& $git -C "$env:USERPROFILE\.dsh\repos\dsh-forage" remote add origin https://github.com/mikejikichen/dsh-forage.git
& $git -C "$env:USERPROFILE\.dsh\repos\dsh-forage" push -u origin main
```

3. 若仍超时，先给 git 配一个可用代理（端口按你的客户端改）：

```powershell
& $git config --global http.https://github.com.proxy http://127.0.0.1:7890
```

## 发布到 Gitee（备选，国内直连可用）

```powershell
& $git -C "$env:USERPROFILE\.dsh\repos\dsh-forage" remote add origin https://gitee.com/mikejikichen/dsh-forage.git
& $git -C "$env:USERPROFILE\.dsh\repos\dsh-forage" push -u origin main
```

## 发布到 npm（可选）

```powershell
cd "$env:USERPROFILE\.dsh\repos\dsh-forage"
npm publish --access public
```

## 名称可用性（2026-10-25 实测）

- npm：`dsh-forage` 未占用（404）
- DSH 插件目录：无同名条目
- GitHub 仓库搜索：`dsh-forage` 0 个结果
- 对照：`dsh-harvest` 已被 @stifer/dsh-harvest 占用（catalog + 8 个同名仓库）
