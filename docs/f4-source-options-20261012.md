# 赛前盘口来源实测与备选（2026-10-12）

所有检查为只读探测，未写生产盘口样本，未注册账户、订阅、付费或触发通知。时间 UTC；对用户展示需加 8 小时。

## 已实测

- 04:38:59 HKJC 官方 GraphQL 新鲜响应，75 项赛事，仅 1 项英超。今晚 Coventry–Newcastle 唯一匹配 ESPN 401879267 / HKJC 50075993，开球 19:00 UTC（次日 03:00 香港）。HAD 与 HDC 都在售，主队 +0.25，1.95 / 1.87；同公司 1X2 为 3.00 / 3.40 / 2.00。报价改变日期较早，但新 HTTP 响应、PRE_EVENT、在售池的组合证明当前展示。它是窗口外探测，不是 T180/T60/T15 样本。
- ESPN 已给出 10 月 17–19 日全部 10 场英超，队名注册表均可唯一解析。HKJC 目前还没有发布这 10 场，不能因此判定正式采集窗口失败，也不能保证其未来覆盖。
- InferSports /health HTTP200，但 feed_live=false、as_of_stale=true，as_of=2026-10-02T22:02:04Z；不同公司仍共用此过期上游，不构成独立兜底。
- 独立 ESPN 展示链：本场及下周末 10 场的官方 summary 均实际包含 DraftKings 完整 1X2 和双向 Spread（半球档位）。窗口外读回，赛事原生 ID 可核验，当前事件状态 pre，HTTP max-age=1–10 秒。它没有单笔报价更新时间、在售状态，不能在不改证据等级的情况下纳入现有严格有效样本。可以另存为明确未认证的展示参考，不能和 HKJC 等级混算或为它补造 published_at。
- 球探公开 1X2 页面与赔率 JavaScript HTTP200，本场原生 ID 3003902；实际页面引用的亚洲盘页 https://vip.titan007.com/AsianOdds_n.aspx?id=3003902&l=0 在当前环境 ProxyError，web 工具亦不能访问。live.titan007.com/odds.aspx 为 HTTP500。未切换代理或绕过访问控制。
- Sofascore 公开 API 为 HTTP403，停止后续访问，没有绕过。

有限结构化的[赛马会实测摘要](../data/f4/status/source-availability-probe-20261012.json)已归档，明确标记为窗口外探测；不含完整赛事原始 feed。其余公开接口的访问结果与限制列于本页，未计入正式样本。

## 有文档依据但尚无 key 验证的服务

| 服务 | 接入与成本 | 与本项目相关的限制 |
|---|---|---|
| [The Odds API](https://the-odds-api.com/) | 免费 500 credits/月，需 key；最低付费 $30/月、20,000 credits | [历史接口](https://the-odds-api.com/liveapi/guides/v4/#get-historical-odds) 2022 年 9 月起 5 分钟快照，返回请求时点或更早最近快照，仅付费。实时 h2h+spreads 单地区每次 2 credits，历史同组合每次 20 credits。官方仍写 spreads 主要覆盖美国体育和公司；不能据通用字段承诺英超四分之一亚盘完整覆盖。用 key 实际确认 EPL + 同 book 双市场后才接入。 |
| [API-Football](https://www.api-football.com/pricing) | Pro $19/月、7,500 requests/日，需账户/key | [官方指南](https://www.api-football.com/news/post/how-to-get-started-with-api-football-the-complete-beginners-guide) 明确预盘每 3 小时更新，仅保留 7 天，不适合作 T15 变化主要来源。7 天历史并不证明可查询指定分钟快照。 |
| [Odds-API.io](https://odds-api.io/pricing) | Solo £49/月（页面本地显示 $65）、2 公司、5,000 requests/小时；新的免费 key 无限期暂停；WebSocket 翻倍 | [AH 指南](https://odds-api.io/blog/asian-handicap-api-guide) 实际展示 Sbobet 的四分之一 hdp、上下盘和 updatedAt；可同 book 查询 ML 与 Spread。公开 markets/bookmakers 已 HTTP200 实测验证。 [历史详细说明](https://docs.odds-api.io/guides/historical) 仅 closing odds，自 2025 年 12 月起，不能冒充 T180/T60/T15。 [OpenAPI](https://docs.odds-api.io/api-reference/openapi.json) 的 /odds/movements 含 opening/latest/recent 变价，保留深度因 feed 而异，仍不能保证任意历史时间的盘口。 |

## 可执行顺序

1. 立即提高已接通 HKJC 的赛前早期探测与保存时机，独立按 ESPN 赛程逐场查身份和双市场覆盖。先解决调度未及时执行，不因公开来源存在就把漏采归因于无盘口。
2. 将今晚已经验证可取的 HKJC 作为实盘采集来源；周末赛程预先登记并在发行后反复核对，不将“还没发售”误判成失配。
3. 可增补 ESPN / DraftKings 作为较低证据等级的独立展示参考，但只有主流程规定的严格证据才计有效 AH 样本。半球 Spread 的二向结算可研究；不会因此得到 0.25 / 0.75 等四分之一档位。
4. 若确实需要供应商级独立兜底，先做有 key 的同场、同书、同市场验收，再决定订阅。The Odds API 有时间快照，AH覆盖需验；Odds-API.io 有明确四分之一 AH，需持续自己存，历史多为收盘。两者各有未验证项，目前都没有接入生产。

不能把只录比分、HTTP200、两个公司共享一个上游、窗口外报价或赛后收盘历史当成已经补回完整赛前盘口样本。
