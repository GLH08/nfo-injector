# 设计文档：nfo-injector 多库自由映射

- 日期：2026-06-21
- 状态：已通过设计评审，待 spec 复审 → 实现计划
- 影响范围：`nfo-injector/`（后端 config/main/file_browser/task_manager + 前端 + docker-compose）

---

## 1. 背景与问题

当前 `nfo-injector` 的路径模型是「**单一 STRM 根 + 单一媒体根**」：

- `AppConfig.strm_root` / `media_root` 各为单个字符串；
- `path_mappings` 只能在**同一个 `media_root` 内部**做子路径前缀重映射（最终路径恒为 `media_root / media_prefix / 子路径`，见 `config.py:resolve_media_path`）。

用户的真实环境是「**多网盘、多库**」：CloudDrive2 把 115、Google Drive、OneDrive 等挂到本地，彼此同级但隶属不同网盘（如 `/apps/clouddrive2/CloudDrive/115open/Media`、`/apps/clouddrive2/CloudDrive/Gdrive/Media`、`/apps/clouddrive2/CloudDrive/Onedrive/...`），此外还有 openlist 挂载的其它项目；对应的本地 STRM 目录可能在本机任意位置。

现有模型无法表达「不同子库指向**完全不同的媒体根**」，因此需要重构为「**任意 STRM 目录 ↔ 任意媒体目录**」的多库自由映射，且配置要尽量简单。

### 关键事实（已与用户确认）

1. 所有 STRM 目录与所有网盘媒体源目录**全部位于 `/apps/` 之下**。
2. 配置数据模型选用「**库列表**」：每个库 = `{名称, STRM目录, 媒体目录}`。
3. FFprobe 探测参数**全局统一**，不做每库覆盖。

---

## 2. 目标 / 非目标

**目标**
- 支持任意数量、互相独立的库，每个库自由指定 STRM 目录与媒体目录（可分属不同网盘/挂载工具）。
- 新增/修改库只在 Web UI 完成，**不需要再改 docker-compose**。
- 保留现有全部功能：四档状态、目录树徽章、批量注入、实时日志、取消、虚拟注入。
- 现有 115 库**零手工配置**自动迁移。

**非目标**
- 不做每库独立的 FFprobe 参数覆盖（全局统一）。
- 不改动 ffprobe 探测本身（盲猜后缀、超时强杀、403/超时重试逻辑保持不变）。
- 不引入数据库；配置仍是 `data/config.json` 单文件。
- 不做用户认证/多用户。

---

## 3. 核心决策与理由

| 决策 | 选择 | 理由 |
|------|------|------|
| 容器挂载 | 单条 `/apps:/apps`（rw） | 所有库都在 `/apps` 下；一次挂载，未来新增库无需动 compose；host 路径 == 容器路径，消除心智翻译 |
| 数据模型 | 库列表 `List[Library]` | 直观匹配「我有多个库」；每库自带两根，天然支持指向不同网盘；子路径错位（如 `中转/CN`→`Meta/CN`）通过直接指定两根即可表达 |
| 探测参数 | 全局统一 | 现有重试逻辑已通用处理 403/超时；保守值（并发 2、超时 75s）对各网盘通用；配置最简（YAGNI） |
| API 寻址 | `<库id>/<库内相对路径>` | 不暴露绝对路径；天然隔离各库；前端 path 仍是不透明字符串，改动最小 |

被舍弃的备选：
- **映射规则模型**（保留单浏览根 + 多条「前缀→绝对媒体路径」规则）：不够直观，且库的概念被打散。
- **每库独立 docker 挂载**：每次新增库都要改 compose，违背「简单」。

---

## 4. 详细设计

### 4.1 Docker 挂载（一次性改动）

`nfo-injector/docker-compose.yml`：

```yaml
services:
  nfo-injector:
    # ...
    volumes:
      - /apps:/apps          # 一次挂载，覆盖所有 strm + 所有网盘媒体源（host 路径 == 容器路径）
      - ./data:/app/data     # 配置与日志（不变）
    environment:
      - STRM_ROOT=${STRM_ROOT}    # 透传 .env 真实路径，仅用于首次自动建库
      - MEDIA_ROOT=${MEDIA_ROOT}
      - TZ=Asia/Shanghai
    restart: unless-stopped
```

- 挂载为 **rw**：strm 子树需写 NFO；媒体子树仅读。程序逻辑只会向库的 strm 子树写 NFO，不会写媒体目录。
- **已知取舍**：单条 `/apps:rw` 失去原 `:ro` 对媒体目录的硬保护。换取「一次挂载、未来任意新增」。若需硬保护，可改为 `/apps/clouddrive2:ro` + `/apps/moviepilot:rw` 等多条，但牺牲未来灵活性。**本设计采用单条 `/apps:rw`**。
- `.env` 的 `STRM_ROOT/MEDIA_ROOT` 含义变为「首次自动建库的初始值」，不再驱动卷挂载。

### 4.2 配置数据模型（`backend/config.py`）

