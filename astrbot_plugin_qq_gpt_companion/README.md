# AstrBot 插件

插件负责接收白名单 QQ 群文本消息，发送到本地服务；用户在网页控制台粘贴回复后，
插件只有在独立出站开关明确开启后才会发回群聊。不自动调用模型、不读取 ChatGPT Cookie。

## 当前部署

- NapCat 4.18.38 已通过 OneBot v11 反向 WebSocket 与 AstrBot 4.29.1 连接。
- 连接端点仅限本机：ws://127.0.0.1:6199/ws。
- 已配置的群白名单保存在 Git 忽略的 AstrBot 运行配置中，不把群号写进版本化文档。
- 插件 allow_message_intake=false、allow_outbound_sends=false；本地服务 enabled=false。三项均默认关闭，禁用时不会保存群消息或发送回复。
- 没有发送测试消息；真实群消息事件和端到端回复尚未验证。

## 安装与配置

1. 将本目录复制到 AstrBot/data/plugins/。
2. 在插件配置中填写 local_bridge_url、local_token、allowed_group_ids；先保持 allow_message_intake 与 allow_outbound_sends 为 false。
3. 保持 allow_outbound_sends=false，直到群管理员/成员知情且用户明确批准启用。
4. 在本地工程执行 python -m companion.cli allow-group 群号；只有经过明确确认后才启用消息摄取。
5. 避免同时运行 AstrBot 内置回复和其他群聊增强插件，也不要让 Tulpa 同时成为该账号的另一个发送端。

插件使用 AstrBot 的 event_message_type 与 context.send_message 接口。
结果不确定时会标记 uncertain，不盲目重新发送。不要把服务开放到公网。
