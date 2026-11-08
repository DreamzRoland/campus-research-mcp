# 校园统一文献 MCP 技术报告

文档日期：2026-11-08。实现基线：1.13.0。目标读者：维护者、部署管理员、适配器开发者及使用该服务开展文献检索的研究人员。

本报告依据当前源码、部署脚本及仓库内验证记录编写。除文末明确注明当日核对的官方文档外，外部渠道状态来自既有验证，**不是 11 月 8 日重新逐站联网测试的结果**。完整安装与运维步骤见[部署指南](DEPLOYMENT-GUIDE.md)。

## 1. 系统定位与交付边界

系统在由部署者管理的校园主机 上运行，向 ChatGPT 的 my Scopus 私有插件提供文献检索、元数据规范化、原始全文下载、离线图文包及原生附件。名称沿用最初的 Scopus 插件，能力已经扩展为多来源统一文献服务。

覆盖期刊论文、会议论文、预印本及硕博学位论文；检索与全文获取分开处理。检索命中、存在 DOI、网页可达、API Key 已填写或学校已订阅，都不足以单独证明能取得全文。某一个非 OA 样本下载成功，也不代表整个平台或所有年份均获授权。

当前实现不是全球文献的完整镜像，不提供任意 URL 下载，不自动采购数据库权限，不把生成摘要充当来源摘要，也不把服务器下载成功描述成已保存到 Space。来源失败会作为覆盖缺口保留。

### 1.1 当前基线

| 项目 | 当前依据 |
| --- | --- |
| 服务版本 | `server/server.py` 中的 1.13.0 |
| MCP 工具 | 40 个，其中 39 个模型可见、1 个组件专用 |
| 运行平台 | Ubuntu、Python 3.12、systemd；生产 MCP SDK 2.3.0 |
| 本地项目 | `<local-checkout>` |
| 生产代码 | `/opt/campus-scopus` |
| 状态与虚拟环境 | `/var/lib/campus-scopus` |
| 入口 | STDIO MCP，经 OpenAI Secure MCP Tunnel 连接宿主 |
| 持久任务 | 独立 `campus-research-worker.service` |
| 最近完整回归 | 277 项 Linux 离线测试通过 |
| 最近安装组合验证 | 125 项出版社模块组合测试通过 |

公开版的本地验收会另列结果；历史生产状态和私有验证 JSON 不随仓库发布。

## 2. 总体架构

```text
ChatGPT / 支持 MCP 的宿主
  │ 工具调用、ResourceLink、resources/read
  ▼
OpenAI 托管 Tunnel 控制平面
  ▲ 校园端主动出站 HTTPS；无公网文件服务
  │
research-host
  ├─ campus-scopus-tunnel.service
  │    └─ run_tunnel.py → 官方 tunnel-client
  │         └─ STDIO → server.py
  │                    ├─ 旧原生检索与出版社模块
  │                    └─ retrieval 统一检索 / 记录 / 任务接口
  ├─ campus-research-worker.service
  │    └─ retrieval.worker → 深度检索 / 新下载 / 仓储采集
  ├─ campus-scopus-proxy.service
  │    └─ 127.0.0.1:17890，仅用于 Tunnel 出站链路
  └─ 私有 SQLite、文件缓存、任务状态与 systemd 凭据

检索 API / 出版社 / 官方仓储 ← 校园主机直接访问
```

SSH 管理链路与 MCP 链路不同。管理员用本机 SSH 别名 research-host 管理主机，既有穿透由 SSH 配置决定；ChatGPT 调用经 Tunnel 到达 STDIO 服务。出版商请求使用校园主机直接出口，不借用 Tunnel 代理作为机构权限来源。

主服务读取 systemd 临时凭据目录后启动官方 Tunnel 客户端。服务端无需公开监听 MCP HTTP 端口。健康检查仅绑定 `127.0.0.1:8088`，代理仅绑定 `127.0.0.1:17890`。

## 3. 代码结构与职责

