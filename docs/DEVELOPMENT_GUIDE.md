# wb_ops · 开发要求与代码格式规范（DEVELOPMENT_GUIDE.md）

> 本文档面向所有参与 `wb_ops` 项目的开发者、维护者及 AI 编码助手，明确系统未来的**架构设计准则、代码编写规范、文件体积控制、并发安全要求与标准化扩展流程**。
> 所有代码提交必须严格遵循本文档约定，保持系统整洁高可用与高可维护性。
>
> 🔎 **写代码前先查 [REUSE_GUIDE.md](REUSE_GUIDE.md)**：现有依赖/模块/函数速查表、可抄代码模板、复用铁律、按需测试速查——避免通读源码找轮子、避免另立风格。

---

## 一、架构分层与依赖规范

本项目遵循**轻量整洁架构（Lightweight Clean Architecture）**，代码自顶向下划分为 5 个核心层次，各层职责划分与依赖关系必须保持严格单向流动。

```
表现与调度层 (Presentation Layer)
  └── wb.py / wb_ops/cli.py / daily.py / schedule.py
        │ 依赖分发 (Command DTO)
        ▼
业务用例服务层 (Services Layer)
  ├── 业务门面: services/{catalog,discount,order,replicate,support}_svc.py
  └── 领域实现: services/{catalog,discount,order,replicate,support}/*.py
        │ 编排编导
        ▼
仓储与持久化层 (Storage Layer)           外部系统通信适配层 (Adapters Layer)
  ├── storage/mapping_repo.py             ├── adapters/wb_client.py
  └── storage/product_repo.py             ├── adapters/bcs_client.py
        │                                 ├── adapters/mabang_client.py
        │                                 ├── adapters/llm_client.py
        │                                 └── adapters/cookies.py
        ▼                                         │
领域模型层 (Domain Layer) & 基础设施层 (Framework Layer)
  ├── domain/models.py (强类型实体、值对象、枚举)
  └── framework/{safe_io.py, exceptions.py, registry.py}
```

### 1. 严格单向依赖规则
- **高层依赖低层**：上层模块可以调用下层模块，**严禁下层模块反向依赖上层模块**（例如 `domain` 或 `framework` 严禁导入 `services` 或 `adapters`）。
- **同层隔离**：
  - `domain` 保持纯粹，仅包含纯 Python 业务实体、枚举及基础模型，不得依赖网络、文件 IO 或第三方复杂库。
  - `storage` 与 `adapters` 处于同一水平层，负责数据隔离与外部协议防腐，互不强耦合。
- **严禁跨域私有依赖**：
  - 5 大业务领域（`catalog`, `discount`, `order`, `replicate`, `support`）各自高内聚。
  - 跨领域调用**必须**通过目标领域的服务门面（`*_svc.py`）或共享仓储（`Storage`）、适配器（`Adapters`），严禁跨域直接 import 其他领域的私有内部模块实现。
  - *典型反例*：在 `order` 域直接导入 `replicate` 内部的脚本函数；
  - *正确做法*：公共能力下沉至 `framework`、`storage` 或 `adapters`，跨域直接消费下沉模块。

---

## 二、单文件体积控制与拆分原则 (Anti-Monolithic)

为了防止代码重新退化为数千行的“巨石单体”，项目设立严格的单文件体积约束与拆分模式：

### 1. 代码行数硬性限制
- **建议行数**：单个业务脚本代码行数控制在 **300 行以内**。
- **警戒上限**：单个文件**原则上严禁超过 500 行**。
- 一旦单个模块超过 400 行，必须主动进行职责审查并按以下标准模式进行拆分。

### 2. 标准拆分模式
1. **计划与执行解耦（Plan + Executor Pattern）**：
   - 包含写操作的业务，将“状态分析、筛选过滤与计划构造（无副作用）”与“API 分批调用、重试与日志写入（有副作用）”彻底拆离。
   - *标准示范*：`services/replicate/ops_plan.py`（构建 PricePlan/StockPlan） + `services/replicate/ops_executor.py`（执行器与 CSV 记录） + `ops.py`（薄门面编排）。
2. **外部协议与适配层抽离（Adapter Pattern）**：
   - 涉及第三方系统原生 HTTP 交互、请求头构造、会话维持的逻辑，不得内嵌在业务实现中，必须抽取到 `wb_ops/adapters/`。
   - *标准示范*：马帮底层接口抽取为 `adapters/mabang_client.py`；WB 原生接口抽取为 `adapters/wb_client.py`。
