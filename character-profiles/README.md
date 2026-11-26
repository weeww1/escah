# 角色资料自动采集

GitHub Actions 负责使用维护者的 DMM 平台 Cookie 获取新会话并采集公开角色资料。成功后，将公开资料包同步到 Gitee；Gitee 不需要 DMM Cookie。

## 运行

采集工作流：`.github/workflows/character-profiles.yml`，保留原来的手动入口，由 cron-job.org 在每周三北京时间 15:00 通过 `workflow_dispatch` 启动，18:00 补触发；取消 18:30、周四及每日补跑。它使用专用的单仓库 Actions 读写令牌，2027-11-26 到期；DMM Cookie、代理凭据和现有广权限令牌不提供给定时网站。原生备用定时独立保存在 `.github/workflows/character-profiles-schedule.yml`，使用显式 `Asia/Shanghai` 时区调用同一采集入口。GitHub 原生备用定时同样只在周三 15:00、18:00 调用；周去重周期从周三北京时间 15:00（UTC 07:00）开始。每次先检查本周期是否已完成采集、GitHub 发布及 Gitee 镜像；全部成功后，本周其余自动运行直接跳过，不重复采集。电脑关机不影响云端执行。

2026-11-25 19:00 的云端采集与发布已实测成功。2026-11-26 北京时间 07:07，[45718899347](https://github.com/weeww1/escah/actions/runs/45718899347) 由原生 `schedule` 事件自动触发并成功：周去重确认本周期已完成采集及双站同步，正确跳过重复采集、发布和镜像。自动触发与周去重均已有真实云端运行记录；本次自动补跑没有重新采集资料。

上述记录证明原生自动触发已经出现，不足以判断 GitHub 内部此前无触发记录的具体原因，也不能将恢复归因于某一次配置修改。保留补跑和周去重，以应对 GitHub 原生定时事件的延迟或丢失；计划时间不代表保证准点执行。

每次检查日志记录事件类型、触发 cron、实际开始时间、本周期截止点、决定执行／跳过的原因，以及本周已完成的运行编号。`workflow_dispatch` 配合 `scheduled_check: true` 也使用同一个周去重判断，可供独立云端定时器触发。独立定时器现已接入并启用。2026-11-26 的两次实际定时测试中，15:53、15:54 的请求分别延迟 4.5 秒和 6.5 秒，GitHub 对应运行 [45778648628](https://github.com/weeww1/escah/actions/runs/45778648628) 和 [45778775257](https://github.com/weeww1/escah/actions/runs/45778775257) 均成功，正确跳过本周已完成的采集；两项临时探测任务现已停用。这是两次实际触发的结果，不代表长期准点保证。

公开仓库的原生定时工作流可能因 60 天没有仓库活动而自动停用。实际采集工作流只接受 `workflow_dispatch`，与原生备用定时分开，避免备用定时的停用影响外部请求入口。备用定时使用仓库自带的 `GITHUB_TOKEN` 调用，不保存新的长期凭据。

2026-11-22 端到端试跑 [45015850126](https://github.com/weeww1/escah/actions/runs/45015850126) 已成功：云端代理、DMM 认证、角色资料采集、GitHub 提交和 Gitee 镜像均通过；本次补全 1 个角色，复用 388 个已验证缓存，失败 0 个。

在仓库 Actions Secrets 中保存 `DMM_COOKIES_JSON`；如运行地区需要代理，另设云端可用的 `DMM_HTTP_PROXY`。不要在 YAML、仓库文件、运行输入或日志中填写 Cookie。

也可在 `DMM_MIHOMO_NODE_JSON` Secret 中保存单个 Mihomo/Clash 节点。工作流下载固定版本、校验官方 SHA-256 后启动内核，仅监听 runner 本机 `127.0.0.1:18081`。节点配置只写入任务临时目录，权限为当前用户可读写；结束后清理，不进入仓库、缓存或构建产物。配置该节点时，采集优先走临时代理。

维护者可以运行本机 `python tools/configure_cloud_proxy.py`，将 Clash Verge 当前用于 DMM 的一个节点加密更新到上述 Secret，不上传整个订阅，也不修改原客户端设置。

Cookie 过期时，维护者在小号原游戏页面点击登录凭据扩展更新。连接程序使用本机加密保存的 GitHub 授权，通过官方 Secrets API 和 libsodium 加密刷新凭据；不触发游戏登录。

采集步骤失败时，不执行发布步骤。成功时只提交 `character-profiles/public-data/metadata.dat` 混淆包。游戏 Token、启动签名、账号持有和解锁状态均不发布。

## Gitee 镜像

镜像使用 Actions Secret `GITEE_GIT_TOKEN`。单独的 `.github/workflows/mirror-public-data.yml` 可以手动同步已有公开资料，无需登录 DMM。采集工作流成功发布后也执行同一镜像程序。

镜像程序先读取现有 Gitee `master` 分支，校验并更新 `metadata.dat`。旧版的三个重复明文文件会删除；其他目录及历史保留，不使用强制推送。Gitee 云端到 GitHub 的 API 与 Git 拉取已实测超时，因此当前以 GitHub 主动推送方式完成数据同步。

## 公开资料

- `character-profiles/public-data/metadata.dat`（唯一下载文件）

资料包包含角色资料、剧情标题及各自的内置 SHA-256 校验值，不包含 `manifest.json` 文件。内容经 zlib 压缩及 Base64 编码，固定标识为 `ESCA-META-2`；这不提供保密性，任何人都可还原。播放器在本地限制解压大小，核对内容类型、哈希及公开字段后才更新缓存。`.dat` 按二进制传输，避免 Git 改写换行。采集器直接从包中读取历史资料并比对，只补采新增或缺字段的角色。读取端兼容旧版 `ESCA-META-1` 包。

Gitee 对资料文件的公开访问限制独立于同步结果。此前该站对 `characters.json` 返回 451；具体原因尚未确认，将资料从 GitHub 同步过去不能保证限制自动解除。

播放器的 `shared-data.config.json` 可用 `fallbacks` 配置备用资料源。主源及备用源设置 `format: "packed"`，优先从 Gitee 下载单个 `metadata.dat`，本地还原失败或下载失败时改用 GitHub。每次从同一个来源取得完整资料，全部校验通过后再替换本地缓存；所有来源都失败时保留旧缓存。来源可单独配置 `proxy`，代理不可用时仍会尝试普通网络连接。GitHub 备用源仍需要使用者的网络能够访问 GitHub。