```text
paper-search-plugin/
├─ README.md
├─ server/
│  ├─ server.py                    MCP 工具、资源、兼容入口
│  ├─ research_sources.py          旧 Springer/WoS/联合检索
│  ├─ ai_search.py                 arXiv、DBLP、20 个会议目录
│  ├─ article_downloads.py         Elsevier 文件、校验、旧缓存、错误类型
│  ├─ article_bundle.py            ScienceDirect XML、HIGHRES 原图、HTML/ZIP
│  ├─ springer_bundle.py           Springer OA XML、图片、HTML/ZIP
│  ├─ crossref_cambridge.py        Crossref 与 Cambridge 校园 PDF
│  ├─ springer_pdf.py              Springer/Nature 校园 PDF
│  ├─ wiley_downloads.py           Wiley TDM PDF
│  ├─ aip_downloads.py / aip_jobs.py
│  ├─ publisher_downloads.py / publisher_jobs.py   ASME、ACS、MDPI
│  ├─ native_file_output.py        旧文件 ResourceLink / resources/read
│  ├─ download_widget.html         兼容组件，不作为所有客户端保存能力保证
│  ├─ retrieval/
│  │  ├─ query.py                 确定性查询解析、编译、筛选
│  │  ├─ providers.py             来源目录、API 适配、规范化、分页
│  │  ├─ models.py                标识、记录、合并、来源证据
│  │  ├─ engine.py                路由、联合检索、RRF、继续检索
│  │  ├─ records.py               单篇补充、引文关系
│  │  ├─ conferences.py           OpenReview 与官方论文集证据
│  │  ├─ repositories.py          OAI-PMH、METS、仓储本地索引
│  │  ├─ transport.py             HTTPS、预算、限流、缓存、错误脱敏
│  │  ├─ store.py                 SQLite WAL 与数据保留
│  │  ├─ jobs.py / worker.py      持久任务、租约、恢复、取消
│  │  ├─ files.py                 新下载、配额、校验、附件、下载路由
│  │  ├─ scihub.py                可配置 DOI 最后备份
│  │  └─ exports.py               JSON/CSV/RIS/BibTeX/报告、引用导入
│  ├─ export_research_file.py      私有大文件 SSH 导出
│  ├─ write_*secrets.py            本机终端隐藏录入凭据
│  ├─ run_tunnel.py / configure_tunnel.py
│  ├─ *.service / install_remote.sh / finish_setup.sh
│  ├─ requirements.lock.txt        固定版本依赖
│  └─ test_*.py / *.cjs            单元、回归及显式联网测试
├─ scripts/
│  ├─ verify_repository.py         清单、语法、凭据模式检查
│  ├─ verify_remote_checkout.sh    隔离 Linux 回归与工具发现
│  ├─ package_verified_checkout.py 白名单源码归档
│  ├─ package_release.py           发布包生成
│  ├─ deployment_guard.py          当前补丁的部署入口暂停保护
│  └─ deploy_scihub_fallback.sh     有明确前提的窄范围修复部署
├─ docs/                          规范、能力矩阵、验证 JSON、历史说明
└─ outputs/                       发布包、测试证据与文档阅读版
```

新检索核心没有整体迁移旧出版社模块。这样保留旧工具参数和既有下载实现，也意味着目前存在两套任务、缓存与超时语义。扩展代码时必须先确定使用的是旧链路还是统一链路。

## 4. 来源能力与实际覆盖

### 4.1 综合、学科与会议来源

