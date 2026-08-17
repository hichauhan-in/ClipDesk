// Inline SVG icons — no icon font, no sprite sheet, no extra request.

import { h } from "./dom.js";

function icon(path, { size = 14, label = "" } = {}) {
  return h("svg", {
    viewBox: "0 0 16 16",
    width: String(size),
    height: String(size),
    "aria-hidden": label ? null : "true",
    "aria-label": label || null,
    html: `<path fill="currentColor" d="${path}"/>`,
  });
}

const BIN =
  "M6.5 1a1 1 0 0 0-1 1v.5H2.75a.75.75 0 0 0 0 1.5h.32l.64 9a1.75 1.75 0 0 0 1.75 1.62h5.08a1.75 " +
  "1.75 0 0 0 1.75-1.62l.64-9h.32a.75.75 0 0 0 0-1.5H10.5V2a1 1 0 0 0-1-1h-3ZM7 2.5h2v.5H7v-.5Zm-2.42 " +
  "2h6.84l-.63 8.9a.25.25 0 0 1-.25.22H5.46a.25.25 0 0 1-.25-.22l-.63-8.9ZM6.75 6a.75.75 0 0 1 .75.75v4.5a.75.75 " +
  "0 0 1-1.5 0v-4.5A.75.75 0 0 1 6.75 6Zm2.5 0a.75.75 0 0 1 .75.75v4.5a.75.75 0 0 1-1.5 0v-4.5A.75.75 0 0 1 9.25 6Z";

const LINK =
  "M7.06 4.94a3.5 3.5 0 0 1 4.95 0 3.5 3.5 0 0 1 0 4.95l-1.5 1.5a.75.75 0 1 1-1.06-1.06l1.5-1.5a2 2 0 1 " +
  "0-2.83-2.83l-1.5 1.5A.75.75 0 0 1 5.56 6.44l1.5-1.5Zm1.88 6.12a3.5 3.5 0 0 1-4.95 0 3.5 3.5 0 0 1 " +
  "0-4.95l1.5-1.5A.75.75 0 0 1 6.55 5.67l-1.5 1.5a2 2 0 1 0 2.83 2.83l1.5-1.5a.75.75 0 1 1 1.06 1.06l-1.5 1.5Z";

const POWER =
  "M7.25 1.75a.75.75 0 0 1 1.5 0v5a.75.75 0 0 1-1.5 0v-5ZM4.18 3.27a.75.75 0 0 1 .16 1.05A5 5 0 1 0 " +
  "11.66 4.32a.75.75 0 1 1 .9-1.2A6.5 6.5 0 1 1 3.12 3.1a.75.75 0 0 1 1.05.16Z";

export const binIcon = (options) => icon(BIN, options);
export const linkIcon = (options) => icon(LINK, options);
export const powerIcon = (options) => icon(POWER, options);
