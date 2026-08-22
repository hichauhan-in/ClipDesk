// One place that knows what work is in flight, anywhere in the app.
//
// Jobs outlive the view that started them, which is the whole point: a search
// that takes forty seconds should survive the user wandering off to read the
// transcript. So the list lives here rather than inside a tab, and every view
// subscribes to it instead of owning it.

import { api } from "./api.js";

const ACTIVE = new Set(["queued", "running"]);

const listeners = new Set();
let jobs = [];
let timer = null;
let inFlight = false;
//: Jobs already announced, so a finished job is toasted once and not on every poll.
const announced = new Set();
const announcers = new Set();
//: jobId -> handlers waiting for it to finish, however the app finds out.
const waiters = new Map();

export function isActive(job) {
  return ACTIVE.has(job.status);
}

/** Poll faster while something is running; there is nothing to see when idle. */
function interval() {
  return jobs.some(isActive) ? 1500 : 6000;
}

async function poll() {
  if (inFlight) return;
  inFlight = true;
  try {
    const payload = await api.listJobs();
    const next = payload.jobs || [];
    announce(next);
    jobs = next;
    settle(next);
    for (const listener of listeners) listener(jobs);
  } catch {
    // A failed poll is not worth surfacing; the next one will either work or
    // the app is already showing that the server is unreachable.
  } finally {
    inFlight = false;
    schedule();
  }
}

function announce(next) {
  const before = new Map(jobs.map((job) => [job.id, job.status]));
  for (const job of next) {
    if (isActive(job)) continue;
    const was = before.get(job.id);
    // Only announce a transition we actually saw, so a page load does not
    // replay every finished job as if it had just happened.
    if (!was || !ACTIVE.has(was) || announced.has(job.id)) continue;
    announced.add(job.id);
    for (const fn of announcers) fn(job);
  }
}

function schedule() {
  clearTimeout(timer);
  timer = setTimeout(poll, interval());
}

/** Hand a finished job to whoever asked to be told about it. */
function settle(next) {
  for (const job of next) {
    if (isActive(job)) continue;
    const handlers = waiters.get(job.id);
    if (!handlers) continue;
    waiters.delete(job.id);
    for (const handler of handlers) handler(job);
  }
}

/**
 * Fold a freshly fetched list into the store.
 *
 * Polling is on a timer, so a view that has just navigated would otherwise ask
 * "what ran here?" against whatever the store last saw — which on a cold load is
 * nothing at all. Priming with the list the view already fetched means a tab can
 * restore itself immediately instead of looking empty until the next tick.
 */
export function primeJobs(incoming) {
  if (!incoming?.length) return jobs;
  const byId = new Map(jobs.map((job) => [job.id, job]));
  for (const job of incoming) byId.set(job.id, job);
  jobs = [...byId.values()].sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
  settle(jobs);
  for (const listener of listeners) listener(jobs);
  return jobs;
}

/**
 * Call back when a job reaches a terminal state, whoever is watching.
 *
 * The event stream belongs to whichever job the progress panel is showing, so it
 * cannot be what decides that work finished — start a second job and the first
 * one's stream is gone. Polling sees every job, so results survive both tab
 * switches and other jobs starting.
 */
export function whenJobSettles(jobId, handler) {
  if (!jobId) return () => {};
  const known = jobs.find((job) => job.id === jobId);
  if (known && !isActive(known)) {
    handler(known);
    return () => {};
  }
  const handlers = waiters.get(jobId) || new Set();
  handlers.add(handler);
  waiters.set(jobId, handlers);
  return () => {
    handlers.delete(handler);
    if (!handlers.size) waiters.delete(jobId);
  };
}

export function startJobStore() {
  if (timer === null) poll();
}

/** Ask for a refresh now, e.g. immediately after starting something. */
export function refreshJobs() {
  clearTimeout(timer);
  poll();
}

export function subscribeJobs(listener) {
  listeners.add(listener);
  listener(jobs);
  return () => listeners.delete(listener);
}

/** Called when a job reaches a terminal state while the app was watching. */
export function onJobSettled(fn) {
  announcers.add(fn);
  return () => announcers.delete(fn);
}

export function allJobs() {
  return jobs;
}

export function jobsForProject(projectId) {
  return jobs.filter((job) => job.project_id === projectId);
}

export function activeCount(projectId = "") {
  return jobs.filter((job) => isActive(job) && (!projectId || job.project_id === projectId)).length;
}

/**
 * The job a tab should pick back up: whatever ran there most recently.
 * Running jobs win over finished ones, so returning mid-flight reattaches to the
 * live one rather than restoring a stale result.
 */
export function latestForTab(projectId, tab, kinds = null) {
  const mine = jobs.filter(
    (job) =>
      job.project_id === projectId &&
      (job.tab === tab || (kinds && kinds.includes(job.kind))) &&
      (!kinds || kinds.includes(job.kind))
  );
  const running = mine.find(isActive);
  return running || mine.find((job) => job.status === "done") || null;
}
