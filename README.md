# Steam MCP

一个给 AI agent 用的 Steam MCP：登录一次，之后档案、游戏库、愿望单、好友、库存、市场、商店、评测、购物车都能读写，
另外还有一组「只读分析」工具（游玩时长、折扣雷达、Deck 兼容、库对比、价格快照）和「跨平台比价」（Steam 市场 vs 网易 BUFF、IsThereAnyDeal 历史低价）。

- 当前版本：**v1.4.1，53 个工具 + 3 个 prompt + 2 个 resource**
- 平台：Windows / Linux（纯 Python，无 GUI 依赖）
- 定位：agent 优先——所有工具结构化返回、明确的 `error` 码 + `hint`、批量任务给进度而不是卡死
- **明确不做**：市场交易（上架/改价/撤单/接报价）、账号安全设置、本地客户端层

## 能拿它做什么

下面这些是实测跑通的完整流程，不是能力清单。

**买游戏不踩坑**

```
steam_deals                     扫愿望单，列出所有正在打折的
steam_price_history(appid)      查该游戏在 Steam 的历史最低价
```

两个一起用才能判断「现在是不是史低」。**只看 `discount_percent` 会踩坑**——-90% 的那款可能史低只有 -75%，折扣深不等于历史低。

注意 `low.shop` 字段：ITAD 记的是全渠道史低。`shop` 不是 `"Steam"` 说明这个价在 Epic/GOG 之类的地方，你的 Steam 钱包买不到。**只有 `shop == "Steam"` 才能说「这是 Steam 史低」**。

**从充值到入库**

```
steam_redeem_wallet_code(code)  兑换充值码（码有地区限制，印尼区码只能充印尼区号）
steam_checkout(preview)         先看余额够不够，钱不够就地停
steam_cart(add, appid)          加车
steam_checkout(init)            建交易，不扣款
steam_checkout(price)           拿到准确金额
steam_checkout(finalize, confirm=true, expected_total=<上一步的 total>)
```

`finalize` 的 `expected_total` 必须传 `price` 返回的那个数。它会二次核价，不传或传错会被拒，一分钱不扣但单子作废——**作废的 transid 会锁住购物车**，之后加车全部静默失败，必须先 `cancel` 掉。

到账验证看两处：`steam_library` 里有这个 appid + `preview` 的余额减少对应金额。`charged:true` 只是接口接受了。

**账号体检**

```
steam_net_check     5 个域名是否可达
steam_auth_status   登录态快照
steam_me            只有这个能证明会话真活着
```

前两个都是历史快照，`logged_in:true` 而会话已死是可能的。判断会话死活只看 `steam_me` 能不能返回档案。

**饰品行情**

```
steam_market_price(hash_name)            Steam 市场最低价
steam_buff_search(keyword)               拿 goods_id
steam_buff_price(goods_id)               BUFF 实时在售 + 求购
steam_market_compare(hash_name)          两边最低价对比
```

`hash_name` 要和市场完全一致（含磨损、StatTrak 前缀），差一个字就查不到。BUFF 的价格是买家实付，手续费卖家承担。

**看自己在玩什么**

```
steam_playtime_stats    总时长、Top 20、吃灰榜、半年没启动的
steam_achievement_hunter  「再拿 N 个就全成就」的游戏
```


## 安装

```bash
git clone <this-repo> && cd steam-mcp
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```

然后在 MCP 客户端里注册（路径换成本机绝对路径）：

```json
{
  "steam": {
    "type": "stdio",
    "command": "/path/to/steam-mcp/.venv/bin/python",
    "args": ["/path/to/steam-mcp/mcp_server.py"]
  }
}
```

运行时会用到 `data/` 目录（配置、加密凭证、价格缓存），**该目录不在版本库里**，
首次启动会自动创建；`data/credentials.bin` 与 `.vault.key` 是加密后的登录态，
换机无法迁移，必须重新登录。

## 第一次使用

1. 对话里先调 `steam_auth_status`，确认是未登录状态。
2. 登录，二选一：
   - **对话里**：`steam_auth_import_cookie(cookie="...")`
   - **终端里（更安全，cookie 不进对话记录）**：
     ```
     cd /path/to/steam-mcp
     .venv/bin/python login_helper.py
     ```
3. 再调 `steam_auth_status`，看到 `logged_in: true` 且 `verification.valid: true` 就成了。

### 怎么拿到 cookie

