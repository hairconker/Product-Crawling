# 小白装机问卷 · 微信小程序原型

> 对《docs/小白装机问卷设计稿.md》§十一 ~ §十四 的小程序端落地。
> 逻辑 100% 本地计算，不调用任何 AI。与 `docs/问卷测试页.html` 共享同一套评分规则，改动需两边同步。

## 目录

```
docs/miniapp-prototype/
├── README.md                       本文件
├── data/
│   ├── questions.js                题目 + 评分规则（Q1_GROUPS / QUESTIONS / SIDE_WEIGHTS）
│   ├── configs.js                  预算档位 + 三档配置模板
│   └── samples.js                  典型用户样例（联调时"填样例"用）
├── utils/
│   └── scoring.js                  评分引擎（纯函数，框架无关）
└── pages/diy-quiz/
    ├── index.json                  页面配置
    ├── index.js                    页面逻辑
    ├── index.wxml                  模板
    └── index.wxss                  样式
```

## 植入步骤

1. 把 `data/`、`utils/`、`pages/diy-quiz/` 三个目录**整体**拷到你的小程序根目录下（文件名都是标准的 miniapp 目录，直接合并即可）。
2. 在你的 `app.json` 的 `pages` 数组里追加一行：
   ```json
   {
     "pages": [
       "pages/index/index",
       "pages/diy-quiz/index"
     ]
   }
   ```
3. 跳转到这个页面：
   ```js
   wx.navigateTo({ url: "/pages/diy-quiz/index" });
   ```
4. 打开微信开发者工具预览。首次打开默认展示空问卷——点"填样例"可立刻看到一轮结果。

## 数据流

```
用户填答 → this.data.answers / this.data.sideState
       → scoring.decideAll(answers, sideState)
       → { S, dir, budget, tiers, tips, sideTags }
       → setData({result}) → 结果卡片渲染
```

## 核心数据结构

- `answers`: `{ q2: "q2a", q3: "q3b", ... }` — 单选题答案
- `sideState`: `{ g_work: ["qs_office", ...], g_dev: [...], g_game: [...] }` — Q1 三类内部有序数组，下标即优先级
- `Q1_GROUPS[n].opts[n].s`: 每个选项的加分 `{cpu, gpu, balance, office, upgrade, budget, budgetFloor, peripheral}`
- `SIDE_WEIGHTS = [1.0, 0.6, 0.3, 0.15]` — 类内位次权重

## 在哪里扩展

| 需求 | 改哪 |
|---|---|
| 加/改题 | `data/questions.js` |
| 改三档配置硬件清单 | `data/configs.js` 的 `CONFIG_TEMPLATES` |
| 改预算档边界 | `data/configs.js` 的 `BUDGET_BUCKETS` |
| 改方向判定阈值（目前都是 ±2） | `utils/scoring.js` 的 `decideDirection` |
| 接入爬虫实价替换静态模板 | `data/configs.js` 整体替换为从接口拉数据 |
| 改 UI 样式 | `pages/diy-quiz/index.wxss` |

## 与 HTML 测试页的关系

- `docs/问卷测试页.html` 是纯前端验证工具，用于快速迭代评分规则
- 本原型是小程序实现，逻辑与 HTML 测试页**完全一致**
- 任何评分/判定规则的改动，两边都要同步（源头是 §十一 ~ §十四 设计稿）

## 后续计划

- `data/configs.js` 改成异步从后端拉（让运营同学改模板不用发版）
- 结果页加"加入收藏 / 分享给朋友" 按钮
- 埋点：用户选的 Q1 组合与最终落档方向的分布（用于校准评分规则）