| 来源 ID | 用途 | 权限与限制 |
| --- | --- | --- |
| `scopus` | 综合书目、被引次数、原生语法、可用摘要 | 需要 Key；既有实测取得 FULL 原始摘要，权限不足会降级；不等于 ScienceDirect 全文 |
| `wos` | WoS Starter 书目与被引次数 | 需要 Starter Key；不承诺摘要、完整引文网络或正文 |
| `crossref` | DOI、出版元数据、登记摘要及参考文献 | 公共 API；摘要和参考文献依赖出版者实际登记 |
| `openalex` | 综合发现、可用摘要、引文与 OA 位置 | 需要部署者自行配置 Key 并验证分页；额度依账号当前政策 |
| `datacite` | 仓储 DOI、学位及研究成果 | 公共 API；Dissertation 不能直接判定博士 |
| `springer` | Springer Meta 补充检索 | 需要 Meta Key；OA XML 与校园 PDF 是另外的权限/下载路径 |
| `arxiv` | 预印本、摘要、作者、版本与 PDF | 预印本不是顶会录用证据 |
| `dblp` | 20 个会议目录及论文索引 | 无摘要；被 DBLP 收录与已由官方确认出版分别记录 |
| `openreview` | 登记年度的公开录用集合 | 不将全部投稿当录用；受公开 venue/年份/track 覆盖限制 |
| `proceedings` | PMLR、NeurIPS、CVF、ACL 官方出版信息及可用文件 | 部分发现依赖 DBLP 后再查官方页；不是全年度全轨道镜像 |
| `pubmed` / `europepmc` | 生物医学、摘要、PMID/PMCID、开放位置 | 原始全文依 PMC/来源文件实际开放情况 |
| `core` | 仓储元数据、可分类学位论文 | 需要 Key；普通检索使用 outputs，学位检索使用 works |
| `ieee` | IEEE Xplore 元数据和可用摘要 | 需要已激活的独立 API Key；未激活时不能计作可用渠道 |
| `semanticscholar` | 参考文献、施引与相关推荐补充 | 需要部署者配置并验证独立 Key |

CORE 修复说明：outputs 的 `documentType` 是集合，旧过滤方式导致上游内部查询异常并表现为 HTTP 500；学位查询已改用 works 的可过滤类型字段，普通查询继续使用 outputs，work/output 标识空间分开。相关行为由离线回归覆盖，私有测试记录不发布。HTTP 500 曾由调用方式触发，不应简单归为“没有论文”或“用户 Key 无效”。

### 4.2 学位论文渠道

| 渠道 | 当前自动化范围 | 未覆盖部分 |
| --- | --- | --- |
| DataCite / OpenAIRE | 公共学位元数据、摘要及来源给出的文件位置 | 上游缺学位层次、导师或全文时如实缺失 |
| CORE | 类型明确的仓储学位记录 | Key 额度、仓储元数据分类质量及来源全文访问 |
| theses.fr | 法国博士及在研信息、公开元数据 | 默认排除在研记录；非公开文件不强行下载 |
| MIT DSpace | 已验证 OAI-PMH + METS/文件关系，本地 FTS 索引 | 现有证据为采集 100 条，不能宣称已采完整库 |
| Caltech / HKUST | 代码中预留仓储目录 | 探测未通过，未启用；域名白名单存在不代表仓储已上线 |
| 万方 OpenThesis | 适配器已实现 | 需要机构 API 权限，普通网页订阅不能替代 |
| 知网 / 维普 / ProQuest | 官方入口、RIS/BibTeX 导入 | 机构 API、全文通道尚未确认；知网验证页不算检索成功 |
| OATD / NDLTD / EThOS | 官方人工补查入口 | 未验证自动 API，不计作自动检索来源 |

机构仓储采集使用 OAI-PMH 的日期、分页令牌和删除记录语义，再建立 SQLite FTS5 索引。它是可追溯的元数据采集，不是每次主题查询都即时搜索整个远端仓储。单仓储初始最多 5,000 条；覆盖数量、日期和是否完成必须随索引结果解释。

### 4.3 出版社原始文件

| 出版社 | 现有路径 | 文件能力 |
| --- | --- | --- |
| Elsevier / ScienceDirect | API + 当前机构权限 | 原始 PDF/XML；XML+原始 HIGHRES 图片离线包 |
| Springer Nature | OA API 或校园网站授权 | OA JATS XML、图片离线包；可授权非 OA PDF |
| Cambridge Core | Crossref 定位 + 校园页面 | 原始 PDF；不是通用 XML API |
| Wiley | 官方 TDM Token + 实际内容权限 | PDF；Token 不保证所有订阅内容可下载 |
| AIP | 校园页面定位 + 受控 Silverchair 交付跳转 | PDF，慢速后台下载 |
| ASME / ACS | 校园官网路径 | PDF，后台任务 |
| MDPI | 官方 OA 路径 | PDF，后台任务；XML/原图包未验收 |

SAE、AIAA 不在当前 40 工具中。曾研究或测试过的渠道，不能因此写成已接入。出版社身份、OA 标识、订阅权限、下载结果是不同证据，分别保留。

## 5. 统一检索规范

### 5.1 路由与兼容

