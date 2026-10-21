# 自进化主循环 标准操作流程(SOP)

> 注:本文为上游 SOP 的拷贝,已做脱敏(内部仓库名/机器地址/共享盘路径以中性描述替代),其余内容未改动。

**日期:** 2026-06-24 | **用途:** TB2 / EvoAgentBench 后续跑分照此走,产出可拼进论文的统一格式。
**性质:** 操作手册(人/Claude 照着做),非理论。理论/设计依据见文末"定稿来源"。
**配套:** `2026-06-24-actual-runs-vs-paper-design-gap.md`(差距+各步定稿)、`2026-06-16-sampling-and-
decision-design.md`(三层采样)、`2026-06-15-paper-positioning.md` §4c(贡献)。
> ⚠ **跨仓库**:本 SOP 留在上游 harness 仓(运营 runbook);上面这些 paper 设计文档 2026-06-26 已迁至
> **论文仓 `docs/specs/`**(本文 §6 引用的所有 paper 文档同理,去论文仓找)。

---

## 0. 通则(每轮都适用)

**★ Gate0 / 环境与测量有效性(最前置,跑分前+跑分后两道,违反则该 trial 数据无效):**
- **跑分前——环境健康预检(intra-env / sandbox 检查)**:跑任何题前先验环境本身正常——sandbox 起得来、docker
  网络通、依赖装好、verifier 能产出 ctrf。脏环境(TB2 d0fast 前的 debian-apt 假阴 infra_err 38-45 /
  proxy×docker 路由劫持 connErr)下跑出来的分**全废**,先修(如 d0fast overlay)再跑。
- **跑分后——infra 失败处置阶梯(检测→补跑≤2次→修infra→[2次仍崩]记为不成功):**
  - **检测**:ctrf 不存在 / sandbox 崩 / verifier 超时 = infra 失败(L1),**不是 agent 没解出来**;
  - **★ 补跑(上限 2 次)**(user 2026-06-25):infra 崩的 trial **环境正常后重跑**、补满 K——把那道题真实能力测出来;
  - **修 infra**:若是**确定性** infra 失败(每次必崩,如 d0fast 前的 debian-apt)→ 补跑无用,先修 infra 再补跑(仍计入 2 次);
  - **[2 次仍崩]记为不成功**(user 2026-06-25):补跑 2 次+修 infra 仍有问题的极少数,**该题记为不成功(numerator 计 0)**,
    **但留在分母**(不剔除、不搞覆盖率账)。每题最终都落成 0/1 → 分母干净 = 总题数。
- **★ pass@1 分母 = 题目总数(2026-06-25 user,硬规则):**
  - **分母 = 全部题目数,绝不把 infra 失败题从分母剔除**。补跑(≤2)让能救的题成真实测量;救不回的记 0、仍留分母。
  - **为什么**:剔除 infra 失败题 = 缩小分母 = **高估 pass@1**(被剔的题往往偏难;呼应 v7 uv 假阴教训:fair-subset 外推全集是高估)。
  - **三方对照(都 denom=总题数,差在 numerator 怎么填)**:
    - HarnessX A.3:infra 失败**一上来就当没解 = 记 0**(无补跑)→ 把大量可修/瞬态假阴也压低(污染);
    - 旧 timeout-fair"剔除不计0":infra 失败**从分母也剔掉** → 缩分母 → 高估;
    - **我们**:**先补跑≤2 次 + 修 infra**(把 TB2 那 38-45→7 的可修假阴救成真实测量),**只剩真持久的才记 0、不剔除**
      → denom=总题数、保守无偏 —— 这是 C1 测量完整性(Gate-f)的核心(差异化 = "先给 2 次补跑再判",非"一上来记 0")。


**分工线:**
- **语义操作 = Claude(CC SDK)**:诊断、选 WHY、设计候选、写 patch、verdict 草拟。
- **确定性代码/脚本**:跑分调度、gate、记账、坐标绑定、聚合。

**两口径(双阈值,别混):**
| 用途 | 口径 |
|---|---|
| 对内导航(进 bank / 选 parent) | **K=3 均值 pass@1 > vanilla**(松)。⚠ 用 K=3 均值,不用 single-run |
| 对外宣称(论文 credited) | **配对 2σ**(同题集逐题配对,σ=实测配对差 std,per-task 透明实算)。**配对 2σ < 非配对 2σ**:配对消题间难度方差,残余只由边缘题主导 → TB2 门槛 ~3-4pp(child ~47-48%) vs 非配对 ~8pp(~52%)。论文仍写"2σ(z≥1.96)",只是 σ 用配对算;不能口头说"配对所以小",必须 per-task 实算贴表。详 gap 文档显著性节 |

