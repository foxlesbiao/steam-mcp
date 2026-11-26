# Steam MCP 功能扩展路线图

> 版本基线：1.2.5（37 个工具）
> 本文档记录从 1.2.5 起评估过的扩展方向、实测结论、实现依据与落地顺序。
> 结论按「价值 ÷ 成本 ÷ 风险」排序，不是愿望清单。

## 0. 现状与三个空白

现有覆盖：登录与续期、档案、好友、游戏库、成就、商店搜索与详情、愿望单读写、免费许可证领取、
购物车、结账、库存、市场查价、新闻、评测、通用请求、网易 BUFF 行情。

明显缺口：

| 编号 | 空白 | 影响 |
|---|---|---|
| G1 | **没有本地客户端层** | 全链路只走 HTTP，碰不到 `steam.exe`、`steamapps/manifest`、截图目录；"装了没玩""占多少盘""该卸哪个"这类问题回答不了 |
| G2 | **市场只有"查价"，没有"我的挂单 / 成交历史 / 库存估值"** | 交易闭环缺一半，只能看别人的价格 |
| G3 | **只做"原始拉取"，没做"重组分析"** | 时长统计、库对比、折扣雷达都是现有接口的二次加工，几乎零成本，但价值最高 |

---

## 1. 优先推荐（第一批）

| 优先级 | 工具 | 能力 | 数据来源 | 难点 |
|---|---|---|---|---|
| P0 ✅ | `steam_playtime_stats` | 总时长 / Top N / 近两周 / **吃灰榜** / 久未启动 | 现有 owned games | 无，纯重组 |
| P0 ✅ | `steam_deals` | 愿望单折扣 + 0 元限免 + 阈值过滤 | 逐个 `appdetails` + 30 分钟缓存（见 7.1） | appdetails 不支持批量 |
| P0 | `steam_local_library` | 是否安装 / 磁盘占用 / 上次更新 / 下载中 | `libraryfolders.vdf` + `appmanifest_*.acf` | 需定位 Steam 路径 |
| P1 | `steam_market_listings` | 我的在售挂单 + 成交历史 | `/market/mylistings/`、`/market/history` | 页面结构解析 |
| P1 ✅ | `steam_app_extras` | Deck 兼容等级 + 封面/平台/评分（含单条 `steam_deck_compat`） | `ajaxgetdeckappcompatibilityreport`（**已实测**：730 → `resolved_category=2`）+ appdetails | 无 |

`steam_deals` × `steam_local_library` 组合起来是一条完整的 Agent 工作流：
「哪些在打折 + 我愿望单里哪些 + 我库里哪些装了没玩」，可挂每日定时任务。

---

## 2. 本地客户端层（G1）— 决定不做

> **2026-11-26 决策：整层不做。** 用户明确不需要（"那不需要这种功能"）。
> 现实约束：这份 MCP 跑在 Linux 主机（无 Steam），Steam 客户端在另一台 Windows 上；
> 要落地必须跨机部署或加远程执行层，维护成本 > 收益。此节保留作为决策记录，不再实现。

这层不碰网络、不碰凭证，只用本地文件与进程，风险最低。

| 工具 | 返回什么 | 实现依据 | 门槛 |
|---|---|---|---|
| `steam_client_status` | 是否运行、安装路径、客户端版本、当前登录账号 | `tasklist` 查 `steam.exe`；版本取文件版本；账号读 `config/loginusers.vdf` | 只读 |
| `steam_launch` | 执行 `steam://run/<id>`、`install`、`uninstall`、`store/<id>`、`open/friends`、`connect/<ip:port>` | `os.startfile("steam://...")` | `run`/`install`/`uninstall` 需 `confirm=true`；打开页面不需要 |
| `steam_local_library` | 每个 appid 的安装状态、磁盘占用、上次更新时间、是否下载中 | `steamapps/libraryfolders.vdf` → 各库路径；`appmanifest_<appid>.acf` | 只读 |
| `steam_local_profile` | 分类/收藏夹、每个游戏的启动参数、本机登录过的账号 | `userdata/<accountid>/config/localconfig.vdf` | 只读 |
| `steam_screenshots` | 截图列表（游戏、时间、路径、说明） | `userdata/<id>/760/remote/<appid>/screenshots/` + `screenshots.vdf` | 只读 |
| `steam_disk_advisor` | 卸载候选：装了没玩 / 占盘最大 / 半年没启动 | 交叉 `steam_local_library` × API 游玩时长 | 只读 |

