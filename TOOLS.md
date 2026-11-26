# 工具总览

> 自动生成，勿手改。重新生成：`python3 scripts/gen_tools_doc.py > TOOLS.md`
> 当前版本 **v1.3.2**，共 **49** 个工具。
>
> 参数列里带 `?` 的是可选参数。所有工具都返回 JSON 对象，失败时带 `error` 码与 `hint`，
> 不抛裸异常。写操作与花钱操作在说明开头标了 `[写入]` / `[花钱]`。

## 约定

| 约定 | 说明 |
|---|---|
| 返回值 | 始终是 dict；成功 `ok: true`，失败 `ok: false` + `error` + `hint` |
| 凭证 | cookie / token / 兑换码只出现在请求体里，返回值只给 `len=… sha256:…` 或 `ABCD****` 指纹 |
| 只读 | 标注 `[只读]` 的工具不会改动任何状态 |
| 写入门槛 | 标注 `[写入]` 需要 `confirm=true`；标注 `[花钱]` 还需要 `behavior.allow_sensitive=true` |
| 限速 | store 0.4s/次、community 2s/次、api 0.3s/次，内置退避重试 |
| 批量 | 长任务返回 `{scanned, remaining, has_more}` 之类进度字段，不阻塞到超时 |


## 登录与续期

账号接入、Steam Guard、会话保活

| 工具 | 参数 | 说明 |
|---|---|---|
| `steam_auth_status` | — | 查看 Steam 登录状态：是否已登录、SteamID、cookie 是否仍有效、能否静默续期。任何 Steam 操作前先调它。 |
| `steam_auth_import_cookie` | `cookie`，`verify?`=True | 导入浏览器 cookie 完成登录（推荐方式，不需要密码，国内网络可用）。从浏览器 F12 → Application → Cookies → store.steampowered.com 复制 steamLoginSecure 整行粘贴进来；若同时提供 steamRefresh_steam 则可后续自动续期。支持 Cookie 头 / DevTools 表格 / 纯值三种格式。 |
| `steam_auth_login` | `username`，`password` | 用账号密码登录 Steam（走官方 IAuthenticationService）。密码只在内存里 RSA 加密后发出，不落盘、不写日志。需要 api.steampowered.com 可达。返回 stage：awaiting_confirmation=可先在手机 App 确认（用 steam_auth_poll 等待，无需输码）；awaiting_guard=需提交验证码；awaiting_poll=直接轮询等待确认。返回里带 allowed_confirmations / confirmation_types / code_types / guard_labels 说明各数字含义。 |
| `steam_auth_guard` | `code`，`code_type?`=3 | 提交 Steam Guard 验证码。code_type 用 Steam 官方 EAuthSessionGuardType：2=邮箱验证码，3=手机令牌动态码（TOTP），4=手机 App 确认，5=邮箱确认。优先用 login 返回的 suggested_code_type。验证码留空不会报错，只返回跳过提示。 |
| `steam_auth_poll` | `timeout?`=90 | 轮询登录会话，等待手机 App / 邮箱确认或验证码生效。timeout 为等待秒数（默认 90，最少 5）。超时返回 stage=pending，会话保留，可继续轮询或改用 steam_auth_guard 输验证码。密码登录返回 awaiting_confirmation 时先调这个，不要一上来就输码。 |
| `steam_auth_refresh` | — | 手动续期登录态：用 refresh token 换新的 access token，并重新走 finalizelogin 把store/community/help/checkout/steam.tv 各域名的完整 cookie 收齐写回凭证库。平时不需要手动调（每次调用需要登录的工具时会自动续期），仅在自动续期被关掉或想立刻刷新时用。 |
| `steam_auth_logout` | — | 清除本机保存的 Steam 凭证（退出登录）。 |
| `steam_session_warm` | — | 预热网页会话（从 store 拿 Steam 下发的 sessionid/browserid）。写操作会自动调用，一般不需要手动用；写操作返回 success:false 时可先跑一次再重试。 |
| `steam_webapi_key` | `action?`=fetch，`domain?`=localhost | 获取 Steam Web API Key。action=fetch 从 /dev/apikey 抓已注册的 key 存入凭证库（有 key 就用这个，成就/新闻/档案即刻解锁）；action=register 注册新 key（会代表账号同意 Steam Web API 使用条款，domain 随意填）。 |