**核心纪律(踩坑后定型,违反则数据作废):**
1. 只读 train/失败轨迹做诊断;**held-out / test 只拿数字,绝不看其轨迹**(防泄漏/过拟合)。
2. 配置自始至终一致(timeout-fair 同 cap);失败兜底**不改 max_tokens/temp/thinking**,脏数据由分析层显式过滤。
3. 同 benchmark / 同 K / 同 verifier 才可比;single-run pass@1 摆动 ~5pp,**进 bank 用 K=3 均值**。

**★ train/test 铁律 + sealed test set(C3 命根子,违反则 retention 作废):**
- **train**:跑分 + **读轨迹诊断** → 驱动进化(诊断/选WHY/设计/选parent 的依据**全部、且只**来自 train)。
- **test = sealed test set(密封测试集)**:只跑分拿数字,绝不读轨迹、绝不据它改 harness。
- **每轮跑 test,但密封(sealed)**:test 评分由**确定性脚本跑 + 存盘到 Claude 读不到的位置**,
  **进化决策时 Claude 看不到 test 数字**(withheld from the evolution agent)——风险来自"看到 test 后用了它"
  (早停/选parent/人脑潜意识方向泄漏),不来自"跑"本身。**靠机制隔离(决策时不可见)才叫 sealed;靠"人忍住不看"
  不算 sealed**,审稿人会质疑。
- **进化结束后揭封(unseal)**:调出每轮 test 数字 → 画 train/test 曲线(抓过拟合拐点)+ 选 test 最高轮当交付 +
  算 **retention rate = test lift / train lift**(C3 指标)。
- = sealed-but-logged(每轮跑+存但对决策者密封):既拿到曲线又零泄漏,泄漏风险≈fully-sealed(最后才跑)。
  呼应分工线:test 评分=确定性脚本(跑+存不给看)/ 进化决策=Claude(只喂 train)。
- **论文措辞**:"We evaluate on a **sealed test set**: scored each round by an automated harness but
  **withheld from the evolution agent**—no test signal informs diagnosis, candidate design, or parent
  selection; unsealed only after evolution completes." (对照 HarnessX §6.1 自承 same-set 无 held-out)

---

## 1. 冷启动 SOP(每个 (模型 × benchmark) 一次性)

> ★ **冷启动也只用 train**:下面"全集/失败题/失败地图"全部指 **train 集**(test 是 sealed,从不进诊断/选样/
> 厚账)。vanilla 的 test 评分由脚本盲跑+存盘,不参与冷启动。

```
1. 跑 vanilla(root)train 全集 × K=3 → 厚账(给后代借账 + 当锁定基准 anchor)
2. 诊断范围(失败题):
   - 训练集 ≤ 200 题 → 全看失败题(边缘 + 全错),全对题抽几道当对照/哨兵
   - 训练集 > 200 题 → 失败题按"失败签名"分层采样保覆盖(本工作 TB2 89 / LiveCode 97 都 ≤200,全看)
3. Claude 逐条诊断失败轨迹,只用"失败签名(来源A,原始轨迹天然有)":
   finish_reason=length+空(thinking-runaway/空响应)/ 重复 tool_call / 撞 iter 上限 / docker 错 / 没贴代码…
   (不用 affinity——此时无机制)
4. 顺手吐结构化 failure_map.json(轻量,CC SDK schema 输出,非四阶段 pipeline):
   每条 = { WHY(失败模式), 哪些题(task_id + stability bucket/签名), WHERE-hint(软建议杠杆,不绑定) }
5. 检查覆盖:failure_map 覆盖到的 WHY 类数(目标 ≥ ~7);够了冷启动完成
```
产物:`vanilla 厚账(全集 K=3)` + `failure_map.json`。

---

## 2. 每轮进化 SOP(七步漏斗)

> 标注:【现做】当前规模就该做 /【缓】大 benchmark 才需要 /【人=Claude】语义步。
> **谁做哪步、调哪个代码 → 见 §8 配合契约**(本节只讲流程,§8 讲 Claude Code ↔ 准备代码 的分工)。