### 2.1 路径解析（本机实测）

- 本机 `C:\Program Files (x86)\Steam`、`C:\Program Files\Steam`、`D:\Steam` 等常见路径**都不存在**，
  说明这台机器没装（或装在非标准盘）。
- 注册表查询被安全策略拦截：`reg.exe` 在程序黑名单里，`reg query HKCU\Software\Valve\Steam` 被直接阻止。
  因此实现**不得依赖外部命令**，应在 Python 内用 `winreg` 读
  `HKCU\Software\Valve\Steam\SteamPath`，并允许 `config.client.steam_path` 手工覆盖。
- 路径解析失败必须明确返回 `steam_not_installed`，**不猜目录、不扫全盘**。

### 2.2 appmanifest 解析

`appmanifest_*.acf` 是 VDF 文本，正则即可，无需新增依赖：

```python
ACF_KEYS = (
    "appid", "name", "StateFlags", "SizeOnDisk",
    "LastUpdated", "BytesToDownload", "BytesDownloaded",
)

def parse_acf(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    return {
        k: (m.group(1) if (m := re.search(rf'"{k}"\s+"([^"]*)"', text)) else "")
        for k in ACF_KEYS
    }

# StateFlags 按位判断：4 = StateFullyInstalled
is_installed = lambda d: bool(int(d.get("StateFlags") or 0) & 4)
```

`SizeOnDisk` 单位是字节，`LastUpdated` 是 unix 时间戳 —— 直接支撑"占盘排行"和"多久没更新"。

### 2.3 设计约束

1. 所有本地读取工具默认只读，不写 `localconfig.vdf`（写坏了 Steam 会重置用户设置）。
2. `steam_launch` 只允许白名单协议动作，不接受任意 `steam://` 字符串拼接。
3. 启动游戏属于副作用操作，返回里必须带 `launched: true` 和实际执行的 URI，便于审计。

---

## 3. 数据重组层（G3）

| 工具 | 输出 | 数据来源 |
|---|---|---|
| `steam_playtime_stats` | 总时长、Top 20、近两周、**吃灰榜**（有库无时长）、按发行商/类型聚合 | 现有 `owned games` |
| `steam_library_diff` | 两人共同游戏、各自独有、共同游戏总时长 | 两次 `owned games` |
| `steam_deals` | 愿望单折扣（按折扣率/绝对降价排序）、全站特惠、0 元限免、阈值过滤 | `search/results/?specials=1`（已实测）+ 现有愿望单 |
| `steam_app_extras` | Deck 等级（Verified/Playable/Unsupported + 未通过项）、截图/预告片/封面/hero 资源、云存档/远程同乐/手柄支持 | `ajaxgetdeckappcompatibilityreport`（已实测）+ `appdetails` |
| `steam_price_watch` | 关注列表价格快照 + 与上次快照的 diff | 本地 `data/price_snapshots.json`，幂等 |
| `steam_achievement_hunter` | 全库完美度排行、"再拿 N 个就全成就" | 逐 app 查，需限速队列 |

### 3.1 折扣雷达的实测坑

`specials=1` 返回 `total_count=14958`，`results_html` 里能直接抓到标题和折扣；
但 `maxprice=free` 返回的是 F2P 常驻免费（CS2、Dota2、Apex），**不是限免**。

限免判定只能靠：

- 折扣 `-100%` 且原价 > 0；或
- `steam_free_license(action="preview")` 里的 `is_free_license=true && price_in_cents_with_discount==0`。

与 README「免费游戏 / 免费许可证」一节的结论一致，不要重复踩。

### 3.2 Deck 兼容接口

```
GET https://store.steampowered.com/saleaction/ajaxgetdeckappcompatibilityreport?nAppID=<appid>&l=schinese
→ {"success":1,"results":{"appid":730,"resolved_category":2,"resolved_items":[...]}}
```

