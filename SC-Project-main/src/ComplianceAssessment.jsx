import React, { useEffect, useMemo, useState } from "react";
import { AlertTriangle, Check, Download, FileUp, LoaderCircle, Play, Save, Search, ShieldCheck, Square, Upload } from "lucide-react";
import "./compliance-assessment.css";
import ClauseReview from "./ClauseReview.jsx";
import ExcelPreview from "./ExcelPreview.jsx";
import { waitForAssessment } from "./assessment-wait.mjs";
import { toAuditNotes } from "./clause-answers.mjs";

async function api(path, options = {}) {
  const binary = options.body instanceof ArrayBuffer || ArrayBuffer.isView(options.body);
  const response = await fetch(path, { credentials: "same-origin", ...options, headers: { ...(!options.body || options.body instanceof FormData || binary ? {} : { "Content-Type": "application/json" }), ...options.headers } });
  const contentType = response.headers.get("content-type") || "";
  const data = contentType.includes("json") ? await response.json() : await response.text();
  if (!response.ok) throw new Error(data?.detail || data?.error || `Request failed (${response.status})`);
  return data;
}

export default function ComplianceAssessment({ projects = [] }) {
  const [standards, setStandards] = useState([]);
  const [availableProjects, setAvailableProjects] = useState(projects);
  const [projectId, setProjectId] = useState("");
  const [previewOpen, setPreviewOpen] = useState(false);
  const [assessment, setAssessment] = useState(null);
  const [assessmentRun, setAssessmentRun] = useState(null);
  const [standardId, setStandardId] = useState("");
  const [businessType, setBusinessType] = useState("");
  const [departments, setDepartments] = useState("");
  const [auditor, setAuditor] = useState("");
  const [observer, setObserver] = useState("");
  const [processingMode, setProcessingMode] = useState("codex");
  const [draft, setDraft] = useState({});
  const [filter, setFilter] = useState("");
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [file, setFile] = useState(null);
  const [standardName, setStandardName] = useState("");
  const [standardVersion, setStandardVersion] = useState("");

  const project = availableProjects.find(item => item.id === projectId);
  const endpoint = `/api/compliance/projects/${encodeURIComponent(projectId)}`;
  const controls = useMemo(() => (assessment?.controls || []).filter(control => {
    const text = `${control.id} ${control.requirement} ${draft[control.id]?.text || ""}`.toLowerCase();
    return text.includes(filter.toLowerCase());
  }), [assessment, filter, draft]);

  useEffect(() => {
    Promise.all([api("/api/compliance/standards"),api("/api/compliance/projects")]).then(([items,data])=>{setStandards(Array.isArray(items)?items:[]);setAvailableProjects(data.projects||[]);}).catch(e => setError(e.message));
  }, []);
  useEffect(() => {
    let active = true;
    setPreviewOpen(false); setAssessment(null); setAssessmentRun(null); setDraft({}); setError(""); setNotice("");
    if (!projectId) return () => { active = false; };
    api(`${endpoint}/assessment`).then(data => {
      if (!active) return;
      const value = data.assessment;
      setAssessment(value);
      if (value) {
        setDraft(toAuditNotes(value.answers || {})); setProcessingMode("codex");
        api(`${endpoint}/assessment/${value.id}/runs`).then(runs => { if (active) setAssessmentRun(runs.at(-1) || null); }).catch(() => { if (active) setError("Could not load assessment results."); });
      }
      else setNotice("No assessment is linked to this project. Select a standard to create an audit checklist.");
    }).catch(e => { if (active) setError(e.message); });
    return () => { active = false; };
  }, [projectId]);

  async function createAssessment(event) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      const selected = standards.find(item => item.id === standardId);
      const result = await api(`${endpoint}/assessment`, { method: "POST", body: JSON.stringify({
        name: `${project.name} · ${selected?.name || "Compliance assessment"}`,
        standard_id: standardId,
        context: { company: project.client, business_type: businessType.trim(), departments: departments.split(",").map(x => x.trim()).filter(Boolean), auditor: auditor.trim(), observer: observer.trim(), question_language: "en" },
        question_provider: "checklist"
      }) });
      setAssessment(result.assessment); setDraft(toAuditNotes(result.assessment.answers || {})); setProcessingMode("codex"); setAssessmentRun(null); setNotice("Assessment created from the checklist.");
    } catch (e) { setError(e.message); }
    finally { setBusy(false); }
  }
  async function saveAnswers() {
    if (!assessment) return;
    setBusy(true); setError("");
    try {
      const result = await api(`${endpoint}/assessment/${assessment.id}/answers`, { method: "PUT", body: JSON.stringify({ expected_version: assessment.current_version, answers: draft }) });
      setAssessment(result); setDraft(toAuditNotes(result.answers || {})); setNotice("Answers saved.");
    } catch (e) { setError(e.message); }
    finally { setBusy(false); }
  }
  async function reviewNote(controlId, text) {
    setBusy(true); setError("");
    try {
      return await api(`${endpoint}/assessment/${assessment.id}/review-note`, {
        method: "POST", body: JSON.stringify({ expected_version: assessment.current_version, control_id: controlId, text, processing_mode: processingMode, authorize_external: true })
      });
    }
    finally {setBusy(false);}
  }
  async function runAssessment(fullReassessment = false) {
    if (!assessment) return;
    setBusy(true); setError(""); setNotice("");
    let jobId = "";
    try {
      const dirty = JSON.stringify(draft) !== JSON.stringify(toAuditNotes(assessment.answers || {}));
      const { assessment: latest } = await api(`${endpoint}/assessment`);
      if (!latest || latest.id !== assessment.id) throw new Error("The linked assessment changed. Reopen the project to assess it.");
      if (dirty && latest.current_version !== assessment.current_version) {
        throw new Error("A newer version was saved while you were editing. Your draft is still here. Copy your edits before reopening the project.");
      }
      const modeSaved = await api(`${endpoint}/assessment/${assessment.id}/processing-mode`, { method: "PUT", body: JSON.stringify({ expected_version: latest.current_version, mode: processingMode }) });
      const saved = dirty
        ? await api(`${endpoint}/assessment/${assessment.id}/answers`, { method: "PUT", body: JSON.stringify({ expected_version: modeSaved.current_version, answers: draft }) })
        : modeSaved;
      setAssessment(saved); setDraft(toAuditNotes(saved.answers || {}));
      const job = await api(`${endpoint}/assessment/${assessment.id}/run-jobs`, { method: "POST", body: "{}" }); jobId = job.id;
      setProgress({ id: jobId, status: "pending", completed: 0, total: 0 });
      const runPromise = api(`${endpoint}/assessment/${assessment.id}/runs`, { method: "POST", body: JSON.stringify({ job_id: jobId, processing_mode: processingMode, authorize_external: true, full_reassessment: fullReassessment }) });
      await waitForAssessment({ request:runPromise, getState:()=>api(`/api/compliance/projects/${encodeURIComponent(projectId)}/jobs/${jobId}`), onProgress:setProgress });
      const refreshed = await api(`${endpoint}/assessment`);
      setAssessment(refreshed.assessment); setDraft(toAuditNotes(refreshed.assessment?.answers || saved.answers));
      const runs=await api(`${endpoint}/assessment/${assessment.id}/runs`);setAssessmentRun(runs.at(-1)||null);setNotice("Assessment completed.");
    } catch (e) { setError(e.message); }
    finally { setBusy(false); setProgress(null); }
  }
  async function stopAssessment() {
    if (!progress?.id && !progress) return;
    try { await api(`/api/compliance/projects/${encodeURIComponent(projectId)}/jobs/${progress.id}/cancel`, { method: "POST", body: "{}" }); }
    catch (e) { setError(e.message); }
  }
  async function importStandard(event) {
    event.preventDefault(); if (!file) return;
    setBusy(true); setError("");
    try {
      await api("/api/compliance/standards/import", { method: "POST", body: await file.arrayBuffer(), headers: { "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "X-Standard-Name": standardName, "X-Standard-Version": standardVersion } });
      setStandards(await api("/api/compliance/standards")); setFile(null); setNotice("Checklist imported.");
    } catch (e) { setError(e.message); }
    finally { setBusy(false); }
  }
  async function exportWorkbook() {
    setBusy(true); setError("");
    try {
      let saved=assessment;
      const dirty=JSON.stringify(draft)!==JSON.stringify(assessment.answers || {});
      if(dirty){
        saved=await api(`${endpoint}/assessment/${assessment.id}/answers`,{method:"PUT",body:JSON.stringify({expected_version:assessment.current_version,answers:draft})});
        setAssessment(saved);setDraft(toAuditNotes(saved.answers || {}));
      }
      const path=!dirty && run?.processing_mode === "codex" ? `agent-runs/${encodeURIComponent(run.id)}/export` : "export";
      const response=await fetch(`${endpoint}/assessment/${assessment.id}/${path}`,{credentials:"same-origin"});
      if(!response.ok){const detail=await response.json().catch(()=>({}));throw new Error(detail.detail || detail.error || "Excel export failed.");}
      const url=URL.createObjectURL(await response.blob());const link=document.createElement("a");
      link.href=url;link.download=`audit-checklist-${assessment.id}-v${saved.current_version}.xlsx`;document.body.appendChild(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),60000);
      setNotice("Excel exported with saved answers. Current assessment findings are included when available.");
    }catch(error){setError(error.message);}finally{setBusy(false);}
  }
  const run = assessmentRun && !assessmentRun.stale && assessmentRun.revision === assessment?.current_version ? assessmentRun : null;
  const grade = item => ({comply:"C",ofi:"OFI",nc_minor:"NC-Minor",nc_major:"NC-Major",needs_review:"Needs review",not_applicable:"N/A"}[item.audit?.category] || item.audit?.grade || item.status || "Not assessed");

  return <section className="module-page compliance-page">
    <div className="module-heading"><span className="module-icon mint"><ShieldCheck size={24}/></span><div><small>SC WORKSPACE / AUDIT CHECKLIST</small><h2>Audit Checklist</h2><p>Import a checklist, record audit notes, and assess each control with AI.</p></div></div>
    {error && <div className="notice"><AlertTriangle size={18}/><div><b>Unable to complete this action</b><p>{error}</p></div></div>}
    {notice && <div className="notice"><Check size={18}/><div><p>{notice}</p></div></div>}
    <section className="panel">
      <div className="panel-heading"><div><h3>Project and checklist</h3><p>Each assessment is linked to its project and saved in this workspace.</p></div></div>
      <div className="audit-grid three">
        <label className="audit-field">Project<select disabled={busy} value={projectId} onChange={e=>setProjectId(e.target.value)}><option value="">Select a project</option>{availableProjects.map(p=><option key={p.id} value={p.id}>{p.name} · {p.client}</option>)}</select></label>
        {assessment ? <div className="audit-field"><span>Standard</span><strong>{assessment.standard_name} · {assessment.standard_version}</strong></div> : <label className="audit-field">Standard<select value={standardId} onChange={e=>setStandardId(e.target.value)}><option value="">Select a standard</option>{standards.map(s=><option key={s.id} value={s.id}>{s.name} · {s.version} ({s.controls?.length || 0} controls)</option>)}</select></label>}
      </div>
      {!assessment && project && <>
        {!standards.length && <form className="panel" onSubmit={importStandard}><div className="panel-heading"><div><h3>Import Excel checklist</h3><p>Your checklist is stored in this workspace.</p></div></div><div className="audit-grid three"><label className="audit-field">Standard name<input required value={standardName} onChange={e=>setStandardName(e.target.value)} placeholder="ISO/IEC 27001"/></label><label className="audit-field">Version<input required value={standardVersion} onChange={e=>setStandardVersion(e.target.value)} placeholder="2022"/></label><label className="audit-field">Excel file (.xlsx)<input required type="file" accept=".xlsx" onChange={e=>setFile(e.target.files?.[0] || null)}/></label></div><button className="primary-button" disabled={busy || !file}><Upload size={15}/>Import checklist</button></form>}
        {standards.length>0 && <form onSubmit={createAssessment} className="audit-grid three">
          <label className="audit-field">Business type<input required value={businessType} onChange={e=>setBusinessType(e.target.value)} placeholder="Enter the client’s business type"/></label>
          <label className="audit-field">Departments (comma separated)<input required value={departments} onChange={e=>setDepartments(e.target.value)} placeholder="Departments in scope"/></label>
          <label className="audit-field">Auditor<input value={auditor} onChange={e=>setAuditor(e.target.value)}/></label>
          <label className="audit-field">Observer<input value={observer} onChange={e=>setObserver(e.target.value)}/></label>
          <button className="primary-button" disabled={busy || !standardId}><FileUp size={15}/>Create assessment</button>
        </form>}
      </>}
    </section>
    {assessment && <>
      <section className="panel"><div className="panel-heading"><div><h3>{assessment.name}</h3><p>{assessment.controls?.length || 0} controls · Version {assessment.current_version} · {assessment.context?.company}</p></div><div className="audit-actions"><button className="secondary-button" onClick={saveAnswers} disabled={busy}><Save size={15}/>Save</button><label className="audit-field">AI mode<select value={processingMode} disabled={busy} onChange={e=>setProcessingMode(e.target.value)}><option value="colab">Colab / configured provider</option><option value="codex">Codex CLI</option></select></label><button className="primary-button" onClick={() => runAssessment()} disabled={busy}><Play size={15}/>Assess notes</button><button type="button" className="secondary-button" onClick={() => runAssessment(true)} disabled={busy}>Recheck all</button>{progress && <button className="secondary-button" onClick={stopAssessment}><Square size={14}/>Stop</button>}<button className="secondary-button" disabled={busy} onClick={() => setPreviewOpen(true)}>View Excel</button><button className="secondary-button" disabled={busy} onClick={exportWorkbook}><Download size={15}/>Export Excel</button></div></div>

        {progress && <p role="status">Assessing {progress.completed || 0}/{progress.total || assessment.controls?.length || 0} · {progress.phase || progress.status}</p>}

        <div className="compliance-filter"><Search size={15}/><input value={filter} onChange={e=>setFilter(e.target.value)} placeholder="Search clauses or audit notes"/></div>
        {previewOpen && <ExcelPreview load={api} key={`${assessment.id}:${assessment.current_version}`} url={`${endpoint}/assessment/${assessment.id}/${run?.processing_mode === "codex" ? `agent-runs/${encodeURIComponent(run.id)}/preview` : "preview"}`} onClose={() => setPreviewOpen(false)}/>}
        <ClauseReview key={assessment.id} controls={controls} draft={draft} busy={busy} run={assessmentRun} runIsCurrent={Boolean(run)} savedAnswers={assessment.answers} onReviewNote={reviewNote} onAnswer={(id, text) => { setDraft(current => ({...current, [id]: {...(current[id] || {status:"unanswered"}), text}})); }}/>
      </section>
    </>}
  </section>;
}
