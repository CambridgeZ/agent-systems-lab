"""可离线运行、可逐行解释的混合检索内核：BM25 + TF-IDF / 稠密语义余弦 + RRF 融合（可选二阶段重排）。

一、统一检索架构全景（对照 Anthropic《Introducing Contextual Retrieval》与《The components of agent memory》）
为什么静态工程文档（RAG）与动态跨会话记忆（Memory Wiki + Session Transcripts）必须共用同一个检索内核？
在实际 Agent 系统中，若为静态知识库写一套分词/切块器、又为长期记忆写另一套简易字符串匹配，
会立即引发“命名规范断层”（驼峰 vs. 蛇形失配）与“代码块切碎语境”（围栏内 `# 注释` 被误判为标题）两类高频缺陷。
同时，离线 Dreaming（Pass 1）随时可能将某篇旧记忆标记为 `contradicted` 或就地纠错重写；若维护独立的后台增量
倒排缓存，极易发生缓存失效滞后导致的“脏旧记忆复活”。本模块通过双入口共享同一套无状态四阶管线，实现零漂移同构检索：

```text
  [入口 A: 静态知识目录]                 [入口 B: 动态记忆与历史轨迹]
  KnowledgeBase(directory)               KnowledgeBase.from_documents(docs)
  受 Git 管控的本地 *.md 规范             MemoryStore.search / DreamEngine Pass 3
            │                            (非 contradicted 双层 Wiki + KV 偏好 + Session Transcripts)
            └────────────────────┬────────────────────┘
                                 ▼
  1. 围栏安全结构切块 (_tag_fenced_lines -> _chunks)
     跳过 ``` / ~~~ 代码块内的伪标题 `# 注释`，提取真实 `title / heading` 作为确定性上下文前缀拼入分块
                                 ▼
  2. 代码感知混合分词 (tokenize -> _identifier_subtokens)
     保留完整标识符高权重 + 拆分 camelCase/PascalCase/snake_case 子词 + 保留状态码数字 + 汉字 Bigram
                                 ▼
  3. 全局统计统一结算 (_finalize_index)
     跨短规则页与长 Transcript 统一计算全局 `_avg_len`、平滑 IDF（稀疏 TF-IDF）或批量 L2 归一化稠密向量
                                 ▼
  4. 双路召回 + RRF 无量纲融合 + 可选精排 (search -> _bm25_scores + _cosine_scores -> _apply_reranker)
     以 `1 / (60 + rank)` 融合词法饱和得分与向量方向余弦，严禁零分噪声凑数（`score > 1e-12`）
```

二、跨静态文档、动态记忆 Wiki 与历史 Session Transcripts 的同构复用剖析
当 `MemoryStore.search`（或 `DreamEngine` Pass 3 断链实证补洞）调用 `KnowledgeBase.from_documents` 时，
三类结构迥异的动态知识源与静态 `docs/*.md` 经历完全一致的处理闭环，逐一化解混合记忆检索的四大顽疾：
1. 围栏代码块抗切碎：Session Transcript 的 `## 最终结论` 与 `traps/*.md` 中常内嵌含 `# Step 1` 注释的 Python/Bash
   代码块；`_tag_fenced_lines` 确保代码块不被腰斩，且外层 `# Session a1b2c3d4 (completed) / 显著性标记` 被自动
   注入每个分块首行，使检索命中结果天然携带“该证据出自哪场已完成/已验证会话”的完整溯源语境；
2. 跨命名规范子词桥接：用户提问前端驼峰字段 `maxDiscountCents` 时，`_identifier_subtokens` 能精准召回静态规范或
   10 场会话前 Transcript 中以蛇形 `max_discount_cents` 记录的后端规则，且精确全词命中始终排在仅子词命中之前；
3. 短规则抗长流水账淹没：单条 `rules/*.md` 往往仅百字，而单场 Transcript 长达数千字；`_finalize_index` 将二者放入
   同一统计池计算全局 `_avg_len`，使 BM25 长度惩罚（`b=0.75`）自动压制冗长会话记录的词频堆砌，保护高密度规则页；
4. 冷启动单笔记防坍缩：新仓库仅写入第 1 篇记忆笔记（`N=1, df=1`）时，经典 `log(N/df)=0` 会令向量通道彻底哑火，
   而平滑 TF-IDF（`1 + log((1+N)/(1+df)) = 1.0`）与非负 BM25+ IDF 确保首篇记忆立即可检。

三、关键工程权衡与诚实能力边界
1. 稀疏词项余弦基线 vs. 真实稠密语义 Embedding：
   - 未注入 `embedder` 时，第二路向量为**稀疏词项 TF-IDF 单位向量**而非神经网络语义向量。
     显式区分二者是工程诚实的底线：`BM25 + TF-IDF 余弦` 能兼顾“关键词词频饱和”与“整体词袋方向夹角”，
     并在极小语料（`N=1, df=1`）下稳定工作，但无法跨越零词面重合的纯同义词鸿沟
     （如英文 `rate limit` 无法纯靠词法召回只写了 `throttling` 的段落）；
   - 注入 `embedder`（如 Voyage / OpenAI / 本地 BGE）与 `reranker`（Cross-Encoder）后，即刻升级为
     完整的“词法 + 稠密语义双路召回 + 二阶段精排”管线，而内容寻址引用 ID（Citation ID）与评测契约零改动。
2. 确定性标题前缀 vs. LLM 生成式 Contextual Retrieval：
   - 朴素定长切块会把“赔偿上限 15%”与其所属的 `# 合同 A / ## 违约责任` 割裂，也会把某条报错栈与其所属的
     `# Session a1b2c3d4 (completed) / ## 最终结论 (verification=passed)` 会话成败语境割裂；
   - 论文原版方案对每个分块调用一次 LLM 生成背景摘要（离线成本较高，适合静态大库）；本模块采用零延迟、
     100% 确定性的 `f"{title} / {heading}\\n{fragment}"` 结构前缀注入，既满足毫秒级在线构建
     `MemoryStore.search` 临时索引的实时性要求，又彻底消除了章节归属与会话验证状态丢失问题。
"""

from __future__ import annotations

from collections import Counter, OrderedDict
from collections.abc import Callable, Sequence
import hashlib
import math
import os
from pathlib import Path
import re
import threading
from typing import Any
from urllib.parse import urlparse


# 可插拔模型契约：用纯函数签名解耦具体向量供应商（如 Voyage / OpenAI / 本地 BGE）与精排器 SDK。
# Embedder 输入文本列表，返回等长同维浮点序列；Reranker 输入 (query, 候选列表)，返回重排后字典序列或打分序列。
Embedder = Callable[[list[str]], Sequence[Sequence[float]]]
Reranker = Callable[[str, list[dict]], list[dict] | Sequence[float]]


