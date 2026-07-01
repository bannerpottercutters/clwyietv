# 本地工具运维排错

> 本地工具联网与排错参考。术语"本地工具"见 `AGENTS.md`。

## 本地工具联网走 v2rayN 代理

本机常挂 v2rayN：`127.0.0.1:10808`（SOCKS5）/ `10809`（HTTP），以本机实际端口为准。

本地工具拉 GitHub / 访问 OpenAI 等海外端点超时或连不上时，临时走代理：
- git：`git -c http.proxy=http://127.0.0.1:10809 -c https.proxy=http://127.0.0.1:10809 clone ...`
- 环境变量：`$env:HTTPS_PROXY='http://127.0.0.1:10809'; $env:HTTP_PROXY='http://127.0.0.1:10809'`（或 `socks5://127.0.0.1:10808`）
- 实例：Codex 装 `skill-creator` 时 GitHub API / 直连均超时，临时走 `127.0.0.1:10808` 后装成。

只在必要时加代理，别全局长期挂（会拖慢本可直连的请求）。

## 本地 Chrome 打开含中文名的 html

用 `file:///` 在 Chrome 地址栏打开会把中文段吞掉导致 404。先复制一份 ASCII 名打开即可（交付给用户的中文名文件不受影响）。