`resolved_category`：3 = Verified，2 = Playable，1 = Unsupported。
匿名可访问，不需要登录，实现成本接近零。

---

## 4. 社区与市场层（G2）

| 工具 | 端点 | 风险 |
|---|---|---|
| `steam_market_listings` | `GET /market/mylistings/?norender=1` | 只读 |
| `steam_market_history` | `GET /market/history`（HTML 解析 `g_rgHistory`） | 只读 |
| `steam_inventory_value` | 现有 inventory + 批量 market price | 只读，但受市场限速约束，必须 `top_n` 限制 |
| `steam_badges` | `GET /my/badges?l=schinese` | 只读 |
| `steam_notifications` | `GET /actions/ajaxgetusernotifications` | 只读 |
| `steam_tradeoffers` | `GET /tradeoffers/`、`/tradeoffer/<id>/` | 列表只读；accept/decline 需 confirm + allow_sensitive |
| `steam_purchase_history` | `GET /account/licenses/` | 只读 |
| `steam_market_sell` | `POST /market/sellitem/` | 写入，见 4.1 |

> 这些端点基于 Steam 网页既有接口，**尚未逐个实测**（当前会话未登录探测）。
> 实现第一步应加一个只读 `steam_probe(url)` 开发工具，先把页面结构确认下来再写解析。

### 4.1 市场"上架"的特殊设计

`sellitem` 会返回 `needs_mobile_confirmation: true` 和确认 id，真正的确认在手机 App 里，
或者需要 `identity_secret` 才能程序化完成。正确做法不是不实现，而是把状态如实抛出来：

```python
{
    "ok": True,
    "listing_created": True,
    "needs_mobile_confirmation": True,
    "confirmation_id": "...",
    "hint": "在 Steam 手机 App → 确认 → 市场 里点确认；"
            "或用 steam_2fa_confirm(id) 自动确认（需已导入 identity_secret）",
}
```

流程是完整的：MCP 建单 → 用户掏手机点一下。既不代确认，也不假装失败。

---

## 5. 明确不做的方向

| 功能 | 工程理由 |
|---|---|
| 云存档同步 | 没有 HTTP API，只有客户端本地文件 + 远端不透明存储，跨设备不可靠，做出来也是假同步 |
| 改密码 / 换邮箱 / 手机令牌绑定 / 家庭监护 | 动的是账号安全根，失败代价不可逆，收益极低 |
| 自动确认市场 / 报价 | 需要 `shared_secret` + `identity_secret`，等于把 2FA 私钥交给 MCP。要做必须是独立 opt-in 模块，DPAPI 存、永不进日志、默认关闭 |
| CD-Key 激活 | 可做，但要 confirm + 敏感开关，且报错信息里必须对 key 脱敏（只留前 4 位） |
| 客户端 UI 自动化点按钮 | 脆弱且不可幂等，除非要改"设置"这类确实没有 API 的东西 |

---

## 6. 架构层改进（比加功能更重要）

1. **读缓存**：`appdetails` 批量结果按 appid 缓存 10 分钟到 `data/cache/`。
   现在市场估值、成就扫描这类批量任务会直接撞上 0.4s/次的限速，缓存能砍掉一半请求。
2. **批量任务串行队列 + 进度**：community 域 2s/次是硬限制。
   批量工具应返回 `{"done": 12, "total": 40, "partial": [...]}`，而不是卡死等全量。
3. **工具分级标注**：在 description 开头统一加 `[只读]` / `[写入]` / `[花钱]`，
   agent 选工具时不会再误触。
4. **`evidence` 字段**：每个返回值带 `source_url`、`status`、`fetched_at`，
   排查"接口回 200 但数据是空的"这类问题时不用重跑。

---

## 7. 落地顺序

| 批次 | 内容 | 预估 |
|---|---|---|
| 第一批 ✅ | `steam_playtime_stats`、`steam_deals`、`steam_app_extras`（+ `steam_deck_compat`） | **已实现**，见 7.1 |
| 第二批 ✗ | `steam_client_status` + `steam_launch` + `steam_local_library` + `steam_disk_advisor` | **决定不做**（见 §2） |
| 第三批 | `steam_market_listings` / `market_history` / `badges` / `notifications` | `market_listings` 已做（7.2）；其余按需 |
| 第四批 | `steam_market_sell`、`steam_tradeoffers` 写操作、2FA 自动确认 | 按需 |