def _identifier_subtokens(raw_token: str, lowered: str) -> list[str]:
    """拆分 camelCase、PascalCase 缩写与复合 snake_case 子词并去重，实现跨命名规范互通召回。

    为什么普通自然语言分词器在工程代码与 Agent 记忆库上会失效：
    - 仓库源码可能是 Python 的 ``exponential_backoff`` / ``max_discount_cents``，而前端配置、API 文档
      或用户在 REPL 中的提问可能是 ``exponentialBackoff`` / ``maxDiscountCents`` / ``HTTPServer``；
    - 若不拆分子词，词法倒排索引会把它们视为完全无关的词项，导致召回率为 0。

    两条正则的先后顺序不可颠倒：
    1. ``([A-Z]+)([A-Z][a-z])`` 先切开连续大写缩写与后续驼峰词边界（如 ``HTTPServer`` -> ``HTTP_Server``）；
    2. ``([a-z0-9])([A-Z])`` 再切开常规小驼峰与数字边界
       （如 ``retryV2 Policy`` / ``camelCase`` -> ``camel_Case``）。

    排序优先级不变量：``seen = {lowered}`` 保证单次词项内子词去重，同时调用方已先输出完整 ``lowered`` 词项。
    因此精确命中 ``exponential_backoff`` 的文档同时匹配
    ``[exponential_backoff, exponential, backoff]``（3 项），
    天然排在仅跨命名命中 ``[exponential, backoff]``（2 项）的 ``exponentialBackoff`` 文档之前。
    """
    # 第一步：先在连续大写缩写与后续驼峰词之间插入下划线（如 HTTPServer -> HTTP_Server）
    split_camel = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", raw_token)
    # 第二步：再在小写字母/数字与大写字母边界插入下划线（如 retryV2Policy -> retryV2_Policy）
    split_camel = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", split_camel)
    # 预置完整小写原词：避免纯单词重复吐出自身，同时保持“完整词命中 > 仅子词命中”的天然分差
    seen = {lowered}
    subtokens: list[str] = []
    for part in split_camel.split("_"):
        sub = part.lower()
        if sub and sub not in seen:
            seen.add(sub)
            subtokens.append(sub)
    return subtokens


def tokenize(text: str) -> list[str]:
    """混合词法分词器：保留完整代码标识符、拆分子词、独立数字字面量与中日韩汉字二元组（Bigram）。

    工程权衡说明：
    - 数字独立成词（``\\d+``）：许多通用 NLP 管线会把纯数字当停用词过滤，但在研发与排障记忆中，HTTP 状态码
      ``429``、端口号 ``8080``、上限阈值 ``200`` 往往是最具区分度的精确检索锚点；
    - 汉字字符二元组（``[㐀-鿿]+``，覆盖 CJK 基本区与扩展 A 区）：无需引入结巴词典或 C 扩展即可获得零依赖、
      跨平台确定性的中文召回基线；代价是无法区分词边界歧义，不能替代生产级领域分词器；
    - 单字兜底（``len(sequence) == 1``）：防止“锁”或“表”等单汉字独立片段在滑窗 ``len - 1`` 时退化为空列表。
    """
    result: list[str] = []
    for raw_token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*|\d+", text):
        lowered = raw_token.lower()
        # 先写入完整原词（保留精确全词匹配的高 IDF 权重），再追加拆分后的跨命名子词
        result.append(lowered)
        result.extend(_identifier_subtokens(raw_token, lowered))
    for sequence in re.findall(r"[㐀-鿿]+", text):
        result.extend(sequence[index : index + 2] for index in range(len(sequence) - 1))
        if len(sequence) == 1:
            result.append(sequence)
    return result


def _tag_fenced_lines(text: str) -> list[tuple[str, bool]]:
    """按 CommonMark 规范标记每行是否处于围栏代码块（``` 或 ~~~，最多 3 个前导空格）内部。

    为什么朴素按行正则 ``^#{1,6}\\s+`` 切分章节或直接提取 ``[[wiki-link]]`` 会产生严重缺陷：
    - 工程知识库、记忆笔记与 Session Transcript 中包含大量代码块（如 Python 注释 ``# Step 1``、Shell 注释
      ``# root check``、嵌套列表索引 ``matrix[[0]]``）。若不识别围栏状态，代码块内的 ``# 注释`` 会被误判为
      Markdown 一级标题，把一段完整函数切碎并污染 ``title`` / ``heading`` 上下文前缀；
    - ``memory.py`` 同样复用本函数隔离代码块内的伪 ``[[wiki-links]]``，确保静态分块与动态图谱解析一致。

    关键边界不变量：
    1. 换行符统一归一化为 ``\\n``，保证同一文档在 Windows（CRLF）与 Linux/macOS（LF）下生成一致的分块哈希；
    2. 开启反引号围栏时，CommonMark 规定 info string 中不能再含反引号（``"`" not in line[marker.end():]``），
       否则单行行内代码如 ```` ```foo``` ```` 会误开启一个未闭合的多行围栏，吞掉后续整篇文档；
    3. 闭合围栏必须使用同种字符（``` 对 ```，~~~ 对 ~~~）、长度不少于开启围栏，且行尾除空白外无任何多余字符。
    """
    # 先归一化 CRLF / CR 换行符，消除跨平台 Git autocrlf 导致分块边界与 SHA-256 引用 ID 漂移
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    fence: str | None = None
    tagged: list[tuple[str, bool]] = []
    for line in normalized.splitlines():
        # CommonMark 规范：最多允许 0~3 个前导空格（4 个前导空格属于缩进代码块而非围栏标记）
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if fence is None:
                # 排除单行多反引号内联代码片段，仅在合法 info string 时进入围栏态
                if not (token[0] == "`" and "`" in line[marker.end() :]):
                    fence = token
                tagged.append((line, fence is not None))
                continue
            # 仅当围栏类型一致、标记长度 >= 开启长度且行尾无杂质时闭合围栏（闭合行自身仍标 True 隔离）
            if token[0] == fence[0] and len(token) >= len(fence) and not line[marker.end() :].strip():
                tagged.append((line, True))
                fence = None
                continue
        tagged.append((line, fence is not None))
    return tagged


