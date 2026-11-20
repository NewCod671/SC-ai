import React, { useState } from "react";
import { ChevronLeft, ChevronRight, Search } from "lucide-react";

export default function ClauseReview({ controls, draft, busy, run, onAnswer, onReviewNote }) {
  const [selected, setSelected] = useState("");
  const [reviews, setReviews] = useState({});
  const [error, setError] = useState("");
  const [checking, setChecking] = useState(false);
  const control = controls.find(item => item.id === selected) || controls[0];
  const index = controls.indexOf(control);
  const note = draft[control?.id]?.text || "";
  const review = reviews[control?.id]?.note === note ? reviews[control.id].result : null;
  const finding = run?.results?.find(item => item.control_id === control?.id);
  const grade = { comply: "C", ofi: "OFI", nc_minor: "NC-Minor", nc_major: "NC-Major", needs_review: "Needs review", not_applicable: "N/A" }[finding?.audit?.category] || finding?.status || "Not assessed";
  function select(id) { setSelected(id); setError(""); }
  async function check() {
    setError("");
    setChecking(true);
    setReviews(current => ({ ...current, [control.id]: undefined }));
    try {
      const result = await onReviewNote(control.id, note);
      setReviews(current => ({ ...current, [control.id]: { note, result } }));
    } catch (failure) { setError(failure.message); }
    finally { setChecking(false); }
  }
  const previous = <button className="secondary-button" disabled={busy || index <= 0} onClick={() => select(controls[index - 1].id)}><ChevronLeft size={16}/>Previous clause</button>;
  const next = <button className="primary-button" disabled={busy || index < 0 || index >= controls.length - 1} onClick={() => select(controls[index + 1].id)}>Next clause<ChevronRight size={16}/></button>;
  return <section className="clause-review" aria-label="Audit notes by clause">
    <div className="clause-navigation">
      <label className="audit-field">Clause / control<select aria-label="Select clause or control" disabled={busy} value={control?.id || ""} onChange={event => select(event.target.value)}>{controls.map(item => <option key={item.id} value={item.id}>{item.id} — {item.requirement.split("\n")[0]}</option>)}</select></label>
      <span>{controls.length ? index + 1 : 0} / {controls.length}</span>
    </div>
    <div className="clause-workspace">
      {control && <nav id="clause-sidebar" className="clause-sidebar" aria-label="Jump to a clause or control">
        {controls.map(item => <button key={item.id} type="button" disabled={busy}
          aria-current={item.id === control.id ? "true" : undefined}
          onClick={() => select(item.id)}>
          <strong>{item.id}</strong>
          <span>{item.requirement.split("\n")[0]}</span>
        </button>)}
      </nav>}
    {!control ? <p>No controls match your search.</p> : <article className="clause-detail">
      <header className="clause-title"><div><small>CLAUSE / CONTROL {control.id}</small><h3>{control.requirement.split("\n")[0]}</h3></div><span className="clause-grade">{grade}</span></header>
      <details className="clause-requirement"><summary>Standard reference</summary><p>{control.requirement}</p></details>
      <label className="audit-field clause-note-field">Audit note<textarea aria-label={`Audit note for ${control.id}`} rows={12} maxLength={20000} disabled={busy} value={note} onChange={event => onAnswer(control.id, event.target.value)} placeholder="Record short interview notes, observations, document references and any known gaps for this clause."/></label>
      <div className="note-review-actions"><button className="secondary-button" disabled={busy || !note.trim()} onClick={check}><Search size={16}/>{busy ? "Working…" : "Check with AI"}</button><span>{note.length.toLocaleString()} / 20,000 characters</span></div>
      {!note.trim() && <p className="note-review-hint">Enter an audit note to enable AI review.</p>}
      {checking && <p role="status">Waiting for the AI review…</p>}
      {error && <p role="alert" className="notice">{error}</p>}
      {review && <section className="note-coverage-review" aria-label="AI note coverage review"><h4>AI note coverage review</h4><p>{review.summary}</p>{review.question_coverage?.length > 0 && <section><h5>Information required by the standard</h5><ol className="note-question-coverage">{review.question_coverage.map((item, position) => <li key={position}><strong>{(item.topic || item.question)}</strong><p className={item.status === "covered" ? "note-no-follow-up" : "note-follow-up-needed"}>{({covered:"Covered", partial:"Partially covered", missing:"Missing"})[item.status]}</p>{item.quote && <p>Clause {item.source_control_id}: “{item.quote}”</p>}{item.status !== "covered" && <p className="note-follow-up-needed"><strong>Missing information</strong> · {item.missing_detail || `The note does not fully document: ${(item.topic || item.question)}`}</p>}</li>)}</ol></section>}{review.related_controls?.length > 0 && <p>Related saved notes considered: {review.related_controls.join(", ")}</p>}{review.question_coverage?.length ? (review.question_coverage.every(item => item.status === "covered") && <h5 className="note-no-follow-up">Required information covered</h5>) : <p role="status">Coverage details unavailable. Run the check again to identify missing information.</p>}<small>Suggestion only · {review.provider}</small></section>}
      {finding && <div className="compliance-finding">{finding.rationale || "No assessment result yet"}</div>}
      <nav className="clause-bottom-navigation" aria-label="Navigate between clauses">{previous}<span>{index + 1} / {controls.length}</span>{next}</nav>
    </article>}
    </div>
  </section>;
}