```python
class Library(BaseModel):
    id: str            # 稳定短 id（创建时生成，重命名不变），API 与树节点寻址用
    name: str          # 显示名，如 "JP主库"
    strm_path: str     # 绝对容器路径，如 /apps/moviepilot/115strm/Emby
    media_path: str    # 绝对容器路径，如 /apps/clouddrive2/CloudDrive/115open/Media
    enabled: bool = True

class AppConfig(BaseModel):
    libraries: List[Library] = []
    # 全局 FFprobe 参数（原样保留，约束不变）：
    ffprobe_timeout: int = 75
    max_concurrency: int = 2
    guess_extensions: List[str] = [".mp4", ".mkv", ".ts", ".avi", ".iso", ".rmvb", ".flv", ".mpg", ".mpeg"]
    max_retries: int = 3
    retry_delay: float = 2.0
    forbidden_retry_delay: float = 5.0
    exclude_dirs: List[str] = ["trailers", "extrafanart", "behind the scenes", "featurettes"]
    # 删除：strm_root / media_root / path_mappings（被 libraries 取代）
```

- **`id` 生成**：创建库时生成稳定短 id（`uuid4().hex[:8]`）。`id` 与 `name` 解耦——重命名不破坏已有树状态/任务寻址。
- **删除 `PathMapping` 类**及相关字段。

### 4.3 首次自动建库（迁移）

`load_config()` 逻辑增补：
- 若 `config.json` 存在且含 `libraries` → 直接用。
- 若 `config.json` 存在但是**旧格式**（有 `strm_root/media_root`、无 `libraries`）→ 迁移：用旧 `strm_root/media_root` 建一个库；旧 `path_mappings` 每条转成一个独立库（`strm_path = strm_root/strm_prefix`，`media_path = media_root/media_prefix`）。
- 若**无 `config.json`**（当前用户即此情况）→ 用环境变量 `STRM_ROOT/MEDIA_ROOT` 生成单个库 `{name:"主库", strm_path:$STRM_ROOT, media_path:$MEDIA_ROOT, enabled:True}`。

→ 用户现有 115 库零手工配置即在；之后在 UI 加 Gdrive/OneDrive 等。

### 4.4 路径解析（`backend/config.py`）

新增两个函数，替换 `resolve_media_path` 现有实现：

```python
def resolve_library(abs_strm_path: Path, config) -> Optional[Library]:
    """返回 strm_path 为 abs_strm_path 父级（或自身）、且 strm_path 最长的那个 enabled 库。"""

def resolve_media_path(abs_strm_path: Path, config) -> Optional[Path]:
    lib = resolve_library(abs_strm_path, config)
    if lib is None:
        return None
    rel = abs_strm_path.relative_to(Path(lib.strm_path)).with_suffix("")
    return Path(lib.media_path) / rel     # 之后 ffprobe 盲猜后缀，逻辑不变
```

- **最长前缀匹配**：处理库嵌套（如 `Emby` 与 `Emby/中转/CN` 并存时，`中转/CN` 下的文件归 CN 库）。
- 无库匹配 → 返回 `None`，上层报错/跳过。

### 4.5 API 寻址（`backend/main.py` / `file_browser.py`）

所有 API 的 `path` 参数语义从「相对单一根」改为 **`<库id>/<库内相对路径>`**（库根本身的 path 即 `<库id>`）。

新增后端辅助函数（集中拆解 + 越权校验）：

```python
def split_lib_path(path: str, config) -> tuple[Library, Path]:
    """
    path 形如 "<lib_id>/<rel...>" 或 "<lib_id>"。
    返回 (library, abs_strm_path)。
    校验：abs_strm_path 必须位于 library.strm_path 之内（防 ../ 越权）；否则抛 400/404。
    """
```

各端点行为：
- `GET /api/browse?path=`（空）→ 返回**库列表**作为顶层条目（见 4.6）。
- `GET /api/browse?path=<库id>[/...]` → 经 `split_lib_path` 浏览该子目录。
- `GET /api/scan?path=`（空）→ 对所有 **enabled** 库递归扫描并**求和**（全局统计）。
- `GET /api/scan?path=<库id>[/...]` → 扫描该子树。
- `issues / nfo / ffprobe / inject` → 一律走 `split_lib_path`。
- `inject` 任务的 `relative_path` 也变为 `<库id>/<相对>`。

### 4.6 目录浏览（`backend/file_browser.py`）

- 新增 `EntryType.LIBRARY`，仅在根层（`path=""`）返回；每个库一个条目：
  `{name, relative_path: <库id>, entry_type: "library", has_children: True, enabled}`，跳过 `enabled=False` 的库。
- 统一传参方式（不给二选一）：`main.py` 先用 `split_lib_path(path)` 得到 `(library, abs_dir)`，再调用 `browse_directory / scan_directory_recursive / get_strm_files_in_path`，签名统一为 `(abs_dir: Path, lib_id: str, lib_strm_path: Path, exclude_dirs)`。函数内部用 `lib_strm_path` 算库内相对路径，对外返回的 `relative_path` 一律拼成 `<lib_id>/<库内相对>`，保证前端 path 全局唯一。传 `lib_id:str + lib_strm_path:Path` 这两个原始值（而非整个 `Library` 对象），避免 `file_browser` 反向耦合 `config`。
- `exclude_dirs` 仍取全局配置。