def _chunks(text: str, max_chars: int = 1000, overlap: int = 120):
    """两级结构感知切块：先按围栏代码块之外的 Markdown 标题切分语义章节，再做带重叠定长滑窗。

    为什么这样设计：
    - 章节级边界优先：不同 ``## 小节`` 往往讨论独立规则或不同会话阶段（如 Transcript 中的 ``## 用户任务``、
      ``## 显著性标记``、``## 最终结论``），先按标题切开可避免同一分块混入跨主题噪声；
    - 确定性上下文锚定：提取首个围栏外一级标题 ``# Title`` 与当前章节 ``heading`` 一并产出，调用方将其拼成
      ``f"{title} / {heading}\\n{fragment}"``，使每个分块自带来源语境；
    - 滑窗重叠（``overlap=120``）：当单节超长（> 1000 字符）时，相邻窗口保留 120 字符重叠，降低关键标识符
      或因果句恰好被腰斩在窗口交界处的概率。但滑窗重叠不能保证完整保留整个函数体，Agent 命中证据后仍可通过
      ``read_file`` 或 ``memory_read`` 回读完整原文。
    """
    tagged_lines = _tag_fenced_lines(text)
    title = "未命名笔记"
    # 扫描首个位于代码块围栏之外的一级标题作为整篇文档的根语境锚点
    for line, in_fence in tagged_lines:
        if not in_fence:
            title_match = re.match(r"^#\s+(.+)$", line)
            if title_match and title_match.group(1).strip():
                title = title_match.group(1).strip()
                break
    heading = title
    body: list[str] = []
    sections: list[tuple[str, str]] = []
    # 第一级切分：仅在非围栏态遇到 1~6 级 Markdown 标题时开启新语义小节，确保代码块内部完整不断裂
    for line, in_fence in tagged_lines:
        if not in_fence and re.match(r"^#{1,6}\s+", line):
            if body and "\n".join(body).strip():
                sections.append((heading, "\n".join(body).strip()))
            heading = re.sub(r"^#{1,6}\s+", "", line).strip()
            body = []
        else:
            body.append(line)
    if body and "\n".join(body).strip():
        sections.append((heading, "\n".join(body).strip()))
    # 第二级切分：步长保底为 1，防止调用方传入 overlap >= max_chars 时陷入 range(..., 0) 异常
    step = max(1, max_chars - overlap)
    for heading, content in sections:
        for start in range(0, len(content), step):
            fragment = content[start : start + max_chars]
            if fragment.strip():
                yield title, heading, fragment
            # 若当前窗口右边界已覆盖章节末尾，立即终止，避免多产出一段完全被当前窗口包含的冗余短尾块
            if start + max_chars >= len(content):
                break


def _normalize_dense(vector: Sequence[float]) -> list[float]:
    """校验并将稠密语义向量做 L2 单位归一化，使后续余弦相似度退化为低开销的纯点积运算。

    防御性检查：
    - Python 中 ``str`` 和 ``bytes`` 也满足 ``Sequence`` 协议，必须显式拦截以防字符被误转成浮点数；
    - 拒绝空向量及含 ``NaN`` / ``Inf`` 的异常模型输出，避免单条脏向量污染整个索引排序（Python 中 ``NaN``
      参与 ``sort`` 会破坏全序关系）；
    - 全零向量归一化为零向量而非抛出除零异常，使其与任意查询的点积安全得 0 分并在阈值过滤中被自然剔除。
    """
    if isinstance(vector, (str, bytes)):
        raise ValueError("embedding 向量必须是数值序列")
    values = [float(value) for value in vector]
    # 拦截空序列及 NaN / Inf：一旦单条脏向量混入索引，Python 排序时的全序比较不变式将被破坏
    if not values or not all(math.isfinite(value) for value in values):
        raise ValueError("embedding 向量必须是非空有限浮点数序列")
    norm = math.sqrt(sum(value * value for value in values))
    # 全零向量安全退化为零向量（避免 ZeroDivisionError），后续点积得 0 分并被 > 1e-12 阈值自然过滤
    return [value / norm for value in values] if norm > 0 else [0.0 for _ in values]


