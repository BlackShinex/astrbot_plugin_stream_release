# 串流版本更新监控（astrbot_plugin_stream_release）

> 仓库：https://github.com/BlackShinex/astrbot_plugin_stream_release

AstrBot 插件：监控三个串流项目的 GitHub Releases，推送更新信息 + AI 摘要，并把新版本安装包自动下载、改名、上传到 QQ 群文件指定文件夹。

- Foundation Sunshine：https://github.com/AlkaidLab/foundation-sunshine/releases
- Moonlight V+（Android）：https://github.com/qiin2333/moonlight-vplus/releases
- Moonlight V+ PC：https://github.com/qiin2333/moonlight-qt/releases

## 功能

- 按开关推送更新通知：正式版 / 预发布版分开（Sunshine 分稳定版、预发布版；Moonlight V+ 与 PC 各一个开关），支持推送到多个群
- 更新说明用当前模型压缩成 3–6 条中文要点（只有新版本时才调用模型，轮询本身 0 token）
- 安装包自动搬运：下载 → 改名 → 上传到群文件指定文件夹；空间不足时按项目名清理旧安装包并提醒管理员
- 自带独立设置页面（WebUI「扩展功能」→「串流版本更新监控」），含「测试通知 / 测试上传」按钮
- 默认配置为发布安全档：监控、通知、上传全部默认关闭，群号、平台名、群文件夹全部默认空

## 安装

**方式一（推荐）**：AstrBot WebUI → 插件 → 从文件安装，上传本仓库 Release 里的 `astrbot_plugin_stream_release-v*.zip`。

**方式二**：把整个插件目录放到 `AstrBot/data/plugins/astrbot_plugin_stream_release/`，重启 AstrBot 或重载插件。

依赖：`httpx`（AstrBot 自带，无需额外安装）。

## 首次使用

1. 打开 WebUI → 左侧「扩展功能」→「串流版本更新监控」
2. 在「全局设定」里添加推送群（可用「从群列表选择」）
3. 需要自动传安装包时：在「文件上传设置」里用文件夹旁的「浏览」选中真实群文件夹（名称自动填入），再打开对应项目的上传开关，最后打开「文件上传总开关」
4. 打开「发布通知总开关」和「启用监控」，保存
5. 可以先用页面右上角的「测试通知」「测试上传」各点一次验证通道

## 默认行为

- 首次运行只静默记录基线，不补发历史版本
- 群文件空间不足时：按项目名删除目标文件夹里较旧的安装包（保留最新 N 个），推送文本提醒；仍不够则本次只发链接
- 后台轮询不消耗 token；每个新版本仅一次摘要调用

## 指令（管理员）

| 指令 | 说明 |
| --- | --- |
| `/releasecheck` | 立即检查一次 |
| `/releasestatus` | 查看开关、群、文件夹、已记录版本 |
| `/releasesync` | 把当前最新版设为基线（不推送） |

## 可移植性

源码与默认配置中不包含任何群号、机器人名、群文件夹 ID：平台自动检测，群与文件夹在设置页从机器人所在群 / 群文件夹列表中选择；解析到的 folder_id 按群缓存到 `state.json`。

## 许可证

[MIT](LICENSE)