浏览器登录 https://store.steampowered.com → F12 → Application（应用）→ Cookies → `https://store.steampowered.com`
→ 复制 `steamLoginSecure` 的 value（形如 `7656119xxxxxxxxxx||eyJhbGci...`）。
顺手把 `steamRefresh_steam` 也复制下来，之后就能用 `steam_auth_refresh` 静默续期，不用反复粘。

## 三种登录方式

| 方式 | 工具 | 需要什么 | 说明 |
|---|---|---|---|
| 账号密码 | `steam_auth_login` → `steam_auth_guard` | `api.steampowered.com` 可达（302 已接管时可直连） | **已用真实账号验证通过**。密码只在内存做 RSA 加密，不落盘 |
| cookie 导入 | `steam_auth_import_cookie` | 浏览器已登录 | 不走 api 域名；适合已在浏览器登录的场景 |
| 静默续期 | `steam_auth_refresh` | refresh token | 续期实现已就位，长期行为待观察 |

### 密码登录的几个关键实现点（踩过的坑）

1. **不要带 `device_details`**：Steam 现行接口对这个参数一律回 400「参数不全」。
2. **`PollAuthSessionStatus` 和 `GenerateAccessTokenForApp` 都必须 POST**，GET 一律 405。
3. **先换 `access_token`，再做 `finalizelogin`**：finalize 会消费会话，反过来的话 token 只能拿到空。
4. **本地反代（302 / Watt Toolkit）会吞掉 `settoken` 下发的 `steamLoginSecure`**（同批响应里 `steamCountry`/`sessionid` 都在，唯独它缺席）。所以登录 cookie 用 `steamid||access_token` 自己拼，不依赖 Set-Cookie。

### 写操作的前提：`warm_session`

Steam 校验写操作（加愿望单、加购物车…）时要求 `sessionid` 是**它自己下发的**（cookie 值与表单值一致）。内置写工具和 `steam_request` 的 POST 会自动 warm（十分钟一次）；返回 `success:false` 时可以先手动跑 `steam_session_warm` 再重试。

## 网络这块（重点）

国内网络下三个域名的实际情况不一样，工具会自动降级，但你要知道边界：

- `store.steampowered.com`：**直连可用**。商店、详情、愿望单、搜索、评测、购物车都走它。
- `steamcommunity.com`：**会被 DNS 污染**（实测解析到过 `157.240.9.36`、`115.126.100.160` 这类无关 IP）。
  需要 Steamcommunity 302 / Watt Toolkit 之类的本地反代，把 hosts 指到本机。
- `api.steampowered.com`：**多数线路不通**。成就、最近游玩、新闻、按游玩时长排序的游戏库依赖它。

对应的配置（对话里用 `steam_config_set` 改，或直接编 `data/config.json`）：

```json
{
  "network": {
    "mode": "direct",        // direct=直连(配合 302 本地反代) / env=跟随系统代理 / custom=用 proxy
    "proxy": null,           // mode=custom 时填 http://127.0.0.1:7890
    "tls_trust": "system"    // system=信任 Windows 证书库，能接受本地反代的根证书
  }
}
```

- 开了 302 之后 **必须用 `mode=direct`**，否则请求会绕到上游代理，反代白做（会看到 502）。
- `tls_trust=system` 是为了兼容本地反代的自签证书；报证书错误时先查这个，不要直接上 `insecure`。
- 排查网络：`steam_net_check`，它会给每个域名标 `<dns ip / 状态 / 判定 / 建议>`。

## 工具清单

> 📖 每个工具的参数、返回与注意事项见 **[TOOLS.md](TOOLS.md)**（自动生成，覆盖全部 53 个工具，含错误码速查表）。
> 重新生成：`python3 scripts/gen_tools_doc.py > TOOLS.md`
>
> 另有 3 个 prompt（`wishlist_sale_check` / `purchase_advisor` / `account_health`）和 2 个 resource
> （`steam://docs/tools` / `steam://docs/roadmap`）——带 UI 的客户端会列出来，纯 agent 用不到。

**登录**：`steam_auth_status` `steam_auth_import_cookie` `steam_auth_login` `steam_auth_guard` `steam_auth_poll` `steam_auth_refresh` `steam_auth_logout` `steam_session_warm` `steam_webapi_key`

**诊断/配置**：`steam_net_check` `steam_config_get` `steam_config_set`

**账号与库**：`steam_me` `steam_user_profile` `steam_friends` `steam_library` `steam_recent_games` `steam_achievements`