| profile | 默认来源 |
| --- | --- |
| `general` | scopus、openalex、crossref、wos |
| `ai` | arxiv、dblp、openreview、proceedings、openalex |
| `theses` | datacite、openaire、core、wanfang、thesesfr、repositories |
| `biomedical` | pubmed、europepmc、openalex、scopus |

用户指定 `sources` 时只执行指定范围，不用不相关网站悄悄替代失败源。默认范围中有未启用来源时仍报告缺口。Springer 出版商筛选可在自动计划中补入对应适配器。

`search_literature` 保留原生参数；只使用旧参数时走兼容行为。新参数 `query/profile/sources/filters/...` 使用 schema version `2`。同时提交统一查询和旧原生检索式会拒绝执行，不能依赖未说明的优先级。

### 5.2 查询、筛选与预算

解析器支持短语、括号、AND/OR/NOT，以及 `title`、`abstract`、`author`、`all` 字段；相邻词按 AND 处理。输入最多 1,500 个可打印字符、100 个词法 token，可显式提供最多 4 个 `query_variants`。服务端不调用额外收费模型翻译或扩写查询。

支持解析一种操作符，不代表每个来源都能表达它。例如部分开放书目端点只能表达普通关键词 AND；遇到 OR/NOT 或不支持的字段会返回 unsupported。结果还可能执行本地筛选；字段缺失时不能假定满足严格筛选。因此，应同时检查 `source_status`、来源查询、筛选信息及返回数量。

`filters` 当前键包括：`from_year`、`to_year`、`language`、`degree`、`institution`、`conferences`、`track`、`publisher`、`type`、`include_unknown`、`include_in_progress`、`sort`、`citation_source`。按被引排序必须指定引用来源。会议年份与轨道须处于官方目录已登记范围。

| 参数 | 日常 | 深度 |
| --- | --- | --- |
| 执行 | 同步，分来源受控并发 | 独立 worker、短轮询 |
| 默认时间预算 | 25 秒 | 20 分钟 |
| 上游请求预算 | 12 次 | 150 次 |
| 结果 | 每来源每批 1–10 条；默认预览 20 条 | 最多 500 条规范记录 |
| 引文扩展 | 按需调用 | 默认关闭；显式启用后一跳、最多 20 种子/200 关系 |
| 达到限制 | 返回 partial/原因与可用记录 | 保存进度、停止原因与结果 |

这里的时间预算是应用控制，不是对外部网络或宿主交互延迟的保证。摘要在预览中超过 1,500 字符会截断并标记，单篇读取及导出用于取得保留的完整内容。

### 5.3 分页、去重与排序

继续检索使用返回的 `continuation_id`，冻结原计划。续查时不能同时改变 query、sources、filters 或扩展式；要改变条件应新建检索。`has_more` 只表达本计划允许的继续范围，不表达全球文献是否查全。

有效 DOI、同源 ID、已核实持久标识用于精确合并。相似题名只产生候选关联，不自动把两篇论文合并。预印本、正式版和修订版可保留版本关系；学位论文与衍生期刊文章保持不同记录。不同数据库的被引次数不相加。

默认排序为等权 Reciprocal Rank Fusion：`score = Σ 1/(60 + source_rank)`。输出来源名次与合并依据。年份排序和指定来源的被引排序需显式选择。

### 5.4 一次可复核的检索

1. 用 `list_research_sources` 确认渠道状态和覆盖。
2. 明确主题、年份、类型、语言及是否包括未知学位层次。
3. 执行日常检索或启动深度任务，记录 run_id/job_id。
4. 阅读各来源状态；“未配置、超时、失败”不能当作零命中。
5. 用记录 ID 获取原始摘要、来源证据、参考关系和全文位置。
6. 对继续分页、引文扩展及人工补查分别记账。
7. 导出结果和检索报告。报告执行完成只针对该计划，不能据此声称文献查全。

示例参数对象（工具调用，不是独立 Shell 命令）：

```json
{"query":"machine learning","profile":"general","sources":"openalex,crossref","filters":{"from_year":2020},"count_per_source":5}
```

```json
{"query":"combustion","sources":"datacite,openaire,core","filters":{"degree":"doctoral","include_unknown":false},"count_per_source":5}
```

