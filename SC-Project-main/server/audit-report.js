const pending = "[TO BE CONFIRMED]";
const grades = {
  comply: "C",
  ofi: "OFI",
  nc_minor: "Minor NC",
  nc_major: "Major NC",
  needs_review: "Needs Review",
  not_applicable: "N/A"
};

function sourceField(documents, labels) {
  for (const document of documents) {
    for (const line of document.text.split(/\r?\n/)) {
      const field = line.replace(/^\s*(?:[-*]\s+)?/, "").replaceAll("**", "").match(/^(.+?)\s*[:—]\s*(.+)$/);
      if (!field || !labels.includes(field[1].trim().toLowerCase())) continue;
      const value = field[2].trim();
      if (value && !/^\[|^(?:TBD|TBC|N\/A)$/i.test(value)) return value;
    }
  }
  return "";
}

export function buildChecklistAuditReport(project, assessment, runs, documents = []) {
  const context = assessment.context || {};
  const revision = assessment.current_version;
  const run = runs.findLast(item =>
    item.revision === revision && item.stale === false &&
    (item.processing_mode !== "codex" || ["completed", "completed_with_warnings"].includes(item.status))
  );
  const controls = new Map(assessment.controls.map(control => [control.id, control]));
  const results = new Map((run?.results || []).filter(row => controls.has(row.control_id)).map(row => [row.control_id, row]));
  const failed = new Set([...(run?.analysis_plan?.failed_control_ids || []), ...(run?.analysis_plan?.skipped_control_ids || [])]);
  const counts = Object.fromEntries(Object.keys(grades).map(key => [key, 0]));
  const findings = [];
  const unresolved = [];
  for (const [id, control] of controls) {
    const row = results.get(id);
    const category = failed.has(id) ? undefined : row?.audit?.category;
    if (!Object.hasOwn(grades, category)) {
      unresolved.push(`${id} (unassessed)`);
      continue;
    }
    counts[category]++;
    if (category === "needs_review") {
      unresolved.push(id);
      continue;
    }
    if (["nc_minor", "nc_major", "ofi"].includes(category)) {
      findings.push(
        `### ${id} — ${grades[category]}`,
        `Department — ${control.department || pending}`,
        `Audit finding — ${row.rationale?.trim() || row.audit?.reason || pending}`,
        ...(row.audit?.requirement_gap ? [`Requirement gap — ${row.audit.requirement_gap}`] : []),
        ...(row.recommendation ? [`Recommendation — ${row.recommendation}`] : [])
      );
    }
  }
  const company = context.company || pending;
  const departments = Array.isArray(context.departments) ? context.departments.join(", ") : context.departments;
  const field = (...labels) => sourceField(documents, labels) || pending;
  const plan = documents.find(document => document.type === "audit_plan");
  const noteCount = [...controls.keys()].filter(id => assessment.answers?.[id]?.text?.trim()).length;
  return {
    title: `Internal Audit Report — ${project.name}`,
    content: [
      "## Introduction",
      `Organization — ${company}`,
      `Business activity — ${context.business_type || field("business activity", "business type")}`,
      `Organization background — ${field("organization background", "client background", "company background")}`,
      `Project — ${project.name}`,
      "## 1 Objective",
      `Project-specific audit objectives — ${field("audit objective", "audit objectives", "project objectives")}`,
      "## 2 Guidelines and methodologies",
      `Assessment criteria — ${[assessment.standard_name, assessment.standard_version].filter(Boolean).join(" · ") || pending}`,
      `Source — Audit Checklist revision ${revision}, ${controls.size} controls, ${noteCount} with saved notes.`,
      ...documents.map(document => `Source document — ${document.title}`),
      `Assessment run — ${run?.id || pending}`,
      `Audit methods and activities performed — ${pending}`,
      "## 3 Audit analysis plan",
      `Audit team — ${context.auditor || field("audit team", "auditor", "lead auditor")}`,
      `Observer — ${context.observer || pending}`,
      `Departments — ${departments || pending}`,
      `Auditee names and roles — ${field("auditee", "auditees", "auditee names and roles")}`,
      `Assessment scope — ${context.scope || field("audit scope", "assessment scope", "project scope", "scope")}`,
      `Locations — ${field("audit location", "audit locations", "assessment location", "locations")}`,
      `Audit dates — ${field("audit date", "audit dates", "assessment dates")}`,
      `Audit Plan reference — ${plan?.title || pending}`,
      `Audit schedule and detailed agenda — ${field("audit schedule", "audit agenda", "assessment agenda")}`,
      "## 4 Audit findings",
      ...(findings.length ? findings : [run ? "No OFI or nonconformity findings are recorded in the current assessment run." : "Run Assess notes on the current Checklist revision to populate findings."]),
      "## 5 Summary",
      ...(run ? [
        ...Object.entries(grades).map(([key, label]) => `- ${label} — ${counts[key]}`),
        `- Unassessed — ${unresolved.length - counts.needs_review}`,
        ...(unresolved.length ? [`Controls awaiting review — ${unresolved.join(", ")}`] : [])
      ] : ["Results summary pending a current assessment run."]),
      `Comparison with prior gap assessment — ${pending}`,
      "## Conclusion",
      "Draft based on saved Checklist data. Auditor review and confirmation of the remaining report fields are pending."
    ].join("\n\n")
  };
}