**一轮 = 一次"诊断 → 设计 n 个候选 → 每个候选各自走 anchor 分叉 → 选 parent"。**
"anchor 完就回下一轮" = 那个*候选*在 screen 被判明显输家、就地淘汰、**不上全集**——不是每轮必走全集。

```
① 诊断【人】——分第一轮 vs 后续轮:
   - **round 1**:还没"上一轮 child" → **直接用冷启动产出的最初 failure_map**(基于 vanilla 失败的地图)。
   - **round 2+**(复诊):读"上一轮 child 的轨迹"(不是再扫全错/全对),看:机制 fire 的题(起效没/变好变坏)
     + 翻转题(救活的确认功劳/弄坏的=退化)+ 仍失败题(下个靶子)→ **append 更新 failure_map**。
   注:**failure_map 是跨轮累积的活地图**(冷启动建初版,每轮 append 本轮新失败 + WHY 分布变化),非冻结——
   这正是它"可审计 + 可对比轮次(WHY 分布随进化变)"的价值。

② 选 WHY + 设计候选 C1..Cn【人】:从 failure_map 选本轮打哪几个 WHY;每 WHY 设计候选、跨杠杆
   (prompt/knowledge/runtime/config)。patch env-gated、默认关=vanilla 字节级一致、不碰 kernel。
   · **候选数 n(2026-06-24 拍,小 benchmark 标准档)**:**每轮打 1-2 个 WHY × 每 WHY 2-3 候选 = 3-4 候选/轮**。
     n 由"本轮诊断出几个值得打的 WHY"驱动 + 预算封顶,非固定值;完整 3×3=9 只在大预算开【缓】。

③ 免费剪枝(零 GPU)【现做】:beacon_guard(无 activation_beacon 拒收)+ preflight(触发谓词历史 0 命中=
   inert,不上场)。

──── for 每个候选 Ci(候选级分叉)────────────────────────
④ apply 建子节点【确定性】:git apply(path_guard 挡 kernel)→ 建节点 + 记账(nodes/)

⑤a 探测器 screen:anchor 子集(~15道=affinity 主体 + 全错破冰候选 + 2-3 全对哨兵)× K=1 快筛
    · ★ **anchor 只从 train 池挑题,绝不含 test**(test 是 sealed,anchor 是进化决策的一部分,碰 test=泄漏)。
      所以 anchor ⊂ train。screen/confirm 都在 train 上比 vanilla;test 始终只由脚本盲跑、不进 anchor/决策。
    · 借账【缓】:机制不 fire 的题借父厚账、子节点不跑/K=1
    · 三档判据(★宽进;噪声带 σ_screen 见下):
        明显超 vanilla       → ⑤b 上全集
        略低 / 跨噪声带       → ⑤b 上全集(C3:略低**不淘汰**!anchor 均值不代表全集)
        低于 vanilla > ~1.2–1.5×σ_screen → ✂ **淘汰 Ci,跳过 ⑤b/⑥**,记 status=pruned_at_screen ← "anchor 完就回"
            (轻量记账:Ci/anchor 表现/淘汰原因;anchor 那几题结果进厚账供借账;防下轮重复设计同 Ci)
    · ★ **σ_screen 来源(2026-06-24 拍)**:从**冷启动厚账**(vanilla 全 train × K=3)取 anchor 那~15 题的 per-task
      pass 率 p_i,实算 **σ_screen = sqrt( (1/n²)·Σ_i p_i(1−p_i) )**(= K=1 anchor 均值的抽样 σ)。
      **这是大而松的 σ**(K=1 + n 小 + 故意挑高方差边缘题),≠ Gate2 的"全集 K=3 配对 σ"(小而紧)——同一本厚账、两个口径。
      · 淘汰阈 = **1.5·σ_screen**(不写死 pp);"跨噪声带" = 落在 ±σ_screen 内(Ci 与 vanilla 分不开,照样上全集)。
      · ⚠ **旧"−6pp"作废**:那是偏紧的临时占位(≈0.5σ_screen,会砍掉分不开的候选、违背宽进)。σ_screen 待 anchor
        选定后从厚账实算才有数;**粗估 9–13pp,远大于全集单跑 5pp**(n 92→15 + 选高方差题)。
      · ★ **谁算**:σ_screen + cull_threshold 由 `select_anchor()` 顺带吐(同一本厚账、同一批 p_i(1−p_i),
        与挑边缘题复用,见 §8.2)——不是 Claude 临场估。

⑤b 估计器/全集 confirm(仅 screen 幸存者):全集 × K=3
    · TB2 89 / 小 benchmark:估计器 = 全集;大 benchmark 全集跑不起:代表性子集(按占比分层+加权)【缓】

⑥ 三盾【确定性】:
    · Gate0/Gate-f(测量有效,见通则★):环境健康预检 + infra 失败按阶梯处置(检测→**补跑**→修infra→剔除报覆盖,
      不计 0)。TB2 d0fast 修 debian-apt 假阴(38-45→7)。对照 HarnessX A.3"infra 计入失败"=污染,我们补跑/剔除
    · Gate-b:activation ledger 记到机制 fire 才允许归属
    · Gate2:配对 lift;导航 pass@1(K=3均值)>van → 进 bank,记 status=promoted;没过 → 留档 pruned_at_confirm
──────────────────────────────────────────────────────

⑦ 选 parent【人/确定性】:bank 里最好的 → 下一轮 parent;若本轮候选全淘汰/没过 → 无新 parent,
   **沿用旧 parent + 新诊断**,进下一轮。
   → 检查终止条件(见下);未触发则回 ①
```

