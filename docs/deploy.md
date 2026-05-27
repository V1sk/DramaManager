# 部署运维手册

本文面向把这套 HLS 管理服务器部署到一台**内网 Linux 主机**的运维人员，覆盖首次部署、日常运维、升级、备份、救场。所有路径都假设你在仓库根目录下执行命令。

> **前提**：服务必须放在 VPN / 内网。`/api`、`/drm`、`/videos` 全部无鉴权，公网暴露 = 把 DRM key 和视频直接送人。`/admin` 有登录但密码强度由你自己决定。

## 1. 前置条件

| | 版本 / 说明 |
|---|---|
| OS | 任何能跑 docker engine 的 Linux（开发也能在 macOS 上跑） |
| Docker Engine | ≥ 24.x（自带 compose v2 插件，命令是 `docker compose` 不是 `docker-compose`） |
| 磁盘 | OUT_DIR 会越长越大：每集三档切片约 = 源视频 × 1.5。预估按一集 100 MB × 集数 × 2（保留旧版本）。挂大盘更稳。 |
| 端口 | 8000（容器默认监听，可在 `docker-compose.yml` 里改 ports 映射） |
| 网络 | 出站要能访问 OSS / TOS endpoint（启用桶时）+ 业务服务器（启用 sync 时） |

**不需要**在 host 上装 ffmpeg / openssl / xxd / python，全在镜像里。

## 2. 首次部署

```bash
# 1. 拉代码
git clone <repo-url> playerhls && cd playerhls

# 2. 准备环境变量
cp .env.example .env
$EDITOR .env                                   # 至少改 ADMIN_INITIAL_PASSWORD；想保证重启不掉登录就填 SESSION_SECRET_KEY

# 3. 启用桶（可选；用 OSS/TOS 同步到业务服务器才需要）
cp app/storage/credentials.example.py app/storage/credentials.py
$EDITOR app/storage/credentials.py             # 填 AK/SK（按厂商按人分配）
# 然后在 .env 把 STORAGE_PROVIDER 改成 oss 或 tos

# 4. 构建镜像 + 起服务
docker compose up -d --build

# 5. 看日志确认起来了
docker compose logs -f hls
# 期望看到 "Uvicorn running on http://0.0.0.0:8000"

# 6. 浏览器访问 http://<host>:8000/admin
#    - 自动跳到 /login
#    - 用户名 admin，密码 = 你刚填的 ADMIN_INITIAL_PASSWORD
#    - 首次登录强制改密 → 改成长一点的密码
#    - 进入后第一件事：/admin/languages 至少添加一种语言，否则建剧会被卡住
```

> 第一次构建会拉 python:3.11-slim 基础镜像 + apt install ffmpeg + pip install，**~5 分钟**。后续 rebuild 只走 COPY/chmod 层，秒级。

## 3. 日常运维

```bash
docker compose logs -f hls          # 实时日志
docker compose logs --tail=200 hls  # 看近 200 行
docker compose ps                   # 容器状态
docker compose restart hls          # 改 .env 或 credentials.py 不算改代码,restart 即可
docker compose stop hls             # 临时停服(数据保留)
docker compose start hls            # 启回来
docker compose down                 # 停服 + 拆网络(数据卷保留)
```

容器内调试：

```bash
docker compose exec hls bash                              # 进容器 shell
docker compose exec hls python -c "from app import db; print(len(db.list_users()))"
docker compose exec hls ffmpeg -version
```

## 4. 升级

```bash
git pull
docker compose up -d --build       # 走构建缓存,只重打改动的层;然后无停机滚动重启
```

`init_db()` 启动期自动跑 `_migrate_add_columns` / `_migrate_drop_columns`，所有增量 schema 改动（新加列、删旧列）自动应用。**不需要手动 migrate**。

历史上有过几次"销毁性"schema 改动（drama-as-entity 那波），但都集中在重传前完成；当前的演进策略是 additive only。如果未来某次升级文档说要 wipe `hls.db`，按 6.2 节"全量重建"操作。

## 5. 备份与恢复

所有持久化都在 `./data/`：

```
data/
├── hls.db          ← SQLite，账号 + 剧目 + 集状态 + 审计日志
├── hls.db-wal      ← SQLite WAL（运行时存在）
├── hls.db-shm      ← SQLite 共享内存（运行时存在）
├── out/            ← 编码后的 m3u8 + 切片 + DRM key + 封面 + 字幕
└── tmp/            ← 上传暂存（重启时被清，不必备份）
```

**冷备份**（推荐，强一致）：

```bash
docker compose stop hls
tar czf hls-backup-$(date +%Y%m%d-%H%M).tar.gz data/
docker compose start hls
```

**热备份**（不停机；hls.db 用 WAL 模式，热备**不一定一致**，要靠 SQLite 自身的 `.backup`）：

```bash
docker compose exec hls sqlite3 /data/hls.db ".backup /data/hls.db.bak"
tar czf hls-data-$(date +%Y%m%d-%H%M).tar.gz -C data/ hls.db.bak out
```

**恢复**：把备份 tar 解压到一个空的 `./data/`，然后 `docker compose up -d`。

`credentials.py` 和 `.env` 不在备份范围（它们在仓库根目录），单独保管。`.gitignore` 已经覆盖 `data/` 和 `.env`。

## 6. 救场操作

### 6.1 admin 忘了密码

```bash
docker compose exec hls python scripts/reset_admin_password.py NEW_PASSWORD --db /data/hls.db
```

下次登录用 `NEW_PASSWORD`，会被强制再改一遍密（脚本设了 `must_change_pw=1`，临时密码不会留在 shell 历史里）。

如果连 `admin` 这个用户名都被删了：

