# Campus Research MCP · 校园统一文献 MCP

面向私有校园主机的多来源文献检索与原始文件获取服务，通过 STDIO MCP 和 OpenAI Secure MCP Tunnel 为兼容宿主提供工具。当前源码基线 **1.13.0 / 40 个工具**（39 个模型可见、1 个组件专用）。

## 功能

- 综合检索：Scopus、WoS Starter、Crossref、OpenAlex、Springer Meta。
- 预印本与 AI 会议：arXiv、DBLP、登记年份的 OpenReview 录用集合，以及 PMLR、NeurIPS、CVF、ACL 官方出版证据。
- 学位论文：DataCite、OpenAIRE、CORE、theses.fr、机构仓储 OAI-PMH/METS 和本地 FTS5 索引。
- 生物医学：PubMed、Europe PMC。IEEE、Semantic Scholar、万方适配器按凭据和真实权限启用。
- 原始文件：ScienceDirect、Springer/Nature、Cambridge、Wiley、AIP、ASME、ACS、MDPI；部分来源支持 XML 与原图离线 HTML/ZIP。
- 统一记录、字段来源、精确标识去重、RRF、受控分页、引文补充、检索导出及 RIS/BibTeX 导入。
- SQLite WAL、独立 systemd worker、可恢复搜索、新下载任务、取消和配额预留。
- 原生 MCP 文件资源；大文件可通过管理员既有 SSH/SCP 交付，无需公网下载服务。

## 文档

- [技术报告](docs/TECHNICAL-REPORT.md)：架构、模块、协议、数据模型、安全边界与全部工具签名。
- [部署指南](docs/DEPLOYMENT-GUIDE.md)：Ubuntu 安装、凭据、Tunnel、服务、验收、备份和排错。
- [公开发布范围](docs/PUBLICATION.md)：本仓库脱敏和能力说明。
- [公开快照验收](docs/VALIDATION.md)：277 项离线回归、工具发现与隐私检查范围。
- [安全报告](SECURITY.md) / [许可证](LICENSE)。

## 快速开始

运行环境为 Ubuntu + Python 3.12 + systemd。Windows 可用于 SSH 管理，完整服务请在 Linux/WSL 中运行。

```bash
cd server
sudo bash install_remote.sh
```

此步骤不安装 Tunnel 客户端或代理，不代表已经完成连接。继续按部署指南安装官方客户端，在本机 SSH 终端隐藏输入凭据，选择直连/专用代理，再配置 Tunnel 与 ChatGPT。不要把密钥写进命令、Issue、截图或 Git。

## 测试

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r server/requirements.lock.txt
bash scripts/test_offline.sh
```

离线测试不加载真实凭据或访问出版社。`test_live_*.py` 为可选联网验收，可能消耗 API 额度，应在自己的授权环境中显式运行。

## 使用边界

渠道覆盖不等于查全，API Key 不等于全文授权，网页订阅不等于 API 权限。来源缺摘要、未配置、拒绝访问或超时时会显式报告。全文以当前网络和内容授权为准，不处理真正的人机验证挑战。

服务器返回 ResourceLink 只证明文件已准备；保存至 Space 和桌面下载由宿主实现，需独立确认。20 MiB 原生附件阈值是本地实现阈值，并非宿主或 Tunnel 的保证。

可选最后 DOI 备份实现随源码提供，但仓库不包含启用策略文件；默认不开启。部署管理员应独立决定是否配置。许可证只覆盖本项目代码，不授予出版社内容或第三方平台访问权。

## 隐私与公开范围

此仓库采用全新 Git 历史，只包含经过筛选的源码、测试、通用模板和公开文档。原部署的主机/IP/穿透配置、密钥、工作区与文件标识、个人路径、私有验收记录、数据库、缓存、论文附件及截图均不包含。`research-host` 是示例 SSH 别名。

MIT License。固定依赖见 `server/requirements.lock.txt`；依赖与平台分别遵循其自身许可和服务条款。
