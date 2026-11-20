import { toAuditNotes } from "./clause-answers.mjs";
import React, { useState } from "react";
import { ChevronLeft, ChevronRight, Search } from "lucide-react";

export default function ClauseReview({ controls, draft, busy, run, runIsCurrent, savedAnswers, onAnswer, onReviewNote }) {
  const [selected, setSelected] = useState("");
  const [reviews, setReviews] = useState({});
  const [error, setError] = useState("");
  const [checking, setChecking] = useState(false);
  const control = controls.find(item => item.id === selected) || controls[0];
  const index = controls.indexOf(control);
  const note = draft[control?.id]?.text || "";
  const review = reviews[control?.id]?.note === note ? reviews[control.id].result : null;
  const finding = run?.results?.find(item => item.control_id === control?.id);
  const savedNote = control ? toAuditNotes({ [control.id]: savedAnswers?.[control.id] || {} })[control.id].text : "";
  const historical = finding && (!runIsCurrent || note !== savedNote);
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
      <header className="clause-title"><div><small>CLAUSE / CONTROL {control.id}</small><h3>{control.requirement.split("\n")[0]}</h3></div><span className="clause-grade">{historical ? `Previous: ${grade}` : grade}</span></header>
      <details className="clause-requirement"><summary>Standard reference</summary><p>{control.requirement}</p></details>
      <label className="audit-field clause-note-field">Audit note<textarea aria-label={`Audit note for ${control.id}`} rows={12} maxLength={20000} disabled={busy} value={note} onChange={event => onAnswer(control.id, event.target.value)} placeholder="Record short interview notes, observations, document references and any known gaps for this clause."/></label>
      <div className="note-review-actions"><button className="secondary-button" disabled={busy || !note.trim()} onClick={check}><Search size={16}/>{busy ? "Working…" : "Check with AI"}</button><span>{note.length.toLocaleString()} / 20,000 characters</span></div>
      {!note.trim() && <p className="note-review-hint">Enter an audit note to enable AI review.</p>}
      {checking && <p role="status">Waiting for the AI review…</p>}
      {error && <p role="alert" className="notice">{error}</p>}
      {review && <MissingInformation review={review}/>} 
      {finding && <div className="compliance-finding">{historical && <p>Previous assessment · Version {run.revision}. Run Assess notes for an updated grade.</p>}{finding.rationale?.replace(/^[ \t]*document reference[ \t]*:[ \t]*/gim, "") || "No assessment rationale available."}</div>}
      <nav className="clause-bottom-navigation" aria-label="Navigate between clauses">{previous}<span>{index + 1} / {controls.length}</span>{next}</nav>
    </article>}
    </div>
  </section>;
}


function MissingInformation({ review }) {
  const topics = review.question_coverage || [];
  const missing = topics.filter(topic => ["missing", "partial"].includes(topic.status));
  const unverified = topics.some(topic => topic.status === "unverified");
  return <section className="note-coverage-review" aria-label="Missing information">
    {!topics.length ? <p role="status">Coverage details unavailable.</p>
      : !missing.length ? (unverified
        ? <p role="status">Some AI references could not be verified. Check again to confirm coverage.</p>
        : <p className="note-no-follow-up">No missing information identified.</p>)
      : <><h4 className="note-follow-up-needed">Missing information</h4>
        <ul>{missing.map((item, index) => <li className="note-follow-up-needed" key={index}>
          <strong>{item.topic || item.question}</strong>{item.missing_detail && <p>{item.missing_detail}</p>}
        </li>)}</ul></>}
  </section>;
}
