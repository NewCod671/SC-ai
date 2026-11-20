# Grading knowledge provenance

Reviewed 2026-11-11. This is decision support for ISO 27001 audit notes, not a replacement for the imported checklist or the certification body's applicable scheme rules. The JSON cases are original fictional examples, not official ISO examples or client findings.

## Source-derived principles

- [European Accreditation, FAQ 39.1](https://european-accreditation.org/sp_accordion_faqs/question-39-1-findings-classification-nonconformities/): a confirmed requirement failure belongs in the nonconformity category, rather than OFI. Establish nonfulfilment before assigning severity.
- [European Accreditation, FAQ 35.10, response section](https://european-accreditation.org/sp_accordion_faqs/question-35-10-definition-of-nonconformity/): severity concerns the system's ability to deliver intended results. Context matters; evidence of ineffective process control or related lapses showing a common breakdown can support a major. The questioner's proposed blanket escalation is not the response's conclusion.
- [ISO/IAF Auditing Practices Group, Nonconformity — Documenting, 2016](https://committee.iso.org/files/live/sites/tc176/files/PDF%20APG%20New%20Disclaimer%2012-2023/ISO-TC%20176-TF_APG-DocumentNonconformity.pdf): document the observed facts, relevant obligation and precise failure. This is ISO 9001 writing guidance; it does not add ISO 27001 obligations.

## Application-specific interpretation

SC's recorded onsite note is the assessment record. Attachments remain archival. This application convention does not mean the AI inspected documents or verified external facts. A missing attachment alone is not an NC. Missing facts in the note can require review.

OFI means full conformity plus a specific optional enhancement under this application's grading contract. Minor examples show a real mandatory lapse and facts bounding its effect. Major examples establish a failure affecting system capability; an actual incident is not a prerequisite. A note saying only “review missing” cannot establish severity without context.

## Runtime use

`grading-cases.json` contains 18 original examples: OFI, NC-Minor and NC-Major across access restriction, backup, suppliers internal audit, risk treatment and scope definition. Each includes a requirement condition to avoid inventing mandatory schedules, technology or paperwork.

The backend selects at most three examples matching the exact control identifier. Unknown identifiers receive no guessed examples. Both Colab and Codex receive the same selected cases. Source links and the full library are not repeated in every prompt. Results remain one per control.

When refining examples, preserve category meanings and fictional labeling. Do not tune toward a target grade distribution. A new knowledge hash invalidates reuse of affected prior assessment inputs.

Clause 8.3 (risk treatment) and Annex A.8.3 (access restriction) are separate identifiers. Annex A prefixes must never be removed to select examples.