**循环终止条件(2026-06-24 user 拍;任一触发即停):**
```
① 连续 10 轮没有找到"train 上优于 vanilla"的 harness  ← 主信号(探索枯竭)
② 轮数达 20(hard cap,兜底)
```
- **★ ① 的比较基准是 vanilla,不是上一轮 parent**(user 强调):每轮判"候选有没有 train 上 K=3 均值
  pass@1 > **vanilla**";连续 10 轮**一个候选都没过 vanilla** → 停。和 vanilla 比(固定基准)比和"上一轮
  parent"比更稳、可复现(后者水涨船高越来越难);与 promotion 口径一致。
- **绝不看 test 决定停**(sealed 铁律):① 只用 train vs vanilla,test 全程 sealed、不参与终止判断。
- **10 / 20 = 初值,后续按实际跑分调**(low-ceiling 模型上可能很快连续 10 轮无 promotion = 天花板信号)。
- **终止后收尾**:(a) unseal test → 画 train/test 曲线 + 选 test 最高轮当交付 + 算 retention rate;
  (b) 记终止原因(连续10轮无 promotion / 轮数到 20)——诚实记录,非"跑够了就停"。

**节点状态(记账标)**:`pruned_at_screen`(anchor 挂)/ `pruned_at_confirm`(全集没过)/ `promoted`(进 bank 当 parent)/
`pruned_inert`(③ preflight 挂)。每个候选无论挂在哪,都留痕(可审计 + 借账复用 + 防重复设计)。

**WHERE 坐标绑定**(诊断只出 hint):patch 写完后,WHERE 从 artifact 机械绑定(target_file),ε 集中 WHY 轴。

---

## 3. 状态留痕标准(跨窗口/跨 benchmark 统一 —— 这是 SOP 的核心价值)

**循环由 Claude 驱动、非程序驱动;状态靠三层留痕(LiveCode 已验,标准化两边都用):**

| 层 | 内容 | 文件 |
|---|---|---|
| **findings 工作日志**(主状态) | 每轮一节:试了什么、结果、下个靶子 | `docs/specs/handoffs/<date>-<bench>-evolution-log.md` |
| **memory**(跨会话,冷启动直读) | 项目状态(每轮更新)+ 纪律 feedback | `memory/project_<bench>_*.md` + `MEMORY.md` 索引 |
| **box 持久产物**(诊断原料+结果存证) | failure_map.json / 各轮 reports(pass@1+失败分布)/ session.jsonl(轨迹=下轮只读输入)/ 节点账本 | `nodes/`(TB2 nodes_qwen36)/ `reports/` / `jobs/*/session.jsonl` |

**统一要求(两 benchmark 都照此,才能拼进论文):**
- 每轮 reports 必含:**per-task 结果(过几次/K)**(算配对 σ 用)+ 失败签名分布 + WHY 分布;
- **pass@1 分母 = 题目总数**(§0 硬规则):infra 失败题补跑(≤2 次)进有效测量、不从分母剔除;2 次仍崩则该题记 0、仍留分母;
- 机制 patch:env-gated、记下开关名 + target_file(供 WHERE 坐标绑定);
- 跑分 job 命名带:benchmark / 机制 / K / split(train|test|full) / 日期。

### 3.1 harness 用 git 维护 + 节点账本(进化树的物理载体)

