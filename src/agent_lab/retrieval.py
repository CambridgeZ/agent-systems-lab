"""可离线运行、可逐行解释的检索器：BM25 + TF-IDF cosine + RRF。

这里的向量是**词项向量**，不是神经网络产生的语义 embedding。保留这个
区别很重要：组合两个词法排序器能演示融合管线，但不能据此宣称解决了同义词
或跨语言语义匹配。生产中可替换 ``_cosine_scores``，保留分块、引用和评测接口。
"""

from __future__ import annotations

from collections import Counter
import hashlib
import math
from pathlib import Path
import re


def tokenize(text: str) -> list[str]:
    """保留 snake_case 标识符，并用汉字二元组避免依赖中文分词模型。

    二元组是透明、低成本的基线，不能代替领域分词器。数字独立保留，因此错误码
    ``429``、百分比 ``100`` 和函数名能够走精确词法匹配。
    """
    result = re.findall(r"[a-z_][a-z0-9_]*|\d+", text.lower())
    for sequence in re.findall(r"[\u3400-\u9fff]+", text):
        result.extend(sequence[index : index + 2] for index in range(len(sequence) - 1))
        if len(sequence) == 1:
            result.append(sequence)
    return result


def _chunks(text: str, max_chars: int = 1000, overlap: int = 120):
    """先按 Markdown 标题切分，再生成有少量重叠的窗口。

    标题提供稳定的文档语境；重叠只减少窗口边界的信息丢失，不能保证一个函数或
    论证永远完整。检索结果仍须回到原文件核验。标题也进入索引，这是本项目的
    确定性上下文前缀，不是 Anthropic 文章中由 LLM 生成的 chunk context。
    """
    title_match = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else "未命名笔记"
    heading = title
    body: list[str] = []
    sections: list[tuple[str, str]] = []
    for line in text.splitlines():
        if re.match(r"^#{1,6}\s+", line):
            if body and "\n".join(body).strip():
                sections.append((heading, "\n".join(body).strip()))
            heading = re.sub(r"^#{1,6}\s+", "", line).strip()
            body = []
        else:
            body.append(line)
    if body and "\n".join(body).strip():
        sections.append((heading, "\n".join(body).strip()))
    for heading, content in sections:
        for start in range(0, len(content), max_chars - overlap):
            fragment = content[start : start + max_chars]
            if fragment.strip():
                yield title, heading, fragment
            if start + max_chars >= len(content):
                break


class KnowledgeBase:
    """一次构建的内存索引，适合小型、受版本控制的教学知识库。

    分块 ID 由相对路径、块序号及内容哈希构成；同一份版本在不同电脑上会得到
    相同 ID。内容变化会使 ID 变化，因此引用对应的是具体证据版本。
    大规模服务应将索引构建与请求进程分离，并记录索引版本与文档访问权限。
    """

    def __init__(self, directory: str | Path):
        self.directory = Path(directory).resolve()
        self._documents: list[dict] = []
        self._terms: list[Counter] = []
        self._df: Counter = Counter()
        self._vectors: list[dict[str, float]] = []
        if self.directory.exists() and not self.directory.is_dir():
            raise ValueError("知识库路径必须是目录")
        for path in sorted(self.directory.rglob("*.md")):
            # 不允许知识库中的符号链接读到目录外，也不读取过大的文档。
            if path.is_symlink() or not path.resolve().is_relative_to(self.directory):
                continue
            if path.stat().st_size > 128_000:
                continue
            relative = path.relative_to(self.directory).as_posix()
            for index, (title, heading, fragment) in enumerate(_chunks(path.read_text(encoding="utf-8"))):
                # 引用绑定返回给模型的完整证据，标题/小节变化也必须产生新版本。
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
                self._df.update(terms.keys())
                if len(self._documents) > 20_000:
                    raise ValueError("教学知识库最多支持 20000 个分块")
        self._avg_len = (
            sum(sum(terms.values()) for terms in self._terms) / len(self._terms) if self._terms else 1.0
        )
        for terms in self._terms:
            self._vectors.append(self._tfidf(terms))

    def _tfidf(self, terms: Counter) -> dict[str, float]:
        """使用 log TF 与平滑 IDF，然后进行 L2 归一化。"""
        count = len(self._documents)
        vector = {
            term: (1 + math.log(tf)) * (1 + math.log((1 + count) / (1 + self._df[term])))
            for term, tf in terms.items()
            if term in self._df
        }
        norm = math.sqrt(sum(weight * weight for weight in vector.values()))
        return {term: weight / norm for term, weight in vector.items()} if norm else {}

    def _bm25_scores(self, query_terms: Counter) -> list[float]:
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
                idf = math.log(1 + (count - df + 0.5) / (df + 0.5))
                score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * length / self._avg_len))
            scores.append(score)
        return scores

    def _cosine_scores(self, query_terms: Counter) -> list[float]:
        query = self._tfidf(query_terms)
        return [
            sum(weight * vector.get(term, 0) for term, weight in query.items()) for vector in self._vectors
        ]

    def search(self, query: str, limit: int = 5) -> list[dict]:
        """融合两个排名；无匹配返回空列表，不为凑数返回零分证据。

        RRF 使用排名而非直接相加不同量纲的分数：sum(1/(60+rank))。
        结果里的 score 是排序分数，**不是正确率或置信概率**。
        """
        if not isinstance(query, str) or len(query) > 2000:
            raise ValueError("query 必须是最多 2000 字符的字符串")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ValueError("limit 必须是 1 到 20 的整数")
        query_terms = Counter(tokenize(query))
        if not query_terms or not self._documents:
            return []
        bm25, cosine = self._bm25_scores(query_terms), self._cosine_scores(query_terms)
        fused: Counter = Counter()
        for scores in (bm25, cosine):
            ranked = sorted(
                (index for index, score in enumerate(scores) if score > 0),
                key=lambda index: (-scores[index], self._documents[index]["id"]),
            )
            for rank, index in enumerate(ranked, 1):
                fused[index] += 1 / (60 + rank)
        ranked = sorted(fused, key=lambda index: (-fused[index], self._documents[index]["id"]))[:limit]
        return [
            {
                **self._documents[index],
                "score": round(fused[index], 8),
                "bm25_score": round(bm25[index], 6),
                "vector_score": round(cosine[index], 6),
            }
            for index in ranked
        ]

    def index_stats(self) -> dict:
        return {
            "documents": len({doc["source"] for doc in self._documents}),
            "chunks": len(self._documents),
            "vocabulary": len(self._df),
            "retrieval": "BM25 + lexical TF-IDF cosine + RRF",
            "semantic_embeddings": False,
        }