class KnowledgeBase:
    """不可变内存混合检索引擎：同时服务静态 Markdown 知识目录与动态跨会话记忆/Transcript 检索。

    核心工程不变量：
    1. 内容寻址的版本化引用（Content-Addressed Citation ID）：
       分块 ID 由 ``来源相对路径 \\0 块序号 \\0 文档标题 \\0 小节标题 \\0 正文片段`` 的 SHA-256 前缀生成。
       使用 POSIX 相对路径（而非机器绝对路径）保证跨不同开发机 Checkout 得到完全一致的 ID；使用 ``\\0`` 分隔符
       防止字段拼接边界歧义（如 ``("a", "bc")`` 与 ``("ab", "c")`` 碰撞）；任何标题或正文改动都会立即改变
       ID，使 Agent 的证据引用严格绑定具体内容快照。
    2. 目录边界与资源上限守卫：
       静态扫描拒绝任何符号链接（防止软链逃逸读取宿主机私钥或 ``/etc/passwd``）、拒绝超过 128KB 的超大文件、
       并将总分块数硬限制在 20,000 以内，防止病态目录耗尽进程内存。
    3. 生产演进边界：
       本类面向单机教学与轻量多租户内存构建；超大规模服务应将索引持久化到带 ACL 预过滤的独立向量/倒排引擎。
    """

    def __init__(
        self,
        directory: str | Path,
        *,
        embedder: Embedder | None = None,
        reranker: Reranker | None = None,
    ):
        """扫描本地目录中的 ``*.md`` 构建静态混合索引；目录不存在时安全退化为空索引。"""
        if embedder is not None and not callable(embedder):
            raise ValueError("embedder 必须是可调用对象")
        if reranker is not None and not callable(reranker):
            raise ValueError("reranker 必须是可调用对象")
        self.directory = Path(directory).resolve()
        self._embedder = embedder if embedder is not None else build_default_embedder()
        self._reranker = reranker if reranker is not None else build_default_reranker()
        self._documents: list[dict] = []
        self._terms: list[Counter] = []
        self._df: Counter = Counter()
        self._vectors: list[dict[str, float]] = []
        self._dense_vectors: list[list[float]] = []
        if self.directory.exists() and not self.directory.is_dir():
            raise ValueError("知识库路径必须是目录")
        # 排序遍历保证跨文件系统（APFS / ext4 目录项顺序不同）的文档编号与同分平局裁剪完全确定
        for path in sorted(self.directory.rglob("*.md")):
            # 安全守卫：先拦截文件自身符号链接，再核验 resolve() 后未逃逸出受控知识库根目录
            if path.is_symlink() or not path.resolve().is_relative_to(self.directory):
                continue
            if path.stat().st_size > 128_000:
                continue
            relative = path.relative_to(self.directory).as_posix()
            for index, (title, heading, fragment) in enumerate(_chunks(path.read_text(encoding="utf-8"))):
                # 用 NUL 字节分隔各结构化字段做哈希：标题、小节或正文任一变化都会派生全新版本引用 ID
                digest = hashlib.sha256(
                    f"{relative}\0{index}\0{title}\0{heading}\0{fragment}".encode()
                ).hexdigest()[:10]
                chunk_id = f"{path.stem}-{digest}"
                document = {
                    "id": chunk_id,
                    "chunk_id": chunk_id,
                    "title": title,
                    "heading": heading,
                    "source": relative,
                    "text": f"{title} / {heading}\n{fragment}",
                }
                terms = Counter(tokenize(document["text"]))
                self._documents.append(document)
                self._terms.append(terms)
                # 文档频率（DF）按分块去重统计（terms.keys()），每个词项在同一分块内多次出现只计 1 次 DF
                self._df.update(terms.keys())
                if len(self._documents) > 20_000:
                    raise ValueError("教学知识库最多支持 20000 个分块")
        self._finalize_index()

    def _finalize_index(self) -> None:
        """在全部静态或动态分块扫入后，一次性结算全局平均块长、全库 IDF 稀疏向量或批量稠密语义向量。

        为什么两阶段末尾统一结算对混合记忆库尤其关键：
        1. 跨源公平的全局统计量：在 `from_documents` 场景下，精炼的 `rules/*.md`（常仅百余字）与冗长的
           `Session Transcript`（数千字）被放入同一索引池。统一结算全局 `_avg_len` 与 `_df` 后，BM25 的
           长度惩罚 `b * length / self._avg_len` 会精准压制长流水账的词频堆砌优势，防止淹没精炼的核心规则页；
        2. 批量推理与维度强校验：挂载稠密 `embedder` 时一次性传入全部分块文本，吃满 GPU/API 批处理吞吐，
           并在入库前强制核验向量条数与维度一致。
        """
        self._avg_len = (
            sum(sum(terms.values()) for terms in self._terms) / len(self._terms) if self._terms else 1.0
        )
        if self._embedder is not None:
            if self._documents:
                raw_vectors = self._embedder([doc["text"] for doc in self._documents])
                if len(raw_vectors) != len(self._documents):
                    raise ValueError("embedder 返回向量数量必须与分块数一致")
                self._dense_vectors = [_normalize_dense(vec) for vec in raw_vectors]
                dim = len(self._dense_vectors[0])
                if any(len(vec) != dim for vec in self._dense_vectors):
                    raise ValueError("embedder 返回向量维度必须一致")
        else:
            for terms in self._terms:
                self._vectors.append(self._tfidf(terms))

    @classmethod
    def from_documents(
        cls,
        documents: Sequence[dict],
        *,
        embedder: Embedder | None = None,
        reranker: Reranker | None = None,
    ) -> KnowledgeBase:
        """绕过磁盘扫描，将内存中的动态双层记忆 Wiki、键值偏好与历史 Session Transcripts 构造成同构混合索引。

        端到端复用机制拆解（对照 `MemoryStore.search` 与 `DreamEngine._pass3_repair_broken_links_unlocked`）：
        1. 零磁盘开销同构实例化：
           通过 `cls.__new__(cls)` 跳过 `__init__` 的 `rglob("*.md")` 遍历，直接初始化一致的 `_documents`、
           `_terms`、`_df`、`_vectors`、`_dense_vectors` 表结构，使每次 `memory_search` 都能在毫秒级按最新
           未证伪（非 `contradicted`）状态实时建库，无需维护易失步的后台增量倒排缓存；
        2. 围栏安全 Markdown 分块复用（`_chunks` -> `_tag_fenced_lines`）：
           无论是记忆笔记里的 Python/Shell 示例，还是 Session Transcript 中的多行代码块，`_tag_fenced_lines`
           都会屏蔽围栏内部的 `# 注释`；同时将 `# Session <id> / ## 显著性标记` 或 `# 规则标题 / ## 验证方式`
           自动拼成分块头部的确定性上下文前缀；
        3. 代码标识符子词化互通（`tokenize` -> `_identifier_subtokens`）：
           用户在当前会话用驼峰提问 `maxDiscountCents`，能无缝召回 10 场会话前 Transcript 或 `rules/` 笔记里
           用蛇形记录的 `max_discount_cents` 证据；
        4. 来源命名空间防碰撞与元数据防篡改：
           `source` 带上层前缀（如 `project:rules/x.md`、`global:kv_memories`、`transcript:a1b2c3d4e5f6`）
           参与 NUL 分隔 SHA-256 计算，即使 Wiki 摘抄了某场 Transcript 原句，二者也拥有无碰撞的 `chunk_id`；
           字典构造时将 `**extra_meta` 置于核心键之前，透传 `note_status`、`sources`、`run_id` 等溯源字段，
           同时严防外部同名键覆写受保护的 `id` / `source` / `text`；
        5. 统一汇入 `_finalize_index` + `search` 的 BM25 + TF-IDF/稠密余弦 + RRF 管线：
           与静态 `search_docs` 100% 共享非负 BM25+ IDF、小语料平滑 TF-IDF 及 `k=60` 倒数排名融合。
        """
        if embedder is not None and not callable(embedder):
            raise ValueError("embedder 必须是可调用对象")
        if reranker is not None and not callable(reranker):
            raise ValueError("reranker 必须是可调用对象")
        # 用 __new__ 跳过静态目录扫描构造器，在内存中直接组装与静态索引完全同构的内部状态
        instance = cls.__new__(cls)
        instance.directory = Path(".")
        instance._embedder = embedder if embedder is not None else build_default_embedder()
        instance._reranker = reranker if reranker is not None else build_default_reranker()
        instance._documents = []
        instance._terms = []
        instance._df = Counter()
        instance._vectors = []
        instance._dense_vectors = []
        for doc_spec in documents:
            source = str(doc_spec.get("source") or "memory")
            raw_content = str(doc_spec.get("content") or "")
            if not raw_content.strip():
                continue
            # 动态 Transcript 可能包含长段回答：先按 128KB UTF-8 字节预算检测，超限时按 Python 码点切片 [:64_000]
            # （切 str 而非切 bytes[:128_000]，避免把 3~4 字节的中文字符/多字节 UTF-8 序列从中间劈裂导致解码异常）
            if len(raw_content.encode("utf-8")) > 128_000:
                raw_content = raw_content[:64_000]
            stem = str(doc_spec.get("stem") or Path(source).stem or "mem")
            extra_meta = dict(doc_spec.get("metadata") or {})
            for index, (title, heading, fragment) in enumerate(_chunks(raw_content)):
                # 与静态索引保持一致的 NUL 分隔内容寻址哈希，确保动态证据同样具备精确快照级 Citation ID
                digest = hashlib.sha256(
                    f"{source}\0{index}\0{title}\0{heading}\0{fragment}".encode()
                ).hexdigest()[:10]
                chunk_id = f"{stem}-{digest}"
                # 字典解包优先级守卫：先铺开 extra_meta，再显式覆盖核心键，严防外部 metadata 同名键篡改 id/source/text
                doc_entry = {
                    **extra_meta,
                    "id": chunk_id,
                    "chunk_id": chunk_id,
                    "title": title,
                    "heading": heading,
                    "source": source,
                    "text": f"{title} / {heading}\n{fragment}",
                }
                terms = Counter(tokenize(doc_entry["text"]))
                instance._documents.append(doc_entry)
                instance._terms.append(terms)
                instance._df.update(terms.keys())
                # 动态入口采用优雅截断（break）而非静态启动时的抛异常（raise），确保历史增长绝不中断在线会话检索
                if len(instance._documents) >= 20_000:
                    break
            if len(instance._documents) >= 20_000:
                break
        instance._finalize_index()
        return instance

    def _tfidf(self, terms: Counter) -> dict[str, float]:
        """计算对数词频（Sublinear TF）+ 平滑逆文档频率（Smoothed IDF）稀疏向量并做 L2 归一化。

        公式选型原因：
        - ``1 + log(tf)`` 抑制日志或代码片段中同一关键字重复出现几十次造成的权重爆炸；
        - ``1 + log((1 + N) / (1 + df))`` 在极小语料（如刚起步只有 1 篇记忆笔记，``N=1, df=1``）时
          仍保持正权重 ``1.0``，避免经典 ``log(N / df)`` 归零导致单篇记忆库无法通过余弦通道召回。
        """
        count = len(self._documents)
        # 平滑 IDF = 1 + log((1 + N) / (1 + df))：即使全库仅 1 个分块（N=1, df=1）仍保底为 1.0 而非归零
        vector = {
            term: (1 + math.log(tf)) * (1 + math.log((1 + count) / (1 + self._df[term])))
            for term, tf in terms.items()
            if term in self._df
        }
        norm = math.sqrt(sum(weight * weight for weight in vector.values()))
        # L2 单位长度归一化：后续计算余弦夹角只需遍历查询词做稀疏点积，时间复杂度仅与查询词数成正比
        return {term: weight / norm for term, weight in vector.items()} if norm else {}

    def _bm25_scores(self, query_terms: Counter) -> list[float]:
        """计算非负改进型 BM25 词法相关性得分（默认超参 ``k1=1.5, b=0.75``）。

        关键数学细节：
        - 词频饱和（``k1=1.5``）：当 ``tf`` 趋于无穷时，单项贡献收敛于 ``idf * (k1 + 1)``，防刷屏词霸榜；
        - 长度惩罚（``b=0.75``）：用 ``length / self._avg_len`` 归一化块长，防止冗长的
          Session Transcript 仅凭字数多就压过精炼的 ``rules/*.md`` 短笔记；
        - 非负 IDF（``log(1 + (N - df + 0.5) / (df + 0.5))``）：标准 Okapi BM25 不加 ``1 +``，
          当核心词出现在过半文档（``df > N / 2``，小型项目记忆库极常见）时 IDF 会变负数，命中更多词反而扣分！
          采用 Lucene / BM25+ 风格的 ``log(1 + ...)`` 严格保证 ``idf > 0``。
        """
        count, k1, b = len(self._documents), 1.5, 0.75
        scores: list[float] = []
        for terms in self._terms:
            length = sum(terms.values())
            score = 0.0
            for term in query_terms:
                tf = terms.get(term, 0)
                if not tf:
                    continue
                df = self._df[term]
                # BM25+ / Lucene 风格非负 IDF：外层加 1 + 防止高频核心词（df > N/2）出现负权重倒扣分
                idf = math.log(1 + (count - df + 0.5) / (df + 0.5))
                # 分母 b * length / _avg_len 按全局平均长度惩罚冗长 Transcript，保护短小精悍的规则页
                score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * length / self._avg_len))
            scores.append(score)
        return scores

    def _cosine_scores(self, query_terms: Counter | str, query: str = "") -> list[float]:
        """计算第二路余弦相似度：有 ``embedder`` 时走稠密语义单位向量点积，否则退化为稀疏 TF-IDF 点积。"""
        if isinstance(query_terms, str):
            query = query_terms
            query_terms = Counter(tokenize(query))
        if self._embedder is not None:
            if not self._dense_vectors:
                return []
            # 稠密编码器依赖完整自然语言语序与标点语境，因此优先传入原始 query 字符串而非分词后的词袋拼接
            query_text = query if query else " ".join(query_terms.elements())
            raw_query = self._embedder([query_text])
            if len(raw_query) != 1:
                raise ValueError("embedder 查询必须返回单个向量")
            query_vector = _normalize_dense(raw_query[0])
            if len(query_vector) != len(self._dense_vectors[0]):
                raise ValueError("embedder 查询向量维度与索引不一致")
            return [
                sum(q_val * d_val for q_val, d_val in zip(query_vector, doc_vector))
                for doc_vector in self._dense_vectors
            ]
        # 稀疏分支仅遍历查询命中词项（O(|Q|)），无需扫描词表中未出现的海量零维
        tfidf_query = self._tfidf(query_terms)
        return [
            sum(weight * vector.get(term, 0) for term, weight in tfidf_query.items())
            for vector in self._vectors
        ]

    def _apply_reranker(self, query: str, candidates: list[dict], limit: int) -> list[dict]:
        """对一阶段 RRF 粗排候选池执行二阶段精排，兼容返回重排字典列表或等长打分数组两种插件形态。

        安全与类型守卫：
        - 传入 ``[dict(item) for item in candidates]`` 浅拷贝，防止外部精排函数原地篡改索引内部缓存字典；
        - 严格白名单校验 ``doc_id in by_id``，禁止不可信重排器凭空伪造未经过索引安全审查的外部文档证据；
        - Python 中 ``isinstance(True, int)`` 为真，故显式排除 ``bool`` 类型以防布尔值混充合法浮点数分数。
        """
        assert self._reranker is not None
        by_id = {item["id"]: item for item in candidates}
        # 向插件传入候选字典浅拷贝，防止外部自定义重排函数原地修改索引内部元数据
        reranked = self._reranker(query, [dict(item) for item in candidates])
        if not isinstance(reranked, Sequence) or isinstance(reranked, (str, bytes)):
            raise ValueError("reranker 必须返回候选字典序列或分数序列")
        if not reranked:
            return []
        if all(isinstance(item, dict) for item in reranked):
            results: list[dict] = []
            seen: set[str] = set()
            for item in reranked:
                doc_id = item.get("id") or item.get("chunk_id")
                # 严格校验候选 ID 白名单，阻断异常精排插件注入未经索引审查的伪造文档
                if doc_id not in by_id:
                    raise ValueError("reranker 返回了未知候选 ID")
                if doc_id in seen:
                    continue
                seen.add(doc_id)
                merged = dict(by_id[doc_id])
                # 显式排除 bool（Python 中 bool 是 int 子类），防止 True/False 被静默转为 1.0/0.0 分数
                if "rerank_score" in item and not isinstance(item["rerank_score"], bool):
                    merged["rerank_score"] = round(float(item["rerank_score"]), 6)
                results.append(merged)
                if len(results) >= limit:
                    break
            return results
        if len(reranked) == len(candidates) and all(
            isinstance(score, (int, float)) and not isinstance(score, bool) and math.isfinite(score)
            for score in reranked
        ):
            scored = [
                (float(r_score), candidate) for r_score, candidate in zip(reranked, candidates)
            ]
            # 同分平局时回退到一阶段 RRF 分数，再回退到确定性的分块 ID 字典序
            scored.sort(key=lambda entry: (-entry[0], -entry[1]["score"], entry[1]["id"]))
            return [
                {**candidate, "rerank_score": round(r_score, 6)}
                for r_score, candidate in scored[:limit]
            ]
        raise ValueError("reranker 必须返回候选字典列表或与候选等长的有限浮点数序列")

    def search(self, query: str, limit: int = 5) -> list[dict]:
        """执行 BM25 + 余弦双路召回并通过倒数排名融合（RRF）产出排序证据；无真实命中时严格返回空列表。

        为什么用 RRF（``sum(1 / (60 + rank))``）而非线性加权（``alpha * bm25 + (1 - alpha) * cosine``）：
        - 量纲不可通约：BM25 得分无上界（随查询词数和 IDF 浮动在 ``[0, +inf)``），而余弦得分位于 ``[-1, 1]``。
          直接相加会导致 BM25 淹没语义通道；而单次查询内做 Min-Max 归一化在仅 1 条命中或存在离群分时又会失真；
        - 排名鲁棒性：平滑常数 ``k=60``（Cormack et al., 2009）抑制单通道排第 1 与排第 2 的过度差距，使双路
          共同认可的证据稳居榜首（最高 ``2 / 61``），并允许纯语义命中（``bm25 == 0, vector > 0``）以
          ``1 / 61`` 单通道救援上榜。
        - 严禁零分凑数（``score > 1e-12``）：未命中时返回 ``[]``，绝不把零相关噪声塞进 Agent 上下文诱发幻觉。
        - 注意：返回结果中的 ``score`` 仅代表相对排序证据强度，**不是校准后的正确率或置信概率**。
        """
        if not isinstance(query, str) or len(query) > 2000:
            raise ValueError("query 必须是最多 2000 字符的字符串")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ValueError("limit 必须是 1 到 20 的整数")
        if not self._documents or not query.strip():
            return []
        query_terms = Counter(tokenize(query))
        # 无词法词项时：若未挂载语义 embedder，或输入纯属标点符号（无字母数字），直接短路返回空
        if not query_terms and (self._embedder is None or not any(ch.isalnum() for ch in query)):
            return []
        bm25 = self._bm25_scores(query_terms) if query_terms else [0.0] * len(self._documents)
        cosine = self._cosine_scores(query_terms, query)
        fused: Counter = Counter()
        for scores in (bm25, cosine):
            # 仅让严格正相关的文档参与 RRF 投票（同时拦截稠密向量负余弦反义文档与零分文档）
            # 平局时用内容寻址 id 做二级字典序决胜，确保跨机器/跨运行返回顺序 100% 确定可复现
            ranked = sorted(
                (index for index, score in enumerate(scores) if score > 1e-12),
                key=lambda index: (-scores[index], self._documents[index]["id"]),
            )
            for rank, index in enumerate(ranked, 1):
                # RRF 无量纲倒数排名累加：k=60 平滑头部极差，双路均排第 1 时得满分 2 / 61
                fused[index] += 1 / (60 + rank)
        ranked_indices = sorted(fused, key=lambda index: (-fused[index], self._documents[index]["id"]))
        if not ranked_indices:
            return []
        # 挂载二阶段精排器时先放大 4 倍（保底 20 条）粗排候选池，提升高延迟精排前的召回上限
        pool_limit = min(len(ranked_indices), max(limit * 4, 20)) if self._reranker is not None else limit
        candidates = [
            {
                **self._documents[index],
                "score": round(fused[index], 8),
                "bm25_score": round(bm25[index], 6),
                "vector_score": round(cosine[index], 6),
            }
            for index in ranked_indices[:pool_limit]
        ]
        if self._reranker is None:
            return candidates
        return self._apply_reranker(query, candidates, limit)

    def index_stats(self) -> dict:
        """披露当前索引的规模与真实检索模式（显式区分纯词法基线与稠密语义 embedding，防止评测误读）。"""
        if self._embedder is not None:
            mode = "BM25 + dense semantic cosine + RRF"
        else:
            mode = "BM25 + lexical TF-IDF cosine + RRF"
        if self._reranker is not None:
            mode = f"{mode} + reranker"
        return {
            "documents": len({doc["source"] for doc in self._documents}),
            "chunks": len(self._documents),
            "vocabulary": len(self._df),
            "retrieval": mode,
            "semantic_embeddings": self._embedder is not None,
        }


