# Vendored 仓库清单

本项目在 `vendor/` 下复用的外部开源代码登记表。所有 clone 都走 `--depth 1` + 固定 SHA，便于复现/审计。

新增规则见 `.claude/skills/github-reuse/SKILL.md`。

| 仓库 | 本地路径 | Commit SHA | 平台 | 用途 | License | 关键入口 | 迁入日期 |
|---|---|---|---|---|---|---|---|
| [Usagi-org/ai-goofish-monitor](https://github.com/Usagi-org/ai-goofish-monitor) | `vendor/ai-goofish-monitor` | `9923efac1ee2` | 闲鱼 | 架构参考（耦合 AI/WebUI，不直接抽函数） | MIT | `src/scraper.py::scrape_xianyu` | 2026-04-18 |
| [superboyyy/xianyu_spider](https://github.com/superboyyy/xianyu_spider) | `vendor/superboyyy-xianyu-spider` | `4cf59de2a744` | 闲鱼 | **主**：FastAPI + Playwright，POST /search/ 入口最适合复用 | MIT | `spider.py` | 2026-04-18 |
| [zhinianboke/xianyu-auto-reply](https://github.com/zhinianboke/xianyu-auto-reply) | `vendor/zhinianboke-xianyu-auto-reply` | `e85d74ace7c6` | 闲鱼 | sign 算法 + 商品搜索备选 | 仅学习 | `utils/xianyu_utils.py`、`utils/item_search.py` | 2026-04-18 |
| [cclient/tmallSign](https://github.com/cclient/tmallSign) | `vendor/cclient-tmallSign` | `505bbfa432cc` | 淘宝 | 淘宝/天猫 mtop sign 算法（不需 appkey） | Apache-2.0 | `app.js`（Express HTTP 服务） | 2026-04-18 |
| [xinlingqudongX/TSDK](https://github.com/xinlingqudongX/TSDK) | `vendor/xinlingqudongX-TSDK` | `e201ad2fc578` | 淘宝 | mtop 搜索历史参考（**已归档 2026-04-03**，作为快照） | 无 | `TSDK/` 模块 | 2026-04-18 |

**总占用**：约 12 MB（其中 `xianyu-auto-reply` 11 MB，含 frontend 资源）

---

## 平台 → vendored 仓库映射（速查）

| 平台 | 主参考 | 备选 | 仅参考 |
|---|---|---|---|
| 闲鱼 | superboyyy-xianyu-spider | zhinianboke-xianyu-auto-reply | ai-goofish-monitor |
| 京东 | （无活跃开源，自写） | — | — |
| 淘宝 | （自写为主） | cclient-tmallSign（sign） | xinlingqudongX-TSDK（mtop 接口约定） |

---

## 操作记录

### 2026-04-18 第二批（按 `/github-reuse` 调研结论）

```bash
git clone --depth 1 https://github.com/superboyyy/xianyu_spider.git vendor/superboyyy-xianyu-spider
git clone --depth 1 https://github.com/zhinianboke/xianyu-auto-reply.git vendor/zhinianboke-xianyu-auto-reply
git clone --depth 1 https://github.com/cclient/tmallSign.git vendor/cclient-tmallSign
git clone --depth 1 https://github.com/xinlingqudongX/TSDK.git vendor/xinlingqudongX-TSDK
```

### 2026-04-18 首批

```bash
git clone --depth 1 https://github.com/Usagi-org/ai-goofish-monitor.git vendor/ai-goofish-monitor
```

---

## 摘代码引用规范（提醒）

把任一函数移植到本项目代码时，**必须**在主代码文件顶部 docstring 写：

```python
"""
本模块部分逻辑参考：
  - superboyyy/xianyu_spider (MIT) —— 闲鱼搜索循环
    commit: 4cf59de2a744  文件: spider.py#L<行号>
"""
```

并在函数级用单行注释指向 vendor 路径：

```python
def search_xianyu(keyword, max_pages):
    # 参考 vendor/superboyyy-xianyu-spider/spider.py::search_items
    ...
```
