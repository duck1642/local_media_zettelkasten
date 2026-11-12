"""Aktif kasanın Markdown bağlantı kuyruklarını okur, düzenler ve çalıştırır."""

import re
import threading
from dataclasses import dataclass
from pathlib import Path

from runtime_context import WorkspaceContext, get_runtime_context
from utils import setup_directories


QUEUE_FILES = {
    "normal": "normal_pending_links.md",
    "force": "force_pending_links.md",
    "failed": "failed_links.md",
}

QUEUE_LABELS = {
    "normal": "Normal",
    "force": "Force",
    "failed": "Failed",
}

INGESTION_LOCK = threading.Lock()
QUEUE_LOCK = threading.RLock()


@dataclass(frozen=True)
class QueueEntry:
    """Bir URL ile kaynak satırını ve gruptan devraldığı metadata'yı taşır."""

    url: str
    artist: str = ""
    platform: str = ""
    group_index: int = 0
    line: int = 0

    def metadata(self) -> dict[str, str]:
        data = {}
        if self.artist:
            data["artist"] = self.artist
        if self.platform:
            data["platform"] = self.platform
        return data

    def to_preview(self) -> dict:
        return {
            "url": self.url,
            "artist": self.artist,
            "platform": self.platform,
            "group_index": self.group_index,
            "line": self.line,
        }


@dataclass(frozen=True)
class QueueWarning:
    """Atlanan satır veya geçersiz grup için arayüzde gösterilebilen uyarı."""

    line: int
    code: str
    message: str
    text: str = ""

    def to_dict(self) -> dict:
        return {
            "line": self.line,
            "code": self.code,
            "message": self.message,
            "text": self.text,
        }


def _ctx(ctx: WorkspaceContext | None = None) -> WorkspaceContext:
    return ctx or get_runtime_context()


def queue_path(queue: str, ctx: WorkspaceContext | None = None) -> Path:
    """Kuyruk adını kasa içindeki dosyaya eşle; tanınmayan adı reddet."""
    if queue not in QUEUE_FILES:
        raise ValueError(f"invalid queue: {queue}")
    return _ctx(ctx).active_vault.queues_dir / QUEUE_FILES[queue]


def ensure_queue_files(ctx: WorkspaceContext | None = None):
    """Eksik kuyrukları başlıklarıyla oluştur; mevcut kuyrukları değiştirme."""
    runtime = _ctx(ctx)
    with QUEUE_LOCK:
        setup_directories(runtime)
        defaults = {
            "normal": "# LMZ Normal Pending Links\n",
            "force": "# LMZ Force Pending Links\n",
            "failed": "# LMZ Failed Links Log\n",
        }
        for queue, text in defaults.items():
            path = queue_path(queue, runtime)
            if not path.exists():
                path.write_text(text, encoding="utf-8")


def read_queue(queue: str, ctx: WorkspaceContext | None = None) -> str:
    """Kuyruk metnini oku; ilk erişimde eksik kuyruk dosyalarını oluşturabilir."""
    runtime = _ctx(ctx)
    with QUEUE_LOCK:
        ensure_queue_files(runtime)
        return queue_path(queue, runtime).read_text(encoding="utf-8", errors="replace")


def write_queue(queue: str, text: str, ctx: WorkspaceContext | None = None):
    """Kuyruğun tüm metnini kilit altında verilen içerikle değiştir."""
    runtime = _ctx(ctx)
    with QUEUE_LOCK:
        ensure_queue_files(runtime)
        queue_path(queue, runtime).write_text(text, encoding="utf-8")


def _strip_list_marker(line: str) -> str:
    return re.sub(r'^(\s*[-*+]|\s*\d+\.)\s+', '', line)


def _line_has_inline_comment(line: str) -> bool:
    return bool(re.search(r'\s#', line))


def parse_queue(text: str) -> dict:
    """Markdown metnini URL kayıtlarına, grup özetlerine ve satır uyarılarına ayır.

    @artist ve @platform sonraki URL'lere uygulanır; üç çizgi yeni grubu
    başlatır ve metadata'yı sıfırlar. Ayrıştırma dosya yazmaz.
    """
    entries: list[QueueEntry] = []
    warnings: list[QueueWarning] = []
    group_warning_counts: dict[int, int] = {}
    groups: list[dict] = []
    current_artist = ""
    current_platform = ""
    current_group_index = 0
    group_had_metadata = False
    group_start_line = 1
    md_link_pattern = re.compile(r'\[.*?\]\((https?://.*?)\)')
    bare_pattern = re.compile(r'(https?://\S+)')

    def close_group(next_line: int):
        nonlocal group_had_metadata, group_start_line
        group_entries = [entry for entry in entries if entry.group_index == current_group_index]
        if group_had_metadata and not group_entries:
            warnings.append(QueueWarning(
                group_start_line,
                "empty_group",
                "Metadata group has no URLs.",
            ))
            group_warning_counts[current_group_index] = group_warning_counts.get(current_group_index, 0) + 1
        if group_entries or group_had_metadata:
            groups.append({
                "index": current_group_index,
                "artist": current_artist,
                "platform": current_platform,
                "url_count": len(group_entries),
                "urls": [entry.url for entry in group_entries],
                "warnings": 0,
            })
        group_had_metadata = False
        group_start_line = next_line

    for line_number, line in enumerate(text.splitlines(), start=1):
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        if raw == "---":
            close_group(line_number + 1)
            current_group_index += 1
            current_artist = ""
            current_platform = ""
            continue

        if _line_has_inline_comment(raw):
            warnings.append(QueueWarning(
                line_number,
                "inline_comment",
                "Inline comments are not supported; put comments on their own line.",
                raw,
            ))
            group_warning_counts[current_group_index] = group_warning_counts.get(current_group_index, 0) + 1
            continue

        processed = _strip_list_marker(raw)
        artist_match = re.match(r"^@artist:\s*(.+)$", processed, re.IGNORECASE)
        if artist_match:
            current_artist = artist_match.group(1).strip()
            group_had_metadata = True
            continue
        platform_match = re.match(r"^@platform:\s*(.+)$", processed, re.IGNORECASE)
        if platform_match:
            current_platform = platform_match.group(1).strip()
            group_had_metadata = True
            continue
        if processed.startswith("@"):
            warnings.append(QueueWarning(
                line_number,
                "unknown_directive",
                "Unknown queue directive.",
                raw,
            ))
            group_warning_counts[current_group_index] = group_warning_counts.get(current_group_index, 0) + 1
            continue

        md_match = md_link_pattern.search(raw)
        if md_match:
            url = clean_url(md_match.group(1))
            if url:
                entries.append(QueueEntry(url, current_artist, current_platform, current_group_index, line_number))
            continue

        bare_match = bare_pattern.search(processed)
        if bare_match:
            url = clean_url(bare_match.group(1))
            if url:
                entries.append(QueueEntry(url, current_artist, current_platform, current_group_index, line_number))
            continue

        warnings.append(QueueWarning(
            line_number,
            "ignored_line",
            "Line is not a supported directive or URL.",
            raw,
        ))
        group_warning_counts[current_group_index] = group_warning_counts.get(current_group_index, 0) + 1

    close_group(len(text.splitlines()) + 1)
    for group in groups:
        group["warnings"] = group_warning_counts.get(group["index"], 0)
    return {
        "count": len(entries),
        "entries": entries,
        "groups": groups,
        "warnings": warnings,
    }


