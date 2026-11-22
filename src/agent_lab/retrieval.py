"""可离线运行、可逐行解释的检索器：BM25 + TF-IDF / 语义 cosine + RRF（可选重排）。

默认离线模式下的向量是**词项向量**，不是神经网络产生的语义 embedding。保留这个
区别很重要：组合两个词法排序器能演示融合管线，但不能据此宣称解决了同义词
或跨语言语义匹配。传入 ``embedder`` 时切换为 L2 归一化稠密语义向量余弦，并可配合
``reranker`` 对融合候选做二阶段重排，同时保留分块、引用和评测接口。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
import hashlib
import math
from pathlib import Path
import re


Embedder = Callable[[list[str]], Sequence[Sequence[float]]]
Reranker = Callable[[str, list[dict]], list[dict] | Sequence[float]]


def _identifier_subtokens(raw_token: str, lowered: str) -> list[str]:
    """拆分 camelCase 与复合 snake_case 子词并去重，使跨命名规范查询可互通。"""
    split_camel = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", raw_token)
    split_camel = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", split_camel)
    seen = {lowered}
    subtokens: list[str] = []
    for part in split_camel.split("_"):
        sub = part.lower()
        if sub and sub not in seen:
            seen.add(sub)
            subtokens.append(sub)
    return subtokens


def tokenize(text: str) -> list[str]:
    """保留完整 snake_case/标识符、数字与汉字二元组，并额外输出复合标识符子词。

    二元组是透明、低成本的基线，不能代替领域分词器。数字独立保留，因此错误码
    ``429``、百分比 ``100`` 和函数名能够走精确词法匹配；camelCase 与 snake_case
    子词按单次出现去重输出，使 ``exponentialBackoff`` 与 ``exponential_backoff`` 互通。
    """
    result: list[str] = []
    for raw_token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*|\d+", text):
        lowered = raw_token.lower()
        result.append(lowered)
        result.extend(_identifier_subtokens(raw_token, lowered))
    for sequence in re.findall(r"[㐀-鿿]+", text):
        result.extend(sequence[index : index + 2] for index in range(len(sequence) - 1))
        if len(sequence) == 1:
            result.append(sequence)
    return result


def _tag_fenced_lines(text: str) -> list[tuple[str, bool]]:
    """标记每行是否位于 Markdown 围栏代码块（``` 或 ~~~，最多 3 个前导空格）内部。"""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    fence: str | None = None
    tagged: list[tuple[str, bool]] = []
    for line in normalized.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if fence is None:
                if not (token[0] == "`" and "`" in line[marker.end() :]):
                    fence = token
                tagged.append((line, fence is not None))
                continue
            if token[0] == fence[0] and len(token) >= len(fence) and not line[marker.end() :].strip():
                tagged.append((line, True))
                fence = None
                continue
        tagged.append((line, fence is not None))
    return tagged


def _chunks(text: str, max_chars: int = 1000, overlap: int = 120):
    """先按围栏代码块之外的 Markdown 标题切分，再生成有少量重叠的窗口。

    标题提供稳定的文档语境；重叠只减少窗口边界的信息丢失，不能保证一个函数或
    论证永远完整。检索结果仍须回到原文件核验。标题也进入索引，这是本项目的
    确定性上下文前缀，不是 Anthropic 文章中由 LLM 生成的 chunk context。
    """
    tagged_lines = _tag_fenced_lines(text)
    title = "未命名笔记"
    for line, in_fence in tagged_lines:
        if not in_fence:
            title_match = re.match(r"^#\s+(.+)$", line)
            if title_match and title_match.group(1).strip():
                title = title_match.group(1).strip()
                break
    heading = title
    body: list[str] = []
    sections: list[tuple[str, str]] = []
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
    step = max(1, max_chars - overlap)
    for heading, content in sections:
        for start in range(0, len(content), step):
            fragment = content[start : start + max_chars]
            if fragment.strip():
                yield title, heading, fragment
            if start + max_chars >= len(content):
                break


def _normalize_dense(vector: Sequence[float]) -> list[float]:
    if isinstance(vector, (str, bytes)):
        raise ValueError("embedding 向量必须是数值序列")
    values = [float(value) for value in vector]
    if not values or not all(math.isfinite(value) for value in values):
        raise ValueError("embedding 向量必须是非空有限浮点数序列")
    norm = math.sqrt(sum(value * value for value in values))
    return [value / norm for value in values] if norm > 0 else [0.0 for _ in values]


class KnowledgeBase:
    """一次构建的内存索引，适合小型、受版本控制的教学知识库。

    分块 ID 由相对路径、块序号及内容哈希构成；同一份版本在不同电脑上会得到
    相同 ID。内容变化会使 ID 变化，因此引用对应的是具体证据版本。
    大规模服务应将索引构建与请求进程分离，并记录索引版本与文档访问权限。
    """

    def __init__(
        self,
        directory: str | Path,
        *,
        embedder: Embedder | None = None,
        reranker: Reranker | None = None,
    ):
        if embedder is not None and not callable(embedder):
            raise ValueError("embedder 必须是可调用对象")
        if reranker is not None and not callable(reranker):
            raise ValueError("reranker 必须是可调用对象")
        self.directory = Path(directory).resolve()
        self._embedder = embedder
        self._reranker = reranker
        self._documents: list[dict] = []
        self._terms: list[Counter] = []
        self._df: Counter = Counter()
        self._vectors: list[dict[str, float]] = []
        self._dense_vectors: list[list[float]] = []
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
        self._finalize_index()

    def _finalize_index(self) -> None:
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
        """从内存文档列表（如双层记忆 Wiki 页面 + 历史会话 Transcript）构建 BM25 + TF-IDF/稠密向量 + RRF 混合索引。"""
        if embedder is not None and not callable(embedder):
            raise ValueError("embedder 必须是可调用对象")
        if reranker is not None and not callable(reranker):
            raise ValueError("reranker 必须是可调用对象")
        instance = cls.__new__(cls)
        instance.directory = Path(".")
        instance._embedder = embedder
        instance._reranker = reranker
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
            if len(raw_content.encode("utf-8")) > 128_000:
                raw_content = raw_content[:64_000]
            stem = str(doc_spec.get("stem") or Path(source).stem or "mem")
            extra_meta = dict(doc_spec.get("metadata") or {})
            for index, (title, heading, fragment) in enumerate(_chunks(raw_content)):
                digest = hashlib.sha256(
                    f"{source}\0{index}\0{title}\0{heading}\0{fragment}".encode()
                ).hexdigest()[:10]
                chunk_id = f"{stem}-{digest}"
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
                if len(instance._documents) >= 20_000:
                    break
            if len(instance._documents) >= 20_000:
                break
        instance._finalize_index()
        return instance

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

    def _cosine_scores(self, query_terms: Counter | str, query: str = "") -> list[float]:
        if isinstance(query_terms, str):
            query = query_terms
            query_terms = Counter(tokenize(query))
        if self._embedder is not None:
            if not self._dense_vectors:
                return []
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
        tfidf_query = self._tfidf(query_terms)
        return [
            sum(weight * vector.get(term, 0) for term, weight in tfidf_query.items())
            for vector in self._vectors
        ]

    def _apply_reranker(self, query: str, candidates: list[dict], limit: int) -> list[dict]:
        assert self._reranker is not None
        by_id = {item["id"]: item for item in candidates}
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
                if doc_id not in by_id:
                    raise ValueError("reranker 返回了未知候选 ID")
                if doc_id in seen:
                    continue
                seen.add(doc_id)
                merged = dict(by_id[doc_id])
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
            scored.sort(key=lambda entry: (-entry[0], -entry[1]["score"], entry[1]["id"]))
            return [
                {**candidate, "rerank_score": round(r_score, 6)}
                for r_score, candidate in scored[:limit]
            ]
        raise ValueError("reranker 必须返回候选字典列表或与候选等长的有限浮点数序列")

    def search(self, query: str, limit: int = 5) -> list[dict]:
        """融合两个排名；无匹配返回空列表，不为凑数返回零分证据。

        RRF 使用排名而非直接相加不同量纲的分数：sum(1/(60+rank))。
        结果里的 score 是排序分数，**不是正确率或置信概率**。
        """
        if not isinstance(query, str) or len(query) > 2000:
            raise ValueError("query 必须是最多 2000 字符的字符串")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ValueError("limit 必须是 1 到 20 的整数")
        if not self._documents or not query.strip():
            return []
        query_terms = Counter(tokenize(query))
        if not query_terms and (self._embedder is None or not any(ch.isalnum() for ch in query)):
            return []
        bm25 = self._bm25_scores(query_terms) if query_terms else [0.0] * len(self._documents)
        cosine = self._cosine_scores(query_terms, query)
        fused: Counter = Counter()
        for scores in (bm25, cosine):
            ranked = sorted(
                (index for index, score in enumerate(scores) if score > 1e-12),
                key=lambda index: (-scores[index], self._documents[index]["id"]),
            )
            for rank, index in enumerate(ranked, 1):
                fused[index] += 1 / (60 + rank)
        ranked_indices = sorted(fused, key=lambda index: (-fused[index], self._documents[index]["id"]))
        if not ranked_indices:
            return []
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