**进化树不是抽象结构,它的物理载体就是 git + 节点账本两件:**
- **代码在 git**:每个候选 harness = 一个 **git 分支 + commit**;**C0/vanilla = 基线分支**(TB2 = `evolver/qwen36-r1`)。
  机制候选 = 从基线/父节点长出的分支(如 `evolver/rN-*`)。机制 patch env-gated、默认关 = vanilla 字节级一致。
  ⚠ **evolver 基础设施(select_anchor / gate / 采样)写 C0 基线,不写机制候选分支**(evolver 操作 harness、本身非机制)。
- **树结构在节点账本** `nodes/*.json`(一节点一文件),锚定到 git:

```json
{ "node_id": "R4F", "parent_id": "v7",          // 树血缘(parent_id) + git 血缘双记
  "git_branch": "evolver/r4f-std-v3", "git_commit_sha": "a996daa",  // ← 指向 git 的代码状态
  "core_version": "v7", "created_at_iter": 4,
  "status": "promoted | pruned_at_screen | pruned_at_confirm | pruned_inert | archived-not-credited",
  "activation_spec": { "kind": "...", "threshold": N },   // 机制触发谓词(Gate-b / preflight 用)
  "patch": { "patch_where": "loop_override", "patch_why": "...",
             "components": [ { "target_file": "...", "diff": "..." } ] } }  // WHERE 从 target_file 绑定
```

- **怎么用**:`select_anchor`/采样/诊断读 vanilla 厚账(基线分支跑分)→ Claude 设计 patch → 新分支 commit +
  写一份 `nodes/<id>.json` 锚定该 commit → 评测 → 回填 `status`。节点账本 = 可审计 + 借账复用(parent_id 树距)+
  防重复设计的唯一真相源;git 是代码真相源,两者靠 `git_branch`/`git_commit_sha` 对齐。

---

## 4. 两 benchmark 适配(差异表)

| 维度 | TB2 | EvoAgentBench (LiveCode) |
|---|---|---|
| 判分 | harbor verifier(d0fast overlay,timeout-fair) | 官方 lcb_runner check_correctness(子进程沙箱) |
| **split** | **train 53 / held-out 36(已切,sealed)** `s_tb2_{train53,heldout36}.txt` | **train 97 / test 39(官方)** ← C3 泛化主实验在此 |
| 跑分流程 | anchor screen(~15,从 train53 挑)→ confirm train53 → held-out36 sealed 盲跑 | 现:train+test 各全跑 K=3(97 题够便宜) |
| 规模 | 89 | train 97 / test 39 |
| infra 关键 | d0fast(docker0 桥),修 debian-apt 假阴(38-45→7) | NO_PROXY=* 直连(免 proxy×docker 路由劫持) |
| K=3 编排 | `--attempts 3` 单 job | conc2×2 交错(控端点漂移) |

**统一动作(为论文 C3 铺路):**
- ~~TB2 也该切 train/test split~~ **已切(53/36,2026-06-24)**;两 benchmark 现都有 split,可进 C3。
- 两 benchmark held-out 都报 **retention rate = test lift / train lift**(C3 指标),不只报两个绝对数。
- ⚠ anchor 现从 **train53** 挑(~15/53≈28%,比 §2 B 默认 15-20% 偏高,小训练集正常);σ_screen 从 train53 厚账实算。

---

## 5. 一轮 Checklist(可勾)

```
[ ] ① 读上一轮 child 轨迹诊断(只读失败,不看 held-out 轨迹)→ 更新 failure_map
[ ] ② 选 WHY + 设计候选(env-gated,跨杠杆,不碰 kernel)
[ ] ③ preflight + beacon_guard 剪 inert(零 GPU)
[ ] 跑分前:Gate0 环境健康预检(sandbox/docker/依赖/verifier 可用),脏环境先修再跑
[ ] 每个候选 Ci:④ apply+记账 → ⑤a anchor K=1(三档宽进)
[ ]   ├ 差到离谱 → 淘汰 Ci,记 pruned_at_screen,跳全集("anchor 完就回")
[ ]   └ 明显超/略低/跨噪声 → ⑤b 全集 K=3 → ⑥ Gate0/Gate-f(infra失败补跑→剔除,不计0)/Gate-b/配对 lift
[ ] ⑦ promotion(K=3 均值 pass@1>van)→ bank 最好的当 parent;全挂则沿用旧 parent;留痕三层 + 节点状态标
[ ] test = sealed:每轮脚本跑+存到决策不可见处(withheld),只用 train 做决策
[ ] 若要 credited:配对 2σ(per-task 实算 σ)
[ ] 终止检查:连续 10 轮无候选 train>vanilla,或轮数达 20 → 停(绝不看 test 决定停)
[ ] 进化结束 unseal:画 train/test 曲线 + 选 test 最高轮 + 报 retention rate=test lift/train lift
```

