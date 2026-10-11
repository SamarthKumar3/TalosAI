from __future__ import annotations

import asyncio
import importlib
import importlib.util
import multiprocessing
import os
from io import BytesIO
from multiprocessing.connection import Connection

from talos.knowledge import KnowledgeError


def _parse_pdf(sender: Connection, payload: bytes, max_pages: int, max_text_bytes: int) -> None:
    try:
        from pypdf import PdfReader

        if os.name == "posix":
            resource = importlib.import_module("resource")
            resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024,) * 2)
            resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
        reader = PdfReader(BytesIO(payload), strict=True)
        if reader.is_encrypted:
            raise ValueError("encrypted")
        if not 1 <= len(reader.pages) <= max_pages:
            raise ValueError("page limit")
        pages: list[str] = []
        total = 0
        for page in reader.pages:
            text = page.extract_text() or ""
            total += len(text.encode("utf-8"))
            if total > max_text_bytes:
                raise ValueError("extracted text limit")
            pages.append(text)
        sender.send(("ok", pages))
    except Exception:
        sender.send(("error", []))
    finally:
        sender.close()


def _extract(payload: bytes, max_pages: int, max_text_bytes: int, timeout: float) -> list[str]:
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_parse_pdf, args=(sender, payload, max_pages, max_text_bytes), daemon=True
    )
    process.start()
    sender.close()
    try:
        if not receiver.poll(timeout):
            raise KnowledgeError("PDF extraction timed out or failed")
        status, pages = receiver.recv()
        if status != "ok":
            raise KnowledgeError("PDF extraction failed or exceeded a limit")
        return list(pages)
    except EOFError as exc:
        raise KnowledgeError("PDF extraction failed") from exc
    finally:
        receiver.close()
        if process.is_alive():
            process.terminate()
        process.join(timeout=2)
        if process.is_alive():
            process.kill()
            process.join(timeout=2)


async def extract_pdf_pages(
    payload: bytes, *, max_pages: int, max_text_bytes: int, parse_seconds: float
) -> list[str]:
    if importlib.util.find_spec("pypdf") is None:
        raise KnowledgeError("PDF files need 'pip install talos-runtime[pdf]'.")
    return await asyncio.to_thread(_extract, payload, max_pages, max_text_bytes, parse_seconds)