**重组分析（只读）**：`steam_playtime_stats`（总时长/Top/吃灰榜/久未启动）`steam_deals`（愿望单折扣雷达，30 分钟缓存）`steam_app_extras` `steam_deck_compat`（Deck 兼容等级）`steam_library_diff`（两人库对比）`steam_price_watch`（价格快照 diff）`steam_achievement_hunter`（全库成就完美度，「再拿 N 个就全成就」）

**市场只读**：`steam_market_listings`（在售挂单，只读，不含买卖）`steam_market_history`（成交历史，分页）`steam_inventory_value`（库存估值，逐件查市场最低价）`steam_market_compare`（Steam 市场 vs 网易 BUFF 同款最低价）

**账号历史**：`steam_purchase_history`（许可/购买记录，分页）`steam_badges`（徽章与卡牌进度）

**价格历史（ITAD）**：`steam_price_history`（历史最低价/折扣/店铺/时间点）`steam_itad_key`（配置免费 API key）

**商店**：`steam_store_search` `steam_app_details` `steam_wishlist` `steam_wishlist_edit` `steam_cart` `steam_free_license` `steam_redeem_wallet_code`（兑换钱包充值码/礼品卡，`[花钱]`）

**库存市场**：`steam_inventory`（默认 753/6，CS2 用 730/2，Dota2 570/2，TF2 440/2）`steam_market_price` `steam_market_search`

**内容**：`steam_news` `steam_reviews`

**通用**：`steam_request(method, url, params, data, json_body, confirm, raw_html)`

**网易 BUFF**（buff.163.com）：`steam_buff_login` `steam_buff_status` `steam_buff_logout` `steam_buff_search` `steam_buff_price`

### BUFF 登录说明

- 走 BUFF 官方的「Steam 登录」通道（OpenID 2.0）：`/account/login/steam` → `steamcommunity.com/openid` → 回跳 BUFF 拿 session。**借用本机已登录的 Steam 会话，不需要网易账号密码**；前提是 Steam 会话有效（先 `steam_auth_status`），且 302 反代在跑（openid 要走 steamcommunity.com）。
- BUFF cookie 和 Steam 凭证同库加密（credentials.bin 的 `buff` 命名空间），只输出指纹。
- `steam_buff_search(keyword)` 拿 goods_id；`steam_buff_price(goods_id)` 返回在售列表（价格、float、图案模板）+ 求购列表。BUFF 价格为买家实付（手续费卖家承担）。
- 只读行情。购买/求购/上架等资金操作未实现，也不会通过通用请求放行（BUFF 不在 steam_request 的放行逻辑里）。
- 该 Steam 账号首次登 BUFF 若被要求绑手机号，需要浏览器里人工补一次，MCP 不代操作。

## 安全设计

- **凭证加密**：Windows DPAPI 绑定当前用户；换机器或换用户解不开，直接按未登录处理。日志只输出 `len=… sha256:…` 指纹。
- **域名白名单**：只允许 `store/api/login/help.steampowered.com`、`steamcommunity.com` 及其子域，外加 `buff.163.com`（BUFF 登录与行情），且必须 https。别的域名在发请求前就拒。
- **敏感操作拦截**：结账、改密码、改邮箱、账号安全、接受交易、市场买卖挂单，默认拦下，必须显式 `confirm=true`（或把 `behavior.allow_sensitive` 打开才放行）。
- **不写登录态**：只读 cookie，不代刷新、不回写浏览器、不碰客户端。
- **限速**：每域名有最小请求间隔（社区 **5.0s** / 商店 0.4s / api 0.3s），429/503 自动退避，降低被风控的概率。
  社区域 2s 实测仍会撞 429（`/dev/apikey`、`/market/history` 都踩过），所以给到 5s。

## 已知限制

这些是实测确认的边界，不是待办事项。

| 限制 | 表现 | 说明 |
|---|---|---|
| `steam_market_search` 不可用 | 返回 `error: market_search_retired` | Steam 已下线 `/market/search/render/`，所有参数组合都 404，无替代接口。按关键词搜饰品请用 `steam_buff_search` |
| `steam_wishlist_edit` 可能返回 `success:false` | `error: wishlist_noop` | 不是故障。三种情况会这样：已在愿望单 / 已拥有 / remove 时本来就不在。先查状态再判断 |
| `/dev/apikey` 会限流 | `error: rate_limited` | 不是"没注册 key"。等 10-30 分钟再试 |
| `steam_auth_logout` 会删掉 refresh token | 之后 `steam_auth_refresh` 返回 `no_refresh_token` | 无法静默恢复，只能重新登录或导 cookie |
| ITAD 的历史低价跨全部商店 | `low.shop` 可能是 Epic/GOG | Steam 钱包买不到那些价。只有 `shop == "Steam"` 才算 Steam 史低 |
| `steamcommunity.com` 国内需反代 | DNS 污染 | 见上面「网络这块」 |

