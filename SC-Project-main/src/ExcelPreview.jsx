import React, { useEffect, useState } from "react";

function Worksheet({ data }) {
  const merged = new Map();
  for (const { row, column, rowSpan, colSpan } of data.merges ?? []) {
    for (let y = row; y < row + rowSpan; y++) {
      for (let x = column; x < column + colSpan; x++) {
        merged.set(`${y}:${x}`, null);
      }
    }
    merged.set(`${row}:${column}`, { rowSpan, colSpan });
  }
  const widths = data.columns.map((_, index) =>
    Math.max(48, Math.min(720, Math.round((data.column_widths?.[index] ?? 18) * 7 + 5)))
  );
  return <div className="excel-preview-scroll" tabIndex={0} aria-label={`${data.sheet} worksheet cells`}>
    <table aria-label={data.sheet} style={{ width: 56 + widths.reduce((sum, width) => sum + width, 0) }}>
      <colgroup><col style={{ width: 56 }} />{widths.map((width, index) => <col key={data.columns[index]} style={{ width }} />)}</colgroup>
      <thead><tr><th scope="col">Row</th>{data.columns.map(column => <th key={column} scope="col">{column}</th>)}</tr></thead>
      <tbody>{data.rows.map((row, index) => <tr key={data.start_row + index}>
        <th scope="row">{data.start_row + index}</th>
        {row.map((cell, column) => {
          const span = merged.get(`${index}:${column}`);
          return span === null ? null : <td key={column} {...span}>{cell}</td>;
        })}
      </tr>)}</tbody>
    </table>
  </div>;
}

export default function ExcelPreview({ url, load, onClose }) {
  const [sheet, setSheet] = useState("");
  const [page, setPage] = useState(1);
  const [result, setResult] = useState({});
  const query = new URLSearchParams({ page });
  if (sheet) query.set("sheet", sheet);
  const request = url + "?" + query;
  useEffect(() => {
    const controller = new AbortController();
    load(request, { signal: controller.signal }).then(
      data => { if (!controller.signal.aborted) setResult({ request, data }); },
      error => { if (!controller.signal.aborted) setResult({ request, error: error.message }); }
    );
    return () => controller.abort();
  }, [request, load]);
  const { data, error } = result.request === request ? result : {};
  return <section className="excel-preview" aria-label="Excel preview">
    <div className="excel-preview-toolbar"><h3>Excel preview</h3><button type="button" className="secondary-button" onClick={onClose}>Close preview</button></div>
    <p>Preview of saved data. Use Save to include changes to your notes.</p>
    {error ? <p role="alert">{error}</p> : !data ? <p role="status">Loading workbook…</p> : <>
      <nav className="excel-preview-sheets" aria-label="Worksheets">
        {data.sheets.map(name => <button type="button" key={name}
          aria-current={name === data.sheet ? "true" : undefined}
          onClick={() => { if (name !== data.sheet) { setSheet(name); setPage(1); } }}>{name}</button>)}
      </nav>
      <Worksheet data={data} />
      <div className="excel-preview-toolbar excel-preview-footer">
        <span>Rows {data.start_row}–{data.start_row + data.rows.length - 1} of {data.total_rows}</span>
        <button type="button" className="secondary-button" disabled={page <= 1} onClick={() => setPage(page - 1)}>Previous</button>
        <span>Page {data.page} of {data.pages}</span>
        <button type="button" className="secondary-button" disabled={page >= data.pages} onClick={() => setPage(page + 1)}>Next</button>
      </div>
    </>}
  </section>;
}