## 诊断与配置

网络、代理、行为开关

| 工具 | 参数 | 说明 |
|---|---|---|
| `steam_net_check` | — | 逐域名探测 Steam 各站点可达性，给出 DNS 污染 / 本地反代 / 代理配置的判断和建议。网络异常时先跑这个。 |
| `steam_config_get` | — | 读取当前配置（网络模式、代理、TLS 信任、语言、国家、敏感操作开关）。 |
| `steam_config_set` | `network_mode?`，`proxy?`，`tls_trust?`，`language?`，`country?`，`allow_sensitive?`，`webapi_key?`，`max_items?` | 修改配置。常用：network_mode=direct\|env\|custom、proxy=http://127.0.0.1:7890、tls_trust=system\|insecure、language、country、allow_sensitive、webapi_key。传 webapi_key 会加密存进凭证库，不写配置文件。留空表示不改该项。 |

## 档案与社交

玩家信息、好友、成就

| 工具 | 参数 | 说明 |
|---|---|---|
| `steam_me` | — | 查看自己的账号概况：昵称、状态、头像、SteamID、等级、是否在游戏中。需已登录。 |
| `steam_user_profile` | `user?` | 查看任意用户档案。user 可传 SteamID64、个人主页链接、自定义 URL 名，或留空表示自己。 |
| `steam_friends` | `user?`，`limit?`=30 | 好友列表。需登录；对方好友列表需公开，否则返回空并说明原因。 |
| `steam_achievements` | `appid`，`user?`，`limit?`=40 | 某个游戏的成就进度与解锁情况（需要 api 可达或 Web API Key）。 |

## 游戏库

库、最近游玩、时长分析

| 工具 | 参数 | 说明 |
|---|---|---|
| `steam_library` | `user?`，`search?`，`limit?`=30，`with_details?`=True | 游戏库列表（默认按游玩时长倒序）。search 可按名称过滤。有 api 权限时带游玩时长；否则走 store 的 dynamicstore + 批量详情，只有 appid/名称/价格。 |
| `steam_recent_games` | `user?` | 最近两周玩过的游戏（需要 api.steampowered.com 可达）。 |
| `steam_playtime_stats` | `user?`，`top?`=20，`idle_days?`=180 | [只读] 游玩时长分析：总时长、Top N、近两周、吃灰榜（有库无时长）、久未启动。纯重组现有游戏库数据，不发额外请求。 |
| `steam_library_diff` | `a?`，`b?`，`limit?`=30 | [只读] 两人游戏库对比：共同游戏、各自独有、共同游戏的时长差。不传参数则与自己比（a 为自己）。 |

## 商店

搜索、详情、愿望单、购物车、结账、兑换

