"""
Qwen Memory Semantic Search — TF-IDF + 余弦相似度语义检索
支持中文，基于 scikit-learn，无需 GPU，无需外部 API

用法：
  from semantic import SemanticIndex
  idx = SemanticIndex()
  idx.add("doc_id_1", "这是关于手机控制的文档")
  idx.add("doc_id_2", "桌面自动化脚本编写")
  results = idx.search("ADB 命令", top_k=5)
"""
import os
import sys
import json
import re
import hashlib
from pathlib import Path

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

# 中文分词（简单实现，基于字符 n-gram + 常见词）
# 对于 TF-IDF 来说，字符级 n-gram 对中文效果已经不错


def tokenize_chinese(text):
    """中文分词：字符 n-gram + 英文单词分割"""
    if not text:
        return ""

    # 移除标点和特殊字符
    text = re.sub(r'[^\w\s\u4e00-\u9fff]', ' ', text)

    # 提取英文单词
    english_words = re.findall(r'[a-zA-Z]+', text)

    # 提取中文字符（bigram）
    chinese_chars = re.findall(r'[\u4e00-\u9fff]', text)
    chinese_bigrams = [chinese_chars[i] + chinese_chars[i+1]
                       for i in range(len(chinese_chars) - 1)]
    chinese_trigrams = [chinese_chars[i] + chinese_chars[i+1] + chinese_chars[i+2]
                        for i in range(len(chinese_chars) - 2)]

    # 组合
    tokens = english_words + chinese_bigrams + chinese_trigrams + chinese_chars

    return " ".join(tokens)


class SemanticIndex:
    """语义索引：TF-IDF + 余弦相似度"""

    def __init__(self, cache_dir=None):
        if cache_dir is None:
            cache_dir = Path(__file__).parent / "data"
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.vectorizer = TfidfVectorizer(
            analyzer='word',
            max_features=10000,
            ngram_range=(1, 2),
            sublinear_tf=True,
        )

        self.doc_ids = []
        self.doc_texts = []
        self.matrix = None
        self._dirty = True

    def add(self, doc_id, text, metadata=None):
        """添加文档到索引"""
        if doc_id in self.doc_ids:
            # 更新已有文档
            idx = self.doc_ids.index(doc_id)
            self.doc_texts[idx] = text
        else:
            self.doc_ids.append(doc_id)
            self.doc_texts.append(text)
        self._dirty = True

    def remove(self, doc_id):
        """从索引移除文档"""
        if doc_id in self.doc_ids:
            idx = self.doc_ids.index(doc_id)
            self.doc_ids.pop(idx)
            self.doc_texts.pop(idx)
            self._dirty = True

    def build(self):
        """构建 TF-IDF 矩阵"""
        if not self.doc_texts:
            return

        # 对每篇文档做中文分词
        tokenized = [tokenize_chinese(t) for t in self.doc_texts]
        self.matrix = self.vectorizer.fit_transform(tokenized)
        self._dirty = False

    def search(self, query, top_k=10, min_score=0.05):
        """语义搜索：返回 (doc_id, score) 列表"""
        if self._dirty or self.matrix is None:
            self.build()

        if self.matrix is None or len(self.doc_ids) == 0:
            return []

        query_tokenized = tokenize_chinese(query)
        query_vec = self.vectorizer.transform([query_tokenized])
        scores = cosine_similarity(query_vec, self.matrix).flatten()

        # 排序取 top_k
        top_indices = scores.argsort()[::-1][:top_k]
        results = []
        for idx in top_indices:
            score = float(scores[idx])
            if score >= min_score:
                results.append((self.doc_ids[idx], score))

        return results

    def save(self, path=None):
        """保存索引到文件"""
        if path is None:
            path = self.cache_dir / "semantic_index.json"

        data = {
            "doc_ids": self.doc_ids,
            "doc_texts": self.doc_texts,
        }
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)

    def load(self, path=None):
        """从文件加载索引"""
        if path is None:
            path = self.cache_dir / "semantic_index.json"

        if not os.path.exists(path):
            return False

        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        self.doc_ids = data["doc_ids"]
        self.doc_texts = data["doc_texts"]
        self._dirty = True
        self.build()
        return True

    def stats(self):
        """返回索引统计"""
        return {
            "total_documents": len(self.doc_ids),
            "vocabulary_size": len(self.vectorizer.vocabulary_) if hasattr(self.vectorizer, 'vocabulary_') else 0,
            "matrix_shape": str(self.matrix.shape) if self.matrix is not None else "not built",
        }


# ============ 与 store.py 集成 ============

def build_index_from_db():
    """从数据库构建语义索引（自动保存元数据用于失效检测）"""
    if __package__:
        from . import store
    else:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import store

    idx = SemanticIndex()

    conn = store.get_db()
    try:
        sessions = conn.execute("""
            SELECT session_id, summary, summary_short, tags, project_path
            FROM sessions
        """).fetchall()

        for s in sessions:
            doc_id = f"session:{s['session_id']}"
            text = " ".join(filter(None, [
                s['summary'] or '',
                s['summary_short'] or '',
                s['tags'] or '',
                s['project_path'] or '',
            ]))
            idx.add(doc_id, text)

        observations = conn.execute("""
            SELECT id, session_id, obs_type, content, context, impact, tags
            FROM observations
        """).fetchall()

        for o in observations:
            doc_id = f"obs:{o['id']}"
            text = " ".join(filter(None, [
                o['content'] or '',
                o['context'] or '',
                o['impact'] or '',
                o['tags'] or '',
            ]))
            idx.add(doc_id, text)
    finally:
        conn.close()

    idx.build()
    idx.save()
    store.save_semantic_meta()
    return idx


def semantic_search(query, top_k=10, min_score=0.05):
    """便捷搜索（自动检测索引过期并重建）"""
    idx = SemanticIndex()

    # 检查索引是否过期
    fresh, reason = None, None
    try:
        if __package__:
            from . import store
        else:
            import store
        fresh, reason = store.check_semantic_index_fresh()
    except Exception:
        pass

    if not fresh:
        idx = build_index_from_db()
    else:
        if not idx.load():
            idx = build_index_from_db()

    results = idx.search(query, top_k=top_k, min_score=min_score)

    sessions = []
    observations = []
    for doc_id, score in results:
        if doc_id.startswith("session:"):
            sessions.append({
                "session_id": doc_id.replace("session:", ""),
                "score": round(score, 4),
            })
        elif doc_id.startswith("obs:"):
            observations.append({
                "observation_id": int(doc_id.replace("obs:", "")),
                "score": round(score, 4),
            })

    return {"sessions": sessions, "observations": observations, "rebuilt": not fresh}


if __name__ == "__main__":
    # 测试
    print("Building index from database...")
    idx = build_index_from_db()
    print(f"Index stats: {idx.stats()}")

    # 测试搜索
    test_queries = ["手机控制", "ADB 冲突", "桌面自动化", "记忆系统", "压力测试"]
    for q in test_queries:
        results = idx.search(q, top_k=3)
        print(f"\nQuery: '{q}'")
        for doc_id, score in results:
            print(f"  {doc_id}: {score:.4f}")
