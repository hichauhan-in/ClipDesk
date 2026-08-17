// Notifications that stay put.
//
// A toast is fine for "saved", and useless for "the download failed because the
// session cookies were rejected" — the one message worth reading is the one that
// scrolls away while the user is looking elsewhere. So anything that matters is
// also recorded here, kept until it is dismissed, and shown in full.

const listeners = new Set();
const items = [];
const MAX = 60;
const STORAGE_KEY = "clipdesk.notifications";

function persist() {
  try {
    // Survives a reload: a failure the user has not read yet is not resolved by
    // refreshing the page.
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(items.slice(0, MAX)));
  } catch {
    // Private mode or a full quota. Losing the history is not worth an error.
  }
}

function restore() {
  try {
    const saved = JSON.parse(sessionStorage.getItem(STORAGE_KEY) || "[]");
    if (Array.isArray(saved)) items.push(...saved);
  } catch {
    /* start clean */
  }
}
restore();

function emit() {
  for (const listener of listeners) listener(items);
}

let counter = 0;

/**
 * @param {{title: string, detail?: string, kind?: "error"|"warn"|"ok"|"info",
 *          href?: string, jobId?: string}} entry
 */
export function notify(entry) {
  const record = {
    id: `n${Date.now().toString(36)}${counter++}`,
    at: Date.now(),
    kind: entry.kind || "info",
    title: entry.title,
    detail: entry.detail || "",
    href: entry.href || "",
    read: false,
  };
  // The same failure retried three times should not fill the panel three times.
  const duplicate = items.find(
    (item) => item.title === record.title && item.detail === record.detail && !item.read
  );
  if (duplicate) {
    duplicate.at = record.at;
    duplicate.count = (duplicate.count || 1) + 1;
  } else {
    items.unshift(record);
    items.splice(MAX);
  }
  persist();
  emit();
  return record.id;
}

export function subscribeNotifications(listener) {
  listeners.add(listener);
  listener(items);
  return () => listeners.delete(listener);
}

export function allNotifications() {
  return items;
}

export function unreadCount() {
  return items.filter((item) => !item.read).length;
}

export function markAllRead() {
  for (const item of items) item.read = true;
  persist();
  emit();
}

export function dismiss(id) {
  const index = items.findIndex((item) => item.id === id);
  if (index >= 0) items.splice(index, 1);
  persist();
  emit();
}

export function clearAll() {
  items.splice(0);
  persist();
  emit();
}