| 工具 | 参数 | 说明 |
|---|---|---|
| `steam_store_search` | `term`，`limit?`=10 | 商店搜索游戏/DLC，返回 appid、名称、价格、平台。 |
| `steam_app_details` | `appid` | 游戏详情：价格、折扣、发售日、开发商、类型、DLC、媒体评分、推荐数。 |
| `steam_wishlist` | `user?`，`limit?`=30 | 查看愿望单（含价格与优先级）。需登录或愿望单公开。 |
| `steam_wishlist_edit` | `action`，`appid` | 把游戏加入/移出愿望单。action: add 或 remove。需已登录。 |
| `steam_cart` | `action?`=view，`appid?`，`subid?`，`line_item_id?` | 购物车：action=view 读明细 / add 加购 / remove 移除 / clear 清空。add 传 appid（自动查 subid）或直接传 subid；remove 传 view 返回的 line_item_id。加购不扣款。仅用于付费游戏；免费游戏请用 steam_free_license。 |
| `steam_free_license` | `action?`=preview，`appid?`，`subid?`，`confirm?`=False | 免费游戏领取，与付费结账是两条独立链路。默认 preview 只读：判定 appid 是 F2P 常驻免费（不用领、直接 steam://run）、限免可领取（给出 claimable_packages 的 subid）、已拥有，还是付费游戏（该走 steam_checkout）。claim 才真正把免费许可证写入游戏库：必须 confirm=true + behavior.allow_sensitive=true + 显式 subid，走 store 域 /freelicense/addfreelicense/，不花钱、不创建交易、不经过 checkout 域名；领取后复核游戏库。付费游戏请勿用本工具。 |
| `steam_checkout` | `action?`=preview，`transid?`，`expected_total?`，`confirm?`=False，`payment_method?`=steamaccount，`country?`，`count?`=1 | 结账/购买游戏。默认 preview（只读，展示购物车 + 钱包余额 + checkout 会话状态）。完整流程：init 创建交易（不扣款）→ price 核对最终金额（total 单位分）→ finalize(confirm=true, expected_total=<price 返回的 total>) 才真正扣款。finalize 会二次核价：价格变动、钱包不足需外部支付时一律拒绝。钱包不足用 action=pay_link 只返回支付链接，MCP 不代付、不自动 finalize。另有 cancel / status 取消与查询交易。花钱动作需 behavior.allow_sensitive=true 且 confirm=true。 |
| `steam_checkout_session` | `action?`=status，`cookie?` | 管理 checkout.steampowered.com 的独立登录态（结账必需，与 store 域 cookie 不通用）。status 查看是否就绪并试探结账页；密码登录会自动收集，cookie 导入方式需 action="import" 粘贴 checkout 域 cookie，或 action="refresh" 用 refresh_token 补发。 |
| `steam_redeem_wallet_code` | `code`，`confirm?`=False，`captcha_gid?`，`captcha_text?` | [花钱] 兑换 Steam 钱包充值码 / 礼品卡，直接增加余额。必须 confirm=true 且 behavior.allow_sensitive=true；码有地区限制（码必须与账号地区一致）。返回与日志里的码只显示前 4 位。若返回 captcha_required，需带 captcha_gid + captcha_text 重试。 |

## 市场（只读）

行情、我的挂单、跨平台比价

| 工具 | 参数 | 说明 |
|---|---|---|
| `steam_inventory` | `steamid?`，`appid?`=753，`contextid?`=6，`limit?`=30 | 查库存。默认 appid=753（Steam 社区物品，contextid=6）；查 CS2 用 730/2，Dota2 用 570/2，TF2 用 440/2。需登录且库存公开。 |
| `steam_market_price` | `appid`，`hash_name`，`currency?`=CNY | 查单件市场物品的最低/中位价与成交量。hash_name 必须与市场名称完全一致（含磨损、StatTrak 等前缀）。 |
| `steam_market_search` | `appid`，`query?`，`limit?`=10 | 在市场里搜索物品，返回挂单价与在售数量。 |
| `steam_market_listings` | `limit?`=50 | [只读] 我当前的市场在售挂单（价格/手续费/状态/待确认数）。只读视图，不含上架、改价、撤单等交易动作。 |
| `steam_market_compare` | `hash_name`，`appid?`=730，`game?`=csgo | [只读] 同一件饰品在 Steam 市场与网易 BUFF 的最低价对比（人民币口径）。BUFF 需先 steam_buff_login；两侧都取不到会如实返回 error。 |

## 分析与价格

折扣雷达、Deck 兼容、价格快照、历史低价

| 工具 | 参数 | 说明 |
|---|---|---|
| `steam_deals` | `user?`，`min_discount?`=0，`limit?`=40，`max_fetch?`=120，`refresh?`=False | [只读] 愿望单折扣雷达：按折扣率排序的降价项 + 0 元限免。Steam 不支持批量查价，只能逐个扫（约 0.4s/个），结果缓存 30 分钟；返回 has_more=true 时再调一次即可继续累积。 |
| `steam_app_extras` | `appids`，`limit?`=20 | [只读] Deck 兼容等级 + 商店资源（封面/平台/媒体评分/推荐数），批量传入 appid。Deck 数据匿名可取，不需要登录。 |
| `steam_deck_compat` | `appid` | [只读] 单个 app 的 Steam Deck 兼容等级（verified / playable / unsupported）。 |
| `steam_price_watch` | `user?`，`commit?`=False，`limit?`=30 | [只读] 愿望单价格变动：基于 steam_deals 缓存做快照对比，不发额外请求。commit=true 会把当前价格存为新基线（默认只读）。 |
| `steam_price_history` | `appid`，`country?`=CN | [只读] 某游戏的历史最低价（IsThereAnyDeal）。返回最低价、折扣、店铺、时间点与 ITAD 链接。country 用 ISO 两位码（CN/ID/US），默认 CN。未配置 key 时返回 itad_key_missing。 |
| `steam_itad_key` | `key?` | 配置 IsThereAnyDeal API key（历史最低价数据源，SteamDB 的合规替代）。去 https://isthereanydeal.com/apps/my/ 注册应用获取；传空字符串清除。 |

