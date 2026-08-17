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
