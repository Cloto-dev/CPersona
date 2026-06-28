"""MGP message interceptor middleware (MGP §12.7 cancel, §12.9 gap).

The MCP Python SDK's ``BaseSession._receive_loop`` validates every incoming
message against the closed ``ClientRequest`` / ``ClientNotification`` Pydantic
unions before dispatching. Custom MGP Layer-3 methods (e.g.
``mgp/stream/cancel``) and custom notifications (``notifications/mgp.stream.gap``)
are not members of those unions, so they get dropped as ``INVALID_PARAMS``
before any handler sees them.

This module provides :func:`mgp_message_interceptor`, an async task that sits
between the raw stdio read stream and the ``ServerSession``'s read stream. It
pre-dispatches MGP messages (responding directly via the write stream) and
forwards everything else untouched, leaving the MCP SDK none the wiser.

Mount point (see :func:`llm_provider.run_server`)::

    stdio_server()  →  raw_read  ──┐
                                    │ mgp_message_interceptor
    ServerSession  ←──  inner_recv ─┘

Cancel flow (MGP §12.7)
    Kernel sends ``{"method":"mgp/stream/cancel","id":N,"params":{"request_id":M,"reason":"..."}}``
    Interceptor looks up ``_active_streams[M]`` → sets ``cancel_event`` and
    writes a ``JSONRPCResponse`` with ``{cancelled: true, partial_result:
    {content: accumulated_text}}`` to the kernel. Main streaming loop observes
    ``cancel_event`` on the next iteration and terminates cleanly.

Gap flow (MGP §12.9)
    Kernel sends ``notifications/mgp.stream.gap`` with ``missing_indices``.
    Interceptor re-emits matching chunks from ``state.chunk_buffer`` via
    ``write_mgp_stream_chunk``. If any requested index is older than the
    buffer retention window, the interceptor emits
    ``notifications/mgp.stream.gap_unrecoverable`` and skips further
    retransmission for this request.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from mcp.types import JSONRPCNotification, JSONRPCRequest

from cpersona._vendored_mcp_common.mgp_utils import (
    write_mgp_method_response,
    write_mgp_notification,
    write_mgp_stream_chunk,
)

if TYPE_CHECKING:
    from anyio.streams.memory import (
        MemoryObjectReceiveStream,
        MemoryObjectSendStream,
    )
    from mcp.shared.message import SessionMessage

    # `StreamState` is the contract from `mcp_common.llm_provider`, which
    # lands in Phase 2 Step 2-ε. Until then this is a forward reference
    # only resolved by static type checkers — runtime never executes this
    # block, so the deferred import does not affect imports or tests.
    from cpersona._vendored_mcp_common.llm_provider import StreamState

logger = logging.getLogger(__name__)


# MGP error codes (see MGP_COMMUNICATION.md §14)
_ERROR_INVALID_PARAMS = -32602


async def mgp_message_interceptor(
    raw_read: MemoryObjectReceiveStream[SessionMessage | Exception],
    inner_send: MemoryObjectSendStream[SessionMessage | Exception],
    write_stream: MemoryObjectSendStream[SessionMessage],
    active_streams: dict[int, StreamState],
) -> None:
    """Forward messages to ``inner_send``; intercept MGP method/notification calls.

    Exits when ``raw_read`` is closed (stdio EOF) or the task is cancelled.
    Exceptions from the stream are forwarded verbatim so the SDK can surface
    them through its normal error path.
    """
    async with raw_read, inner_send:
        async for msg in raw_read:
            if isinstance(msg, Exception):
                await inner_send.send(msg)
                continue

            root = msg.message.root

            if isinstance(root, JSONRPCRequest) and root.method == "mgp/stream/cancel":
                await _handle_mgp_cancel(root, write_stream, active_streams)
                continue  # consumed: do not forward to ServerSession

            if (
                isinstance(root, JSONRPCNotification)
                and root.method == "notifications/mgp.stream.gap"
            ):
                await _handle_mgp_gap(root, write_stream, active_streams)
                continue  # consumed

            await inner_send.send(msg)


async def _handle_mgp_cancel(
    request: JSONRPCRequest,
    write_stream: MemoryObjectSendStream[SessionMessage],
    active_streams: dict[int, StreamState],
) -> None:
    """Respond to ``mgp/stream/cancel`` and signal the active stream to stop.

    Response shape (MGP §12.7)::

        {"cancelled": true,  "partial_result": {"content": "<accumulated>"}}
        {"cancelled": false, "reason": "not_active"}             # no stream

    ``partial_result`` is a best-effort snapshot taken at cancel-receipt time.
    The streaming handler returns its own authoritative final response (with
    ``_mgp.cancelled = true``) when it observes ``cancel_event``; these two
    payloads are intentionally independent — kernel implementations may pick
    either one.
    """
    params = request.params or {}
    target_id = params.get("request_id")
    reason = params.get("reason", "unspecified")

    if not isinstance(target_id, int):
        await _reply_error(
            write_stream,
            request.id,
            _ERROR_INVALID_PARAMS,
            "mgp/stream/cancel: params.request_id must be an integer",
        )
        return

    state = active_streams.get(target_id)
    if state is None:
        # Stream already completed or never existed. Not a protocol error —
        # respond success=false so the kernel can log and move on.
        await write_mgp_method_response(
            write_stream,
            request.id,
            {"cancelled": False, "reason": "not_active"},
        )
        return

    # Signal the main streaming loop to terminate on its next iteration.
    # String assignment is atomic in Python, so the handler observes the
    # reason consistently once it sees cancel_event.is_set().
    state.cancelled_reason = str(reason)
    state.cancel_event.set()

    await write_mgp_method_response(
        write_stream,
        request.id,
        {
            "cancelled": True,
            "partial_result": {"content": state.accumulated_text},
        },
    )
    logger.debug(
        "mgp/stream/cancel acknowledged: request_id=%s reason=%s chunks_buffered=%s",
        target_id,
        reason,
        len(state.chunk_buffer),
    )


async def _handle_mgp_gap(
    notification: JSONRPCNotification,
    write_stream: MemoryObjectSendStream[SessionMessage],
    active_streams: dict[int, StreamState],
) -> None:
    """Handle ``notifications/mgp.stream.gap`` by retransmitting from the buffer.

    For each index in ``missing_indices`` found in ``state.chunk_buffer``, emit
    a ``notifications/mgp.stream.chunk`` with the original content (same index,
    same text). If any requested index is older than the retention window,
    emit ``notifications/mgp.stream.gap_unrecoverable`` once and stop; further
    retransmission cannot help the kernel recover the missing data.
    """
    params = notification.params or {}
    target_id = params.get("request_id")
    missing = params.get("missing_indices") or []

    if not isinstance(target_id, int) or not isinstance(missing, list):
        logger.debug("mgp.stream.gap: ignoring malformed params: %s", params)
        return

    state = active_streams.get(target_id)
    if state is None:
        # Stream already completed; kernel will fall back to the final response.
        await write_mgp_notification(
            write_stream,
            "notifications/mgp.stream.gap_unrecoverable",
            {"request_id": target_id, "reason": "stream_completed"},
        )
        return

    buffer_by_index = {idx: content for idx, content in state.chunk_buffer}

    for idx in sorted(i for i in missing if isinstance(i, int)):
        content = buffer_by_index.get(idx)
        if content is None:
            # Index fell out of the retention window.
            await write_mgp_notification(
                write_stream,
                "notifications/mgp.stream.gap_unrecoverable",
                {
                    "request_id": target_id,
                    "missing_index": idx,
                    "reason": "chunk_evicted",
                },
            )
            return
        await write_mgp_stream_chunk(
            write_stream,
            request_id=target_id,
            index=idx,
            content=content,
            done=False,
            mgp_meta={"retransmit": True},
        )


async def _reply_error(
    write_stream: MemoryObjectSendStream[SessionMessage],
    request_id: int | str,
    code: int,
    message: str,
) -> None:
    from cpersona._vendored_mcp_common.mgp_utils import write_mgp_method_error

    await write_mgp_method_error(write_stream, request_id, code, message)