class LocalSubwordDenseEmbedder:
    """零依赖确定性子词随机投影稠密编码器（字符 3/4-gram + 代码标识符子词 + 汉字 n-gram）。

    采用 BLAKE2b 确定性双路符号随机投影（CountSketch）生成单位 L2 长度稠密向量：
    - 跨进程、跨平台 100% 确定（不依赖受 PYTHONHASHSEED 影响的内置 hash()）；
    - 无外部神经网络模型下载即可在离线环境中对词形变化（authenticate/authentication、throttle/throttling）、
      拼写近似和跨语言子词提供平滑余弦相似度。
    """

    def __init__(self, dim: int = 256) -> None:
        if isinstance(dim, bool) or not isinstance(dim, int) or dim < 8:
            raise ValueError("dim 必须是 >= 8 的正整数")
        self.dim = dim

    def _extract_features(self, text: str) -> Counter[str]:
        features: Counter[str] = Counter()
        for tok in tokenize(text):
            features[f"tok:{tok}"] += 1
        # 边界锚定字符 3-gram / 4-gram：捕获英文词根词缀变化与拼写近似
        for word in re.findall(r"[a-z0-9]+", text.lower()):
            padded = f"<{word}>"
            for n in (3, 4):
                if len(padded) >= n:
                    for idx in range(len(padded) - n + 1):
                        features[f"c{n}:{padded[idx : idx + n]}"] += 1
        # 汉字单字与三元组补充，缓解双字切分错位时的语义召回损失
        for seq in re.findall(r"[㐀-鿿]+", text):
            for ch in seq:
                features[f"zh1:{ch}"] += 1
            if len(seq) >= 3:
                for idx in range(len(seq) - 2):
                    features[f"zh3:{seq[idx : idx + 3]}"] += 1
        return features

    def _embed_one(self, text: str) -> list[float]:
        features = self._extract_features(text)
        if not features:
            return [0.0] * self.dim
        vec = [0.0] * self.dim
        dim = self.dim
        for feat, count in features.items():
            base_weight = 1.5 if feat.startswith("tok:") else (0.7 if feat.startswith("zh1:") else 1.0)
            weight = base_weight * (1.0 + math.log(count))
            digest = hashlib.blake2b(feat.encode("utf-8"), digest_size=16).digest()
            idx1 = int.from_bytes(digest[0:4], "little") % dim
            sign1 = 1.0 if (digest[4] & 1) == 0 else -1.0
            idx2 = int.from_bytes(digest[5:9], "little") % dim
            sign2 = 1.0 if (digest[9] & 1) == 0 else -1.0
            vec[idx1] += sign1 * weight
            vec[idx2] += sign2 * weight
        return _normalize_dense(vec)

    def __call__(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return [self._embed_one(str(t)) for t in texts]


class OpenAICompatibleEmbedder:
    """同步批量 OpenAI 兼容 `/v1/embeddings` 适配器：带线程安全 LRU 缓存、L2 归一化与优雅故障降级。

    工程健壮性设计：
    - 建库期远端不可用（尚未观测到远端维度）时自动回退至 `LocalSubwordDenseEmbedder`；
    - 运行中（已建库观测到维度 `self._dim`）若遇网络闪断，返回同维零向量（而非异维本地向量），
      保证 `KnowledgeBase.search` 维度不变式不被破坏并平滑降级为 BM25 召回，同时不缓存降级结果以便网络恢复后自愈。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 15.0,
        cache_size: int = 2048,
        *,
        batch_size: int = 64,
        fallback_embedder: Embedder | None = None,
        client: Any | None = None,
    ) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url 必须是非空字符串")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model 必须是非空字符串")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or float(timeout) <= 0:
            raise ValueError("timeout 必须是正数")
        if isinstance(cache_size, bool) or not isinstance(cache_size, int) or cache_size < 0:
            raise ValueError("cache_size 必须是非负整数")
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
            raise ValueError("batch_size 必须是正整数")

        cleaned = base_url.strip().rstrip("/")
        if cleaned.endswith("/embeddings"):
            self.endpoint_url = cleaned
        elif cleaned.endswith("/v1"):
            self.endpoint_url = f"{cleaned}/embeddings"
        else:
            self.endpoint_url = f"{cleaned}/v1/embeddings"

        self.base_url = cleaned
        self.api_key = str(api_key or "").strip()
        self.model = model.strip()
        self.timeout = float(timeout)
        self.cache_size = cache_size
        self.batch_size = batch_size
        self._fallback: Embedder = (
            fallback_embedder if fallback_embedder is not None else LocalSubwordDenseEmbedder(dim=256)
        )
        self._external_client = client
        self._client: Any | None = client
        self._cache: OrderedDict[str, list[float]] = OrderedDict()
        self._lock = threading.Lock()
        self._dim: int | None = None
        self.cache_hits = 0
        self.cache_misses = 0
        self.fallback_count = 0

    def _get_client(self) -> Any:
        import httpx

        with self._lock:
            if self._client is None:
                self._client = httpx.Client(timeout=self.timeout)
            return self._client

    def close(self) -> None:
        with self._lock:
            if self._client is not None and self._client is not self._external_client:
                self._client.close()
                self._client = None

    @staticmethod
    def _cache_key(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _fallback_vectors(self, batch: list[str], *, expected_dim: int | None) -> list[list[float]]:
        with self._lock:
            self.fallback_count += 1
            known_dim = expected_dim if expected_dim is not None else self._dim
        if known_dim is not None:
            if self._fallback is not None:
                fb_dim = getattr(self._fallback, "dim", None)
                if fb_dim is None or fb_dim == known_dim:
                    try:
                        raw = self._fallback(batch)
                        normed = [_normalize_dense(v) for v in raw]
                        if len(normed) == len(batch) and all(len(v) == known_dim for v in normed):
                            return normed
                    except Exception:
                        pass
            return [[0.0] * known_dim for _ in batch]
        raw = self._fallback(batch)
        return [_normalize_dense(v) for v in raw]

    def _fetch_batch(self, batch: list[str]) -> list[list[float]]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        client = self._get_client()
        resp = client.post(
            self.endpoint_url,
            headers=headers,
            json={"model": self.model, "input": batch},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        body = resp.json()
        items = body.get("data") if isinstance(body, dict) else None
        if not isinstance(items, list) or len(items) != len(batch):
            raise ValueError("embeddings 响应缺少等长 data 列表")
        ordered = sorted(
            enumerate(items),
            key=lambda pair: int(pair[1].get("index", pair[0])) if isinstance(pair[1], dict) else pair[0],
        )
        vectors: list[list[float]] = []
        for _, item in ordered:
            if not isinstance(item, dict) or "embedding" not in item:
                raise ValueError("embeddings 条目缺少 embedding 字段")
            vectors.append(_normalize_dense(item["embedding"]))
        dim = len(vectors[0])
        if any(len(v) != dim for v in vectors):
            raise ValueError("embeddings 批次内维度不一致")
        return vectors

    def __call__(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        import httpx

        str_texts = [str(t) for t in texts]
        keys = [self._cache_key(t) for t in str_texts]
        resolved: dict[str, list[float]] = {}
        missing_keys: list[str] = []
        missing_texts: list[str] = []
        seen_missing: set[str] = set()

        with self._lock:
            for key, text in zip(keys, str_texts):
                cached = self._cache.get(key)
                if cached is not None:
                    self._cache.move_to_end(key)
                    resolved[key] = cached
                    self.cache_hits += 1
                else:
                    self.cache_misses += 1
                    if key not in seen_missing:
                        seen_missing.add(key)
                        missing_keys.append(key)
                        missing_texts.append(text)
            expected_dim = self._dim

        call_fallback_dim: int | None = None
        for start in range(0, len(missing_texts), self.batch_size):
            batch_texts = missing_texts[start : start + self.batch_size]
            batch_keys = missing_keys[start : start + self.batch_size]
            if call_fallback_dim is not None:
                fb_vecs = self._fallback_vectors(batch_texts, expected_dim=call_fallback_dim)
                for key, vec in zip(batch_keys, fb_vecs):
                    resolved[key] = vec
                continue
            try:
                vecs = self._fetch_batch(batch_texts)
                if expected_dim is not None and len(vecs[0]) != expected_dim:
                    raise ValueError("embeddings 远端维度与已缓存维度不一致")
            except (httpx.HTTPError, ValueError, KeyError, TypeError, OSError):
                fb_vecs = self._fallback_vectors(batch_texts, expected_dim=expected_dim)
                if expected_dim is None and fb_vecs:
                    call_fallback_dim = len(fb_vecs[0])
                for key, vec in zip(batch_keys, fb_vecs):
                    resolved[key] = vec
                continue

            with self._lock:
                if self._dim is None and vecs:
                    self._dim = len(vecs[0])
                    expected_dim = self._dim
                for key, vec in zip(batch_keys, vecs):
                    resolved[key] = vec
                    if self.cache_size > 0:
                        self._cache[key] = vec
                        self._cache.move_to_end(key)
                        while len(self._cache) > self.cache_size:
                            self._cache.popitem(last=False)

        return [list(resolved[key]) for key in keys]


class HttpReranker:
    """Cohere / Voyage / Jina 兼容 `/rerank` HTTP 二阶段精排适配器，异常时无缝回退原始 RRF 序。"""

    def __init__(
        self,
        url: str,
        api_key: str,
        model: str,
        timeout: float = 10.0,
        *,
        top_n: int | None = None,
        client: Any | None = None,
    ) -> None:
        if not isinstance(url, str) or not url.strip():
            raise ValueError("url 必须是非空字符串")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model 必须是非空字符串")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or float(timeout) <= 0:
            raise ValueError("timeout 必须是正数")

        cleaned = url.strip().rstrip("/")
        path_lower = urlparse(cleaned).path.rstrip("/").lower()
        if "rerank" in path_lower:
            self.endpoint_url = cleaned
        elif path_lower in {"", "/v1", "/v2"}:
            self.endpoint_url = f"{cleaned}/rerank"
        else:
            self.endpoint_url = cleaned

        self.url = cleaned
        self.api_key = str(api_key or "").strip()
        self.model = model.strip()
        self.timeout = float(timeout)
        self.top_n = int(top_n) if top_n is not None and int(top_n) > 0 else None
        self._external_client = client
        self._client: Any | None = client
        self._lock = threading.Lock()
        self.fallback_count = 0

    def _get_client(self) -> Any:
        import httpx

        with self._lock:
            if self._client is None:
                self._client = httpx.Client(timeout=self.timeout)
            return self._client

    def close(self) -> None:
        with self._lock:
            if self._client is not None and self._client is not self._external_client:
                self._client.close()
                self._client = None

    def __call__(self, query: str, candidates: list[dict]) -> list[dict]:
        if not candidates:
            return []
        import httpx

        payload: dict[str, Any] = {
            "model": self.model,
            "query": str(query),
            "documents": [str(item.get("text", "")) for item in candidates],
        }
        if self.top_n is not None:
            payload["top_n"] = min(self.top_n, len(candidates))
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            client = self._get_client()
            resp = client.post(
                self.endpoint_url,
                headers=headers,
                json=payload,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            body = resp.json()
            if not isinstance(body, dict):
                raise ValueError("rerank 响应必须是 JSON 对象")
            items = body.get("results")
            if not isinstance(items, list):
                items = body.get("data")
            if not isinstance(items, list) or not items:
                raise ValueError("rerank 响应缺少 results/data 列表")

            scored: list[tuple[float, int, int]] = []
            seen_idx: set[int] = set()
            for pos, entry in enumerate(items):
                if not isinstance(entry, dict):
                    continue
                raw_idx = entry.get("index")
                if isinstance(raw_idx, bool) or not isinstance(raw_idx, int):
                    continue
                if not (0 <= raw_idx < len(candidates)) or raw_idx in seen_idx:
                    continue
                raw_score = entry.get("relevance_score", entry.get("score"))
                if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
                    continue
                score = float(raw_score)
                if not math.isfinite(score):
                    continue
                seen_idx.add(raw_idx)
                scored.append((score, pos, raw_idx))

            if not scored:
                raise ValueError("rerank 响应未包含有效候选打分")
            scored.sort(key=lambda item: (-item[0], item[1]))
            return [
                {**candidates[idx], "rerank_score": round(score, 6)}
                for score, _, idx in scored
            ]
        except (httpx.HTTPError, ValueError, KeyError, TypeError, OSError):
            with self._lock:
                self.fallback_count += 1
            return [dict(item) for item in candidates]


def build_default_embedder() -> Embedder | None:
    """按环境变量构造默认稠密编码器；未配置时返回 None 以保持离线确定性词法基线。

    支持：
    - `AGENT_LAB_EMBEDDING_URL=local`（或 `AGENT_LAB_EMBEDDING_MODE=local`）：启用 `LocalSubwordDenseEmbedder`
    - `AGENT_LAB_EMBEDDING_URL=https://...`：启用 `OpenAICompatibleEmbedder`（配合
      `AGENT_LAB_EMBEDDING_MODEL`、`AGENT_LAB_EMBEDDING_API_KEY`、`AGENT_LAB_EMBEDDING_TIMEOUT`、
      `AGENT_LAB_EMBEDDING_CACHE_SIZE`）
    """
    url = os.environ.get("AGENT_LAB_EMBEDDING_URL", "").strip()
    mode = os.environ.get("AGENT_LAB_EMBEDDING_MODE", "").strip().lower()
    if url.lower() == "local" or (not url and mode == "local"):
        dim_raw = os.environ.get("AGENT_LAB_EMBEDDING_DIM", "").strip()
        dim = int(dim_raw) if dim_raw.isdigit() and int(dim_raw) >= 8 else 256
        return LocalSubwordDenseEmbedder(dim=dim)
    if not url:
        return None
    model = os.environ.get("AGENT_LAB_EMBEDDING_MODEL", "").strip() or "text-embedding-3-small"
    api_key = (
        os.environ.get("AGENT_LAB_EMBEDDING_API_KEY", "").strip()
        or os.environ.get("OPENAI_API_KEY", "").strip()
    )
    timeout_raw = os.environ.get("AGENT_LAB_EMBEDDING_TIMEOUT", "").strip()
    try:
        timeout = float(timeout_raw) if timeout_raw else 15.0
        if timeout <= 0:
            timeout = 15.0
    except ValueError:
        timeout = 15.0
    cache_raw = os.environ.get("AGENT_LAB_EMBEDDING_CACHE_SIZE", "").strip()
    try:
        cache_size = int(cache_raw) if cache_raw else 2048
        if cache_size < 0:
            cache_size = 2048
    except ValueError:
        cache_size = 2048
    return OpenAICompatibleEmbedder(
        base_url=url,
        api_key=api_key,
        model=model,
        timeout=timeout,
        cache_size=cache_size,
    )


def build_default_reranker() -> Reranker | None:
    """按环境变量构造默认 HTTP 精排器；未配置 `AGENT_LAB_RERANK_URL` 时返回 None。"""
    url = os.environ.get("AGENT_LAB_RERANK_URL", "").strip()
    if not url:
        return None
    model = os.environ.get("AGENT_LAB_RERANK_MODEL", "").strip() or "rerank-v3.5"
    api_key = os.environ.get("AGENT_LAB_RERANK_API_KEY", "").strip()
    timeout_raw = os.environ.get("AGENT_LAB_RERANK_TIMEOUT", "").strip()
    try:
        timeout = float(timeout_raw) if timeout_raw else 10.0
        if timeout <= 0:
            timeout = 10.0
    except ValueError:
        timeout = 10.0
    return HttpReranker(url=url, api_key=api_key, model=model, timeout=timeout)
