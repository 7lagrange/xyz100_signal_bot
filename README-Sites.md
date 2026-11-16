# 私人 ChatGPT Sites 看板

Windows 继续每分钟采集 XYZ100、计算 30m SMA14/99 并发送 Slack。网页可查看 30m（6 小时至 30 天）与只读 5m（最多 72 小时）图表；5m 数据从分钟行情派生并每五分钟同步，5m 不产生 Slack 信号。Sites 只保存并展示采集端同步的快照；网页访问不会额外请求交易所。

站点入口：<https://xyz100-sma1499-20261101.open-finch-8278.chatgpt.site>。需要登录获准访问的 ChatGPT 账户。是否已成功发布及同步，以当前 Sites 发布结果和页面中的最新分钟时间为准。

## 连接 Windows 采集端

1. 更新整个 `xyz100_signal_bot` 文件夹，包含新增的 `site_sync.py`；保留本机 SQLite。
2. 在 `%LOCALAPPDATA%\XYZ100SignalBot\config.json` 保留现有 Slack 配置，补上下面两个字段：

```json
{
  "site_url": "https://xyz100-sma1499-20261101.open-finch-8278.chatgpt.site",
  "site_service_token": "填入这个私人站点的机器访问凭据"
}
```

这两个字段应合并进原来的 JSON 对象，不是覆盖原有 Slack 字段。凭据保存在本机私有配置，不放进代码、ZIP、OneDrive 或网页。当前 Mac 的私有配置位于 `~/Library/Application Support/XYZ100SignalBot/config.json`；可从已配置好的文件中安全转移这两个字段到 Windows，保留 Windows 自己的 Slack token 和频道。

3. 重启原采集程序。若已有 `XYZ100-MA14-99` 计划任务，在更新代码后执行：

```powershell
schtasks /End /TN XYZ100-MA14-99
schtasks /Run /TN XYZ100-MA14-99
```

没有安装计划任务时，按 [原运行说明](README.md) 启动普通模式。看板连接不要求在 Windows 启动本地网页服务，也不需要浏览器一直开着。

日志中出现 `Site acknowledged minute ...` 表示站点已确认保存该分钟快照。页面会自动读取云端数据；采集电脑应保持开机联网，只选择一台电脑正式采集和同步。

## 同步边界

- 每个开市采集周期后提交最新快照，网络请求在独立后台线程执行；最多一个上传在途，不堆积旧快照。5m 历史序列只在每个五分钟周期更新时附带。按上海时间周六 05:00 至周一 06:00 计划暂停行情采集和快照上传。
- 同步失败独立退避，并遵守 Retry-After；不会拖慢行情轮询、改变策略状态或阻断 Slack。
- 网络恢复后上传最新完整快照；云端拒绝旧快照覆盖新数据。云端保存最近 72 小时的图表数据和最多 200 条实际信号，完整历史仍在采集电脑的 SQLite。
- 站点会显示数据过期，不能因为网页仍可打开就认为采集正常。历史不足的 SMA 保留空白，不把历史交叉补画成已经发过的通知。
- Slack token 不会上传。站点机器访问凭据单独用于通过私人站点访问边界及写入授权，网页不会获得该凭据。
- 云端快照按 ID 覆盖，重复上传不会新增信号或触发交易、Slack。
- 同步线程不阻止主进程退出；`--once` 并不保证异步上传已经完成，查看站点回读和日志确认。

当前 Mac 可用于验证同步；这不表示 Windows 那台电脑已经配置完毕。安装 Mac 自启后需要重装以复制最新模块；本方案仍以 Windows 为正式采集端。

## 本地验证

```text
python -m unittest -q test_bot.py test_macos.py test_dashboard.py test_site_sync.py
```

当前 50 项测试通过，其中包含同步超时、限流、后台线程、写入确认、目标地址及凭据隔离测试。Windows 实机接入应在那台电脑上确认。
