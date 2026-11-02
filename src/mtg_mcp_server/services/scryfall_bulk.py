"""Scryfall bulk card data service — lazy download and in-memory card cache.

Download Scryfall's Oracle Cards bulk data once and keep it in memory for O(1)
card lookups and fast substring searches. The download is streamed to a temp
file and parsed line by line, so peak memory is roughly the final card set,
not the whole decompressed payload plus every raw card dict. Refresh automatically when the data
becomes stale (default 12h). On refresh failure with existing data, serve stale
data rather than propagating the error.

Unlike :class:`BaseClient`, this is a standalone service managing its own HTTP
downloads. It does not use rate limiting or retries — the bulk-data endpoint is
a single lightweight request and the bulk download is a large file fetch, neither
benefiting from the per-request rate-limit pattern that ``BaseClient`` provides.

Returns :class:`~mtg_mcp_server.types.Card` objects with prices, legalities,
EDHREC rank, and image URIs. Checks the ``/bulk-data/oracle_cards`` metadata
endpoint before download, supports ETag-based conditional downloads, uses
``asyncio.Lock`` to prevent duplicate concurrent downloads, and runs a
background refresh loop via ``asyncio.create_task()``.
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import json
import os
import random
import sys
import tempfile
import time
import zlib
from pathlib import Path
from typing import TYPE_CHECKING, Self

import httpx
import structlog
from pydantic import ValidationError

from mtg_mcp_server.services.base import DEFAULT_USER_AGENT, ServiceError
from mtg_mcp_server.types import Card

if TYPE_CHECKING:
    import io
    from collections.abc import Iterator
    from typing import Any as JSONValue

__all__ = ["ScryfallBulkClient", "ScryfallBulkDownloadError", "ScryfallBulkError"]

log = structlog.get_logger(service="ScryfallBulkClient")

# Scryfall Oracle Cards includes non-playable entries (minigames, art series,
# tokens, emblems) that can share names with real cards. Filter these out during
# parsing. Uses a deny-list so new playable layouts are included by default.
_EXCLUDED_LAYOUTS: frozenset[str] = frozenset(
    {
        "art_series",
        "double_faced_token",
        "emblem",
        "minigame",
        "placeholder",
        "token",
    }
)


# Download in 1 MiB chunks so the body is never held in memory as a whole.
_DOWNLOAD_CHUNK_SIZE = 1 << 20


def _intern_card(card: Card) -> None:
    """Share repeated short strings across cards to cut resident memory.

    Every card carries a ~20-entry ``legalities`` dict whose keys and values
    ("commander", "legal", ...) repeat across ~36k cards, plus repeated
    rarity/layout/set/color/keyword strings. JSON parsing creates a fresh
    string object for each, so interning them saves tens of MB.
    """
    card.legalities = {sys.intern(k): sys.intern(v) for k, v in card.legalities.items()}
    card.colors = [sys.intern(c) for c in card.colors]
    card.color_identity = [sys.intern(c) for c in card.color_identity]
    card.keywords = [sys.intern(k) for k in card.keywords]
    card.rarity = sys.intern(card.rarity)
    card.layout = sys.intern(card.layout)
    card.set_code = sys.intern(card.set_code)


class ScryfallBulkError(ServiceError):
    """Base exception for Scryfall bulk data service errors.

    Always passes ``status_code=None`` since bulk data operations don't map
    cleanly to a single HTTP status.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=None)


class ScryfallBulkDownloadError(ScryfallBulkError):
    """Error downloading Scryfall bulk data (network or HTTP failure)."""


