"""Execution-thread Sessions and per-tool scoped Readers, with no framework types."""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, field, replace

from sqlalchemy.orm import Session

from dlr.control.ai.attachment_reading import ScopedAttachmentReader
from dlr.control.ai.tools import ToolExecutionContext

type ReaderFactory = Callable[[], AbstractContextManager[ScopedAttachmentReader]]


@dataclass(frozen=True)
class ToolResourceFactory:
    session_factory: Callable[[], Session] = field(repr=False)
    template: ToolExecutionContext = field(repr=False)
    reader_factory: ReaderFactory | None = field(default=None, repr=False)
    permission_check: Callable[[], object] | None = field(default=None, repr=False)

    @contextmanager
    def open(self) -> Iterator[ToolExecutionContext]:
        if self.permission_check is not None:
            self.permission_check()
        reader_context = (
            self.reader_factory()
            if self.reader_factory is not None
            else nullcontext(self.template.attachment_reader)
        )
        # Construct and close the Session in the actual execution thread. A
        # live Reader context owns its pin until physical tool work completes.
        with self.session_factory() as session, reader_context as reader:
            yield replace(
                self.template,
                session=session,
                secret_values=list(self.template.secret_values),
                attachment_reader=reader,
            )
