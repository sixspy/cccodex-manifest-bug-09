# cc-manifest-lab

CC_Codex 二期（26.11）**数据工程**产品线：**湖区落地清单台**（Litestar + Preact CDN + SQLite）。

## 一句话骨架（自有，非外仓抄题）

**湖区落地批次 → 对象指纹登记 → 清单闭合 → 可见性对账**

与现有七线正交（不是采集目录、ETL 管道、同步源、解析入库、编排流、种子摄取、留存裁撤）。
不碰外仓常见指纹（Feed 归并 / CDC 游标 / 质检签发 / Slicegate 类仓名）。

## 领域

- **所属领域**：数据工程
- **子方向**：数仓与数据湖（落地清单 / 对象登记闭环）

## 技术栈

| 层 | 选型 |
|----|------|
| 后端 | Python 3.12、Litestar、Uvicorn、SQLite |
| 前端 | 原生 JS SPA（全中文，侧栏），靛青竖纹 + 亮青强调；不依赖外网 CDN |
| 测试 | pytest + httpx（本地秒级闭环，优先于 compose） |

## 基线已通主链

1. 湖区落地批次列表（收集中 / 已闭合）
2. 向批次登记对象键与内容指纹
3. 闭合清单（对象数固化，拒绝空批）
4. 可见性对账报告（闭合批 vs 开放批 vs 孤儿对象）

## 闭合批拒登护栏（manifest-bug-09）

- **读态口径（已拍板，全链路唯一）**：提交瞬间读库内最新批次态；不使用开页快照态。
- 登记与闭合共用 `BEGIN IMMEDIATE` 立即写锁，锁内同事务读态一次拦截：已闭合/已作废批登记返回 409，带 `reason`（`BATCH_CLOSED` / `BATCH_VOID`）与 `read_source`。
- SQLite 触发器 `trg_landing_objects_gate` 存储层兜底，绕开应用层直插同样拒写。
- 批次卡片、登记详情、对象列表、去重扫库台四处同拒因同短句展示。
- 去重扫库台：清洗入库只认“收集中”批内“已登记”行，脏行同因拒收，同内容指纹跨批聚组去重；复跑不会再吃到新脏行。
- 复现单：`python -I scripts/repro_closed_batch.py`，说明见 `docs/repro_closed_batch.md`。

预留切口（未做，供后续功能题）：清单换版生效窗、闭合作废回开放、批量失败闭互斥、跨区依赖冻结、清单回灌等。

## 本地（推荐反馈环）

```powershell
cd claude-workspace\cc-manifest-lab
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
python run.py
```

http://127.0.0.1:6900/login · **admin / 123456**

## Docker

```powershell
docker compose up -d --build
```

端口 **6900**。冒烟：`python ..\scripts\_smoke_lab_access.py --dir .`
