import React, { useEffect, useState } from "react";
import { ArrowDownToLine, FileText } from "lucide-react";
import "./audit-workflow.css";

async function readResponse(response) {
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || "The request could not be completed.");
  return data;
}

function reportSections(markdown = "") {
  const sections = [];
  let section = { title: "Overview", lines: [] };
  for (const raw of markdown.split(/\r?\n/)) {
    const line = raw.trim();
    const heading = line.match(/^#{1,2}\s+(.+)$/);
    if (heading) {
      if (section.lines.length || sections.length) sections.push(section);
      section = { title: heading[1], lines: [] };
    } else if (line) {
      section.lines.push(line);
    }
  }
  if (section.lines.length || !sections.length) sections.push(section);
  return sections;
}

function ReportReader({ report }) {
  const [selectedSection, setSelectedSection] = useState(0);
  const sections = reportSections(report.content_markdown);
  const section = sections[selectedSection];
  return <article className="panel report-reader">
    <header className="report-reader-header">
      <div><h3>{report.title}</h3><time>{new Date(report.created_at.replace(" ", "T") + "Z").toLocaleString()}</time></div>
      <a className="secondary-button" href={`/api/audit/documents/${report.id}/export/docx`}><ArrowDownToLine size={16}/> Download Word</a>
    </header>
    <div className="report-reader-layout">
      <nav className="report-section-nav" aria-label="Report sections">
        {sections.map(({ title }, index) => <button key={index} type="button"
          aria-current={index === selectedSection ? "true" : undefined}
          onClick={() => setSelectedSection(index)}>
          {title}
        </button>)}
      </nav>
      <section className="report-section-content" aria-labelledby="report-section-title" key={selectedSection}>
        <h4 id="report-section-title">{section.title}</h4>
        {section.lines.map((line, index) => {
          if (/^#{3,6}\s+/.test(line)) return <h5 key={index}>{line.replace(/^#+\s+/, "")}</h5>;
          const text = line.replace(/^[-*]\s+/, "").replaceAll("**", "");
          return <p key={index} className={/^[-*]\s/.test(line) ? "report-bullet" : undefined}>
            {text.split(/(\[TO BE CONFIRMED\])/).map((part, partIndex) => part === "[TO BE CONFIRMED]"
              ? <mark key={partIndex}>{part}</mark> : part)}
          </p>;
        })}
      </section>
    </div>
  </article>;
}

export default function AuditWorkspace() {
  const [projects, setProjects] = useState(null);
  const [projectId, setProjectId] = useState("");
  const [reports, setReports] = useState(null);
  const [reportId, setReportId] = useState(null);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const route = `/api/audit/projects/${encodeURIComponent(projectId)}`;

  useEffect(() => {
    const request = new AbortController();
    fetch("/api/audit/projects", { signal: request.signal }).then(readResponse).then(data => {
      setProjects(data.projects);
      setProjectId(data.projects[0]?.id || "");
    }).catch(err => {
      if (!request.signal.aborted) {
        setProjects([]);
        setError(err.message);
      }
    });
    return () => request.abort();
  }, []);

  useEffect(() => {
    if (!projectId) return;
    const request = new AbortController();
    fetch(route, { signal: request.signal }).then(readResponse).then(data => {
      if (!request.signal.aborted) setReports(data.documents.filter(doc => doc.document_type === "audit_report"));
    }).catch(err => {
      if (!request.signal.aborted) {
        setError(err.message);
        setReports([]);
      }
    });
    return () => request.abort();
  }, [projectId, route]);

  async function generateReport() {
    setGenerating(true);
    setError("");
    setNotice("");
    try {
      const { document } = await readResponse(await fetch(`${route}/documents`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ documentType: "audit_report" })
      }));
      setReports(current => [document, ...current]);
      setReportId(document.id);
      setNotice("Audit report generated.");
    } catch (err) {
      setError(err.message);
    } finally {
      setGenerating(false);
    }
  }

  const selectedReport = reports?.find(report => report.id === reportId) || reports?.[0];
  return <section className="module-page audit-workspace">
    <div className="module-heading"><span className="module-icon mint"><FileText size={24}/></span><div><h2>Audit Report</h2><p>Generate a Word report from the linked checklist and available project documents.</p></div></div>
    {error && <p role="alert" className="notice">{error}</p>}
    {notice && <p role="status" className="notice">{notice}</p>}
    {projects?.length > 0 && <div className="audit-project-picker panel">
      <label htmlFor="audit-project-select">Project</label>
      <select id="audit-project-select" value={projectId} disabled={generating} onChange={event => {
        setReports(null);
        setError("");
        setNotice("");
        setProjectId(event.target.value);
      }}>
        {projects.map(project => <option key={project.id} value={project.id}>{project.name}</option>)}
      </select>
      <button type="button" className="primary-button" disabled={reports === null || generating} onClick={generateReport}>
        {generating ? "Generating…" : "Generate Audit Report"}
      </button>
    </div>}
    {projects === null && <p role="status">Loading projects…</p>}
    {projects?.length === 0 && !error && <p>No accessible projects.</p>}
    {projectId && reports === null && <p role="status">Loading reports…</p>}
    {reports?.length === 0 && <p>No reports generated for this project.</p>}
    {selectedReport && <>
      {reports.length > 1 && <label className="report-version-picker">Report version
        <select value={selectedReport.id} onChange={event => setReportId(Number(event.target.value))}>
          {reports.map(report => <option key={report.id} value={report.id}>
            {new Date(report.created_at.replace(" ", "T") + "Z").toLocaleString()} · {report.review_status}
          </option>)}
        </select>
      </label>}
      <ReportReader key={selectedReport.id} report={selectedReport}/>
    </>}
  </section>;
}
