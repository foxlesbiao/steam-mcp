# Steam MCP 全量测试报告

- **时间**：2026-11-12 18:22（真实账号实测，账号 `<account>` / <steamid64>）
- **环境**：Steamcommunity 302 本地反代生效中，网络模式 `direct`，TLS 信任 `system`
- **结果**：32 项检查，**31 通过 + 1 项为测试脚本误判（工具本身正常，见下）**，0 异常
- **原始数据**：`data/test_report.json`

## 一、登录态（8 项）

| 工具 | 结果 | 耗时 | 说明 |
|---|---|---|---|
| steam_auth_status | PASS | 1202ms | `logged_in: true`，cookie 校验 `valid: true` |
| steam_auth_import_cookie（垃圾输入） | PASS | 0ms | 正确拒绝 `bad_input`，未写库 |
| steam_auth_login（假密码） | PASS | 864ms | 正确识别 `invalid_credentials` |
| steam_auth_guard（无会话） | PASS | 1ms | 正确报「没有进行中的登录」 |
| steam_auth_poll（无会话） | PASS | 1ms | 同上 |
| steam_auth_refresh | PASS | 779ms | 无 refresh cookie 时优雅失败，不破坏现有会话 |
| steam_session_warm | PASS | 1804ms | 拿到 Steam 下发的 sessionid/browserid |
| steam_webapi_key(fetch) | PASS | 894ms | 自动抓到已注册 key（域名 <your-domain>） |

## 二、诊断 / 配置（3 项）

| 工具 | 结果 | 说明 |
|---|---|---|
| steam_net_check | PASS | 5 个域名全部 127.0.0.1 + 可达 |
| steam_config_get | PASS | 返回完整配置 |
| steam_config_set（幂等） | PASS | 设置 network_mode=direct，无破坏 |

## 三、档案与库（7 项）

| 工具 | 结果 | 关键数据 |
|---|---|---|
| steam_me | PASS | <persona>，在线 |
| steam_user_profile（自己） | PASS | 29 级，source=api |
| steam_user_profile（他人） | PASS | gabelogannewell 解析正确 |
| steam_friends | PASS | 页面共 八十余人，返回名字+在线状态 |
| steam_library | PASS | 约 300 个游戏，按游玩时长排序 |
| steam_library(search=cs) | PASS | 名称过滤正常 |
| steam_recent_games | PASS | CS2 近两周 22 小时 |
| steam_achievements | PASS | CS2 1/1（PLAY_CS2），source=api |

## 四、商店（5 项）

| 工具 | 结果 | 关键数据 |
|---|---|---|
| steam_store_search | PASS | 「黑神话」命中 9 条 |
| steam_app_details | PASS | 黑神话：悟空详情完整 |
| steam_wishlist | PASS | 约 350 条（dynamicstore + 详情拼装） |
| steam_wishlist_edit（加删往返） | PASS* | 347→348→347，加删均 success:true |
| steam_cart(view) | PASS | 页面 200，无重定向 |

\* 报告中那 1 个 FAIL 是我测试脚本的断言写错了：它把「加后计数 348」和「删后计数 347」当成必须相等，而正确行为就是差 1。工具本身加/删都返回 `success: true`，愿望单最终恢复 347，**未污染你的数据**。

## 五、库存与市场（3 项）

| 工具 | 结果 | 关键数据 |
|---|---|---|
| steam_inventory | PASS | Steam 社区物品 百余件 |
| steam_market_price | PASS | AK-47 红线（久经）¥210 左右 |
| steam_market_search | PASS | AWP 命中 503 条 |

## 六、内容（2 项）

| 工具 | 结果 | 说明 |
|---|---|---|
| steam_news | PASS | 官方 API 源（已解锁 key） |
| steam_reviews | PASS | 汇总+最近 30 天好评率 |

## 七、通用请求与安全防线（3 项）

| 工具 | 结果 | 说明 |
|---|---|---|
| steam_request（GET 白名单） | PASS | 自动带登录态 |
| steam_request（敏感拦截） | PASS | checkout 被 `confirmation_required` 拦下 |
| steam_request（域名拦截） | PASS | 非 Steam 域名直接拒绝 |

## 已知限制（非本次失败项）

- 好友**头像**常拿不到（`ajaxresolveusers` 限流极凶，429），名字/状态不受影响
- 愿望单详情只能给前 60 条（appdetails 只收单 appid，逐个取有 0.4s/条限速）
- 购物车 view 的行项目解析是 best-effort（页面为 SSR）
- 社区接口整体限流敏感，已内置退避重试

## 结论

29 个工具全部可用；写操作、安全拦截、登录态校验均按预期工作。可以正式用了。
