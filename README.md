# MS Rewards自动化

微软 Microsoft Rewards 每日任务的本地自动化工具：纯 Python 实现，PyQt5 图形界面，无任何第三方运行时依赖（GUI 除外），支持一键登录、长期无感后台守护挂机。

> 本项目移植自 ScriptCat 油猴脚本 [微软Rewards积分自动脚本 #6241](https://scriptcat.org/zh-CN/script-show-page/6241)（作者 liyan20001124-byte，MIT 许可），在其逻辑基础上重写为本地桌面程序并扩展了大量调度与界面能力。

## 功能

- **8 个任务独立开关**：PC/App 签到、新闻阅读、活动卡片、Bing 搜索、Quiz/拼图、每日活动、待领积分检测、连签检测
- **一键登录**：拉起本地 Edge（CDP），自动采集 Cookie 与 OAuth Token，全程图形界面点按钮完成
- **纯 HTTP / DAPI 优先**：搜索、签到、阅读、活动卡片均通过接口完成，浏览器 DOM 只做极少数兜底
- **每日随机目标**：搜索/阅读不每天做满，按配置的百分比区间随机，行为更接近真人
- **守护模式**：等电脑空闲才动手、任务之间随机间隔几分钟到几十分钟、两轮之间再歇几十分钟，可整夜挂着
- **优先快线**：每轮开头先把「签到 + 1 次搜索 + 3 张每日卡」做掉，保住连签不断
- **搜索热词**：默认抓 B 站今日热搜作为搜索词，自动去重、当日不重复
- **通知**：支持 5 种渠道（Windows Toast、企业微信、钉钉、飞书、Telegram，按配置启用）
- **测试一轮**：12 个得分点逐个体检，全部以服务器返回的真实积分变动为准，不谎报成功
- **日志**：每天一个文件、默认保留 7 天自动清理、详细模式下记录每个请求与决策（敏感信息脱敏）

## 环境要求

- Windows 10/11
- Python 3.9+（源码运行）
- PyQt5（仅界面需要，CLI 完全零依赖）
- 系统自带 Microsoft Edge（一键登录与浏览器兜底用）

## 快速开始（源码）

```bash
cd python
python gui_qt.py          # 图形界面
python main.py --gui      # 同上
python main.py            # CLI，首次运行进入配置向导
python main.py --test     # 测试一轮（12 个得分点）
```

首次使用：打开界面 → 「账号与授权」→ 一键登录 → 在弹出的 Edge 里登录微软账号 → 「正式运行」或「开始守护」。

## 打包成 exe

```bash
cd python
python pack_release.py            # 单文件 exe + 使用说明 + 桌面快捷方式
python pack_release.py --zip      # 额外生成免个人数据的分发包
python pack_release.py --onedir   # 文件夹版（启动更快，约 1s vs 4s）
```

> conda 环境打包需注意：conda 的 C 运行库在 `Library\bin` 下，PyInstaller 默认收集不到（缺 `ffi-8.dll` 会导致 `ctypes` 崩溃）。`pack_release.py` 已内置这份 DLL 清单。

## 目录结构

```
ms-rewards/
├── original.user.js      # 移植来源：原 ScriptCat 脚本（MIT）
└── python/
    ├── main.py           # CLI 入口 / 配置向导
    ├── gui_qt.py         # PyQt5 主界面（5 页导航 + 托盘）
    ├── rewards.py        # OAuth + 全部 HTTP/DAPI 接口 + 日志
    ├── tasks.py          # 任务注册表 + 总调度（空闲闸门 / 优先快线）
    ├── http_client.py    # 标准库 urllib 封装，请求级 trace
    ├── browser_login.py  # 纯标准库手写 CDP 一键登录
    ├── browser_dom.py    # 浏览器 DOM 兜底任务
    ├── idle_guard.py     # 空闲检测（纯 ctypes）
    ├── netguard.py       # 网络就绪闸门
    ├── notify.py         # 通知渠道
    ├── endpoint_test.py  # 「测试一轮」
    ├── pack_release.py   # 一键打包
    └── make_shortcut.py  # 纯标准库写 .lnk 快捷方式
```

## 已知限制

- dashboard「待领积分」按钮等个别交互仍需浏览器 DOM 兜底（复用登录 profile，后台窗口）
- 单文件 exe 启动约 4 秒（解包成本），杀毒软件可能误报，需加白名单
- 微软接口随时可能变更，后续如果微软有更新有空会做适配

## 免责声明

1. **本项目仅供学习交流与个人自动化技术研究使用，禁止用于任何商业用途。**
2. 本项目的运行方式（自动搜索、自动签到等）**可能违反 Microsoft Rewards 及微软相关服务的使用条款**，由此可能导致积分清零、奖励资格取消乃至**微软账号受限或封禁**。是否使用由你自行决定，**一切风险与后果由使用者本人承担**，作者不对任何账号损失负责。
3. 本项目与 Microsoft 公司及其产品无任何关联，亦非官方工具。
4. 请妥善保管程序目录下的 `auth.json`、`cookies.txt` 等凭据文件，**切勿分享给他人**；分发包在打包时已自动剔除这些文件。
5. 本软件按「现状」提供，不提供任何明示或默示的担保；使用前请阅读 [LICENSE](LICENSE)。

## 许可证

[MIT](LICENSE)。原脚本版权归 [liyan20001124-byte](https://scriptcat.org/zh-CN/script-show-page/6241) 所有，本项目在其 MIT 授权下移植并扩展，延续 MIT 许可发布。