### 7.1 第一批实现记录（v1.2.6）

新增 `steam_mcp/insights.py`，四个只读工具：

| 工具 | 说明 |
|---|---|
| `steam_playtime_stats` | 直接重组 `IPlayerService/GetOwnedGames`，零额外请求。返回 summary / top / recent_2weeks / idle / stale |
| `steam_deals` | 愿望单逐个查价，结果落 `data/cache/deals.json`（TTL 30 分钟）。`max_fetch` 控制单轮抓取量（默认 120 ≈ 50s），`has_more` 提示继续调用 |
| `steam_app_extras` | 批量 Deck 兼容 + 商店资源，复用 `app_details` |
| `steam_deck_compat` | 单条 Deck 等级，匿名可取 |

实测结论（修正了原文两处判断）：

1. **`specials=1` 不能用来做愿望单折扣雷达**。`total_count=22048`（比原记录的 14958 更多），
   分页 100/页要 220 次请求，且默认排序不保证覆盖愿望单里的长尾。只能逐个 `appdetails`。
2. **`appdetails` 不支持批量**：`appids=730,570,1174180` 直接 400；`filters=price_overview` 现在返回
   `data:[]`（该参数已失效），必须取完整详情再摘 `price_overview`。
3. `wishlistdata/?p=0` 在无会话时 302，仍要回落 dynamicstore 拿 appid 列表。
4. `resolved_items` 里的 `display_type` 语义未完全确认（Verified 游戏也可能带若干项），
   因此原样返回为 `flags: [{type, token}]`，不替调用方判定“失败项”。

### 7.2 第二批补充（v1.2.7，面向 agent 的只读扩充）

| 工具 | 说明 |
|---|---|
| `steam_market_listings` | 在售挂单。`/market/mylistings/?norender=1` 直接是 JSON（`listings/assets/buy_orders/listings_to_confirm`），无需解析 HTML。价格同时给「元」和「原始分」两个字段——`price`/`fee` 的确切语义（卖家实收 vs 买家支付）随版本有出入，不替调用方下结论 |
| `steam_library_diff` | 两人库对比，两次 `GetOwnedGames` 求交/差 |
| `steam_price_watch` | 复用 `deals.json` 缓存做快照 diff，零额外请求；`commit=true` 才写基线 |

范围声明：**市场交易（上架/改价/撤单/接报价）明确不做**，`market_listings` 只是只读视图。

### 7.3 第三批（v1.2.8）：账号历史 + 跨市场比价

| 工具 | 说明 |
|---|---|
| `steam_purchase_history` | 解析 `/account/licenses/` 的 `account_table`。98 行实测可用；页面不给金额，只给日期/名称/来源 |
| `steam_market_compare` | 同一 `market_hash_name` 在 Steam 市场与网易 BUFF 的最低价对比。BUFF 按名字精确匹配，取不到就返回候选而不是瞎猜 |

### 7.4 SteamDB 不可用（实测）

`steamdb.info` 对机器请求一律 **403**（Cloudflare，浏览器 UA 也无效），其 API 同样 403；官方 API 需要付费 key，且站点条款禁止抓取。**不加入白名单**。

历史低价的可行替代，按落地难度排：

1. **自建快照**（已实现 `steam_price_watch`）——零依赖、零成本，缺点是要积累时间。
2. **IsThereAnyDeal**（`api.isthereanydeal.com`）——官方免费 key，直接给历史最低价与历史低价日期；**已接入 v1.2.9**（见 7.6）。
3. 商业聚合 API（付费）。

### 7.6 ITAD 接入（v1.2.9）

| 工具 | 说明 |
|---|---|
| `steam_itad_key` | 保存/清除免费 API key（加密进凭证库，只回指纹） |
| `steam_price_history` | `appid` → `/games/lookup/v1` 拿 ITAD UUID → `POST /games/historylow/v1` 拿历史最低价 |

实现要点：