---

## 6. 定稿来源(每步依据)
> ⚠ **下列 paper 设计文档均在论文仓 `docs/specs/`**(2026-06-26 迁出 harness 仓);本 SOP 留 harness 仓。
- 冷启动/失败签名/200阈值/failure_map:`2026-06-24-actual-runs-vs-paper-design-gap.md` §3b、failure_map 节
- 三层/anchor/借账/N*/配对2σ:`2026-06-16-sampling-and-decision-design.md` + 差距文档 显著性口径节
- 两消融(anchor/借账):差距文档 §3c
- 贡献 C1/C2/C3 + 措辞红线:`2026-06-15-paper-positioning.md` §4c
- 三盾/Gate 定义:`2026-06-12-gsme-theorems.md`(Gate↔(b,f))、`2026-06-15-evolver-code-architecture.md`

## 7. 当前状态(诚实标注,2026-06-24)
- 实跑 = run_round MVP + scripts + Claude-in-loop,**非全确定性漏斗**;②3×3广度/③preflight/⑤借账 多缺或缓。
- 头号待办(非 SOP 内,是主实验):把 LiveCode 改成 **GSME vs task-aware 两臂 + 报 retention**(C3 主结果)。
- TB2 数(van 44%/harness 45.3%)有 infra 问题、重跑中,结论待复核。

## 8. Claude Code ↔ 准备代码 配合契约(谁做哪步 / 调哪个代码)

### §8.0 总原则:语义靠 Claude Code,确定性算术靠准备的代码
- **语义操作 = Claude Code**:读轨迹诊断、选 WHY、设计 patch、写代码、判"探索没到位 vs 天花板"。
- **确定性/算术 = 准备的代码**:采样挑题、借账折价、记账、算 σ、gate 判定、聚合。
- **为什么 anchor 挑选 / 借账 必须用代码、不让 Claude Code 临场算**(本契约的核心):
  1. **可复现 / 可审计**(论文命根子):挑哪 15 道、借账折几折、配对 2σ 算多少 —— 必须确定性脚本算、可核查。
     让 Claude 临场"凭感觉挑题、估个折价" = 不可复现、不可审计 = 正是我们批 HarnessX 的"诊断 ad-hoc"坑。
  2. **LLM 算不准**:伯努利方差排序、ancestry kernel 折价、配对 σ 实算 = 精确数值,心算必出错 → 交代码。
- **Claude Code 在这些步的角色 = 调用代码 + 读输出做下一步语义决策**,不是自己算。

### §8.1 逐步分工表(映射 §2 七步)
| §2 步 | Claude Code 做 | 准备代码(确定性) | 数据交接 |
|---|---|---|---|
| ①诊断 | **读轨迹、判 WHY/severity/WHERE-hint**(语义) | 失败签名预提取 `analysis/proxy_features.py`;活地图 `analysis/failure_map_builder.py` | 代码吐 signature+failure_map.json → Claude 读后写诊断 |
| ②选WHY+设计+写码 | **全是 Claude**(选靶、设计 patch、写 patch 代码) | — | Claude 产出 patch 工件(WHERE 从工件机械绑定) |
| ③preflight 剪枝 | 读剪枝结论决定丢不丢 | **`activation/preflight.py`**(扫历史轨迹算可达性,零 GPU) | 代码吐 reachable? → Claude 决策 |
| ④apply 建节点 | 触发 | `tree/*`(建节点 + git + 记账,确定性) | 代码建节点、回 node_id |
| **⑤a anchor 挑选** | 触发 + 读子集 | **`analysis/stability_bucket.py`(分桶)+`scheduler/bandit_tasks.py`(方差排序)+`scheduler/affinity_picker.py`(机制fire题)** | 代码吐 anchor 题号清单(⊂train) |
| **⑤ 借账** | 触发 | **`scheduler/tree_aware_bandit.py`**(ancestry kernel 折价 + 加权后验,纯数学) | 代码吐"哪些题免跑/折几折" |
| ⑤b 全集 confirm | 触发 + 读结果 | 跑全集 + `scripts/aggregate_keq3.py`(K=3 聚合) | 代码吐 per-task + pass@1 |
| ⑥Gate0/f/b/2 | 读 gate 结论 | **Gate-f `scripts/gate0_ctrf_audit.py`(→外部评测引擎)/ Gate-b `activation/gate_audit.py` / Gate2 配对σ `analysis/paired_significance.py`(通用,6 benchmark 共用)** | 代码吐 pass/fail + σ → Claude 决策 |
| ⑦选 parent | **判定阈值=代码;"探索没到位 vs 天花板"=Claude** | bank 比较(确定性) | 代码吐 promotion 判定;Claude 做终止语义判断 |