## 6. 记录、身份与证据模型

共同记录保留 `record_id`、规范标识、题名、作者、年份、类型、出版信息、摘要、访问位置、来源及证据。学位子结构保留层次、原始名称、机构、导师等来源实际提供的字段；不能保证各渠道均完整提供专业或答辩日期。

摘要含原文、来源及缺失/预览截断说明。多个来源冲突不应靠模型补写“原摘要”。字段来源和获取证据用于追溯。引用关系、被引计数按来源保存。

学位层次使用 `masters / doctoral / other / unknown`。默认目标是硕博；明确其他层次和在研论文默认排除，类型不明论文单列 `unknown_degree_results`。`include_unknown=false` 可严格排除未知，但会减少召回。只看到 Dissertation 或 thesis 时不得自动标成 doctoral。

人工 RIS/BibTeX 导入最多 1 MiB、200 条，标记 `metadata_independently_verified=false`，不会自动执行全文下载。BibTeX 支持有限语法，不支持宏表达式；复杂管理器导出文件必要时先规范化。

## 7. SQLite、任务与恢复

SQLite 处于 WAL 模式，保存 schema version、records、record_aliases、runs、cache、rate、capabilities、jobs、files、harvest；仓储本地检索使用 FTS5。跨进程共享缓存、限流与新文件配额，不依赖 Redis 或外部数据库。

| 项目 | 当前策略 |
| --- | --- |
| 检索记录、终态任务等 | 默认 30 天清理策略 |
| 普通元数据缓存 | 通常 24 小时 |
| 官方会议元数据缓存 | 7 天 |
| 新文件缓存 | 24 小时，独立 2 GiB 配额 |
| worker 并发 | 2 个深度检索、2 个新下载、1 个采集 |
| 排队数量 | 每任务种类最多 20 个 queued/running；相同有效载荷可复用 |
| 任务租约 | 60 秒，worker 循环续租，过期运行任务恢复排队 |
| 轮询 | `get_research_job` 每次最多等待 20 秒，默认 0 秒 |

持久任务状态为 queued/running/completed/failed/cancelled。取消是协作式：设置取消标记，在检查点停止并保留已提交结果，不承诺瞬时打断所有网络读操作。

Tunnel 重启不终止独立 worker 中的新任务；worker/主机重启后从持久检查点恢复搜索。新 PDF 下载默认从头重新下载，不承诺 Range 续传。深度检索恢复依据提交的来源游标和结果，不把内存尚未提交状态视作已完成。

旧 AIP 与 publisher_jobs 使用另外的子进程和 JSON 任务目录。它们可跨 MCP 会话继续工作，但仍处于 Tunnel 服务的 systemd cgroup；`start_new_session=True` 不会脱离 cgroup，服务重启仍可能终止。不能将新 worker 的恢复承诺套用到旧任务。

## 8. 全文、图片与原生文件

### 8.1 下载路由

统一 `download_literature(record_id)` 仅接受存储记录；根据标识、已登记位置与出版者选择通道。先解析正式仓储/原始开放位置及已验证出版社路径，失败原因随路由保留。`resolve_fulltext` 返回位置和访问说明，不等于已取得文件。

学位文件优先核对 DOI、Handle、仓储文件关系或馆藏标识；缺持久标识时结合题名、作者、机构和页面文件关系验证。正文、摘要、封面、附录和补充材料应区分。多个正文分卷分别交付并附清单，不偷偷拼接。扫描 PDF 可用准确仓储关系验证，不能因无可提取文本就一律判坏，也不能声称做过未执行的 OCR。CAJ 不改名成 PDF、不做非官方转换。

### 8.2 校验与大小

文件校验包含 PDF 签名、EOF、结构、长度、SHA-256 和身份依据。单页封面/预览、错误文献或截断文件拒绝按完整文章交付；仅通过“多页 PDF”仍不足以独立证明出版社全文完全无缺页。

| 链路 | 单文件/配额 | 时间与特性 |
| --- | --- | --- |
| 旧文章缓存 | 20 MiB；512 MiB 目标；24 小时 | 旧锁主要在进程内，不宣称严格全局配额 |
| 旧 AIP、ASME/ACS/MDPI | 20 MiB | 下载 30 分钟、无数据 60 秒；轮询最多 20 秒 |
| 新统一文章下载 | 20 MiB | 总预算 30 分钟、无数据 60 秒 |
| 新学位论文下载 | 100 MiB；与新文件共享 2 GiB | 事务配额预留；超附件阈值走 SSH |

