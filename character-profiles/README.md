# 角色资料自动采集

GitHub Actions 负责使用维护者的 DMM 平台 Cookie 获取新会话并采集公开角色资料。成功后，将公开 JSON 同步到 Gitee；Gitee 不需要 DMM Cookie。

## 运行

工作流：`.github/workflows/character-profiles.yml`。每周三北京时间 18:00 自动采集并同步，也可在 Actions 中手动运行。

2026-11-22 端到端试跑 [45015850126](https://github.com/weeww1/escah/actions/runs/45015850126) 已成功：云端代理、DMM 认证、角色资料采集、GitHub 提交和 Gitee 镜像均通过；本次补全 1 个角色，复用 388 个已验证缓存，失败 0 个。

在仓库 Actions Secrets 中保存 `DMM_COOKIES_JSON`；如运行地区需要代理，另设云端可用的 `DMM_HTTP_PROXY`。不要在 YAML、仓库文件、运行输入或日志中填写 Cookie。

也可在 `DMM_MIHOMO_NODE_JSON` Secret 中保存单个 Mihomo/Clash 节点。工作流下载固定版本、校验官方 SHA-256 后启动内核，仅监听 runner 本机 `127.0.0.1:18081`。节点配置只写入任务临时目录，权限为当前用户可读写；结束后清理，不进入仓库、缓存或构建产物。配置该节点时，采集优先走临时代理。

维护者可以运行本机 `python tools/configure_cloud_proxy.py`，将 Clash Verge 当前用于 DMM 的一个节点加密更新到上述 Secret，不上传整个订阅，也不修改原客户端设置。

Cookie 过期时，维护者在小号原游戏页面点击登录凭据扩展更新。连接程序使用本机加密保存的 GitHub 授权，通过官方 Secrets API 和 libsodium 加密刷新凭据；不触发游戏登录。

采集步骤失败时，不执行发布步骤。成功时只提交 `character-profiles/public-data/` 下的三个 JSON 和 `metadata.dat` 混淆包。游戏 Token、启动签名、账号持有和解锁状态均不发布。

## Gitee 镜像

镜像使用 Actions Secret `GITEE_GIT_TOKEN`。单独的 `.github/workflows/mirror-public-data.yml` 可以手动同步已有公开资料，无需登录 DMM。采集工作流成功发布后也执行同一镜像程序。

镜像程序先读取现有 Gitee `master` 分支，只替换三个公开 JSON 及 `metadata.dat` 并提交，不覆盖仓库其他目录或历史，不使用强制推送。Gitee 云端到 GitHub 的 API 与 Git 拉取已实测超时，因此当前以 GitHub 主动推送方式完成数据同步。

## 公开资料

- `character-profiles/public-data/manifest.json`
- `character-profiles/public-data/characters.json`
- `character-profiles/public-data/stories.json`
- `character-profiles/public-data/metadata.dat`（新版播放器下载入口）

公开 JSON 固定使用 LF 换行，并由清单中的 SHA-256 校验。混淆包将这三个文件进行 zlib 压缩及 Base64 编码，固定标识为 `ESCA-META-1`；它不提供保密性，任何人都可还原。播放器只在本地还原，限制压缩包和还原后的大小，核对文件列表、哈希及公开字段后才更新缓存。`.dat` 按二进制传输，避免 Git 改写换行。Gitee 同步程序核对混淆包与三个 JSON 完全一致后再发布。

Gitee 对资料文件的公开访问限制独立于同步结果。此前该站对 `characters.json` 返回 451；具体原因尚未确认，将资料从 GitHub 同步过去不能保证限制自动解除。

播放器的 `shared-data.config.json` 可用 `fallbacks` 配置备用资料源。主源设置 `format: "packed-v1"`，优先从 Gitee 下载单个 `metadata.dat`，本地还原失败或下载失败时改用 GitHub。省略 `format` 时仍读取旧版 JSON。每次从同一个来源取得完整资料，全部校验通过后再替换本地缓存；所有来源都失败时保留旧缓存。来源可单独配置 `proxy`，代理不可用时仍会尝试普通网络连接。GitHub 备用源仍需要使用者的网络能够访问 GitHub。