3. **数据格式转换与报表生成下沉（Report/Excel Helper）**：
   - 复杂的 Excel 表格生成、多 Sheet 格式化渲染逻辑，从业务核心流程中剥离。
   - *标准示范*：映射总表 8-Sheet 聚合生成抽取为 `services/catalog/mapping_excel.py`。
4. **领域模型与数据清洗提取**：
   - 复杂的卡片字段解析、多版本格式兼容提取为专职解析器。
   - *标准示范*：WB 卡片结构解析抽取为 `services/replicate/wb_card.py`。

---

## 三、类型注解与代码格式规范

### 1. Python 版本与类型注解
- 项目基于 **Python 3.10+**。
- 所有新增或重构的函数、方法、公共类，必须提供完整的 **Type Hints**：
  ```python
  from typing import Optional, Any
  from wb_ops.domain.models import ProductCard, TaskResult

  def calculate_pricing(
      card: ProductCard,
      discount_rate: float,
      target_price: Optional[int] = None,
  ) -> TaskResult:
      ...
  ```
- 容器类型推荐使用内置泛型语法：`list[str]`, `dict[str, Any]`, `set[int]`, `tuple[str, ...]`。

### 2. 数据实体一律使用 `@dataclass`
- 严禁在业务层随意传递松散无约束的 `dict`（极易出现字典键拼写错误或 `KeyError`）。
- 跨层传递的业务对象必须在 `wb_ops.domain.models` 中定义为 `@dataclass`。
  ```python
  from dataclasses import dataclass, field
  from typing import Optional

  @dataclass
  class CustomStockPlan:
      shop_id: int
      vendor_code: str
      warehouse_id: int
      amount: int
      sku: Optional[str] = None
  ```

### 3. Docstring 与命名规范
- **代码命名**：
  - 模块名与文件名：`snake_case.py`
  - 类名：`PascalCase`
  - 函数名与变量名：`snake_case`
  - 全局常量：`UPPER_SNAKE_CASE`
- **注释与文档字符串**：
  - 公共函数统一采用结构化文档说明（包含简述、参数说明、返回值及可能抛出的自定义异常）：
  ```python
  def find_mismatched_products(
      shop_ids: list[int],
      days: int = 1,
  ) -> list[ProductCard]:
      """根据创建时间筛选各店铺可能存在货不对板的在架商品。

      Args:
          shop_ids: 需要检查的目标店铺 ID 列表。
          days: 检查最近几天的上架商品，1 代表仅今天。

      Returns:
          筛选出的待复查商品卡片列表。

      Raises:
          DataConsistencyError: 当快照数据损坏或不可读时抛出。
      """
  ```

---

## 四、异常分层与并发安全规范

### 1. 统一异常分层
- 所有业务异常必须继承自 `wb_ops.framework.exceptions.WbOpsError` 基类。
- 严禁捕获宽泛的 `except Exception:` 后无声吞掉错误（`pass`）。若需容错降级，必须显式记录告警日志并指明原因。
- 异常分类层级：
  - `AuthenticationError`: 凭证失效、Token 过期（401/403）；
  - `RateLimitError`: 平台限流、触发 429 退避；
  - `NetworkError`: HTTP 请求超时或网络断连；
  - `BusinessValidationError`: 业务前置校验不通过（如价格下限拦截、参数非法）；
  - `DataConsistencyError`: 本地状态与数据映射冲突。

### 2. 文件 I/O 与并发安全铁律
- **禁止原始 `open(..., 'w')` 直写核心状态文件**：
  - 本地快照（`data/products/`）、状态池（`data/state/`）、凭证（`credentials.json`）等写操作，**必须统一调用 `wb_ops.framework.safe_io`**：
  ```python
  from wb_ops.framework.safe_io import atomic_dump_json, atomic_write_text

  # 原子覆写（临时文件 + os.replace，保证写中断不损坏原文件）
  atomic_dump_json(file_path, data, indent=2)
  ```
- **跨进程共享资源互斥**：
  - 对全局共享状态（如 `vc_known.json`、`vc_override.json`、映射表聚合）进行写操作时，必须通过 `safe_io.FileLock` 加锁：
  ```python
  from wb_ops.framework.safe_io import FileLock

  with FileLock(state_lock_path, timeout=10.0):
      # 执行安全的读改写操作
      ...
  ```
- **编码与换行**：
  - Windows 控制台输出强制兼容 UTF-8（启动时由 `common.ensure_utf8_stdout()` 初始化）；
  - 导出 CSV 文件一律强制使用 `utf-8-sig` 编码，确保 Excel 打开无乱码。

---

## 五、业务写操作与防风控铁律（核心准则）

