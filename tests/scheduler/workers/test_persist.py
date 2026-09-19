from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

from crawlme.scheduler.workers import PersistWorker
from crawlme.schemas import URL, FetchResult, Page, Payload


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_index", [None, 1])
async def test_payload_files_ready_before_page_is_queued(tmp_path, caplog, failed_index):
    url = URL(raw="https://example.com/p", canonical="https://example.com/p", url_key="p")
    result = FetchResult(
        item_id="fetch-1",
        url=url,
        url_key="p",
        status_code=200,
        raw=b"body",
        payloads=[Payload(url="https://example.com/api", body=str(i).encode()) for i in range(3)],
    )
    page = Page(url=url, url_key="p")
    loop_thread = threading.get_ident()
    expected = [str(tmp_path / str(i)) for i in range(3) if i != failed_index]
    storage = MagicMock()

    def save_payload(url_key, fetch_id, index, content):
        assert threading.get_ident() != loop_thread
        assert (url_key, fetch_id) == ("p", result.item_id)
        if index == failed_index:
            raise OSError("disk error")
        path = tmp_path / str(index)
        path.write_bytes(content)
        return str(path)

    def save_page(saved):
        assert threading.get_ident() == loop_thread
        assert saved is page
        assert saved.payload_paths == expected
        for i in range(3):
            if i != failed_index:
                assert (tmp_path / str(i)).read_bytes() == str(i).encode()

    storage.save_payload.side_effect = save_payload
    storage.save_page.side_effect = save_page
    await PersistWorker(storage).save_extracted("p", result, page)
    storage.save_page.assert_called_once_with(page)
    assert storage.save_payload.call_count == 3
    if failed_index is not None:
        assert "fetch.payload_unsaved url_key=p index=1" in caplog.text
