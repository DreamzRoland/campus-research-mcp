# 部署指南（公开版）

基线：1.13.0。推荐 Ubuntu 24.04、Python 3.12、systemd、SSH/SCP、出站 HTTPS。
本指南采用通用路径；`research-host` 是管理员在自己的 SSH 配置中设置的别名，
不是实际服务器地址。新主机安装已按源码核对，尚未完成干净环境验收。

## 1. 获取源码与环境

```bash
git clone https://github.com/DreamzRoland/campus-research-mcp.git
cd campus-research-mcp
sudo apt-get update
sudo apt-get install -y python3 python3.12-venv ca-certificates curl unzip
python3 --version
cd server
sudo bash install_remote.sh
```

安装器实际使用 `python3 -m venv`，必须确认 python3 为 3.12。固定依赖来自
requirements.lock.txt；如果索引缺少确切版本，先查明原因，不要自动放宽版本。
服务代码放 `/opt/campus-scopus`，虚拟环境和状态放 `/var/lib/campus-scopus`，
服务账号为 campus-scopus。安装器不安装 Tunnel 客户端或专用代理，也不复制
全部测试模块到生产根目录；保留原源码用于回归。

## 2. Tunnel 与网络

按照 [OpenAI 官方说明](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
创建或复用 Tunnel，确保组织、工作区、操作用户和 Runtime Key 权限正确。
从官方发行来源取得匹配架构的 tunnel-client，核对校验值后安装到
`/usr/local/bin/tunnel-client`。先查看 `help quickstart` 和 `init --help`，
确认客户端与项目 wrapper 参数兼容。

默认 Tunnel 单元设置了 `127.0.0.1:17890` 专用出站代理。若直连 OpenAI，运行
`sudo systemctl edit campus-scopus-tunnel.service` 加入：

```ini
[Service]
UnsetEnvironment=HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
```

再审核默认代理服务 Wants/After 依赖。若使用代理，自行安装和维护 mihomo、
root-only `/etc/campus-scopus/proxy.yaml` 与对应单元，不在 Git 中提交节点配置。
仓库只提供通用代理单元，不提供任何已运行节点。出版社/API 请求保持直接出站。

## 3. 在本机终端录入凭据

```bash
sudo python3 /opt/campus-scopus/write_secrets.py
sudo python3 /opt/campus-scopus/write_research_secrets.py
sudo python3 /opt/campus-scopus/write_retrieval_secrets.py
```

第一项覆盖 Elsevier 和 Tunnel，第二项覆盖 Springer/WoS，第三项覆盖可选检索来源。
回车保持/跳过以提示为准。密钥只隐藏输入，不放入命令参数、环境截图或聊天。
Wiley 使用 write_wiley_secret.py，但该脚本会立即重启 Tunnel，只在确认无活跃任务
的维护窗口运行。其他密钥变更也需要适当重启服务以重新装载 systemd 凭据。

```bash
sudo stat -c '%a %U:%G %n' /etc/campus-scopus /etc/campus-scopus/secrets.json
```

预期目录 700、文件 600、root:root。不输出文件内容或完整进程环境。

## 4. 首次启动

直连方案在配置 drop-in 后：

```bash
sudo systemctl daemon-reload
sudo python3 /opt/campus-scopus/configure_tunnel.py
sudo systemctl enable --now campus-research-worker.service
sudo systemd-run --quiet --wait --pipe --collect \
 -p User=campus-scopus -p Group=campus-scopus \
 -p Environment=HOME=/var/lib/campus-scopus \
 -p LoadCredential=elsevier.json:/etc/campus-scopus/secrets.json \
 /usr/bin/python3 /opt/campus-scopus/run_tunnel.py --doctor
curl --noproxy '*' -fsS --max-time 10 http://127.0.0.1:8088/readyz
```

默认代理方案可以在专用代理已可用后运行 `sudo bash /opt/campus-scopus/finish_setup.sh`。
该脚本的 doctor 硬编码了代理，因此不直接用于直连方案。其 API 探测允许失败后
继续，必须检查每个来源的实际结果。configure_tunnel.py 使用 init --force，
只用于首次设置或有计划重配，不用于每次升级。

在 ChatGPT 添加私有自定义 MCP server，Connection 选 Tunnel，选择相同 Tunnel。
本服务没有另设 OAuth，当前私有配置使用 No authentication；控制平面仍需权限。
工具 schema 更新后刷新连接并用新普通 Chat 验证，参阅官方连接文档。

## 5. 检索与下载验收

1. 调用 ping 和 list_research_sources，核实版本与来源配置，不把 Key 存在当作授权。
2. search_literature 指定 sources=openalex,crossref，query=machine learning，
   每来源 2 条。检查 source_status，再用返回 continuation_id 续查原计划。
3. search_theses 测试硕士、博士、未知层次、缺摘要和没有 DOI 的记录。
4. get_literature_record、resolve_fulltext 后按存储 record_id 调用 download_literature。
5. 用 get_research_job(job_id,wait_seconds=20) 轮询同一任务至终态，实体化 ResourceLink。
6. 核对原始文件、页数、长度、身份和 SHA-256。只有宿主 Space 中实际存在且可读，
   才确认保存成功。卡片、上传回执和服务端完成不单独作为持久保存证明。

旧 AIP 任务使用 get_aip_download，ASME/ACS/MDPI 使用 get_publisher_download。
三个任务接口不能互换。同步旧下载各有时限；30 分钟后台任务不是全部工具的保证。

## 6. 任务、缓存与大文件

新 worker 与 Tunnel 分离：最多 2 个搜索、2 个下载、1 个仓储采集。SQLite WAL
保存进度，搜索按检查点恢复；新 PDF 下载中断后默认重新开始。旧出版社任务虽能
跨 MCP 会话继续，但仍在 Tunnel systemd cgroup，重启服务可能终止它们。

普通文件缓存通常 24 小时。新学位文件每个最多 100 MiB，新缓存总额 2 GiB；
文章仍为 20 MiB。原生附件代码阈值 20 MiB 不是宿主或 Tunnel 传输保证。
超过阈值或实体化失败时使用任务的 file_id：

```bash
sudo python3 /opt/campus-scopus/export_research_file.py FILE_ID
```

脚本当前按 admin 用户和 research-host 示例别名生成私有 SCP 命令；不同管理员
账号需审查并修改后使用。下载后核对 SHA-256，再按单文件命令清理。不开放公网端口。
取消新任务用 cancel_research_job；取消为协作式，不承诺瞬时打断阻塞读。

## 7. 更新与回滚

公开仓库不附带针对私有安装的历史窄范围补丁脚本。不得把 install_remote.sh
当作无中断滚动升级器。生产升级应：保存源码哈希与配置；备份 SQLite、代码和
systemd 单元；暂停入口；核查新旧活跃任务；等旧下载结束；执行审查过的文件清单
与版本化迁移；显式更新/重启 worker 与 Tunnel；检查 ready、工具发现和真实请求。

scripts/deployment_guard.py 提供暂停入口与部署锁的底层保护，但不替你检查活跃
任务、备份数据库、编写迁移或回滚命令。不要将任意命令套在 guard 内就宣称安全。

SQLite 备份使用 Python sqlite3.Connection.backup，不只复制活库主文件而忽略 WAL。
秘密备份置 root-only 目录，不能进入发布包。故障先保留证据，再恢复相互兼容的
代码、配置和必要的数据库版本。普通代码补丁不应无故回退检索历史。

## 8. 离线测试与排错

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r server/requirements.lock.txt
bash scripts/test_offline.sh
```

使用已安装虚拟环境时可设置 CAMPUS_TEST_PYTHON 为其 Python 路径。
离线测试无需真实 Key。联网测试只能在自己的授权环境中显式执行。

| 现象 | 处理 |
| --- | --- |
| 401/403 | 核对 Key 激活、API 权限和机构范围；不输出完整请求 URL |
| 429 | 保留退避，减少请求，不把限流说成零命中 |
| ready 失败 | 检查 Tunnel 服务、凭据、代理方式和官方客户端兼容性 |
| queued 无进度 | 检查 worker 服务和最近心跳，不反复创建同一任务 |
| 工具仍旧 | 核对实际代码与版本、刷新连接、新开普通 Chat |
| 单页 PDF | 可能为封面/预览；不宣称完整全文 |
| XML 无图片 | 使用对应 bundle；图像地址不等于原图文件 |
| Space 无文件 | 宿主独立搜索/打开/下载核验，不只看成功文案 |
| 桌面按钮不响应 | 宿主可能缺保存接口，使用原生附件或 SSH |

## 9. 可选最后备份

仓库不含启用策略，默认关闭。需要时由管理员创建非秘密
`/opt/campus-scopus/download-policy.json`，键 scihub_final_fallback 为布尔值，
或用 CAMPUS_SCIHUB_FALLBACK=0/1 环境覆盖。它只影响统一非学位 DOI 下载，
正常路线失败后才尝试；取消、超限、配额、安全拒绝和预算耗尽必须停止。
真实挑战不自动处理。此功能不是机构授权证明，也不保证持续可用。