1. **dry-run 默认机制**：
   - 凡涉及外部状态修改的命令（改价、改库存、下架、改折扣、改尺寸、上架、清理等），默认必须仅执行 preview/dry-run，显式传入 `--apply` 参数时方可真正调用写接口。
   - 高危不可逆操作（下架、库存清零）必须二次校验 `--yes` 确认标识。
2. **严禁在写操作后擅自执行同步与写后验证**：
   - 写操作成功返回即代表平台侧已经受理生效。
   - 严禁在写操作完成后顺手执行 `fetch` 或 `merge`，或自写脚本调接口轮询验证。全量同步（`fetch`）耗时长且极易触发平台接口限流风控。

---

## 六、新功能扩展标准开发流程（6 步规范）

当为 `wb_ops` 增加新业务或扩展子命令时，必须按照以下标准步骤进行：

```
步骤 1: 领域建模 (Domain)
   └── 在 domain/models.py 中定义所需的数据实体、枚举和状态对象。

步骤 2: 通信适配 (Adapters)
   └── 若涉及新第三方接口，在 adapters/ 下创建或扩展适配客户端，封装底层 HTTP 调用与重试机制。

步骤 3: 状态仓储 (Storage)
   └── 若涉及持久化缓存或本地数据管理，在 storage/ 中实现仓储类与索引方法，接入 safe_io。

步骤 4: 业务用例与拆分 (Services)
   └── 在 services/<domain>/ 下实现业务逻辑。若有写操作，务必拆分为 Plan 构造与 Executor 执行；
   └── 在对应领域门面 services/*_svc.py 中对外暴露规范函数。

步骤 5: CLI 命令注册 (CLI)
   └── 在 wb_ops/cli.py 中通过动态延迟加载机制注册新子命令与参数解析器，并在 wb.py 入口可用。

步骤 6: 自动化测试集成 (Tests)
   └── 在 tests/test_all_commands.py 中添加该子命令的自动化测试用例（默认测试 dry-run 行为），
   └── 在 tests/run_tests.py 的 PATH_HINTS 中登记「改动文件 → 命令」映射，
   └── 执行 python tests/run_tests.py --cmd <新命令>（仅测该新增命令，无需全量测试）通过后即可提交。
```

---

## 七、测试覆盖与质量把关规范

1. **核心铁律：新增或修改功能时，无需进行全量测试，只需测新增或修改的功能**：
   - 严禁在日常开发、新增命令或修复 bug 时盲目执行全量测试（全量测试耗时约 5 分钟且包含真实网络调用）。
   - 任何改动仅需定向测试新增或修改的功能，保证改动范围内的功能逻辑正确即可。

2. **日常测试方法**：
   - **定向测试指定功能（首选推荐）**：新增/修改某命令时，只需定向运行对应命令的用例：
     ```bash
     python tests/run_tests.py --cmd <命令名>
     # 示例：修改了上架相关功能
     python tests/run_tests.py --cmd shelve,shelve-old
     ```
   - **自动探测改动测试（默认行为）**：
     ```bash
     python tests/run_tests.py
     # 或显式：python tests/run_tests.py --changed
     ```
     选择器自动通过 git 探测改动文件，只跑改动模块对应命令的用例；若改动涉及底层通用工具，只做秒级语法与导入冒烟，绝不自动回退全量测试。
   - **最快参数与语法冒烟**（验证命令注册与参数解析无误）：
     ```bash
     python tests/run_tests.py --help-smoke
     ```
   - **预览用例清单（不真正执行）**：
     ```bash
     python tests/run_tests.py --plan
     ```
   - **全量测试（仅限人工显式手动触发）**：
     日常开发和功能改动**严禁跑全量**。仅在重大版本发布且人工显式指定 `--all` 时才使用：
     ```bash
     python tests/run_tests.py --all  # 仅人工特殊场景显式触发
     ```
   - 严禁带失败测试提交代码。
3. **测试安全保护**：
   - 自动化测试中的写操作命令，严禁附带 `--apply`，必须保证测试过程为安全只读（dry-run），绝不污染线上真实店铺数据。
4. **新增命令的同步义务（缺一不可）**：
   - `tests/test_all_commands.py`：命令名加入 `subcommands` 列表、同步 `assertEqual` 计数（当前 42）、新增只读用例；
   - `tests/run_tests.py`：在 `PATH_HINTS` 中补「改动文件 → 命令」映射（否则该文件改动会被判定为全量）；
   - 文档：`docs/CLI.md`、`docs/USAGE.md`、`docs/REUSE_GUIDE.md`、以及各处「42 个命令」计数。
5. **复用优先**：动手写新功能前先查 **[REUSE_GUIDE.md](REUSE_GUIDE.md)**（可用依赖/函数速查、代码模板、决策表），避免重新造轮子或另立代码风格。