def parse_urls(text: str) -> list[str]:
    return [entry.url for entry in parse_queue(text)["entries"]]


def parse_queue_preview(text: str) -> dict:
    """Ayrıştırma sonucunu API'de JSON olarak döndürülebilen sözlüklere çevir."""
    parsed = parse_queue(text)
    return {
        "count": parsed["count"],
        "groups": parsed["groups"],
        "warnings": [warning.to_dict() for warning in parsed["warnings"]],
        "entries": [entry.to_preview() for entry in parsed["entries"]],
    }


def clean_url(url: str) -> str:
    return url.strip().rstrip(").,;")


def queue_counts(ctx: WorkspaceContext | None = None) -> dict[str, int]:
    runtime = _ctx(ctx)
    with QUEUE_LOCK:
        ensure_queue_files(runtime)
        return {
            queue: len(parse_urls(queue_path(queue, runtime).read_text(encoding="utf-8", errors="replace")))
            for queue in QUEUE_FILES
        }


def append_urls(queue: str, urls: list[str], ctx: WorkspaceContext | None = None):
    runtime = _ctx(ctx)
    with QUEUE_LOCK:
        ensure_queue_files(runtime)
        path = queue_path(queue, runtime)
        existing = path.read_text(encoding="utf-8", errors="replace").rstrip()
        lines = [clean_url(url) for url in urls if clean_url(url)]
        if not lines:
            return
        body = "\n".join(lines)
        path.write_text(f"{existing}\n\n{body}\n" if existing else f"{body}\n", encoding="utf-8")


def append_queue_block(queue: str, url: str, artist: str = "", platform: str = "", ctx: WorkspaceContext | None = None):
    """İsteğe bağlı artist/platform bilgisiyle tek URL'lik bağımsız blok ekle."""
    runtime = _ctx(ctx)
    clean = clean_url(url)
    if not clean:
        return
    with QUEUE_LOCK:
        ensure_queue_files(runtime)
        path = queue_path(queue, runtime)
        existing = path.read_text(encoding="utf-8", errors="replace").rstrip()
        lines = []
        if artist:
            lines.append(f"@artist: {artist.strip()}")
        if platform:
            lines.append(f"@platform: {platform.strip()}")
        lines.append(clean)
        lines.append("---")
        body = "\n".join(lines)
        path.write_text(f"{existing}\n\n{body}\n" if existing else f"{body}\n", encoding="utf-8")


def move_failed_urls(target_queue: str, ctx: WorkspaceContext | None = None) -> int:
    """Başarısız URL'leri normal veya force kuyruğuna ekle, sonra kaynağı boşalt."""
    runtime = _ctx(ctx)
    with QUEUE_LOCK:
        if target_queue not in {"normal", "force"}:
            return 0
        ensure_queue_files(runtime)
        failed_path = queue_path("failed", runtime)
        failed_text = failed_path.read_text(encoding="utf-8", errors="replace")
        urls = parse_urls(failed_text)
        if not urls:
            return 0
        append_urls(target_queue, urls, runtime)
        failed_path.write_text("# LMZ Failed Links Log\n", encoding="utf-8")
        return len(urls)


def clear_failed(ctx: WorkspaceContext | None = None):
    write_queue("failed", "# LMZ Failed Links Log\n", ctx)


def run_queue(queue: str, ctx: WorkspaceContext | None = None) -> dict:
    """Normal veya force kuyruğunu çalıştır; force için benzerlik doğrulamasını atla."""
    if queue not in {"normal", "force"}:
        raise ValueError("failed queue cannot be started")
    from external_ingestion import ExternalIngestor

    path = queue_path(queue, ctx)
    skip_validation = queue == "force"
    ingestor = ExternalIngestor(str(path), skip_validation=skip_validation, ctx=ctx)
    return ingestor.run()
