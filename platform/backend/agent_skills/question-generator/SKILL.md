---
name: question-generator
description: Generate concise, human-friendly compliance interview questions mapped to exact imported controls and the company's departments.
---

# Question generator

Generate questions a person in the named department can answer from their work. Follow the requested language and use the company profile only to make the question relevant. Do not repeat profile inventories (systems, data types, policies, or department lists) in the question.

## Rules

- Use only the supplied imported controls and their exact stable identifiers. Do not invent, rename, omit, or broaden requirements.
- Cover every required control exactly once across the returned questions.
- Combine controls only when the same real process and responsible people can answer them together. Similar wording alone is not enough.
- When combining controls, include each control identifier and give one brief mapping reason. Keep unrelated responsibilities as separate questions.
- Preserve departments already assigned to controls. If a control has no assigned department, select the most relevant department or departments from the supplied company context based on the responsibility. Choose only departments from that context; never default all questions to ALL.
- Ask an open, direct question about what people actually do. Use everyday words, one concise sentence where possible, and at most one relevant follow-up clause.
- Do not ask the respondent to recite standards, provide a policy inventory, or attach evidence. Do not assume a process, system, policy, or implementation exists.
- Return only the exact structured data requested by the output schema. No preamble, commentary, findings, recommendations, or markdown.

## Quality check

Before returning, check that all supplied control IDs appear exactly once, all department names are valid, each question is clear to its intended respondent, and grouped questions share a genuine owner/process.
