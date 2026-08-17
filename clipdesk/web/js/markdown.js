// A small, safe Markdown renderer.
//
// The text comes from a language model, so nothing is ever inserted as raw
// HTML — every character is escaped before any markup is added. That rules out
// script injection through generated notes.

const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (char) => ESCAPES[char]);
}

function inline(text) {
  let out = escapeHtml(text);
  out = out.replace(/`([^`]+)`/g, "<code>$1</code>");
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>");
  out = out.replace(/~~([^~]+)~~/g, "<del>$1</del>");
  // Only http(s) links are turned into anchors; anything else stays as text.
  out = out.replace(
    /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>'
  );
  return out;
}

function renderTable(rows) {
  const cells = (line) =>
    line
      .replace(/^\s*\|/, "")
      .replace(/\|\s*$/, "")
      .split("|")
      .map((cell) => cell.trim());

  const header = cells(rows[0]);
  const body = rows.slice(2).map(cells);
  const head = `<tr>${header.map((c) => `<th>${inline(c)}</th>`).join("")}</tr>`;
  const rest = body
    .map((row) => `<tr>${row.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`)
    .join("");
  return `<table><thead>${head}</thead><tbody>${rest}</tbody></table>`;
}

export function markdownToHtml(markdown) {
  const lines = String(markdown || "").replace(/\r\n/g, "\n").split("\n");
  const out = [];
  let index = 0;

  const closeList = (stack) => {
    while (stack.length) out.push(stack.pop());
  };
  const listStack = [];

  while (index < lines.length) {
    const line = lines[index];

    // Fenced code, including mermaid — shown verbatim, which is exactly what a
    // reader needs since the downloaded .md renders the diagram properly.
    const fence = line.match(/^```(\w*)/);
    if (fence) {
      closeList(listStack);
      const language = fence[1] || "";
      const buffer = [];
      index += 1;
      while (index < lines.length && !/^```/.test(lines[index])) {
        buffer.push(lines[index]);
        index += 1;
      }
      index += 1;
      const label = language ? `<div class="faint small mono">${escapeHtml(language)}</div>` : "";
      out.push(`${label}<pre><code>${escapeHtml(buffer.join("\n"))}</code></pre>`);
      continue;
    }

    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      closeList(listStack);
      const level = heading[1].length;
      out.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      index += 1;
      continue;
    }

    if (/^\s*\|.*\|\s*$/.test(line) && /^\s*\|[\s:|-]+\|\s*$/.test(lines[index + 1] || "")) {
      closeList(listStack);
      const rows = [];
      while (index < lines.length && /^\s*\|.*\|\s*$/.test(lines[index])) {
        rows.push(lines[index]);
        index += 1;
      }
      out.push(renderTable(rows));
      continue;
    }

    if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      closeList(listStack);
      out.push("<hr>");
      index += 1;
      continue;
    }

    const quote = line.match(/^>\s?(.*)$/);
    if (quote) {
      closeList(listStack);
      const buffer = [];
      while (index < lines.length && /^>/.test(lines[index])) {
        buffer.push(lines[index].replace(/^>\s?/, ""));
        index += 1;
      }
      out.push(`<blockquote>${inline(buffer.join(" "))}</blockquote>`);
      continue;
    }

    const bullet = line.match(/^(\s*)[-*+]\s+(?:\[([ xX])\]\s+)?(.*)$/);
    const ordered = line.match(/^(\s*)\d+[.)]\s+(.*)$/);
    if (bullet || ordered) {
      const tag = bullet ? "ul" : "ol";
      if (!listStack.length || listStack[listStack.length - 1] !== `</${tag}>`) {
        closeList(listStack);
        out.push(`<${tag}>`);
        listStack.push(`</${tag}>`);
      }
      if (bullet) {
        const checkbox =
          bullet[2] !== undefined
            ? `<input type="checkbox" disabled ${/[xX]/.test(bullet[2]) ? "checked" : ""}> `
            : "";
        out.push(`<li>${checkbox}${inline(bullet[3])}</li>`);
      } else {
        out.push(`<li>${inline(ordered[2])}</li>`);
      }
      index += 1;
      continue;
    }

    if (!line.trim()) {
      closeList(listStack);
      index += 1;
      continue;
    }

    closeList(listStack);
    const buffer = [];
    while (index < lines.length && lines[index].trim() && !/^(#{1,6}\s|```|>|\s*[-*+]\s|\s*\d+[.)]\s|\s*\|)/.test(lines[index])) {
      buffer.push(lines[index]);
      index += 1;
    }
    out.push(`<p>${inline(buffer.join(" "))}</p>`);
  }

  closeList(listStack);
  return out.join("\n");
}
