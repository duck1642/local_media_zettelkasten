"""Arama yöneticisinin kullandığı küçük, bellek içi arama yapıları."""

import copy


class BKTreeSearcher:
    """Algısal hash'leri Hamming uzaklığıyla arar; kilitleme çağırana aittir."""

    def __init__(self):
        self.tree = None
        self.items = []
        self._item_set = set()

    @staticmethod
    def _hamming_distance(h1: str, h2: str) -> int:


        try:
            val1 = int(h1, 16)
            val2 = int(h2, 16)

            return (val1 ^ val2).bit_count()
        except (ValueError, TypeError):
            return max(len(str(h1 or "")), len(str(h2 or ""))) * 4 + 1

    def add(self, item_hash: str, phash_str: str):
        """Aynı kayıt/hash çiftini ikinci kez eklemeden ağacı güncelle."""

        if not phash_str or phash_str == "None":
            return
        item = (item_hash, phash_str)
        if item in self._item_set:
            return
        self.items.append(item)
        self._item_set.add(item)
        self._add_to_tree(item_hash, phash_str)

    def remove_hashes(self, item_hashes: set[str]) -> dict:
        """Verilen medya kimliklerinin tüm imzalarını kaldır; gerekirse ağacı yeniden kur."""
        if not item_hashes:
            return {"removed": 0, "rebuilt": False}
        before = len(self.items)
        self.items = [item for item in self.items if item[0] not in item_hashes]
        removed = before - len(self.items)
        if not removed:
            return {"removed": 0, "rebuilt": False}
        self._item_set = set(self.items)
        self.tree = None
        for item_hash, phash_str in self.items:
            self._add_to_tree(item_hash, phash_str)
        return {"removed": removed, "rebuilt": True}

    def _add_to_tree(self, item_hash: str, phash_str: str):

        if self.tree is None:
            self.tree = (phash_str, [item_hash], {})
            return

        curr_node = self.tree
        while True:
            node_phash, node_hashes, children = curr_node
            dist = self._hamming_distance(phash_str, node_phash)

            if dist == 0:

                if item_hash not in node_hashes:
                    node_hashes.append(item_hash)
                break

            if dist in children:
                curr_node = children[dist]
            else:
                children[dist] = (phash_str, [item_hash], {})
                break

    def query(self, query_phash: str, threshold: int) -> list[tuple[str, int]]:
        """Eşik içindeki (kayıt hash'i, bit uzaklığı) çiftlerini yakından uzağa ver."""
        # SearchManager serializes queries and mutations with its state lock.
        # Avoid copying the entire index for each lookup inside that lock.
        return self.query_snapshot(self.tree, query_phash, threshold)

    def snapshot(self):
        return copy.deepcopy(self.tree)

    @classmethod
    def query_snapshot(cls, tree, query_phash: str, threshold: int) -> list[tuple[str, int]]:
        """Yalnızca eşik aralığında sonuç verebilecek ağaç dallarını dolaş."""

        if tree is None or not query_phash:
            return []

        results = []
        candidates = [tree]

        while candidates:
            node_phash, node_hashes, children = candidates.pop()
            dist = cls._hamming_distance(query_phash, node_phash)

            if dist <= threshold:
                for h in node_hashes:
                    results.append((h, dist))


            for child_dist, child_node in children.items():
                if dist - threshold <= child_dist <= dist + threshold:
                    candidates.append(child_node)

        return sorted(results, key=lambda x: x[1])

class URLRegistry:
    """Kaynak URL'leri SQLite ile aynı normalleştirme kuralıyla bellekte tutar."""

    def __init__(self):
        self.seen_urls = set()

    def add(self, url: str):
        if url:
            from db.sqlite_operator import normalize_source_url
            self.seen_urls.add(normalize_source_url(url))

    def remove(self, url: str):
        if url:
            from db.sqlite_operator import normalize_source_url
            self.seen_urls.discard(normalize_source_url(url))

    def exists(self, url: str) -> bool:
        if not url:
            return False
        from db.sqlite_operator import normalize_source_url
        return normalize_source_url(url) in self.seen_urls

    def snapshot(self) -> set:
        return set(self.seen_urls)

    def count(self) -> int:
        return len(self.seen_urls)