同步的 ScienceDirect/Springer/Cambridge/Wiley 等原生工具有各自时限，不能把后台 30 分钟宣传成全部工具的时限。应用期限通常在读操作之间检查，单次阻塞读还受网络超时约束。

### 8.3 XML 与图片

XML 是机器可读正文/元数据结构，不天然包含图片二进制。ScienceDirect 的 bundle 从 XML 识别原始 HIGHRES 文件，Springer OA bundle 识别 JATS 图像关系，再下载原图生成离线 HTML/ZIP。只取 XML 或提取图片地址，均不等于完成原图下载。HTML 转换版也不能称为出版社原始 PDF。

### 8.4 ResourceLink 与 Space

MCP 返回文件资源链接后，宿主通过 `resources/read` 读取校验后的原始字节并实体化原生附件。旧组件保留分片工具，但它不是所有桌面客户端均具备的保存接口。没有宿主文件 API 时，组件按钮不能保证直接下载。

当前新链路设置 20 MiB 原生附件实现阈值，**不是 ChatGPT 官方附件上限**。官方 Tunnel 文档另列默认序列化信封大小约束；Base64 也会增加传输体积。因此不能据代码阈值保证任意 20 MiB PDF 都可经 Tunnel 单次实体化，较大附件需按实际宿主/传输测试判断，失败时保留完整文件走 SSH。

`space_saved:false` 表示服务器未独立确认 Space 保存，并非文件一定保存失败。只有在宿主 Space 中实际查到文件、可读取并核对哈希，才能称保存已验证。既有验收确认过普通 Chat→原生 PDF→Space→再次下载哈希一致；这不是所有客户端、文件大小或新任务的普遍保证。服务不会主动切换到 Work；产品额度与计费仍由宿主决定。

## 9. 可配置的最后备份

`retrieval/scihub.py` 是统一下载的最后 DOI 备份；旧出版社专用工具不自动调用它。配置位于 `download-policy.json` 的 `scihub_final_fallback`，环境变量 `CAMPUS_SCIHUB_FALLBACK=0/1` 可覆盖。公共仓库不包含启用策略文件，默认关闭，由部署管理员自行决定。

仅非学位且有合法 DOI 的存储记录适用；正常路径全部失败后才尝试。取消、预算耗尽、缓存满、文件超限及不安全位置是终止条件，不能靠换通道绕过。备份阶段最多 5 分钟，并计入剩余总下载预算。

实现为匿名 GET、内存 CookieJar、固定主机与跳转白名单、页面 PDF 入口解析；不执行任意 JavaScript、不注入浏览器 Cookie 或出版社 Key，不处理真实站点挑战。页面反馈弹窗含验证码脚本不等于 PDF 通道受挑战；实际机器人/Cloudflare 验证仍停止。

已有成功证据 DOI `10.1109/5.726791`：47 页、910489 字节，SHA-256 `2f600b12c277a358dff85c1c7829f62d4a1432d6613c8d3cbf1819183196150a`。身份依据为 PDF 题名/作者及 Crossref DOI 题名关系，不能改写成“PDF 正文一定包含 DOI”。其他样本仍有挑战或不可用，不保证持续可用。此备份不属于机构 API 授权证明，管理员应单独决定策略。公共版仅保留实现与离线测试，不发布原环境的外部下载记录。

## 10. 安全与运维不变量