```bash
docker compose exec hls sqlite3 /data/hls.db "DELETE FROM users"
# 在 .env 把 ADMIN_INITIAL_PASSWORD 改成你想要的初始密码
docker compose restart hls
# init_db() 会重新走 bootstrap 创建 admin 账号
```

### 6.2 全量重建（hls.db 完全坏掉 / 想清零测试）

```bash
docker compose down
mv data/ data-broken-$(date +%Y%m%d)/      # 别直接 rm,先挪开留个证据
docker compose up -d
# 首次启动会重新走 bootstrap
```

桶里的对象不会被自动清。如果你打算彻底废弃，去 OSS / TOS 控制台手工删 `Drama/staging` 和 `Drama/prod` 前缀；或者跑 `scripts/migrate_to_oss.py`（幂等，重新发布到当前选中的桶）。

### 6.3 桶密钥（AK/SK）轮换

`credentials.py` 是**打进镜像**的（内部部署，AK/SK 不外推；见 CLAUDE.md 部署小节）：

```bash
$EDITOR app/storage/credentials.py
docker compose up -d --build       # rebuild → 新镜像带新密钥 → 滚动重启
```

### 6.4 业务服务器 sync 重试

某集同步失败（红色 `sync_failed` 徽章）→ 操作员到 `/admin/sync` 点对应集的"重试"按钮即可，不需要进 shell。如果整个 `BUSINESS_SYNC_BASE_URL` 改了，改 `.env` 然后 `docker compose restart hls`。

### 6.5 sessions 都掉了

如果你没设 `SESSION_SECRET_KEY`，每次重启都会生成新的临时 key，所有人要重登录。日志里第一行会有 `WARNING [admin-accounts-auth]: SESSION_SECRET_KEY 未设置`。生产部署填一个：

```bash
echo "SESSION_SECRET_KEY=$(openssl rand -hex 32)" >> .env
docker compose restart hls
```

## 7. 卷与磁盘

`./data/out` 会越长越大。每次重传 episode 都会**新增一份切片**（旧版本不自动删，保护已缓存的客户端不出现 AES 解密乱码）。两种处置思路：

1. **大盘挂在 `./data/`**：买够大的盘，挂载 / symlink `./data/` 到上面。
2. **定期 GC 旧版本**：删除 episode 时会一次性扫掉**所有版本**的本地目录 + bucket 前缀（按 `episode_uploads` 表列举）。只想瘦身但保留 episode，目前没有现成脚本，需要手工进容器删 `out/{slug}/ep-{n}-v*/`（保留当前版本即可），同时去 bucket 控制台删对应 `Drama/staging/{slug}/ep-{n}-v*/` 前缀。

bucket 的 `Drama/prod/` 是业务服务器读的；删之前确认业务侧已经不再引用该版本。

## 8. 配置参考

完整字段见 `CLAUDE.md` 顶部的环境变量表 + `.env.example`。下面只列"上线前必看"的几个：

| env | 默认 | 上线前是否要改 |
|---|---|---|
| `SESSION_SECRET_KEY` | 自动生成（重启会让所有人掉登录） | **强烈建议设**（`openssl rand -hex 32`） |
| `ADMIN_INITIAL_PASSWORD` | _unset_，首次启动 fail-fast | **首次部署必须填**；admin 登录改密后可留空/不填 |
| `STORAGE_PROVIDER` | `tos`（见 `app/config.py`） | 内部预览不接桶 → 改 `none`；按厂商选 `oss` / `tos` |
| `BUSINESS_SYNC_BASE_URL` | `http://127.0.0.1:9000`（见 `app/config.py`） | 没业务服务器 → 设空串关掉同步功能 |
| `BUSINESS_SYNC_API_KEY` | `demo-secret-key`（见 `app/config.py`） | 上线一定要改 |
| `PIPELINE_CONCURRENCY` | 2 | 看 host 的 CPU 核数，多核可调大 |

## 9. 常见问题

**Q: `docker compose up` 报 8000 端口被占？**
A: `docker-compose.yml` 改 `ports: ["18000:8000"]` 之类。

**Q: 上传视频后一直 `status=pending` 不动？**
A: 看 `docker compose logs -f hls`，正常会看到 `pipeline worker 0/1 started` 和 `encoding start slug=...`。`pending` 不动通常是 worker 进不去（队列锁 bug、ffmpeg 缺失、磁盘满）。先 `df -h ./data/` 看盘是不是满了。

**Q: 加密集播不出来 / 客户端报解码错误？**
A: 99% 是 m3u8 / 切片 / DRM key 路径错配。重传过的集要确认客户端拿到最新的 EpisodeInfo（API 返的 `videoTracks[].url` 应该带 `-v{V}` 后缀；详见 CLAUDE.md "reupload-versioning" 段）。

**Q: `/admin/sync` 一直 503？**
A: `.env` 里 `BUSINESS_SYNC_BASE_URL` 没设或为空，sync 功能被关闭。补上 `BUSINESS_SYNC_BASE_URL` + `BUSINESS_SYNC_API_KEY` 后重启。

**Q: pip install / apt install 在国内网络慢？**
A: 在 Dockerfile 里加镜像源（pypi: 加 `-i https://pypi.tuna.tsinghua.edu.cn/simple`；apt: 改 `sources.list` 到清华/阿里源）。镜像构建慢只影响 rebuild 一次，不影响运行。

**Q: 升级镜像后容器起不来？**
A: 先 `docker compose logs --tail=50 hls` 看错误。最常见是 schema 迁移失败 → 备份 `data/hls.db` 后按 6.2 节全量重建测一下，能起来再人工合并旧数据。

---

**变更历史**：本文档跟 `CLAUDE.md` 部署小节配套维护。深度细节（pipeline 各阶段、SDK 契约、sync 协议）看 `CLAUDE.md`；操作员日常只看本文即可。