## 账号历史

购买与许可记录

| 工具 | 参数 | 说明 |
|---|---|---|
| `steam_purchase_history` | `limit?`=50，`offset?`=0 | [只读] 账号许可/购买记录（日期、名称、来源）。分页用 offset/limit。 |

## 内容

新闻、评测

| 工具 | 参数 | 说明 |
|---|---|---|
| `steam_news` | `appid`，`count?`=5 | 游戏新闻公告。优先官方 Web API，失败时回落到 store 的 RSS 源。 |
| `steam_reviews` | `appid`，`limit?`=10，`language?`=all | 游戏评测：总体好评率 + 若干条评测摘要。 |

## 网易 BUFF

第三方市场行情

| 工具 | 参数 | 说明 |
|---|---|---|
| `steam_buff_login` | — | 登录网易 BUFF（buff.163.com）：走 BUFF 官方的「Steam 登录」通道（OpenID），借用本机已登录的 Steam 会话完成授权，不需要网易账号密码。前置条件：Steam 会话有效（先 steam_auth_status 确认）。BUFF cookie 与 Steam 凭证一样只进加密凭证库，不写日志。 |
| `steam_buff_status` | — | 查看 BUFF 登录状态与 session 是否仍有效。 |
| `steam_buff_logout` | — | 清除本机保存的 BUFF 凭证（不影响 Steam 登录态）。 |
| `steam_buff_search` | `keyword`，`game?`=csgo，`limit?`=10 | 在 BUFF 按名称搜商品，返回 goods_id 列表（后续查价用 goods_id）。例：搜「蝴蝶刀 致命紫罗兰 破损不堪」拿它的 goods_id。需已登录 BUFF。 |
| `steam_buff_price` | `goods_id`，`game?`=csgo，`page_num?`=1 | 查 BUFF 某件商品（goods_id）的实时行情：在售列表（每条含价格、float 磨损、图案模板）+ 求购列表。价格为买家实付（手续费卖家承担）。需已登录 BUFF。 |

## 通用

已登录任意请求

| 工具 | 参数 | 说明 |
|---|---|---|
| `steam_request` | `method`，`url`，`params?`，`data?`，`json_body?`，`confirm?`=False，`raw_html?`=False | 通用已登录请求：对 Steam 官方域名发任意请求，自动带上当前账号 cookie（和 api 的 access_token 不自动加，需自己放进 params）。域名白名单：store/api/login/help.steampowered.com、steamcommunity.com。只允许 https。花钱/改安全设置/接受交易类接口会被拦，必须显式 confirm=true。 |

## 错误码速查

| error | 含义 | 处理 |
|---|---|---|
| `confirmation_required` | 写/花钱操作缺 `confirm` 或 `allow_sensitive` | 补齐后重试 |
| `not_logged_in` | 没有可用会话 | `steam_auth_login` 或 `steam_auth_import_cookie` |
| `bad_input` | 参数不合格 | 按 hint 修正 |
| `library_unavailable` | 游戏库拿不到（可能私密） | 检查档案可见性 |
| `wishlist_unavailable` | 愿望单为空或私密 | 同上 |
| `mylistings_unavailable` | 市场挂单读不到 | 确认登录且 community 域可达 |
| `licenses_unavailable` | 许可页结构变化或未登录 | `steam_auth_status` 排查 |
| `itad_key_missing` | 没配 ITAD key | `steam_itad_key` 写入 |
| `refresh_rejected` | 续期凭证失效 | 重新登录（refresh token 无法自愈） |
| `Blocked` | 域名不在白名单 | 该域名不支持，换官方接口 |

