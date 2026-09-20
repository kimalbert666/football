# 记录、时点与评分输入

## 赛前不可变记录

默认保存于用户工作目录的`f4-data/predictions/YYYY-MM-DD/`，文件名含match_id、horizon和decision_at。重新预测创建新文件/新版本，不覆盖旧快照。赛果另存`f4-data/outcomes/`，评分另存`f4-data/evaluations/`。若用户指定仓库，沿用其结构并使用独立f4命名空间，先检查现有读写器兼容性。

每场记录包括：

- 身份：提供商event ID映射、league、season、home_id、away_id、kickoff_at、market=`90min_1x2`。
- 时点：decision_at、实际距开球分钟数、horizon、generated_at；每个source的captured_at、published_at/as_of（缺则null）、URL、数据哈希和缺失/过期状态。
- 概率：p_market、p_dc_raw、p_f3_base、p_f2_input（可能只有比分）、p_candidate、p_champion；固定H/D/A，未取得的值为null，不用1/3冒充模型预测。
- 版本：model_version、feature_version、baseline_version、trained_through、calibration_through、experiment_id、原始输入引用、完整输入概率和融合参数语义。
- 决策：top1、selected、reason、实际报价和报价时间；即使跳过也存档。证据质量与模型信心分列。

取消、延期、腰斩、未结算、完赛、来源冲突是不同状态；未确认完赛不视为失败样本。90分钟比分与加时/点球比分分开。以ID核对主客和赛事，不能只凭体彩周几编号跨周匹配。赛果更正保存修订记录，重新生成评分而不重写原预测。

## 评分器的导出格式

这里是实际前向存证的评分格式。一行一个JSON对象；每份文件只含同一league、horizon和model_version，一场比赛一条，selected为布尔值。先按预先约定的窗口从不可变记录中选出对应预测，再关联独立赛果。不同候选分别导出但使用同一个比赛清单；不在单份文件中重复一场。

历史重放是另一个模式：现在获取的历史档案，其真实retrieved_at必须保留为现在；若提供商能证明当年已发布，再另记historical_available_at和证明来源。这类探索记录不能倒填captured_at以通过本评分器，也不能冒充真实前向记录。没有供应商历史快照或当年存证时，就只作有局限的探索分析。

下列为纯虚构格式示例，不是足球数据：

```json
{"match_id":"synthetic-only-001","league":"synthetic","horizon":"T-60m","kickoff_at":"2026-01-10T15:00:00Z","decision_at":"2026-01-10T14:00:00Z","captured_at":"2026-01-10T13:55:00Z","trained_through":"2026-01-01T00:00:00Z","outcome":null,"p_baseline":[0.5,0.25,0.25],"p_candidate":[0.48,0.27,0.25],"selected":false,"model_version":"synthetic-v1"}
```

- kickoff_at、decision_at、captured_at、trained_through必须带时区。captured_at是基线/候选全部输入快照抓取时间的最大值；仍须在原始记录逐源审核更新时间。generated_at及记录落盘时间另由原始记录核验，离线评分器不伪造它们。
- trained_through使用基线、候选以及所有选参/校准步骤中最晚使用的数据时间，不能只填主模型训练截止。
- outcome仅接受`H`/`D`/`A`或`null`；null仍须通过元数据校验但不参与评分。
- p_baseline/p_candidate必须为3个有限的[0,1]数且和为1；不能用未经校准的比分、logit或赔率代替。
- 评分器拒绝同match_id重复、混合联赛/窗口/版本、开球后预测、决策后抓取、缺少训练截止、负值及NaN。
- horizon格式为`T-60m`等正整数分钟，评分器默认严格核对实际决策距开球时间。允许误差必须在实验前确定，并显式传入`--horizon-tolerance-minutes`；不能看完成绩才放宽窗口。
- 评分器给出的coverage仅针对输入文件，不证明文件包含了全部目标比赛；需要另存完整赛程清单、缺失和剔除记录对账。

## 旧系统迁移

`data/02-results/*-four-team.json`可以保留作原系统赛果档案。旧schema可能将early/final覆盖到同一条，而且没有完整基线/截止时间；不得填“估计时间”导入严格f4测试。只有能从赛前提交、原始快照或其他真实存证恢复的字段才可迁移，并保留来源。

f4新格式不直接兼容旧`backfill.py`。云端专用入口为`engine/scripts/f4_cloud.py`，账本为`f4_ledger.py`，保存到`data/f4/predictions/`和`data/f4/outcomes/`。它允许明确缺失的基线或候选，并分开计数；本页JSONL评分器要求两组完整概率，所以导出前必须筛选合格配对并保留缺失统计。不能把两种schema直接混用。云端对每个比赛／开球时间／窗口／实验固定最早有效基线快照；该快照缺候选时，不能挑选后来更有利时点替代。真实部署状态见[runtime.md](runtime.md)。
