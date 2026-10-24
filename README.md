# 合并转发视频提取

AstrBot 插件：用户**引用一条合并转发消息**并发送 `/提取视频` 时，解析该合并转发（包括嵌套转发），把其中的视频**逐条单独发送**给用户。

~~绅士们应该都明白这个插件的作用（目移）~~

仅支持 OneBot v11（AstrBot 的 aiocqhttp 适配器，NapCat / Lagrange 等）。

AI 辅助创作声明： 本插件在开发过程中使用了人工智能（AI）工具辅助进行代码编写、调试与优化，最终内容经过作者审核与调整。

## 功能

- 指令：`/提取视频`
- 从引用（reply）段找到被引用的合并转发消息
- 支持 NapCat 内联展开的嵌套合并转发，也支持按 forward id 再请求
- 只发送视频，不下载到本地，直接使用视频 URL
- 按配置间隔逐条发送（默认 3 秒）
- 不去重（按需求）
- 私聊默认可用；群聊默认关闭，开启后还需要配置群白名单
- 合并转发里没有视频时回复提示；没引用、引用普通消息、群未授权时静默忽略
- 发送失败 / 视频缺少链接时给出统计提示

## 安装

把整个 `astrbot_plugin_forward_video` 目录放到 AstrBot 的插件目录：

```
AstrBot/data/plugins/astrbot_plugin_forward_video/
```

然后在 AstrBot WebUI 的插件页重载插件，或重启 AstrBot。

- 无第三方依赖，不需要 `requirements.txt`
- Docker 部署时，只需把插件目录挂载到容器的 `/AstrBot/data/plugins/` 下
- 因为视频直接按 URL 发送，不依赖协议端和 AstrBot 共享文件系统

## 使用

1. 在 QQ 中**引用**一条包含视频的合并转发消息；
2. 发送 `/提取视频`；
3. 插件按合并转发中的顺序，逐条发送其中的视频。

触发条件与行为：

| 情况 | 行为 |
| --- | --- |
| 引用合并转发且其中有视频 | 逐条发送 |
| 引用合并转发但其中没有视频 | 回复提示（可配置文案） |
| 没有引用任何消息 | 静默忽略 |
| 引用的不是合并转发 | 静默忽略 |
| 群聊未开启 / 不在白名单 | 静默忽略 |
| 协议端获取失败 | 回复提示（可关闭） |
| 某条视频发送失败 | 继续发下一条，结束后按配置汇总/提示 |
| 发送过程中收到 /stop | 发完当前这条后停止，并在汇总里说明剩余未发送 |

## 配置

在 AstrBot WebUI 的插件配置页修改：

| 配置键 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| private_enabled | bool | true | 是否允许私聊使用 |
| group_enabled | bool | false | 是否允许群聊使用 |
| group_whitelist | list | [] | 允许的群号；留空表示所有群都不可用 |
| interval_seconds | float | 3 | 每条视频之间的间隔（秒），0 表示不等待 |
| max_videos | int | 15 | 单次最多发送条数，0 表示不限制 |
| max_depth | int | 2 | 嵌套合并转发最大深度，0 表示只看顶层 |
| max_nodes | int | 500 | 最多遍历节点数，0 表示不限制 |
| api_timeout_seconds | int | 10 | 调用 get_msg / get_forward_msg 的超时 |
| no_video_tip | text | 合并转发里没有视频 | 无视频时的提示文案 |
| error_tip | bool | true | 获取/发送失败时是否提示 |
| send_summary | bool | true | 发送完成后回复“共 N 条，成功 x 条，失败 y 条” |
| cooldown_seconds | int | 0 | 同一用户冷却时间，0 表示关闭 |

群聊白名单填写纯数字群号，例如：

```json
["123456789", "987654321"]
```

## 指令入口与 LLM

本插件使用标准的 `@filter.command("提取视频")` 注册指令，不是普通的消息监听器。

需要特别说明：AstrBot 的 `ProcessStage` 在 handler **没有发送任何消息**、且消息属于“指令唤醒”时，仍然会继续调用 LLM。也就是说，如果指令命中了但插件静默返回，消息可能会被 AI 再回复一次。