1. 密钥不进代码、Git、发布包、聊天、截图、命令行参数或原始错误日志。隐藏输入写 root-only JSON，systemd LoadCredential 按服务提供临时可读文件。
2. API 请求直接出站，代理仅服务 Tunnel。网络代理畅通不等于校园内容授权。
3. 固定 HTTPS 来源和下载主机，校验端口、用户信息、路径、DNS 公网地址和重定向；针对特定交付 CDN 的例外限定来源关系与路径。
4. 重定向不携带出版社凭据；签名文件 URL 留在内存，不写检索报告或用户日志。
5. XML 不启用外部实体；挑战 HTML、401/403/429、错误 JSON、空结果分别处理，不伪造成功。
6. 私有缓存目录 700、文件通常 600，systemd UMask=0077，服务无登录 shell，启用 ProtectSystem/ProtectHome/NoNewPrivileges 等隔离。
7. 查询错误输出固定安全代码，不直接回传网络异常字符串。缓存键哈希不等于可以记录含 Key 的请求 URL。
8. 更新代码须同时处理错误类型及调用模块，避免混装新版异常构造器与旧下载模块；未结束任务不得盲目重启。

这些是实现与操作边界，不是形式化安全证明。新增主机、CDN、格式和 API 需要独立审查及失败场景测试。

## 11. 测试与覆盖限制

离线测试覆盖查询、分页、规范化、任务、下载校验、终止条件和部署入口保护。
运行方式见部署指南。原开发环境的验证记录不纳入公共仓库；公共版本以 CI
和部署者自己运行的真实验收结果为准，不能把旧环境成功等同于新环境已授权。

IEEE/Semantic Scholar/万方等需要独立凭据与权限。Caltech/HKUST 仍需额外验证；
会议目录年份与轨道有限。全新主机安装尚未完成干净环境验收。API 和网站
访问条件会变化，真实下载、论文完整性和宿主保存需要分别检查。

## 12. 扩展与维护方法

新增来源先在目录中声明能力、凭据及限制，再实现固定端点、分页和规范化；为真实 JSON/XML 字段、类型与错误状态编写测试。不得仅以 HTTP 200 或首页可达宣布上线。下载来源另做正文身份、重定向、配额、扫描和预览场景验证。

推荐顺序：在取得有效凭据后分别验证 IEEE 和 Semantic Scholar；按授权条件接商业机构接口；逐年补充会议官方目录；最后扩展仓储。新增持久字段先做版本化迁移和 SQLite backup，不能以替换单文件的补丁脚本承担数据库升级。

## 附录 A. MCP 工具与精确签名

下列清单由当前 `server/server.py` 的工具装饰器及 Python AST 自动生成，默认值保留源码语义。`read_article_chunk` 为组件专用；调用者优先使用统一工具，不必手工编排每个数据库。

<!-- GENERATED_TOOL_SIGNATURES -->

| 工具 | 职责 |
| --- | --- |
| `ping` | 版本、配置布尔状态与基本健康 |
| `search_scopus` | Scopus 原生语法检索 |
| `get_scopus_record` | 按 EID 取得 Scopus 记录 |
| `check_sciencedirect_access` | 按 DOI 检查 ScienceDirect 可用访问 |
| `download_article` | ScienceDirect 原始 PDF/XML |
| `download_article_bundle` | ScienceDirect 原图离线图文包 |
| `read_article_chunk` | 组件专用分片读取，不对模型开放 |
| `search_springer` | Springer Meta/OA 原生检索 |
| `get_springer_record` | Springer 单篇元数据 |
| `search_wos` | WoS Starter 原生检索 |
| `get_wos_record` | WoS UID 单篇记录 |
| `download_springer_xml` | Springer OA 原始 XML |
| `download_springer_bundle` | Springer OA 原图离线图文包 |
| `search_crossref` | Crossref 元数据检索 |
| `get_crossref_record` | DOI 元数据及可用原始摘要 |
| `download_cambridge_pdf` | Cambridge 校园 PDF |
| `download_springer_pdf` | Springer/Nature 校园 PDF |
| `download_wiley_pdf` | Wiley TDM PDF |
| `get_aip_download` | 查询旧 AIP 后台下载 |
| `download_aip_pdf` | 启动 AIP 校园 PDF 下载 |
| `download_asme_pdf` | 启动 ASME 校园 PDF 下载 |
| `download_acs_pdf` | 启动 ACS 校园 PDF 下载 |
| `download_mdpi_pdf` | 启动 MDPI OA PDF 下载 |
| `get_publisher_download` | 查询 ASME/ACS/MDPI 后台下载 |
| `search_arxiv` | arXiv 原生检索 |
| `get_arxiv_record` | arXiv 编号/版本查询 |
| `list_ai_conferences` | 20 个会议目录 |
| `search_ai_conferences` | DBLP 会议流/年份/题名检索 |
| `search_literature` | 兼容旧联合检索，提供统一查询与续查 |
| `list_research_sources` | 来源能力、启用状态与最近验证 |
| `search_theses` | 日常学位论文检索 |
| `get_literature_record` | 规范记录精确查询及按需补充 |
| `start_research_search` | 启动持久深度检索 |
| `get_research_job` | 查询新检索/下载/采集任务与结果 |
| `cancel_research_job` | 协作取消新任务 |
| `get_related_papers` | 按来源取得参考/施引关系 |
| `resolve_fulltext` | 解析已存储记录的可用全文位置 |
| `download_literature` | 按已存储记录启动统一原始 PDF 下载 |
| `export_search_run` | 导出完整结果或检索报告 |
| `import_references` | 有界 RIS/BibTeX 导入，保留未核验状态 |