## 出错了怎么查

1. **先看 `error` 码**，每个都有对应的 `hint` 说明下一步怎么做
2. **网络问题** → `steam_net_check`
3. **登录问题** → 先 `steam_auth_refresh`，还不行再考虑重登
4. **写操作 `success:false`** → 多半是 no-op（状态本来就对），先读一次现状
5. **限流 429** → 等几分钟，别重试

错误码速查表在 [TOOLS.md](TOOLS.md) 末尾。

## 排查

| 现象 | 原因 | 处理 |
|---|---|---|
| `not_logged_in` | 凭证库为空或解不开 | 重新导入 cookie |
| `network_unreachable` + community | DNS 污染 | 起 302 / 配代理，再 `steam_net_check` |
| `blocked` 且提到代理 | 本地反代生效了但 mode 还是 env | `steam_config_set(network_mode="direct")` |
| `api_unavailable` | api 域名不通 | 配代理，或 `steam_config_set(webapi_key="...")` |
| 证书错误 | 本地反代自签证书没被信任 | `tls_trust=system`；仍不行说明根证书没装进系统 |
| `confirmation_required` | 撞上敏感操作拦截 | 确认要执行就带 `confirm=true` |

## 已验证 / 已知限制（基于真实账号实测）

**已验证可用**：密码登录全流程（含 Guard）、档案、等级、游戏库（含时长）、最近游玩、商店搜索/详情/评测、愿望单读 + 增删（往返验证）、购物车查看/加入、好友（名字+在线状态）、库存、市场价格/搜索、新闻（RSS）、网络自检。

**已知限制**：

- **成就**：无 Web API Key 时只能读到已解锁列表（无名称/总数）。key 不用手动申请：调 `steam_webapi_key("fetch")` 会从 `/dev/apikey` 页面抓已注册的 key 存进凭证库（注册过的账号直接可用）；没注册过再调 `register`（会代表账号同意 Web API 使用条款）。key 同时解锁：完整新闻、他人档案摘要、vanity 解析。
- **愿望单读取**：官方的 `wishlistdata` JSON 接口对我们的会话 302，走 `dynamicstore` + 商店详情拼装（无前 60 条以外的详情，没有好评率字段）。
- **好友头像**：`ajaxresolveusers` 限流很凶，容易 429；名字/在线状态不受影响（直接从页面拿）。
- **购物车 view**：页面是 SSR，行项目解析是 best-effort。
- **社区接口整体限流敏感**：突发调用会触发 429，工具内置退避重试，但仍建议别高频连打。

## 自检脚本

```bash
cd C:\Users\Administrator\WorkBuddy\steam_mcp
.venv\Scripts\python.exe login_helper.py     # 登录 / 状态 / 网络自检
```

## 服务器部署

分发包（`dist/steam-mcp-*.zip`）里只有代码和文档，**不含本机凭证与配置**（`data/` 从不打包）。

1. 解压到服务器任意目录，装依赖（Python 3.10+）：
   ```bash
   python -m venv .venv && .venv/bin/pip install -r requirements.txt   # Windows 用 .venv\Scripts\pip
   ```
2. 配置 MCP 客户端（如 `~/.workbuddy/mcp.json` 的 `mcpServers`）：
   ```json
   {
     "steam": {
       "command": "/路径/到/.venv/bin/python",
       "args": ["/路径/到/mcp_server.py"],
       "env": {"STEAM_MCP_HOME": "/路径/到/data"}
     }
   }
   ```
   `STEAM_MCP_HOME` 指向服务器自己的数据目录（首次运行自动生成 `config.json`；凭证加密后存这里）。
3. 登录态**不能随包带走**：DPAPI 凭证绑定原机器的 Windows 用户，到服务器必须重新登录
   （Linux/macOS 服务器自动回落到 AES-GCM + 本地密钥，同样是本机不可移植）。
4. 服务器网络按 README「网络这块」调 `steam_config_set`；国内服务器要访问 steamcommunity.com 仍需自行解决污染问题。