### 4.7 任务管理（`backend/task_manager.py`）

- 凡引用 `config.strm_root` / 旧 `resolve_media_path(strm_path, config)` 之处，改走 `split_lib_path` / 新 `resolve_media_path`。
- 收集 STRM 文件：由任务 `relative_path`（`<库id>/<相对>`）经 `split_lib_path` 得到绝对起点，再用 `get_strm_files_in_path` 遍历。
- 处理流程、并发控制、双层取消、进度统计**均不变**。
- 日志中展示路径时，用 `<库id>/<相对>` 或 `库名/<相对>`（择一，便于阅读）。

### 4.8 前端（`frontend/index.html` / `app.js`）

- **左侧树**：根层渲染各库为顶层节点（📚 图标，`entry_type==="library"`）；展开调 `/api/browse?path=<库id>`。懒加载、目录徽章、右键菜单、状态点、搜索等逻辑不变，path 一律带 `<库id>/` 前缀（不透明字符串）。
- **配置弹窗**：删除「路径配置」「路径映射」两个 tab，新增「**媒体库**」tab —— 一个行列表，每行 `{名称 / STRM目录 / 媒体目录 / 启用开关 / 删除}`，底部「+ 添加库」。复用现有 `mapping-rule` 行的交互范式。
- 「FFprobe 参数」tab 不变。
- 保存时提交 `libraries` 数组（含 id；新行无 id 由后端补；空名/空路径行过滤）。
- 中栏详情、右栏任务、WebSocket 日志：**零改动**（仅透传 path）。

---

## 5. 边界情况

- **库嵌套/重叠**：解析按最长前缀匹配，确定无歧义。全局统计（4.5）对各库分别扫描再求和，**若库重叠会重复计数**——约定库之间应互不重叠（推荐），此为已知小限制。
- **越权路径**：`split_lib_path` 校验目标绝对路径必须落在库的 `strm_path` 内，拒绝 `../` 逃逸。
- **路径不属于任何库**：API 返回 4xx（如「该路径不属于任何库」）。
- **enabled=False**：不在树中显示、不计入全局统计、不可处理；保留在配置中便于重新启用（仅在「媒体库」tab 可见）。
- **库 id 冲突/缺失**：保存时后端为无 id 的新库补 id；保证 id 在 libraries 内唯一。
- **重命名库**：`name` 改、`id` 不变 → 已展开的树/进行中的任务寻址不受影响。

---

## 6. 测试

**单元测试**
- `resolve_library` 最长前缀匹配（含嵌套库）。
- `resolve_media_path` 正确拼接、无匹配返回 None。
- `split_lib_path` 正常拆解 + 越权路径拒绝。
- 配置迁移：旧格式（strm_root/media_root/path_mappings）→ libraries；无 config.json + 环境变量 → 单库种子。

**手工验证**
- `docker compose up -d` → 左侧树显示库列表。
- 展开库 → 浏览到 .strm → 「仅探测」成功 → 「注入」单文件 `EMPTY→HEALTHY`。
- 「媒体库」tab 新增第二个库（指向另一网盘目录）→ 保存 → 树出现第二库 → 对其注入成功。
- 顶栏全局统计为各库求和。

---

## 7. 待改文件清单

| 文件 | 改动 |
|------|------|
| `nfo-injector/docker-compose.yml` | 挂载 `/apps:/apps`；环境变量透传 `${STRM_ROOT}/${MEDIA_ROOT}` |
| `nfo-injector/backend/config.py` | `Library` 模型；`AppConfig` 改字段；删 `PathMapping`；`resolve_library`/`resolve_media_path`；首次建库与旧格式迁移 |
| `nfo-injector/backend/main.py` | `split_lib_path` 辅助；browse 根返回库列表；各端点改库寻址；全局 scan 求和；config API 适配新模型 |
| `nfo-injector/backend/file_browser.py` | `EntryType.LIBRARY`；浏览/扫描/收集函数改为按绝对子目录 + 库 strm_path 工作 |
| `nfo-injector/backend/task_manager.py` | 走库解析；`relative_path` 库寻址；流程不变 |
| `nfo-injector/frontend/index.html` | 配置弹窗：删「路径配置/路径映射」，加「媒体库」tab |
| `nfo-injector/frontend/app.js` | 树根渲染库节点；库配置增删 UI；path 带库 id |
| `nfo-injector/.env.example` | 注释说明 `STRM_ROOT/MEDIA_ROOT` 仅用于首次建库 |

---

## 8. 未来可选项（本次不做）

- 每库独立 FFprobe 参数覆盖。
- 媒体目录硬只读（拆分 ro/rw 挂载）。
- 全局统计对重叠库去重。
- 库的拖拽排序 / 分组。