```python
ping()

search_scopus(query: str, count: int=5, start: int=0)

get_scopus_record(eid: str)

check_sciencedirect_access(doi: str)

download_article(doi: str, format: str='pdf', destination: str='auto')

download_article_bundle(doi: str)

read_article_chunk(download_id: str, offset: int=0)

search_springer(query: str, collection: str='meta', count: int=5, start: int=1)

get_springer_record(doi: str, collection: str='meta')

search_wos(query: str, limit: int=5, page: int=1, sort: str='relevance')

get_wos_record(uid: str)

download_springer_xml(doi: str)

download_springer_bundle(doi: str)

search_crossref(query: str='', count: int=5, offset: int=0, issn: str='', has_abstract: bool=False, from_year: int=0, to_year: int=0, member_id: int=0)

get_crossref_record(doi: str, include_abstract_markup: bool=False)

download_cambridge_pdf(doi: str)

download_springer_pdf(doi: str)

download_wiley_pdf(doi: str)

get_aip_download(job_id: str, wait_seconds: int=20)

download_aip_pdf(doi: str)

download_asme_pdf(doi: str)

download_acs_pdf(doi: str)

download_mdpi_pdf(doi: str)

get_publisher_download(job_id: str, wait_seconds: int=20)

search_arxiv(query: str, count: int=5, start: int=0, categories: str='', from_year: int=0, to_year: int=0, sort_by: str='relevance')

get_arxiv_record(arxiv_id: str)

list_ai_conferences()

search_ai_conferences(query: str='', conferences: str='neurips,icml,iclr', count: int=5, offset: int=0, from_year: int=0, to_year: int=0)

search_literature(scopus_query: str='', wos_query: str='', springer_query: str='', count_per_source: int=5, crossref_query: str='', crossref_issn: str='', arxiv_query: str='', conference_query: str='', conferences: str='neurips,icml,iclr', conference_year: int=0, query: str='', profile: str='general', sources: str='auto', filters: dict | None=None, query_variants: list[str] | None=None, continuation_id: str='')

list_research_sources()

search_theses(query: str='', sources: str='auto', filters: dict | None=None, count_per_source: int=5, query_variants: list[str] | None=None, continuation_id: str='')

get_literature_record(record_id: str='', identifier: str='', source: str='auto', enrich: bool=False)

start_research_search(query: str, profile: str='general', sources: str='auto', filters: dict | None=None, query_variants: list[str] | None=None, citation_expansion: bool=False)

get_research_job(job_id: str, wait_seconds: int=0)

cancel_research_job(job_id: str)

get_related_papers(record_id: str, direction: str='references', source: str='auto', count: int=20)

resolve_fulltext(record_id: str)

download_literature(record_id: str)

export_search_run(run_id: str, format: str='json')

import_references(content: str, format: str='ris')
```

<!-- END_TOOL_SIGNATURES -->

## 附录 B. 资料索引

- [部署指南](DEPLOYMENT-GUIDE.md)
- [公开范围](PUBLICATION.md)
- [安全说明](../SECURITY.md)
- [OpenAI Secure MCP Tunnel 官方文档](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
- [ChatGPT 连接与测试](https://developers.openai.com/plugins/deploy/connect-chatgpt)

第三方文档和服务行为可能更新，部署时核对当时的官方说明。