- `api.isthereanydeal.com` 已加入 `http.py` 白名单（第三方域，注释写明理由）。
- **未配置 key 时不发请求**，直接返回 `itad_key_missing` + 注册链接，不做静默降级。
- `historylow` 是 `POST`，body 直接是 `["<uuid>"]` 数组（不是包一层对象）；`country` 走 query。
- ITAD 限速 1000 次/5 分钟（需邮箱已验证），比 Steam 宽松，不需要额外节流。
- 返回里带 `itad_url`，满足 ITAD ToS 的署名/链接要求。
- **key 已配好**（2026-11-26，应用 `2test`，1000 次/5 分钟）。

实测（country=ID，买家在印尼区）：

| 游戏 | ITAD 历史最低 | 折扣 | 店铺 / 日期 | 当日实付 | 结论 |
|---|---|---|---|---|---|
| FF7 REMAKE INTERGRADE | 104,220 IDR | -82% | Epic / 2026-11-03 | 104,220 | **正好史低** |
| 底特律 化身为人 | 36,999 IDR | -90% | Epic / 2026-07-16 | 39,900 | 贵 7.8% |
| 火线迈阿密2 | 19,580 IDR | -85% | GOG / 2026-09-24 | 19,649 | 贵 0.35% |

> 读法注意：ITAD 的「历史最低」跨全部商店，Steam 商店区价格往往不是最低那家。想等史低就要接受在 Epic/GOG 下单，只玩 Steam 库则看 Steam 自己的底价。

### 7.7 钱包充值码兑换（v1.3.1）

`steam_redeem_wallet_code(code, confirm, captcha_gid, captcha_text)` —— 兑换 Steam 钱包充值码 / 礼品卡。

- 端点：`POST /account/ajaxredeemwalletcode`（从 `redeemwalletcode.js` 里挖出来的；同族还有 `ajaxcreatewalletandcheckfunds`、`ajaxrefreshwalletcaptcha`）
- 返回：`{"success":1}` 成功；`{"success":2,"detail":N}` 失败。实测无效码为 `detail=14`，Steam 不细分「打错 / 已用 / 地区不符」，所以 hint 里三种都列出来
- **三道门槛**：`confirm=true` + `behavior.allow_sensitive=true` + 码长 ≥8。前两道任一不满足都不会发请求
- **码脱敏**：返回值与 hint 里只给 `ABCD****`，完整码只存在请求体里
- **验证码**：页面有 `input_captcha_gid` / `input_captcha` 字段。若服务端要求验证码，返回 `captcha_required` + `captcha_gid` + 页面地址，让调用方补 `captcha_text` 重试——不假装成功
- **地区限制**：钱包码必须与账号地区一致（印尼区码充印尼区账号），这是 Steam 侧规则，工具只负责如实报错

测试 `tests/test_redeem_offline.py` 覆盖：短码拒绝 / 未 confirm 不发请求 / allow_sensitive 关闭不发请求 / 成功 / detail 透出 / 验证码透出 / 返回值不含完整码。

### 7.5 通知暂缓

`/actions/ajaxgetusernotifications` 返回 44KB、`/my/notifications/` 返回 125KB，都是 React SSR 片段，没有稳定的 `g_rgNotifications` 之类结构，解析成本高且极可能随改版失效。当前价值不足以支撑这份维护成本，暂不做；若要能拿到，优先考虑用 `steam_request` 临时读取 + 人工确认结构，再决定是否固化。

## 8. 验收标准

每个新工具落地时必须同时满足：

1. **失败可读**：路径不存在 / 未登录 / 限速 / 接口改版，各有独立 `error` 码和 `hint`，不抛裸异常。
2. **只读默认**：写入类必须 `confirm=true`；涉及资金与安全设置的还要 `behavior.allow_sensitive=true`。
3. **不泄密**：返回值与日志里不出现 cookie、token、key 明文，只给 `len=… sha256:…` 指纹。
4. **可离线测**：像 `tests/test_checkout_offline.py` 一样，纯解析逻辑用 mock 数据覆盖，不联网即可跑。
5. **文档同步**：README「工具清单」补一行，本文档对应条目改成「已实现 vX.Y.Z」。
