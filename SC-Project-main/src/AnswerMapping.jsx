import React, { useState } from "react";
import { Search } from "lucide-react";

export default function AnswerMapping({ assessment, draft, busy, authorized, request, onBusy, onError, onNotice, onApply }) {
  const [notes, setNotes] = useState("");
  const [preview, setPreview] = useState(null);
  const [selected, setSelected] = useState({});
  const current = preview?.current_version === assessment.current_version;

  async function analyze() {
    onBusy(true); onError(""); setPreview(null); setSelected({});
    try {
      const result = await request({ text: notes.trim() });
      setPreview(result);
      setSelected(Object.fromEntries(result.mappings.map(item => [item.control_id, item.confidence >= 0.6 && !draft[item.control_id]?.text])));
      if (!result.mappings.length) onNotice("No clear control matches found. Add more detail to your notes.");
    } catch (error) { onError(error.message); }
    finally { onBusy(false); }
  }

  function apply() {
    if (!current) return;
    const next = { ...draft };
    for (const item of preview.mappings) {
      if (selected[item.control_id]) next[item.control_id] = { text: item.answer_text, status: item.suggested_status };
    }
    onApply(next);
    setPreview(null); setSelected({});
    onNotice("Selected answers added to your draft. Review and save to create an answer version.");
  }

  return <section className="compliance-add-question" aria-labelledby="answer-mapping-title">
    <h3 id="answer-mapping-title">Map short notes to controls</h3>
    <p>Paste one note per line. AI suggests matching controls and drafts answers for your review.</p>
    <textarea aria-label="Short notes to map" rows={5} maxLength={50000} disabled={busy} value={notes} onChange={event => { setNotes(event.target.value); setPreview(null); setSelected({}); }} placeholder={"Example:\nMFA is enabled for administrator accounts\nDR tested for some systems; the accounting system is still planned"}/>
    <button className="secondary-button" disabled={busy || !authorized || !notes.trim()} onClick={analyze}><Search size={15}/>Analyze notes</button>
    {preview && <div className="compliance-mapping-preview">
      <p>Mapping by {preview.provider} · {preview.mappings.length} controls · {preview.unmatched.length} notes need review</p>
      <p>Existing answers are not selected for replacement automatically. Selected proposals replace draft answers; save to keep your changes.</p>
      {!current && <p role="alert">The answer version has changed. Analyze your notes again.</p>}
      {preview.mappings.map(item => <article className="compliance-answer-proposal" key={item.control_id}>
        <label><input type="checkbox" disabled={busy || !current} checked={!!selected[item.control_id]} onChange={event => setSelected(value => ({ ...value, [item.control_id]: event.target.checked }))}/><span><b>{item.control_id} · Confidence {Math.round(item.confidence * 100)}%</b><small>{assessment.controls?.find(control => control.id === item.control_id)?.requirement}</small></span></label>
        <p>Source: {item.source_texts.join(" · ")}</p>
        <textarea aria-label={`Proposed answer for ${item.control_id}`} rows={3} disabled={busy || !current} maxLength={20000} value={item.answer_text} onChange={event => setPreview(value => ({ ...value, mappings: value.mappings.map(proposal => proposal.control_id === item.control_id ? { ...proposal, answer_text: event.target.value } : proposal) }))}/>
        <p>{item.reason}</p>
      </article>)}
      {preview.unmatched.map((item, index) => <div className="compliance-unmatched" key={index}><strong>Needs review: {item.source_text}</strong><p>{item.reason}</p></div>)}
      <button className="primary-button" disabled={busy || !current || !Object.values(selected).some(Boolean) || preview.mappings.some(item => selected[item.control_id] && !item.answer_text.trim())} onClick={apply}>Apply selected to draft</button>
    </div>}
  </section>;
}
