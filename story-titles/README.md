# 剧情标题资料包

主线、活动、其他故事的标题独立存放在本目录，沿用角色资料包的下载架构。

- 唯一公开下载文件：`public-data/metadata.dat`。
- 内容为 `event`、`main`、`other` 三类故事，按实际剧情资源编号关联。
- 只保存故事标题、章节标题及已确认的章节编号。账号、登录凭据、解锁状态、剧情正文、图片和音频不进入资料包。
- 使用与角色资料相同的 `ESCA-META-2` 压缩封装、Base64 编码和内置 SHA-256 校验；包内只含 `stories.json`，由独立标题读取器处理。此编码不提供保密性。
- 播放器匿名下载，全部分类与校验通过后才原子替换本地 `data/shared-story-titles.json`。下载或校验失败时保留旧缓存。
- 角色资料刷新不会用旧包内的标题覆盖本标题缓存。

## 维护

当前只建立存储、发布和下载结构，不执行游戏登录或云端采集。后续采集器只需输出同样的标题文档并调用打包函数，无需改变播放器下载流程。

在本播放器项目中，初始可编辑资料保存为 `data/story-titles-source.json`。修改后执行：

```sh
python tools/shared_story_data.py --pack data/story-titles-source.json outputs/github-story-titles-repo/story-titles/public-data
```

将生成的 `metadata.dat` 更新到本目录的 `public-data/metadata.dat` 即可。不要把本机整个 `data` 目录上传。

也可以在本资料仓库检出目录中解包、编辑并重新打包：

```sh
python story-titles/tools/shared_story_data.py --unpack story-titles/public-data/metadata.dat /tmp/story-titles.json
# 编辑临时 JSON，只填写实际取得的标题和编号。
python story-titles/tools/shared_story_data.py --pack /tmp/story-titles.json story-titles/public-data
```

工具复用相邻 `character-profiles/tools` 中的公共校验与路径模块。播放器配置保存在 `story-titles.config.json`，与角色资料来源配置独立。