class ScryfallBulkClient:
    """Manages Scryfall Oracle Cards bulk data with lazy download and refresh.

    Downloads the Oracle Cards JSON on first access and caches parsed
    :class:`Card` models in memory. Refreshes automatically when
    ``refresh_hours`` has elapsed, using ETag-based conditional downloads
    to avoid re-parsing unchanged data.

    Use as an async context manager::

        async with ScryfallBulkClient(base_url=...) as client:
            card = await client.get_card("Sol Ring")
    """

    def __init__(
        self,
        *,
        base_url: str = "https://api.scryfall.com",
        refresh_hours: int = 12,
    ) -> None:
        """Initialize the Scryfall bulk client.

        Args:
            base_url: Scryfall API base URL (for metadata endpoint).
            refresh_hours: Hours before re-downloading data.
        """
        self._base_url = base_url
        self._refresh_seconds = refresh_hours * 3600

        # _cards: lowercase-name -> Card for O(1) exact lookup.
        # _unique_cards: deduplicated list for linear substring search.
        # Separation avoids double-counting DFCs which have two keys
        # in _cards (front-face name + full "//" name) but one entry
        # in _unique_cards.
        self._cards: dict[str, Card] = {}
        self._unique_cards: list[Card] = []
        self._loaded_at: float = 0.0  # monotonic timestamp; 0 = never loaded

        # ETag for conditional download (HTTP 304 Not Modified)
        self._etag: str | None = None
        self._etag_url: str | None = None  # URL the ETag applies to

        # asyncio.Lock prevents duplicate concurrent downloads
        self._load_lock = asyncio.Lock()

        # Background refresh task
        self._refresh_task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Self:
        """Enter async context. Data is loaded lazily on first access, not here."""
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Cancel background refresh and release in-memory data."""
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._refresh_task
            self._refresh_task = None

        self._cards.clear()
        self._unique_cards.clear()
        self._loaded_at = 0.0

    def start_background_refresh(self) -> None:
        """Start the background refresh loop.

        Creates an ``asyncio.Task`` that periodically calls
        :meth:`ensure_loaded` at the refresh interval. The task is
        cancelled in :meth:`__aexit__`.
        """
        if self._refresh_task is not None:
            return  # Already running
        self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def ensure_loaded(self) -> None:
        """Download and parse Oracle Cards if not loaded or stale.

        On first load failure, the error propagates (server cannot start
        without data). On **refresh** failure (data was previously loaded),
        logs a warning and serves stale data.

        Uses ``asyncio.Lock`` to prevent duplicate concurrent downloads.

        Raises:
            ScryfallBulkDownloadError: On first-load network/HTTP failure.
            ScryfallBulkError: On first-load parse failure.
        """
        if not self._is_stale():
            return

        async with self._load_lock:
            # Double-check after acquiring lock — another coroutine may have
            # already completed the load while we were waiting.
            if not self._is_stale():
                return

            is_refresh = self._loaded_at > 0
            log.info("scryfall_bulk.loading", base_url=self._base_url, is_refresh=is_refresh)

            try:
                # Step 1: Fetch metadata to get the current download URL.
                # Scryfall renamed download_uri → jsonl_download_uri (2026);
                # prefer the new field, fall back to the legacy name.
                metadata = await self._fetch_metadata()
                download_url = self._resolve_download_url(metadata)

                # Step 2: Stream the bulk data to a temp file (with ETag if URL matches)
                path = await self._download(download_url)

                if path is not None:
                    # Got new data — parse it off the event loop, then swap in.
                    # Old data keeps serving until the new set is complete.
                    try:
                        cards, unique = await asyncio.to_thread(self._parse_file, path)
                    finally:
                        path.unlink(missing_ok=True)
                    self._cards = cards
                    self._unique_cards = unique

                # Either we parsed new data or got 304 — update timestamp
                self._loaded_at = time.monotonic()
                log.info(
                    "scryfall_bulk.loaded",
                    card_count=len(self._unique_cards),
                )
            except ScryfallBulkError:
                if is_refresh:
                    # Stale data is better than no data — retry in 5 min, not full interval
                    log.warning(
                        "scryfall_bulk.refresh_failed",
                        base_url=self._base_url,
                        exc_info=True,
                    )
                    self._loaded_at = time.monotonic() - self._refresh_seconds + 300
                    return
                raise

    async def get_card(self, name: str) -> Card | None:
        """Look up a card by exact name (case-insensitive).

        Args:
            name: Card name (front-face or full ``//`` name for DFCs).

        Returns:
            Card data, or None if not found.
        """
        await self.ensure_loaded()
        return self._cards.get(name.lower())

    async def search_cards(self, query: str, limit: int = 20) -> list[Card]:
        """Search cards by name substring (case-insensitive).

        Args:
            query: Substring to match against card names.
            limit: Maximum results to return.

        Returns:
            Matching cards, up to ``limit``.
        """
        await self.ensure_loaded()
        query_lower = query.lower()
        results: list[Card] = []
        for card in self._unique_cards:
            if query_lower in card.name.lower():
                results.append(card)
                if len(results) >= limit:
                    break
        return results

    async def search_by_type(self, type_query: str, limit: int = 20) -> list[Card]:
        """Search cards by type line substring (case-insensitive).

        Args:
            type_query: Substring to match against type lines.
            limit: Maximum results to return.

        Returns:
            Matching cards, up to ``limit``.
        """
        await self.ensure_loaded()
        query_lower = type_query.lower()
        results: list[Card] = []
        for card in self._unique_cards:
            if query_lower in card.type_line.lower():
                results.append(card)
                if len(results) >= limit:
                    break
        return results

    async def search_by_text(self, text_query: str, limit: int = 20) -> list[Card]:
        """Search cards by oracle text substring (case-insensitive).

        Args:
            text_query: Substring to match against oracle text.
            limit: Maximum results to return.

        Returns:
            Matching cards, up to ``limit``.
        """
        await self.ensure_loaded()
        query_lower = text_query.lower()
        results: list[Card] = []
        for card in self._unique_cards:
            oracle = card.oracle_text or ""
            if query_lower in oracle.lower():
                results.append(card)
                if len(results) >= limit:
                    break
        return results

    async def filter_cards(
        self,
        *,
        format: str | None = None,
        color_identity: frozenset[str] | None = None,
        type_contains: list[str] | None = None,
        text_contains: list[str] | None = None,
        text_any: list[str] | None = None,
        keywords: list[str] | None = None,
        cmc_eq: float | None = None,
        cmc_lte: float | None = None,
        max_price: float | None = None,
        rarity: str | None = None,
        name_contains: str | None = None,
        limit: int = 100,
    ) -> list[Card]:
        """Multi-criteria filter over all cards. Returns up to ``limit`` matches.

        All filter parameters are optional. Only cards matching **all** specified
        criteria are returned (AND logic). ``text_any`` is the exception: it uses
        OR logic among its terms.

        Args:
            format: Only cards where ``legalities[format] == "legal"``.
            color_identity: Cards whose color identity is a subset of this set.
            type_contains: ALL strings must appear in the type line (case-insensitive).
            text_contains: ALL strings must appear in oracle text (case-insensitive).
            text_any: ANY string must appear in oracle text (case-insensitive).
            keywords: ALL must be in the card's keywords (case-insensitive).
            cmc_eq: Exact CMC match.
            cmc_lte: CMC at or below this value.
            max_price: Maximum USD price (cards without a USD price are excluded).
            rarity: Exact rarity match (e.g. ``"mythic"``).
            name_contains: Substring match on card name (case-insensitive).
            limit: Maximum results to return.

        Returns:
            Matching cards, up to ``limit``.
        """
        await self.ensure_loaded()

        # Pre-compute lowercase versions of string filters
        type_lower = [t.lower() for t in type_contains] if type_contains else None
        text_lower = [t.lower() for t in text_contains] if text_contains else None
        text_any_lower = [t.lower() for t in text_any] if text_any else None
        kw_lower = [k.lower() for k in keywords] if keywords else None
        name_lower = name_contains.lower() if name_contains else None

        results: list[Card] = []
        for card in self._unique_cards:
            if format is not None and card.legalities.get(format) != "legal":
                continue
            if color_identity is not None and not frozenset(card.color_identity).issubset(
                color_identity
            ):
                continue
            if type_lower is not None:
                tl = card.type_line.lower()
                if not all(t in tl for t in type_lower):
                    continue
            if text_lower is not None:
                oracle = (card.oracle_text or "").lower()
                if not all(t in oracle for t in text_lower):
                    continue
            if text_any_lower is not None:
                oracle = (card.oracle_text or "").lower()
                if not any(t in oracle for t in text_any_lower):
                    continue
            if kw_lower is not None:
                card_kw = {k.lower() for k in card.keywords}
                if not all(k in card_kw for k in kw_lower):
                    continue
            if cmc_eq is not None and card.cmc != cmc_eq:
                continue
            if cmc_lte is not None and card.cmc > cmc_lte:
                continue
            if max_price is not None:
                if card.prices.usd is None:
                    continue
                try:
                    if float(card.prices.usd) > max_price:
                        continue
                except ValueError:
                    continue
            if rarity is not None and card.rarity != rarity:
                continue
            if name_lower is not None and name_lower not in card.name.lower():
                continue

            results.append(card)
            if len(results) >= limit:
                break

        return results

    async def cards_by_legality(self, format: str, status: str) -> list[Card]:
        """All cards with a specific legality status in a format.

        Args:
            format: Format name (e.g. ``"commander"``, ``"modern"``).
            status: Legality status (e.g. ``"banned"``, ``"restricted"``, ``"legal"``).

        Returns:
            All matching cards (no limit).
        """
        await self.ensure_loaded()
        return [card for card in self._unique_cards if card.legalities.get(format) == status]

    async def all_cards(self) -> list[Card]:
        """All unique cards in the bulk data set.

        Ensures data is loaded before returning. This is the public
        interface for iterating over the full card pool — providers
        should use this instead of accessing ``_unique_cards`` directly.

        Returns:
            All unique Card objects.
        """
        await self.ensure_loaded()
        return self._unique_cards

    async def get_cards(self, names: list[str]) -> dict[str, Card | None]:
        """Batch exact-name lookup. Returns dict of name -> Card|None.

        Keys in the returned dict preserve the original casing from ``names``.
        Lookups are case-insensitive.

        Args:
            names: Card names to look up.

        Returns:
            Dict mapping each input name to its Card or None if not found.
        """
        await self.ensure_loaded()
        return {name: self._cards.get(name.lower()) for name in names}

    async def random_card(
        self,
        *,
        format: str | None = None,
        color_identity: frozenset[str] | None = None,
        type_contains: str | None = None,
        rarity: str | None = None,
    ) -> Card | None:
        """Random card from a filtered pool. None if pool is empty.

        Args:
            format: Only cards where ``legalities[format] == "legal"``.
            color_identity: Cards whose color identity is a subset of this set.
            type_contains: String that must appear in the type line (case-insensitive).
            rarity: Exact rarity match.

        Returns:
            A random matching card, or None if no cards match.
        """
        await self.ensure_loaded()

        pool: list[Card] = []
        type_lower = type_contains.lower() if type_contains else None

        for card in self._unique_cards:
            if format is not None and card.legalities.get(format) != "legal":
                continue
            if color_identity is not None and not frozenset(card.color_identity).issubset(
                color_identity
            ):
                continue
            if type_lower is not None and type_lower not in card.type_line.lower():
                continue
            if rarity is not None and card.rarity != rarity:
                continue
            pool.append(card)

        if not pool:
            return None
        return random.choice(pool)

    # -------------------------------------------------------------------
    # Private helpers
    # -------------------------------------------------------------------

    def _is_stale(self) -> bool:
        """Check if the loaded data has exceeded the refresh interval."""
        if self._loaded_at == 0.0:
            return True
        return (time.monotonic() - self._loaded_at) >= self._refresh_seconds

    async def _fetch_metadata(self) -> dict:
        """Fetch the bulk-data metadata to get the current download URI.

        Raises:
            ScryfallBulkDownloadError: On HTTP errors or network failures.
        """
        url = f"{self._base_url}/bulk-data/oracle_cards"
        try:
            async with httpx.AsyncClient(
                timeout=30.0,
                headers={
                    "User-Agent": DEFAULT_USER_AGENT,
                    "Accept": "application/json",
                },
            ) as http:
                response = await http.get(url)
                if response.status_code != 200:
                    raise ScryfallBulkDownloadError(
                        f"HTTP {response.status_code} fetching bulk metadata from {url}"
                    )
                try:
                    return response.json()
                except (json.JSONDecodeError, ValueError) as exc:
                    raise ScryfallBulkDownloadError(
                        f"Metadata response is not valid JSON from {url}: {exc}"
                    ) from exc
        except httpx.RequestError as exc:
            raise ScryfallBulkDownloadError(f"Network error fetching bulk metadata: {exc}") from exc

    async def _download(self, url: str) -> Path | None:
        """Stream the Oracle Cards bulk file to a temporary file on disk.

        The body is written to disk in chunks and never held in memory as a
        whole. Sends ``If-None-Match`` when the URL matches the previous
        download's URL and we have a saved ETag. Returns ``None`` on HTTP
        304 (data unchanged). The caller owns the returned file and must
        delete it.

        Raises:
            ScryfallBulkDownloadError: On HTTP errors or network failures.
        """
        headers: dict[str, str] = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "application/json",
        }

        # Only send If-None-Match if the URL matches the one we got the ETag from
        if self._etag is not None and self._etag_url == url:
            headers["If-None-Match"] = self._etag

        tmp_path: Path | None = None
        try:
            async with (
                httpx.AsyncClient(timeout=120.0, headers=headers) as http,
                http.stream("GET", url) as response,
            ):
                if response.status_code == 304:
                    log.info("scryfall_bulk.not_modified", url=url)
                    return None

                if response.status_code != 200:
                    raise ScryfallBulkDownloadError(
                        f"HTTP {response.status_code} downloading bulk data from {url}"
                    )

                fd, name = tempfile.mkstemp(prefix="scryfall-bulk-", suffix=".download")
                tmp_path = Path(name)
                size = 0
                with os.fdopen(fd, "wb") as fh:
                    async for chunk in response.aiter_bytes(_DOWNLOAD_CHUNK_SIZE):
                        fh.write(chunk)
                        size += len(chunk)
                log.info("scryfall_bulk.downloaded", url=url, bytes=size)

                # Save ETag only after the full body arrived
                etag = response.headers.get("ETag")
                if etag:
                    self._etag = etag
                    self._etag_url = url

                path, tmp_path = tmp_path, None  # hand ownership to the caller
                return path
        except httpx.RequestError as exc:
            raise ScryfallBulkDownloadError(f"Network error downloading bulk data: {exc}") from exc
        except OSError as exc:
            raise ScryfallBulkDownloadError(f"Could not write bulk data to disk: {exc}") from exc
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

    @staticmethod
    def _resolve_download_url(metadata: dict) -> str:
        """Extract the download URL from bulk metadata across the field rename.

        Prefers ``jsonl_download_uri`` (current field); falls back to the
        legacy ``download_uri`` for older-shaped responses, with a log line
        so the fallback is never silent.

        Raises:
            ScryfallBulkError: If neither field carries a usable string.
        """
        jsonl_uri = metadata.get("jsonl_download_uri")
        if isinstance(jsonl_uri, str) and jsonl_uri:
            return jsonl_uri

        legacy_uri = metadata.get("download_uri")
        if isinstance(legacy_uri, str) and legacy_uri:
            log.info(
                "scryfall_bulk.legacy_download_uri_fallback",
                hint="metadata lacks jsonl_download_uri; using download_uri",
                metadata_keys=sorted(metadata.keys()),
            )
            return legacy_uri

        raise ScryfallBulkError(
            "Bulk metadata missing both 'jsonl_download_uri' and 'download_uri'. "
            f"Available keys: {list(metadata.keys())}"
        )

    @staticmethod
    def _open_payload(path: Path) -> io.BufferedIOBase:
        """Open the downloaded file, decompressing on the fly if it is gzip.

        Scryfall's current bulk files are served as ``.jsonl.gz`` with
        ``Content-Type: application/gzip`` (no ``Content-Encoding``), so httpx
        does not auto-decompress them. Detection is content-based (magic
        bytes), never URL-based, so either field's download works.
        """
        with path.open("rb") as probe:
            magic = probe.read(2)
        if magic == b"\x1f\x8b":
            return gzip.open(path, "rb")
        return path.open("rb")

    @staticmethod
    def _iter_entries(fh: io.BufferedIOBase) -> Iterator[JSONValue]:
        """Yield card entries one at a time from either payload shape.

        Current shape (``jsonl_download_uri``): JSONL, one card object per
        line, parsed line by line so only one raw entry is in memory at a
        time. Legacy shape (``download_uri``): one JSON array; parsed as a
        whole because the format cannot be split by line. A lone JSON
        object on one line is a degenerate one-line JSONL file.

        Raises:
            ScryfallBulkError: On corrupt gzip, undecodable bytes, a malformed
                JSONL line, or a JSON document that is not an array/object.
        """
        try:
            first = fh.read(1)
            while first and first.isspace():
                first = fh.read(1)
            if not first:
                return

            if first == b"[":
                raw = json.loads(first + fh.read())
                if not isinstance(raw, list):  # pragma: no cover - "[" implies list
                    raise ScryfallBulkError("Bulk data is not a JSON array")
                yield from raw
                return

            lineno = 0
            pending = first
            for line in fh:
                lineno += 1
                if pending:
                    line, pending = pending + line, b""
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ScryfallBulkError(
                        f"Failed to parse bulk data (JSONL line {lineno}): {exc}"
                    ) from exc
            if pending.strip():  # payload was a single byte with no newline
                lineno += 1
                try:
                    yield json.loads(pending)
                except json.JSONDecodeError as exc:
                    raise ScryfallBulkError(
                        f"Failed to parse bulk data (JSONL line {lineno}): {exc}"
                    ) from exc
        except UnicodeDecodeError as exc:
            raise ScryfallBulkError(f"Failed to parse bulk data: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ScryfallBulkError(f"Failed to parse bulk data: {exc}") from exc
        except (gzip.BadGzipFile, EOFError, zlib.error) as exc:
            raise ScryfallBulkError(f"Failed to parse bulk data: corrupt gzip: {exc}") from exc

    def _parse_file(self, path: Path) -> tuple[dict[str, Card], list[Card]]:
        """Parse a downloaded Oracle Cards file into lookup structures.

        Reads the file as a stream: each raw entry is validated into a
        :class:`Card` and then dropped, so peak memory is the final card
        set plus one entry, not the whole decompressed payload. Pure
        function of the file (does not touch ``self`` state), so it is safe
        to run in a worker thread.

        For DFCs, ``card.name`` contains the full ``"Front // Back"`` name.
        We key lookups by both the full name and the front face name
        (``name.split(" // ")[0]``).

        Raises:
            ScryfallBulkError: If the payload is malformed or yields no cards.
        """
        cards: dict[str, Card] = {}
        unique: list[Card] = []
        skipped = 0
        total = 0

        try:
            fh = self._open_payload(path)
        except OSError as exc:
            raise ScryfallBulkError(f"Failed to parse bulk data: {exc}") from exc

        with fh:
            for entry in self._iter_entries(fh):
                total += 1
                if not isinstance(entry, dict):
                    skipped += 1
                    continue

                layout = entry.get("layout", "")
                if layout in _EXCLUDED_LAYOUTS:
                    skipped += 1
                    continue

                try:
                    card = Card.model_validate(entry)
                except (ValidationError, ValueError) as exc:
                    log.warning(
                        "scryfall_bulk.card_parse_error",
                        card_name=entry.get("name", "unknown"),
                        error=str(exc),
                    )
                    skipped += 1
                    continue

                _intern_card(card)
                unique.append(card)

                # Key by full lowercase name for O(1) case-insensitive lookup
                full_name_lower = card.name.lower()
                cards[full_name_lower] = card

                # For DFCs, card.name is "Front // Back". Also key by front-face
                # name so lookup by either form works.
                if " // " in card.name:
                    front_face = card.name.split(" // ")[0].lower()
                    if front_face != full_name_lower:
                        cards[front_face] = card

        if skipped:
            log.info("scryfall_bulk.parse_summary", skipped=skipped, loaded=len(unique))

        if not unique:
            raise ScryfallBulkError(
                f"Parsed 0 cards from {total} entries ({skipped} skipped). "
                "Scryfall bulk data schema may have changed."
            )

        return cards, unique

    async def _refresh_loop(self) -> None:
        """Background loop that periodically calls ensure_loaded().

        Runs until cancelled (via __aexit__). Errors are logged but do
        not stop the loop.
        """
        while True:
            await asyncio.sleep(self._refresh_seconds)
            try:
                await self.ensure_loaded()
            except ScryfallBulkError:
                log.warning("scryfall_bulk.background_refresh_error", exc_info=True)
            except Exception:
                log.error("scryfall_bulk.background_refresh_unexpected_error", exc_info=True)
