# 数据源与研究入口

核查日期：2026-09-15。下列是公开文档所能确认的能力，不代表所有实时接口均已测试成功，也不代表模型在用户比赛上已验证有效。遇到变动重新打开官方页面；只引入能解释来源、时间和使用范围的数据。

## 可用的三个本地适配器

| 适配器 | 使用价值 | 需保留的限制 |
|---|---|---|
| `../f1/SKILL.md` | 赛程、球队身份、历史比赛、Elo、xG、英超伤停 | 先读f1的`references/api-reference.md`。文档描述xG限五大联赛且可能滞后；伤停限英超。空结果不能推导“无伤停”。ID优先。实际覆盖逐次检查 |
| `../f2/SKILL.md` | FootballBin外部比分预测，供前向对照 | 文档覆盖英超/欧冠；不是已校准三向概率；无法证明存在历史赛前快照查询。记录league、fixture和抓取时刻，不能事后倒填预测 |
| `../f3/SKILL.md` | DC/市场模型、比赛记录和赛后流程 | 保留可追溯数据与记录纪律；其手动系数、市场锚、校准与云端时点仍需按f4审查。不能整体复制即认定有效 |

上述路径相对于skills根目录的f4文件夹，先定位真实安装路径。若适配器缺失，使用下列原始来源及可用工具，不假装已调用不存在的接口。

f1的ClubElo W/D/L可能是查询球队视角，查询客队时转换为主队H/D/A。`local-elo`与ClubElo不是同一尺度，local-elo不能作跨级别强度比较。H2H返回未解析球队与真正没有交锋必须区分。

## 更广的数据来源

| 来源与官方入口 | 适合的用途 | 时间、覆盖和接入约束 |
|---|---|---|
| [Football-Data](https://www.football-data.co.uk/data.php) | 已有历史比分/赔率档案的解释和探索性基线 | 2019/20起区分较早采集价与收盘列；固定采集价不等于T−60m。官方警告2025-07-23后Pinnacle公开API报价滞后且不再进入Avg/Max。当前使用文字限制个人用途并排除某些自动抓取/AI训练产品；不能仅因CSV公开就默认获准新增自动训练采集，应选有适用许可的渠道 |
| [ClubElo](https://clubelo.com/) | 独立保存的评级/三向参考，检验历史比分以外的增量 | 旧API文档路径可能重定向；逐次验证接口、as_of及赛事匹配。当前页面不能证明任意历史时刻预测可获取。禁止拿当前评级预测过去 |
| [Understat](https://understat.com/) | 历史已完赛npxG/xGA、射门质量特征研究 | 比赛xG是赛后信息；先滞后再滚动计算，区分主客、对手和数据提供商。网页可读不等于官方稳定API或再分发许可。不能与另一供应商xG无标记拼接 |
| [Hudl/StatsBomb Open Data](https://github.com/hudl/open-data) | 公开事件数据、首发、部分360数据的研究和特征验证 | 只覆盖`competitions.json`所列的部分比赛/赛季；不是四队实时全量feed。按README和LICENSE要求使用及署名，不能把“open”理解成任何方式无限再发布 |
| [The Odds API历史接口](https://the-odds-api.com/historical-odds-data/) | 固定截止时点市场基线的候选数据源 | 主要市场历史自2020-06-06，2022-09起5分钟快照；返回请求时间或更早最近快照，按赛事实际覆盖检查。历史接口需付费key，目前未接入。原始数据再发布遵循其[条款](https://the-odds-api.com/terms-and-conditions.html) |
| [API-Football指南](https://www.api-football.com/news/post/how-to-get-started-with-api-football-the-complete-beginners-guide) | 赛程、覆盖字段以及外部概率的前向候选 | 需账户/key；查询旧比赛不证明得到的是当时的预测。按coverage及更新时间检查，预测百分比也必须校准验证。[服务条款](https://www.api-football.com/terms)不保证所有数据/更新频率 |

其他新来源进入候选库前记录：原始官方地址、方法与输出定义、样本范围、发布时间、预测生成时间、历史快照、失败行为、价目与许可、是否与已有源共享数据。无法检验的商业“命中率XX%”只作为营销声称，不作为权重依据。接入新付费源前先形成具体方案与成本，用户未授权时只列候选。

## 基础方法依据

- [Dixon–Coles，1997](https://academic.oup.com/jrsssc/article-abstract/46/2/265/6990546)：历史比分Poisson建模的候选基础。当前可读摘要是当年数据研究，不能据其旧收益外推今天的准确率/盈利。
- [Egidi等，2018](https://arxiv.org/abs/1802.08848)：将历史比赛和市场信息纳入层次Poisson模型的研究，支持把融合当成可测试模型族；不支持固定某一权重永远有效。
- [Hubáček等，2018/2019](https://link.springer.com/article/10.1007/s10994-018-5704-6)：作者报告其2017足球预测挑战赛获胜方法使用评级特征和梯度提升树；此次读到出版社摘要。可将pi-ratings/梯度提升列入候选，不能把该赛制排名外推为当前五大联赛最强。
- [Mendes-Neves等，2025](https://arxiv.org/html/2501.05873v1)：全文用Elo预测射门数量/质量分布再模拟结果。它把真实xG作为未来扩展，不能把其射门质量近似与已用真实xG混称。表3的策略总收益与相对基线增益不同，不能把后者直接当ROI；新增平局修正仍须新测试。
- [动态贝叶斯模型，2026](https://academic.oup.com/jrsssc/advance-article/doi/10.1093/jrsssc/qlag032/8704597)：读到全文摘要、讨论及市场对照附录。模型允许球队攻防实力随时间变化；附录C中多个半赛季市场Brier/RPS仍更好，末轮结果则有不同。这支持列入挑战者而非直接替换市场基线。
- [Fischer与Heuer，2024](https://arxiv.org/abs/2408.08331)：作者摘要允许除目标比赛外的整季结果作为特征；这与严格赛前任务信息集不同。其比较可作方法线索，不把相应成绩当可部署赛前准确率。
- [scikit-learn概率校准](https://scikit-learn.org/stable/modules/calibration.html)：校准数据要与主模型训练数据隔离；较低Brier也可能来自判别改善，不能只据此声称校准更好。
- [时间切分](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html)与[泄漏风险](https://scikit-learn.org/stable/common_pitfalls.html)：避免未来数据进入训练、选参或预处理。足球赛事不是等间距序列，按比赛周/时间块构建分组，不能机械套行号切分。

新论文优先原文/作者代码/可复现数据。只读到摘要就明确这一限制，不把搜索片段当已复现结论。准确率数字只有在目标、联赛、时点、测试时间、覆盖率及基准一致时才能比较。

## 2026-09-21：f4 云端改用皇冠参考

用户指定皇冠，使用[球探公开欧赔索引](https://1x2.titan007.com/index_vip.aspx)及其页面引用的独立数据子域。三向表中公司ID为545，必须同时核实公司名Crown；不能套用另一个亚洲盘口表的ID3。初盘与即时H/D/A字段分开；索引展示时区UTC+8，比赛JS时间为UTC，按已核验模板解析而不执行提供商JavaScript。保存有限引用字段、真实抓取时间、报价变化时间、URL与响应哈希，不上传原始页面或整份JS。

这是球探转载，不是皇冠官方API；没有独立受注状态，不保证可成交。只对身份和时间唯一匹配的赛前赛事使用，缺失明确报告，不以体彩价格冒充皇冠。当前部署及验收以[runtime.md](runtime.md)所指仓库报告为准。
