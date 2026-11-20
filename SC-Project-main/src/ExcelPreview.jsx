import React, { useEffect, useState } from "react";

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
      <div className="excel-preview-toolbar">
        <label className="audit-field">Worksheet<select value={data.sheet} onChange={event => { setSheet(event.target.value); setPage(1); }}>
          {data.sheets.map(name => <option key={name}>{name}</option>)}
        </select></label>
        <button className="secondary-button" disabled={page <= 1} onClick={() => setPage(page - 1)}>Previous</button>
        <span>Page {data.page} / {data.pages}</span>
        <button className="secondary-button" disabled={page >= data.pages} onClick={() => setPage(page + 1)}>Next</button>
      </div>
      <div className="excel-preview-scroll" tabIndex={0} aria-label="Worksheet cells">
        <table><thead><tr><th scope="col">Row</th>{data.columns.map(column => <th key={column} scope="col">{column}</th>)}</tr></thead>
          <tbody>{data.rows.map((row, index) => <tr key={index}><th scope="row">{data.start_row + index}</th>{row.map((cell, column) => <td key={column}>{cell}</td>)}</tr>)}</tbody></table>
      </div>
    </>}
  </section>;
}