### §8.2 代码盘点:零件齐 vs 缺
- **✅ 齐的零件**(已核,仓库在):preflight、anchor 三件套、借账、Gate-f/b、配对σ+retention(`analysis/paired_significance.py`,通用、6 benchmark 共用)、K=3 聚合、failure_map builder、失败签名。
- **✅ `select_anchor()` 已建**(2026-06-24,branch `feat/evolver_select_anchor` 基于 C0 基线 `evolver/qwen36-r1`,**未合**):
  `evolver/scheduler/anchor_selection.py` —— 一次返回 `AnchorSelection(task_ids, σ_screen, cull_threshold=1.5·σ_screen, tasks, shortfalls)`。
  纯读厚账(只依赖 `stability_bucket.compute_stability`):三档=哨兵(STABLE_PASS)+破冰(STABLE_FAIL,affinity 可选优先)+
  边缘(BORDERLINE_*,按 `p(1−p)` 排);σ_screen=`sqrt((1/n²)Σ p_i(1−p_i))` 由边缘题主导;确定性(tie 按 task_id)。
  单测 5/5 + 同子系统 40 既有测试不破。**待办**:① box 上对 train53 厚账实跑出真 σ_screen ② commit/push/合入 C0。
- **⏸ sealed-test runner —— 暂不建(2026-06-24 user 决定:靠机制怎么做还没想好,过渡期靠 Claude 自觉/纪律)**:
  - **过渡纪律(就是"自觉"的具体内容,可勾)**:进化中任一轮的任何决策(诊断/选WHY/设计/选parent/早停)
    **绝不读 test 的分数或轨迹**;test 评分跑完只存盘、**不在决策上下文里打开**;只在进化全部结束 unseal 时才看。
  - **诚实后果(写论文时必须处理)**:这是 **discipline-based sealed,非机制隔离**;§0 自己写过"靠人忍住审稿人会质疑"。
    交 paper 前二选一:① 补 sealed-test runner(机制隔离);② 在 limitations 诚实披露 test 由纪律密封 + 给可审计证据
    (如每轮 test 评分文件的时间戳早于该轮决策留痕)。**当下接受 ①未做,挂账。**
