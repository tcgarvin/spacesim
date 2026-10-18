"""Streaming Parquet writer with batched writes."""

from pathlib import Path
from typing import Any, Dict, List, Optional

import pyarrow as pa
import pyarrow.parquet as pq


class StreamingParquetWriter:
    """Write rows to Parquet in batches so memory stays bounded."""

    def __init__(self, filepath: Path, schema: pa.Schema, batch_size: int = 1000):
        """Create a writer that flushes every ``batch_size`` rows."""
        self.filepath = filepath
        self.schema = schema
        self.batch_size = batch_size
        self.buffer: List[Dict[str, Any]] = []
        self.writer: Optional[pq.ParquetWriter] = None

    def write_row(self, row_dict: Dict[str, Any]) -> None:
        """Buffer a row keyed by schema field name; flush at batch_size."""
        self.buffer.append(row_dict)

        if len(self.buffer) >= self.batch_size:
            self.flush()

    def flush(self) -> None:
        """Write buffered rows to the Parquet file."""
        if not self.buffer:
            return

        arrays = {}
        for field in self.schema:
            field_name = field.name
            arrays[field_name] = [row.get(field_name) for row in self.buffer]

        table = pa.table(arrays, schema=self.schema)

        if self.writer is None:
            self.writer = pq.ParquetWriter(
                self.filepath, self.schema, compression="snappy"
            )

        self.writer.write_table(table)

        self.buffer = []

    def close(self) -> None:
        """Flush remaining rows and close the writer."""
        self.flush()
        if self.writer:
            self.writer.close()
            self.writer = None
        elif not self.filepath.exists():
            # Write an empty file so readers find every table.
            empty_table = pa.table(
                {field.name: [] for field in self.schema}, schema=self.schema
            )
            pq.write_table(empty_table, self.filepath, compression="snappy")
