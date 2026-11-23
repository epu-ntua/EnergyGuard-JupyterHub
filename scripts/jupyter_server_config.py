# Jupyter server config for the EnergyGuard singleuser image.
# Installed as /etc/jupyter/jupyter_server_config.py (see Dockerfile.singleuser).
import csv
import io
import os

from jupyter_server.services.contents.largefilemanager import AsyncLargeFileManager
from tornado import web
from traitlets import Int

c = get_config()  # noqa: F821

c.MappingKernelManager.default_kernel_name = "eg-default"


class EGContentsManager(AsyncLargeFileManager):
    """Open only a preview of files that are too large for the browser.

    Opening a file in JupyterLab (CSV viewer, text editor, ...) makes the
    Jupyter server read the whole file into memory and send it as one JSON
    response, which takes several times the file size. A pilot CSV of a few
    hundred MB is enough to push the server past the container's memory limit,
    and the server is killed.

    Above max_open_mb, a text file is opened as a preview instead: its first
    preview_lines lines (at most preview_max_bytes), followed by a line that
    says it is a preview and how to read the whole file. A file that is not
    text is refused with a message, which JupyterLab shows in a "File Load
    Error" dialog.

    A preview must never be saved over the real file, so saving a whole file
    over an existing file larger than max_open_mb is refused too. Chunked
    uploads (how JupyterLab uploads large files) are not affected.

    Reading the file from a notebook is not affected, and neither are
    downloads (/files/...), which a separate handler streams from disk.
    """

    max_open_mb = Int(
        100,
        config=True,
        help="Largest file (in MB) opened in full in the browser.",
    )
    preview_lines = Int(
        1000,
        config=True,
        help="Lines shown when a larger text file is opened.",
    )
    preview_max_bytes = Int(
        5 * 1024 * 1024,
        config=True,
        help="Upper bound on the preview, for files with very long lines.",
    )

    def _too_large(self, path):
        """(os_path, size) if *path* is a file over max_open_mb, else None."""
        os_path = self._get_os_path(path)
        try:
            if os.path.isfile(os_path):
                size = os.path.getsize(os_path)
                if size > self.max_open_mb * 1024 * 1024:
                    return os_path, size
        except OSError:
            pass
        return None

    def _read_preview(self, os_path):
        """First preview_lines lines as text, or None if the file is not text."""
        lines = []
        read = 0
        with open(os_path, "rb") as fh:
            for line in fh:
                lines.append(line)
                read += len(line)
                if len(lines) >= self.preview_lines or read >= self.preview_max_bytes:
                    break
        data = b"".join(lines)[: self.preview_max_bytes]
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            # A multi-byte character cut at the byte limit is fine; anything
            # earlier means the file is not UTF-8 text.
            if exc.start < len(data) - 4:
                return None
            text = data[: exc.start].decode("utf-8")
        return text, len(lines)

    @staticmethod
    def _tabular_preview(text, delimiter, size_mb):
        """CSV/TSV preview with a notice row under the header and at the end.

        The CSV viewer shows one short phrase per column well, but cuts long
        cells and splits text at the delimiter. So the notice is spread over
        the columns as short cells, written with the csv module so a comma
        inside a cell (e.g. "1,000") stays in its cell.
        """
        lines = text.splitlines(keepends=True)
        # The preview may end in a line cut at the byte limit; drop it.
        if lines and not lines[-1].endswith("\n"):
            lines.pop()
        if not lines:
            return text
        header, rows = lines[0], lines[1:]
        n_cols = max(len(next(csv.reader([header], delimiter=delimiter))), 1)
        phrases = [
            "PREVIEW ONLY",
            f"first {len(rows):,} rows",
            f"file is {size_mb}",
            "read it in a notebook",
        ]
        cells = phrases[: n_cols - 1] + [" - ".join(phrases[n_cols - 1:])]
        cells += [""] * (n_cols - len(cells))
        out = io.StringIO()
        csv.writer(out, delimiter=delimiter, lineterminator="\n").writerow(cells)
        notice = out.getvalue()
        if not header.endswith("\n"):
            header += "\n"
        return header + notice + "".join(rows) + notice

    async def get(self, path, content=True, type=None, format=None, **kwargs):
        big = None
        if content and type not in ("directory", "notebook"):
            big = self._too_large(path)
        if big is None:
            return await super().get(path, content=content, type=type, format=format, **kwargs)

        os_path, size = big
        name = path.rsplit("/", 1)[-1]
        size_mb = f"{size / 1024 / 1024:,.0f} MB"
        how = (
            f"pd.read_csv('{os_path}', chunksize=1_000_000)"
            if name.lower().endswith(".csv")
            else f"open('{os_path}')"
        )
        preview = None if format == "base64" else self._read_preview(os_path)
        if preview is None:
            raise web.HTTPError(
                413,
                f"{name} is {size_mb}, too large to open in the browser "
                f"(limit: {self.max_open_mb} MB). Opening it would run your "
                f"server out of memory. Read it in a notebook instead, for "
                f"example with {how}.",
            )

        text, n_lines = preview
        delimiter = {".csv": ",", ".tsv": "\t"}.get(os.path.splitext(name)[1].lower())
        if delimiter:
            text = self._tabular_preview(text, delimiter, size_mb)
        else:
            if not text.endswith("\n"):
                text += "\n"
            text += (
                f"# PREVIEW ONLY: the first {n_lines:,} lines of {name} ({size_mb}). "
                f"The file is too large to open in full. To read all of it, use a "
                f"notebook, for example {how}\n"
            )
        self.log.info("Opened a %d-line preview of %s (%s)", n_lines, path, size_mb)
        model = await super().get(path, content=False, type="file")
        model.update(
            content=text,
            format="text",
            mimetype=model.get("mimetype") or "text/plain",
            writable=False,
            size=len(text.encode("utf-8")),
        )
        return model

    async def save(self, model, path=""):
        if model.get("chunk") is None and model.get("type") == "file":
            big = self._too_large(path)
            if big is not None:
                name = path.rsplit("/", 1)[-1]
                raise web.HTTPError(
                    403,
                    f"{name} was opened as a preview of its first lines, so "
                    f"saving it here would replace the whole file with the "
                    f"preview. Use File > Save As to save a copy under a new name.",
                )
        return await super().save(model, path)


c.ServerApp.contents_manager_class = EGContentsManager
c.EGContentsManager.max_open_mb = int(os.environ.get("EG_MAX_OPEN_FILE_MB", "100"))
c.EGContentsManager.preview_lines = int(os.environ.get("EG_PREVIEW_LINES", "1000"))
