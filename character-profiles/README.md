# 角色资料自动采集

GitHub Actions 负责使用维护者的 DMM 平台 Cookie 获取新会话并采集公开角色资料。Gitee 可以通过独立同步任务拉取这个仓库的公开 JSON，不需要 DMM Cookie。

## 运行

工作流：`.github/workflows/character-profiles.yml`。先在 Actions 中手动运行；首次验证成功前不启用定时任务。

在仓库 Actions Secrets 中保存 `DMM_COOKIES_JSON`；如运行地区需要代理，另设云端可用的 `DMM_HTTP_PROXY`。不要在 YAML、仓库文件、运行输入或日志中填写 Cookie。

Cookie 过期时，维护者在小号原游戏页面点击登录凭据扩展更新。连接程序使用本机加密保存的 GitHub 授权，通过官方 Secrets API 和 libsodium 加密刷新凭据；不触发游戏登录。

采集步骤失败时，不执行发布步骤。成功时只提交 `character-profiles/public-data/` 下的三个 JSON。游戏 Token、启动签名、账号持有和解锁状态均不发布。

## 公开资料

- `character-profiles/public-data/manifest.json`
- `character-profiles/public-data/characters.json`
- `character-profiles/public-data/stories.json`

公开 JSON 固定使用 LF 换行，并由清单中的 SHA-256 校验。Gitee 同步程序读取同一个 GitHub 提交中的三个文件，全部校验通过后才更新镜像。

Gitee 对资料文件的公开访问审核独立于同步结果。此前该站对 `characters.json` 返回 451；将资料从 GitHub 同步过去不能保证审核限制自动解除。