- **✅ 原列"还缺的两项其实已在论文仓做好**(2026-07-01 核实,取代旧 round7_paired):
  1. **retention rate 计算** —— `evolver/analysis/paired_significance.py`(输出 retention）。
  2. **通用配对σ** —— 同文件:per-task 配对 z=Δ̄/(σ_paired/√n),credited iff |z|≥1.96,+ p / 95%CI / bootstrap;6 benchmark 共用(由论文仓 e0 移入 harness 仓,MR `feature/evolver_loop`)。
  注:**gate 消融(paper §15,比较 Δ>0/单跑/paired-2σ 三种门)不是循环步骤**,是一次性方法学对照,留 论文仓 `analysis/e6_gate_ablation.py`,不入 evolver。

### §8.3 接口形态 & 集成时机(取决于规模)
- **现状 = 手工编排**:Claude(人)手工跑上述脚本、读输出、串起一轮。零件在、但"Claude Code 自动调代码跑完一轮"的集成没做。
- **小规模(TB2 89 / LiveCode)**:题少、全看/全跑得起,anchor/借账用得少 → **手工编排 + 偶尔调脚本够用**,不必做重集成。
- **大规模(SWE-bench 500)**:anchor/借账才真正必需且高频 → **那时值得把这些包成 Claude Code 一键调的工具**(CLI 子命令 / MCP tool),Claude Code 决策时直接调。
- **一句话**:anchor/借账 = 确定性算术,由代码算、Claude Code 调用(不临场算,否则不可审计+算不准);集成成"一键工具"是大 benchmark 才做的事,小规模手工编排即可。

---

## 9. 起跑前置依赖(这份文件**没**覆盖的 —— 诚实边界)

**本 SOP = 方法规格书(怎么走/什么口径/为什么不能偷看 test/何时停),不是 turnkey runbook。** 新窗口照它能独立
理解并执行循环逻辑,但要真跑出分,还需下列外部依赖。两条线的 benchmark/infra 接线已各有权威 runbook:

### §9.1 benchmark / infra 接线(权威 runbook,本 SOP 只给指针)
| | TB2 | LiveCode (EvoAgentBench) |
|---|---|---|
| **照着跑的 runbook** | TB2 跑分 runbook(上游 `docs/specs/2026-06-24-tb2-scoring-runbook.md`) | box `.../evoagentbench-lcb/RUNBOOK_LIVECODE.md`;入口:上游 handoffs `2026-06-24-evoagentbench-livecode-self-evolution-record.md` |
| box | `<内部机器,已脱敏>` | 同 box |
| 部署 | `<内部路径,已脱敏>` | `<内部共享盘,已脱敏>` |
| 模型/端点 | qwen3.6-27B,Volc 端点(轮换,见 memory `reference_volc_model_endpoints`,须入 no_proxy) | 同模型,`NO_PROXY=*` 直连 |
| split | `s_tb2_train53.txt` / `s_tb2_heldout36.txt`(sealed) | `splits/livecode_task_split.json` train97/test39 |
| 跑法 | `TB2_EXTRA_COMPOSE=…d0fast.yaml CONC=45 ATT=3 bash q36r1b_arm.sh <ARM> <绝对tasklist> <SUFFIX>` | `framework/.venv/bin/python src/run.py --config <cfg> --split <train\|test> --parallel N --job <name>` |
| vanilla 锚 | `q36r1b_van_d0base` pass@1 0.440 | C0 train 64.6 / test 58.1 |
| 判分 | `result.json` reward==1.0,排除 exception_info(timeout-fair) | 官方 `lcb_runner` check_correctness |

### §9.2 还没建的代码(= §8.2;影响"方法学干净度",非"能不能跑")
- ★ **最尖锐**:**sealed-test runner 仍未建** —— 两条线现在的 sealed **靠纪律**(memory feedback 约束只读 train),
  **不是机制隔离**;§0 自己判定"靠人忍住不算 sealed,审稿人会质疑"。要真 sealed 必须建"脚本跑 test+存到 Claude 读不到处"。
- `select_anchor()`(含 σ_screen)**已建且已合**(`evolver/scheduler/anchor_selection.py`,MR `feature/evolver_loop`);retention 计算 + 通用配对σ 也已合入 `evolver/analysis/paired_significance.py`(gate 消融 e6 是 paper 分析,不入循环,留论文仓)。§9.2 现在唯一真缺的是 sealed-test runner(机制隔离)。

### §9.3 跑前必做(每个 模型×benchmark 一次)
- 冷启动产出 `vanilla 厚账(K=3 全 train)` + `failure_map.json`(§1)。**当前状态(2026-06-24)**:
  - **厚账 ✅ 两线都有** —— TB2 `q36r1b_van_d0base`(full-89 K=3,train53 = 筛子集) / LiveCode C0 K=3 train97。
  - **结构化 failure_map.json ❌ 两线都没产出** —— 现是 ad-hoc/签名分类诊断,未落 §1 的 `{WHY,题,WHERE-hint}` json。
    补法便宜:**不必重跑分**,CC SDK 扫现有 vanilla 失败轨迹一遍即吐(原料在 box)。换模型/benchmark 时厚账才要重跑。
- 环境健康预检(§0 Gate0):smoke 1 题验端点(~10k token=健康)、sandbox/docker/verifier 可用。

### §9.4 readiness 判定(诚实)
- **能独立跑分** ✅:两 benchmark 接线齐、split 已切、vanilla 锚在。
- **能跑方法学干净的 C3** ⚠️:sealed-test runner **暂不建**(2026-06-24 user 决定),过渡靠 Claude 自觉/纪律;
  这是 discipline-based sealed,交 paper 前须补机制或在 limitations 诚实披露(挂账,见 §8.2 ⏸)。
- **实践印证**(LiveCode 记录 §8):循环 = **agent 驱动、无状态机代码、仅批量跑分用脚本** —— 与 §8.3"小 benchmark 手工编排够用"一致。
