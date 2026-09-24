# Data Agent 后续工作清单

维护约定:本文档只列**待做/在做**的事,做完的移入 `data-agent-iteration-log.md` 归档;每项标注来源(归因/竞对研究/现场故障),保证每条都有证据链。原则不变:**所有改动必须是通用 data-agent 能力,DAB 只是定位缺口的引导信号**;题目特定规则、预焙答案、评分器博弈明确不做。

竞对研究(2026-08-19,top-5 全轨迹审计)的结论作为本清单的主要输入:榜一领先 = 死题破解(激进 tuned,不学)+ 一致性满格(通用机制,全学);Permute Core 用零调优 prompt 打到 0.8413 证明"机制制造正确性"路线的天花板足够高。详细报告见 iteration-log 的 R-INTEL 节。

已归档(见 iteration-log):survey_sources 枚举单点故障、R22 契约层三规则(GITHUB q4 3/3 收复)、R23 一致性带三机制(8 靶题全 3/3、六哨兵零反转)、R24/R24b survey 层机制(GITHUB q2 全场死题首次可过 ~50%;宽表挑列/配对通用修复;句式家族普查)。

PANCANCER 查询平面挂载缺口(R18b 遗留)已随 survey 三级降级修复关闭:实测 postgres+duckdb 两源正常挂载、survey 见 3 表。

## R25 候选(证据已归档,未实现)

- **时间锚硬门**:crm q12 残差(R21 3/5、R22 2/3、R23 2/3,归因=cohort 时间锚分叉,指引层 ~1/3 押错)——题面含时间跨度而 rejected_readings 无逐变体数字时 REFUSE 一次。风险:触发面宽,须全哨兵验证。
- GITHUB q1:死题带最后一块(~2.1pp),尚无机制假设,需先做轨迹归因。
- agnews(0.30,全场最大单点拖累):吞吐受限(12 万条语义打标),haiku 路由已降本但未提速;方向是 semantic_map 吞吐或窗口配置化,不是判断机制。

## 明确不做(新增)

- **PANCANCER q1 的列选择**:GT 分组用码列 vs 我们用名列,已穷尽取证证明数据侧无可判定信号(方括号限定词在两列均为空操作,题面措辞对称)——任何偏好规则都是任意或有反转风险,退出 in-rules 范围(详见 iteration-log R24b)。
- **GITHUB q1 的版权判定口径**:GT 要 0.33,我们稳定在 0.16-0.18。离线实测队列侧(散文语言列 642 种句式,已上机制)与分子侧(copyright/(c)/©/all rights reserved 的 union 上限 0.1913)**都无法凑到 0.33**——继续推进只能靠猜"版权信息"的判定口径,属评分器口径拟合,退出 in-rules 范围(详见 iteration-log R26)。

## 收官状态(2026-08-21)

12ds **0.8475**(单次全量,R25);机制路线内第一,榜单位置 #2 与 #3 之间。已复现覆盖:R22-R24 全部增益题(crm q9/q12、music q1、GITHUB q2/q4、PATENTS q2/q3、yelp q1/q3/q4/q5/q6)。**未做完整 12ds 复现**(预算),故 0.8475 为单次测量,带宽未定。剩余可动空间:agnews(0.300,吞吐/打标质量,成本最高)、DEPS q1、GITHUB q1(已出范围)、PANCANCER q1(已出范围)。

## 冲榜进展(2026-08-20,R29-R31)

**当前最高:12ds 0.8700**(R31 全量,270/270,审计全绿)——越过 #2 Sentinel 0.8617,距 #1 EQ 0.8713 差 0.0013(约一签)。

- **exclusion_basis 门**(第 6 交付门):DEPS q1 0/5 → 1/5(哨兵轮 2/3,合并 37.5%);瓶颈是"注明依据"可用散文糊过去 → R32 升级为引用存储盖章值的义务。
- **跨模型级联**(semantic_map verify=sonnet / arbiter=opus,config 门控):agnews 0.300 → 0.600(q4 满分、q2 2/5);成本 ~$100/全量轮,常规迭代应关。
- **剩余可动空间**(按期望值):DEPS q1 转化率(1/5 → 3-4/5 = +0.017-0.025,唯一确定性靶)、GITHUB q2 的 fuzzy 掷币(3/5 → 5/5 = +0.0083,非机制)、crm q3/q12 flaky 回摆(+0.008)。**已出范围**:agnews q2/q3、GITHUB q1、PANCANCER q1。
- **预算现实**:全量轮 ~$310(含级联),靶向轮 ~$12;冲线需在 DEPS q1 钉稳后再跑一次全量。

## 基建(不影响分数归因)

- ~~box 的 DataAgentBench checkout 同步到最新 main~~ 已完成(9d6f64c,新 DEPS q1 validator 官方版,GITHUB q2 新题面)。**注意:该 checkout 的 query_dataset 文件是"下载数据覆盖 git 指针"形态,任何 reset/stash 操作都会把真数据打回指针——已踩过一次,靠 stash 外科恢复;动 checkout 前先保护 query_dataset。**
- ~~serving~~ 已定:OpenRouter 直连 + provider 钉扎 Anthropic + AE_SIDECAR_PROXY(公司代理),显式缓存 ~95% 命中,详见 iteration-log R22。
- dab.opus.full.yaml 的 run_id 钉扎——不再复用该文件,全量轮将从 r23 配置面新写 dab.full.yaml。

## 第二轴:exploratory 模式(产品工作,未排期)

DAB 测不到、CSV 分析等真实场景需要的:开放式任务("帮我分析一下")的分解;交付物为报告/图表而非单行答案(裁决记录升格为正文);歧义变体不一致时向用户澄清而非自裁(不变性扫描的机器复用,换终端动作);多轮口径记忆;乱编码/乱分隔符/多 sheet 摄取容错。工具面(survey/resolve/semantic_map/verify_join/grain_check)两轴共用。

## 明确不做

repo/GENERAL 内的 per-question 证据包与点名题号的 prompt 补丁;"避开并列答案"类评分器博弈;泄露 scorer 机制的措辞;一切需要读 ground_truth/validate.py 源码的手段。

**边界更新(2026-08-20,Ethan 拍板)**:rubric 合法的 per-question 调优(EQ 路线)允许作为 **DAB 包上的支线实验**——随提交配置披露、永不进 repo/GENERAL、不作为主线;主线仍是机制路线。

## 第二轴进展(2026-08-20)

- 校准分层落地(R28/R28b):GENERAL(warn 档、零指纹)与 DAB 包(AgentEval `configs/dataagentbench.raven-data-agent-profile.yaml`,钉死 R27 语义)分家;详见 iteration-log R28/R28b。
- **files 数据源已支持**(CSV/TSV/Parquet/JSON/JSONL/xlsx → 视图 + 解析降级报告),exploratory 轴的"乱编码/摄取容错"部分收窄为:GBK 等非 latin-1 编码、xlsx 多 sheet、精确丢行计数;交互层(澄清、多轮口径、报告交付物)仍未排期。
