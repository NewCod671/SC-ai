// Keep the answer transport compatible with the existing assessment engine.
export function checklistPrompts(text) {
  const separated = text.split(/\s*\|\|\s*/).map(value => value.trim()).filter(Boolean);
  if (separated.length > 1) return separated;
  const items = [], context = [];
  let current = '';
  for (const line of text.split(/\r?\n/).map(value => value.trim()).filter(Boolean)) {
    const match = line.match(/^(?:(?:Q|Question)\s*\d+\s*[:.)]|\d{1,2}[.)]|[a-z][.)]|[-*•▪●])\s+/i);
    if (match) { if (current) items.push(current); current = line.slice(match[0].length).trim(); }
    else if (current) current += ' ' + line;
    else context.push(line);
  }
  if (current) items.push(current);
  if (items.length < 2) return [text.trim()];
  if (context.length) items[0] = context.join(' ') + ' ' + items[0];
  return items;
}

export function unpackAnswers(text, prompts) {
  const values = {};
  const named = /(?:^|\n\n)คำถามเพิ่มเติม: ([^\n]+)\nคำตอบ:\s*([\s\S]*?)(?=\n\n(?:คำถามเพิ่มเติม: |\[Q\d+\] )|$)/g;
  for (const match of text.matchAll(named)) {
    const index = prompts.findIndex(prompt => prompt.trim() === match[1].trim());
    if (index >= 0) values[index] = match[2].trim();
  }
  // Retain unmatched legacy notes instead of removing text we cannot assign.
  const cleaned = text.replace(named, (block, question) => prompts.some(prompt => prompt.trim() === question.trim()) ? '' : block).trim();
  const blocks = [...cleaned.matchAll(/(?:^|\n\n)\[Q(\d+)\] [^\n]*\n([\s\S]*?)(?=\n\n\[Q\d+\] |$)/g)];
  const unassigned = [];
  for (const match of blocks) {
    const index = Number(match[1]) - 1;
    if (index >= 0 && index < prompts.length) values[index] = match[2];
    else unassigned.push(match[0].trim());
  }
  const previous = blocks.length ? cleaned.slice(0, blocks[0].index).trim() : cleaned;
  return { previous: [previous, ...unassigned].filter(Boolean).join('\n\n'), values };
}

export function packAnswers(previous, values, prompts) {
  return [previous.trim(), ...prompts.flatMap((prompt, index) => values[index]?.trim()
    ? [`[Q${index + 1}] ${prompt.replace(/\n/g, ' ')}\n${values[index]}`] : [])].filter(Boolean).join('\n\n');
}

export function toAuditNotes(answers) {
  return Object.fromEntries(Object.entries(answers).map(([id, answer]) => {
    // Strip only legacy question headings; retain every recorded answer verbatim.
    const text = (answer.text || '').replace(/(^|\n\n)\[Q\d+\] [^\n]*\n/g, '$1')
      .replace(/(^|\n\n)คำถามเพิ่มเติม: [^\n]+\nคำตอบ:\s*/g, '$1');
    return [id, { ...answer, text }];
  }));
}
