# NAS 部署说明

目标：把闲鱼/淘宝 Playwright 主脚本部署到飞牛/FNOS NAS，默认只跑 `xianyu` 和 `taobao`，不跑 JD。

## 一条命令部署、测试、安装夜间任务

在本机 PowerShell 执行，密码只放在当前会话环境变量里，不写入脚本：

```powershell
$env:NAS_PASSWORD = "这里填 NAS SSH 密码"
python scripts\deploy_nas.py `
  --host 192.168.31.217 `
  --user root `
  --remote-dir /root/autoPCBulid `
  --include-state `
  --install `
  --run-sample `
  --sample-keywords "cpu" `
  --sample-pages 1 `
  --schedule `
  --schedule-time 03:10 `
  --schedule-platforms "xianyu" `
  --schedule-keywords "cpu" `
  --schedule-pages 1
Remove-Item Env:\NAS_PASSWORD
```

如果不想使用环境变量，直接运行脚本也可以，它会交互提示输入 SSH 密码。

## NAS 上手动运行

```bash
cd /root/autoPCBulid
PLATFORMS="xianyu" KEYWORDS="i5-12400F,R5 7500F" PAGES=1 scripts/nas_run_crawl.sh
```

## 日志位置

- 简要日志：`logs/crawl_pw_brief_YYYY-MM-DD.log`
- 详细日志：`logs/crawl_pw_YYYY-MM-DD.log`
- 错误日志：`logs/error.log`
- 定时任务外层日志：`logs/nas_cron.log`
- 诊断截图/HTML：`logs/debug/`

## 账号保护策略

- 闲鱼/淘宝必须有 `state/{platform}_state.json`；缺失或过期会直接停止该平台。
- 命中登录跳转、滑块、风控、惩罚页后，会停止该平台剩余关键词，避免继续请求。
- NAS 默认无头运行，滑块无法人工处理；需要先在本机刷新登录态，再重新部署 `state`。
- 当前 NAS 已验证闲鱼稳定产出；淘宝在 NAS 无头环境会触发跳转/风控，默认不写入夜间任务。

## 当前远程权限要求

部署脚本需要 SSH/SFTP 权限。FNOS Web、SMB、FTP 只能证明 NAS 在线；没有可执行权限时，无法安装依赖、运行 Playwright 或写入 cron。