因此插件在 handler 入口调用了 `event.stop_event()`，所有分支（包括没引用、引用普通消息、群未授权、冷却中等静默忽略）都会终止事件传播，不会进入 LLM/Agent 流程。

## /stop 与运行日志

AstrBot 的 `/stop` 在本地 Agent 模式下只是给活跃事件设置 `agent_stop_requested` 和停止信号，**不会强制杀掉插件协程**，插件需要自己检查。

本插件在以下位置检查停止请求：

- 遍历合并转发节点时；
- 每发送下一条视频之前；
- 两条视频之间的等待间隔中（每 0.5 秒检查一次）。

收到 `/stop` 后会停止后续视频，并在汇总里说明“已停止，剩余 N 条未发送”。注意：已经开始的单条 `event.send`（尤其是大视频上传）无法中途打断，会等它结束后再停止。

运行日志统一以 `[合并转发视频]` 开头。默认 INFO 级别只输出概要信息：

- 插件初始化完成
- 指令触发（发送者、群号/私聊）
- 转发节点数、找到的视频数
- 最终汇总
- 发送失败（WARNING）、停止原因

以下细节降为 DEBUG，INFO 级别不会显示：

- 引用消息 id
- forward id / Reply.chain 解析路径
- 每条视频的 `发送第 i/N 条`
- 每条视频的发送成功

需要排查解析问题时，可以在 WebUI 插件页把该插件的日志级别调到 DEBUG。

## 工作流程

```
收到 /提取视频
  -> 优先从 AstrBot 已解析的 Reply.chain 取 Forward(id)
  -> 没有时，从原始事件的 message 数组读取 reply 段 id
  -> get_msg(message_id=reply_id)
  -> 从返回消息的 forward 段读取 data.id
  -> get_forward_msg(id=forward_id)
  -> 递归遍历 messages[].message[]
       和嵌套 forward.data.content[].message[]
  -> 收集 video.data.url
  -> 按顺序逐条 Video.fromURL(url) 发送
```

兼容处理：

- 优先使用 AstrBot 适配器已经解析好的 Reply.chain，少调一次 get_msg；
- get_msg 返回的是完整响应体还是 data，都能解析；
- get_msg 失败时，直接把 reply id 当作 forward id 再试一次；
- get_msg 成功但没有 forward 段时，也会尝试一次 get_forward_msg；
- 嵌套转发优先使用内联的 data.content；只有 id 时才再调 API；
- 视频链接优先取 data.url，其次取 data.file / data.path（仅接受 http/https）；
- 支持字符串形式的 CQ 码视频段。

## 已知限制

- 只支持 OneBot v11（aiocqhttp）；其他平台不会触发。
- 按需求，必须“引用”合并转发。直接把合并转发发给机器人、再发指令不会触发。
- 视频使用 QQ 提供的临时 URL，链接过期、被风控或需要重新签名时会发送失败；插件不会下载转存。
- 群聊默认关闭，必须同时开启 group_enabled 并配置群白名单。
- 按需求不做去重，同一条视频在转发里出现多次就会发送多次。
- 发送 N 条视频大约需要 (N - 1) * interval_seconds 秒，期间该处理会持续占用。
- `/stop` 只能阻止后续视频，不能中断正在进行的单条视频上传。
- 汇总里的“共 N 条”是本次实际发现/处理的视频数；如果配置了 max_videos 上限，可能不是转发里的全部视频。

## 开发与测试

测试不需要安装 AstrBot：测试文件会注入最小化的 astrbot 模块桩，直接加载 main.py。

```bash
cd astrbot_plugin_forward_video
python -m unittest discover -s tests -v
```

当前测试覆盖：

- 真实引用事件（3.json）的 reply id 提取
- get_msg -> forward id 解析
- 内联嵌套转发 / 按 id 获取嵌套转发
- max_videos、max_depth、max_nodes 限制
- CQ 码视频段、缺少 URL 的视频段
- 私聊开关、群聊开关、群白名单（含空白名单）
- get_msg 失败兜底、引用普通消息时忽略
- Reply.chain 快路径
- 发送汇总、失败统计
- /stop 停止后续发送
